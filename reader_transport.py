"""Archive-specific authentication for the optional loopback reader."""
import hmac
import os
from pathlib import Path
import tempfile


def daemon_port():
    port = int(os.environ.get('THREADSATCHEL_READER_PORT', '18092'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid reader port')
    return port


def daemon_url():
    return f'http://127.0.0.1:{daemon_port()}'


def reader_key(root):
    directory = Path(root) / '.reader-state'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.is_junction():
        raise ValueError('Unsafe reader state directory')
    keyfile = directory / 'rpc.key'
    if not keyfile.exists():
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
            temp.write(os.urandom(32))
        try:
            # Only a fully written key can win a concurrent first start.
            try:
                os.link(temp.name, keyfile)
            except FileExistsError:
                pass
        finally:
            Path(temp.name).unlink()
    if keyfile.is_symlink() or not keyfile.is_file() or keyfile.stat().st_size != 32:
        raise ValueError('Invalid reader key file')
    return keyfile.read_bytes()


def proof(key, purpose, payload):
    return hmac.digest(key, purpose + b'\0' + payload, 'sha256').hex()


def describe_health(key, nonce, response):
    return isinstance(response, dict) and isinstance(response.get('proof'), str) and hmac.compare_digest(
        proof(key, b'health', nonce.encode()), response['proof'])
