"""Explicitly fetch the two public vocabularies for offline token counting."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen


VOCABULARIES = {
    'cl100k_base': '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7',
    'o200k_base': '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d',
}


def prepare(folder, fetch=urlopen):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, expected in VOCABULARIES.items():
        file = folder / (name+'.tiktoken')
        if file.is_file() and file.stat().st_size <= 8_000_000 and hashlib.sha256(file.read_bytes()).hexdigest() == expected:
            continue
        url = 'https://openaipublic.blob.core.windows.net/encodings/' + file.name
        with fetch(url, timeout=30) as response:
            data = response.read(8_000_001)
        if len(data) > 8_000_000 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Vocabulary failed SHA-256 verification: ' + name)
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temporary:
            try:
                temporary.write(data)
                temporary.close()
                os.replace(temporary.name, file)
            finally:
                Path(temporary.name).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(__file__).resolve().parent.parent / '.tokenizer-cache'
    parser.add_argument('--cache-dir', type=Path, default=os.environ.get('TIKTOKEN_CACHE_DIR') or default)
    prepare(parser.parse_args().cache_dir)
    print('Verified both public tokenizer vocabularies. Restart running readers to enable them.')
