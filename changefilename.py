import os

folder = r"E:\Compression footage\UK\Chester\Site 4 Location 1\AI results"
prefix = "VID_20260428_"

for filename in os.listdir(folder):
    if filename.endswith(".xlsx"):
        new_name = prefix + filename
        os.rename(
            os.path.join(folder, filename),
            os.path.join(folder, new_name)
        )
        print(f"Renamed: {filename} → {new_name}")

print("Done!")