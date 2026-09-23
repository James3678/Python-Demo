from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.appName("LLDJoinAgg").master("local[*]").getOrCreate()

file_path = "15SC01_20260801_lld.txt"

# ---- This file mixes 3 record types on one line, identified by the first field: ----
#   "10" = file header (1 row, summary only)      -> skip
#   "20" = loan-level ORIGINATION/static data      -> one row per loan
#   "50" = loan-level MONTHLY PERFORMANCE data     -> one row per loan per month
#
# Column positions below are inferred from the sample data pattern, not the
# official Freddie Mac glossary - loan_id/state/credit_score/rate/upb are
# reliable; anything else here should be verified against Freddie Mac's
# Loan-Level Disclosure Glossary before using this beyond practice.

raw = spark.read.text(file_path)
split_col = F.split(raw["value"], "\\|")

# ---- Record type 20: origination/static ----
orig = raw.filter(split_col.getItem(0) == "20").select(
    split_col.getItem(1).alias("loan_id"),
    split_col.getItem(12).alias("property_state"),
    split_col.getItem(18).cast("double").alias("orig_interest_rate"),
    split_col.getItem(19).cast("double").alias("original_upb"),
    split_col.getItem(29).cast("int").alias("credit_score"),
)

# ---- Record type 50: monthly performance ----
perf = raw.filter(split_col.getItem(0) == "50").select(
    split_col.getItem(1).alias("loan_id"),
    split_col.getItem(2).alias("reporting_period"),   # e.g. 202608 = Aug 2026
    split_col.getItem(5).cast("double").alias("current_interest_rate"),
)

print("Origination rows:", orig.count())
print("Performance rows:", perf.count())

# ---- JOIN: origination static data to its monthly performance record ----
joined = orig.join(perf, on="loan_id", how="inner")

print("\nSample joined rows:")
joined.show(10, truncate=False)

# ---- AGGREGATION: portfolio summary by state ----
agg = orig.groupBy("property_state").agg(
    F.count("loan_id").alias("loan_count"),
    F.sum("original_upb").alias("total_original_upb"),
    F.avg("orig_interest_rate").alias("avg_orig_rate"),
    F.avg("credit_score").alias("avg_credit_score"),
).orderBy(F.desc("total_original_upb"))

print("\nPortfolio summary by state:")
agg.show(20, truncate=False)

# ---- WRITE OUTPUT: portfolio summary as Parquet ----
agg.write.mode("overwrite").parquet("state_summary_output")
print("\nWrote Parquet output to ./state_summary_output")

spark.stop()