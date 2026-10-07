"""Run all synthetic release checks; never use the user's archive."""
from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parent.parent
checks=[['-m','unittest','test_codex_capture','test_direct_delivery','test_inbox_sweep',
         'test_memory_search','test_memory_listing','test_import_limits',
         'test_import_identity_performance','test_import_split_export','test_import_extra_fields',
         'test_sync_retry','test_install_schedule'],
        ['test_capture_export.py'],['test_incremental.py'],
        ['test_reconcile_imports.py'],['test_export_overlap.py'],['test_sync_capture.py']]
for args in checks:
    result=subprocess.run([sys.executable,*args],cwd=root)
    if result.returncode:raise SystemExit(result.returncode)
print('All release checks passed.')
