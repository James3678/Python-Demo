from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("InspectLLD").master("local[*]").getOrCreate()

# Point this at ONE file first, not the whole folder
file_path = "15SC01_20260801_lld.txt"  # <-- update with your actual local path/filename

raw = spark.read.text(file_path)

print("Total rows:", raw.count())
print("\nFirst 5 raw lines:")
for row in raw.take(5):
    print(row.value)

# Freddie Mac LLD files are pipe-delimited - count fields in the first line
first_line = raw.take(1)[0].value
fields = first_line.split("|")
print(f"\nField count: {len(fields)}")
for i, f in enumerate(fields):
    print(f"  [{i}] {f}")

spark.stop()
