"""Run all synthetic release checks; never use the user's archive."""
from pathlib import Path
import subprocess,sys
root=Path(__file__).resolve().parent.parent
checks=[['-m','unittest','test_codex_capture','test_capture_limits','test_store_memory',
         'test_direct_delivery','test_inbox_sweep','test_import_limits','test_import_identity_performance',
         'test_memory_search','test_ai_embeddings','test_ai_quality','test_ai_chat','test_ai_remediation','test_ai_diagnostics',
         'test_capacity_processing','test_memory_resources','test_memory_prewarm','test_memory_health'],
        ['test_capture_export.py'],['test_incremental.py'],
        ['test_reconcile_imports.py'],['test_export_overlap.py'],['test_sync_capture.py']]
for args in checks:
    result=subprocess.run([sys.executable,*args],cwd=root)
    if result.returncode:raise SystemExit(result.returncode)
print('All release checks passed.')
