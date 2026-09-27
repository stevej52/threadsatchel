"""Read queued packets or acknowledge committed hashes; no network or AI calls."""
import base64
import hashlib
import json
from pathlib import Path
import re
def pending(folder):
    folder = Path(folder)
    packets, total = [], 0
    for path in sorted((folder/'packets').glob('*.json')):
        if not re.fullmatch(r'[0-9a-f]{64}', path.stem): continue
        size = path.stat().st_size
        if packets and total + size > 8*1024*1024: break
        if size > 32*1024*1024: raise ValueError('Oversized queued packet')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem: raise ValueError('Queue hash mismatch')
        packets.append({'sha': path.stem, 'data': base64.b64encode(raw).decode('ascii')})
        total += size
        if len(packets) >= 200: break
    status = folder/'status.json'
    return {'packets': packets, 'status': json.loads(status.read_text(encoding='utf-8')) if status.exists() else None}
def acknowledge(folder, hashes):
    if not all(isinstance(s,str) and re.fullmatch(r'[0-9a-f]{64}',s) for s in hashes): raise ValueError('Invalid acknowledgement')
    for sha in hashes:
        p = Path(folder)/'packets'/(sha+'.json')
        if p.exists():
            if hashlib.sha256(p.read_bytes()).hexdigest() != sha: raise ValueError('Acknowledgement hash mismatch')
            p.unlink()
    return {'acknowledged': len(hashes)}
