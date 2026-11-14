"""Public vocabulary provisioning checks with no external requests."""
from io import BytesIO
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_tokenizer


class TokenizerSetupTests(unittest.TestCase):
    def test_corrupt_download_is_never_published_and_retry_reuses_verified_file(self):
        expected = b'Test-only vocabulary'
        checksum = hashlib.sha256(expected).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(prepare_tokenizer.VOCABULARIES,{'fixture':checksum},clear=True):
            folder = Path(temporary)
            file = folder/'fixture.tiktoken'
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                prepare_tokenizer.prepare(folder, lambda *args, **kwargs: BytesIO(b'wrong'))
            self.assertFalse(file.exists())
            prepare_tokenizer.prepare(folder, lambda *args, **kwargs: BytesIO(expected))
            self.assertEqual(file.read_bytes(), expected)
            prepare_tokenizer.prepare(folder, lambda *args, **kwargs: self.fail('Fetched verified file twice'))
