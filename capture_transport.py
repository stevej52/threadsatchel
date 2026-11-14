"""Read queued packets or acknowledge committed hashes; no network or AI calls."""
import base64
import hashlib
import json
from pathlib import Path
import re
import stat

MAX_PACKET_BYTES = 32 * 1024 * 1024


def packet_size(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or
            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
        raise ValueError('nonregular_queued_packet')
    return info.st_size


def bounded_packet(path):
    if packet_size(path) > MAX_PACKET_BYTES:
        raise ValueError('oversized_queued_packet')
    with path.open('rb') as stream:
        raw = stream.read(MAX_PACKET_BYTES + 1)
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError('oversized_queued_packet')
    if hashlib.sha256(raw).hexdigest() != path.stem:
        raise ValueError('queue_hash_mismatch')
    return raw
def pending(folder):
    folder = Path(folder)
    packets, total, held, held_count = [], 0, [], 0
    for path in sorted((folder/'packets').glob('*.json')):
        if not re.fullmatch(r'[0-9a-f]{64}', path.stem): continue
        try:
            size = packet_size(path)
            if size <= MAX_PACKET_BYTES and packets and total + size > 8*1024*1024:
                continue
            raw = bounded_packet(path)
        except (OSError, ValueError) as exc:
            held_count += 1
            if len(held) < 20:
                reason = str(exc) if isinstance(exc, ValueError) else 'queue_read_failed'
                held.append({'sha': path.stem, 'reason': reason})
            continue
        packets.append({'sha': path.stem, 'data': base64.b64encode(raw).decode('ascii')})
        total += len(raw)
        if len(packets) >= 200: break
    status = folder/'status.json'
    capture_status = json.loads(status.read_text(encoding='utf-8')) if status.exists() else {}
    if not isinstance(capture_status, dict):
        capture_status = {'capture_status_error': 'invalid_capture_status'}
    # Existing central adapters retain bundle['status']; holds stay observable.
    capture_status = dict(capture_status, transport_held_count=held_count,
                          transport_held=held, transport_held_details_limit=20)
    return {'packets': packets, 'status': capture_status}
def acknowledge(folder, hashes):
    if not all(isinstance(s,str) and re.fullmatch(r'[0-9a-f]{64}',s) for s in hashes): raise ValueError('Invalid acknowledgement')
    for sha in hashes:
        p = Path(folder)/'packets'/(sha+'.json')
        if p.exists() or p.is_symlink():
            bounded_packet(p)
            p.unlink()
    return {'acknowledged': len(hashes)}
