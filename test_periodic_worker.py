"""Exercise periodic maintenance without starting subprocesses or model servers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import periodic_worker as worker


class PeriodicWorkerTests(unittest.TestCase):
    def test_failed_capture_does_not_block_opt_in_scan_and_sweep(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'capture-local.json').write_text('{}')
            config={'enabled':True,'vision_upload_dirs':{'sample':'uploads'}}
            (root/'memory-ai.json').write_text(json.dumps(config))
            with patch.object(worker,'capture',side_effect=ValueError('synthetic')), \
                    patch('upload_vision.scan_once',return_value={}) as scan, \
                    patch.object(worker,'sweep',return_value={'state':'complete'}) as sweep:
                state=worker.maintenance(root,capture_due=True,sweep_due=True)
            self.assertEqual(state,{'capture':'ValueError','vision':'ok','inbox':'complete'})
            self.assertEqual(scan.call_args.args[0],root)
            self.assertEqual(scan.call_args.args[1]['vision_upload_dirs'],config['vision_upload_dirs'])
            sweep.assert_called_once_with(root)

    def test_empty_configuration_does_not_scan_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(worker,'sweep'), \
                patch('upload_vision.scan_once') as scan:
            state=worker.maintenance(Path(temporary),capture_due=True,sweep_due=True)
            scan.assert_not_called()
            self.assertEqual(state['vision'],'off')
            self.assertNotIn('capture',state)
            self.assertNotIn('reader',state)
