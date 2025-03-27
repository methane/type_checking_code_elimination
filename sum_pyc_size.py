import os
import sys


def get_pyc_files_total_size(directory="."):
    total_size = 0
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".pyc"):
                file_path = os.path.join(root, file)
                total_size += os.path.getsize(file_path)
    return total_size


total_pyc_size = get_pyc_files_total_size(sys.argv[1])
print(f"{total_pyc_size} bytes")
