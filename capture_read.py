"""Bounded JSONL reads shared by local capture and its export-only entry point."""
import json


MAX_HEADER_BYTES = 64 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_PACKET_BYTES = 8 * 1024 * 1024


def byte_budget(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 256 <= value <= MAX_SESSION_BYTES:
        raise ValueError('max_bytes_per_session must be an integer from 256 to 33554432')
    return value


def read_header(stream, max_bytes):
    """Never allocate an unbounded first line, including during source discovery."""
    limit = min(byte_budget(max_bytes), MAX_HEADER_BYTES)
    line = stream.readline(limit)
    if not line.endswith(b'\n'):
        reason = 'header_size_limit' if len(line) == limit else 'incomplete_header'
        raise ValueError(reason + ': transcript held; source and checkpoint retained')
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError):
        raise ValueError('malformed_header: transcript held; source and checkpoint retained') from None
    return line, value


def read_event(stream, remaining, event_capacity):
    """Return a complete bounded line or a reason; never move past a held line."""
    location = stream.tell()
    limit = min(remaining, event_capacity)
    if limit <= 0:
        return None, 'byte_budget_reached'
    line = stream.readline(limit)
    if not line:
        return None, None
    if not line.endswith(b'\n'):
        stream.seek(location)
        if len(line) < limit:
            return None, 'partial_tail_deferred'
        return None, 'oversized_line_blocking' if limit == event_capacity else 'byte_budget_reached'
    return line, None


def fair_paths(files, cursor):
    """Continue after the previous attempted source, including held sources."""
    ordered = sorted(set(files), key=lambda p: (p.stat().st_mtime, str(p)))
    for index, path in enumerate(ordered):
        if str(path) == cursor:
            return ordered[index + 1:] + ordered[:index + 1]
    return ordered


def encode_packets(packet, max_bytes=MAX_PACKET_BYTES):
    """Keep ordinary packet bytes stable; split only an oversized message batch."""
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(raw) <= max_bytes:
        yield raw
        return
    messages = packet['messages']
    if len(messages) < 2:
        raise ValueError('projected_packet_size_limit: source and checkpoint retained')
    middle = len(messages) // 2
    for part in (messages[:middle], messages[middle:]):
        yield from encode_packets(dict(packet, messages=part), max_bytes)
