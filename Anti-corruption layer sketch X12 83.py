"""Anti-corruption layer sketch: X12 837/278 and HL7 v2 -> FHIR R4.

Pattern notes:
- Parse -> map -> emit; legacy structures never leak past this module.
- Deterministic ids + conditional update/create => idempotent replays.
- Failures go to a dead-letter list (a DLQ topic in production).
- Sketch only: production needs full loop handling, code-set validation,
  and FHIR profile validation (e.g., HAPI, fhir.resources, pyx12/hl7apy).
"""
import hashlib
from dataclasses import dataclass


class TranslationError(Exception):
    pass


# ---------- helpers ----------
def _uuid(*parts):
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"urn:uuid:{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _date(v):  # CCYYMMDD -> CCYY-MM-DD
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) >= 8 else v


def _entry(resource, system, value):
    """Transaction entry with conditional update => safe to replay."""
    return {
        "fullUrl": _uuid(system, value),
        "resource": resource,
        "request": {"method": "PUT",
                    "url": f"{resource['resourceType']}?identifier={system}|{value}"},
    }


# ---------- X12 ----------
@dataclass
class Seg:
    id: str
    el: list
    cs: str

    def get(self, n, default=""):  # 1-based element number
        return self.el[n - 1] if n - 1 < len(self.el) and self.el[n - 1] else default

    def sub(self, n, m, default=""):  # 1-based element, 1-based component
        parts = self.get(n).split(self.cs)
        return parts[m - 1] if m - 1 < len(parts) else default


def parse_x12(raw):
    raw = raw.strip()
    if not raw.startswith("ISA") or len(raw) < 106:
        raise TranslationError("Invalid ISA envelope")
    es, cs, st = raw[3], raw[104], raw[105]
    segs = []
    for s in raw.split(st):
        s = s.strip()
        if s:
            p = s.split(es)
            segs.append(Seg(p[0], p[1:], cs))
    return segs


def x12_to_fhir(raw):
    segs = parse_x12(raw)
    st = next(s for s in segs if s.id == "ST")
    kind = st.get(1)
    if kind not in ("837", "278"):
        raise TranslationError(f"Unsupported transaction {kind}")

    claim = {
        "resourceType": "Claim", "status": "active",
        "use": "claim" if kind == "837" else "preauthorization",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type",
                             "code": "professional"}]},
        "priority": {"coding": [{"code": "normal"}]},
        "diagnosis": [], "item": [],
        "identifier": [], "created": None,
    }
    patient = provider = payer = None
    service_date = None

    for s in segs:
        if s.id == "BHT":
            claim["created"] = _date(s.get(4))
        elif s.id == "CLM":                       # 837 claim header
            claim["identifier"] = [{"system": "urn:x12:claim-id", "value": s.get(1)}]
            claim["total"] = {"value": float(s.get(2, "0")), "currency": "USD"}
        elif s.id == "TRN":                       # 278 trace number
            claim["identifier"] = [{"system": "urn:x12:trace", "value": s.get(2)}]
        elif s.id == "NM1":
            q = s.get(1)
            if q == "IL":                         # subscriber / patient
                patient = {"resourceType": "Patient",
                           "identifier": [{"system": "urn:x12:member-id", "value": s.get(9)}],
                           "name": [{"family": s.get(3), "given": [s.get(4)]}]}
            elif q in ("85", "1P"):               # billing / requesting provider
                provider = {"resourceType": "Organization",
                            "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi",
                                            "value": s.get(9)}],
                            "name": s.get(3)}
            elif q in ("PR", "X3"):               # payer / UMO
                payer = {"resourceType": "Organization",
                         "identifier": [{"system": "urn:x12:payer-id", "value": s.get(9)}],
                         "name": s.get(3)}
        elif s.id == "DMG" and patient:
            patient["birthDate"] = _date(s.get(2))
            patient["gender"] = {"M": "male", "F": "female"}.get(s.get(3), "unknown")
        elif s.id == "HI":
            for n, _ in enumerate(s.el, 1):
                qual, code = s.sub(n, 1), s.sub(n, 2)
                if qual in ("ABK", "ABF") and code:
                    claim["diagnosis"].append({
                        "sequence": len(claim["diagnosis"]) + 1,
                        "diagnosisCodeableConcept": {"coding": [{
                            "system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code}]},
                        "type": [{"coding": [{"code": "principal" if qual == "ABK" else "secondary"}]}]})
        elif s.id == "DTP" and s.get(1) == "472":
            service_date = _date(s.get(3))
        elif s.id in ("SV1", "SV2", "SV3"):
            idx = 2 if s.id == "SV2" else 1       # SV2 carries revenue code first
            item = {"sequence": len(claim["item"]) + 1,
                    "productOrService": {"coding": [{
                        "system": "http://www.ama-assn.org/go/cpt", "code": s.sub(idx, 2)}]}}
            if s.id == "SV1":
                item["net"] = {"value": float(s.get(2, "0")), "currency": "USD"}
                item["quantity"] = {"value": float(s.get(4, "1"))}
            claim["item"].append(item)

    if not (patient and provider and claim["item"]):
        raise TranslationError("Missing required loop: subscriber, provider, or service line")
    for it in claim["item"]:
        if service_date:
            it["servicedDate"] = service_date

    ctrl = st.get(2)
    cid = claim["identifier"][0] if claim["identifier"] else {"system": "urn:x12:st", "value": ctrl}
    claim["identifier"] = [cid]
    claim["created"] = claim["created"] or service_date
    pe = _entry(patient, patient["identifier"][0]["system"], patient["identifier"][0]["value"])
    oe = _entry(provider, provider["identifier"][0]["system"], provider["identifier"][0]["value"])
    claim["patient"] = {"reference": pe["fullUrl"]}
    claim["provider"] = {"reference": oe["fullUrl"]}
    entries = [pe, oe]
    if payer:
        ie = _entry(payer, payer["identifier"][0]["system"], payer["identifier"][0]["value"])
        claim["insurer"] = {"reference": ie["fullUrl"]}
        entries.append(ie)
    claim["insurance"] = [{"sequence": 1, "focal": True,
                           "coverage": {"display": "Resolve Coverage from member id"}}]
    entries.append(_entry(claim, cid["system"], cid["value"]))
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


# ---------- HL7 v2 ----------
def parse_hl7(msg):
    lines = [l for l in msg.replace("\n", "\r").split("\r") if l.strip()]
    if not lines or not lines[0].startswith("MSH"):
        raise TranslationError("Missing MSH")
    fs = lines[0][3]
    segs = {}
    for l in lines:
        f = l.split(fs)
        if f[0] == "MSH":
            f.insert(1, fs)                       # align so f[n] == MSH-n
        segs.setdefault(f[0], []).append(f)
    return segs, lines[0][4]                      # component separator


def hl7_to_fhir(msg):
    segs, c = parse_hl7(msg)
    f = lambda seg, n, k=0: (segs[seg][k][n] if n < len(segs[seg][k]) else "")
    comp = lambda v, i: (v.split(c) + [""] * 5)[i]

    if not f("MSH", 9).startswith("ADT"):
        raise TranslationError(f"Unsupported message type {f('MSH', 9)}")
    mrn, auth = comp(f("PID", 3), 0), comp(f("PID", 3), 3) or "unknown"
    pid_sys = f"urn:hl7v2:mrn:{auth}"
    patient = {"resourceType": "Patient",
               "identifier": [{"system": pid_sys, "value": mrn}],
               "name": [{"family": comp(f("PID", 5), 0), "given": [comp(f("PID", 5), 1)]}],
               "birthDate": _date(f("PID", 7)),
               "gender": {"M": "male", "F": "female"}.get(f("PID", 8), "unknown")}
    pe = _entry(patient, pid_sys, mrn)
    entries = [pe]

    if "PV1" in segs:
        cls = {"I": "IMP", "O": "AMB", "E": "EMER"}.get(f("PV1", 2), "AMB")
        visit = comp(f("PV1", 19), 0) or f("MSH", 10)
        enc = {"resourceType": "Encounter", "status": "in-progress",
               "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": cls},
               "subject": {"reference": pe["fullUrl"]},
               "identifier": [{"system": "urn:hl7v2:visit", "value": visit}]}
        entries.append(_entry(enc, "urn:hl7v2:visit", visit))
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


# ---------- router with DLQ ----------
def translate(raw, dlq):
    try:
        return x12_to_fhir(raw) if raw.lstrip().startswith("ISA") else hl7_to_fhir(raw)
    except (TranslationError, StopIteration, ValueError, KeyError, IndexError) as e:
        dlq.append({"payload": raw, "error": repr(e)})
        return None


if __name__ == "__main__":
    import json
    isa = ("ISA*00*          *00*          *ZZ*" + "SENDER".ljust(15) + "*ZZ*" +
           "RECEIVER".ljust(15) + "*260101*1200*^*00501*000000001*0*P*:~")
    x837 = isa + ("ST*837*0001*005010X222A1~BHT*0019*00*123*20260101*1200*CH~"
                  "NM1*85*2*ACME CLINIC*****XX*1234567890~NM1*IL*1*DOE*JANE****MI*W123456~"
                  "DMG*D8*19800101*F~CLM*CLAIM001*75***11:B:1~HI*ABK:E119~"
                  "SV1*HC:99213*75*UN*1~DTP*472*D8*20260101~SE*10*0001~")
    hl7 = ("MSH|^~\\&|EHR|HOSP|ACL|PAYER|202601011200||ADT^A01|MSG0001|P|2.5\r"
           "PID|1||12345^^^HOSP^MR||DOE^JANE||19800101|F\r"
           "PV1|1|I|||||||||||||||||V9001")
    dlq = []
    for m in (x837, hl7, "garbage"):
        out = translate(m, dlq)
        print(json.dumps(out, indent=1)[:400] if out else "-> DLQ", "\n")
    print("DLQ size:", len(dlq))