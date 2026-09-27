"""Run all synthetic release checks; never use the user's archive."""
from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parent.parent
checks=[['-m','unittest','test_codex_capture','test_direct_delivery','test_inbox_sweep'],
        ['test_capture_export.py'],['test_incremental.py'],
        ['test_reconcile_imports.py'],['test_export_overlap.py'],['test_sync_capture.py']]
for args in checks:
    result=subprocess.run([sys.executable,*args],cwd=root)
    if result.returncode:raise SystemExit(result.returncode)
print('All release checks passed.')
