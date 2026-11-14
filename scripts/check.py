"""Run the release checks against isolated synthetic archives."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_TESTS = (
    "test_capture_export.py", "test_incremental.py", "test_reconcile_imports.py",
    "test_export_overlap.py", "test_sync_capture.py",
)


def main():
    modules = [path.stem for path in sorted(ROOT.glob("test_*.py"))
               if path.name not in SCRIPT_TESTS]
    checks = [
        ["-m", "unittest", *modules],
        ["-m", "unittest", "discover", "-s", "connector", "-t", "."],
        *([filename] for filename in SCRIPT_TESTS),
    ]
    for args in checks:
        subprocess.run([sys.executable, "-B", *args], cwd=ROOT, check=True)
    print("All Python release checks passed.")


if __name__ == "__main__":
    main()
