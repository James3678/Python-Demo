"""Run the ACL over every file in demo_data/ and write FHIR output."""
import json, pathlib
from acl_x12_hl7_to_fhir import translate

dlq, out_dir = [], pathlib.Path("demo_output")
out_dir.mkdir(exist_ok=True)
for f in sorted(pathlib.Path("demo_data").iterdir()):
    bundle = translate(f.read_text(), dlq)
    if bundle:
        (out_dir / f"{f.stem}.fhir.json").write_text(json.dumps(bundle, indent=2))
        types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        print(f"OK   {f.name:32} -> {types}")
    else:
        print(f"DLQ  {f.name:32} -> {dlq[-1]['error']}")
