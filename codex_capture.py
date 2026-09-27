"""Incremental, read-only Codex transcript projection into the existing importer.
Only completed user/assistant UI message events are eligible. No model/API calls.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from import_memory import import_file

ROOT = Path(__file__).resolve().parent
VERSION = 1
ALLOWED_SOURCES = {None, 'user', 'chatgpt_handoff'}
ALLOWED_PHASES = {None, 'commentary', 'final', 'final_answer'}
SECRET = re.compile(
    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
    r'|\bsk-[A-Za-z0-9_-]{20,}'
    r'|\b(?:ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{16,}'
    r'|\bAKIA[A-Z0-9]{16}\b'
    r'|\bBearer\s+[A-Za-z0-9._~-]{20,}'
    r'|(?i:\b(?:password|passwd|api[_ -]?key|access[_ -]?token|client[_ -]?secret)'
    r'\s*[:=]\s*["\']?)[^\s"\']{8,}'
)
PRIVACY_OFF = re.compile(
    r"(?is)^\s*(?:/memory\s+off\b|(?:please\s+)?(?:do not|don't)\s+"
    r"(?:save|record|remember|archive)\s+(?:this|anything|the following)\b|off the record\b)"
)
PRIVACY_ON = re.compile(r'(?is)^\s*/memory\s+on\s*$')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.incoming-' + uuid4().hex + '.json.part')
    with temp.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


@contextmanager
def capture_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'capture.lock').open('a+b') as f:
        f.seek(0)
        if not f.read(1):
            f.write(b'0')
            f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def source_session(first):
    if first.get('type') != 'session_meta':
        raise ValueError('Missing leading session_meta')
    meta = first.get('payload')
    if not isinstance(meta, dict):
        raise ValueError('Unsupported session metadata')
    session = meta.get('id')
    if not isinstance(session, str) or not session.strip():
        raise ValueError('Missing recorded session ID')
    # Unknown/background origins fail closed; no guardian/subagent transcripts.
    if meta.get('source') not in ('vscode', 'cli', 'exec', 'app-server', 'desktop'):
        return meta, False
    return meta, meta.get('thread_source') in ALLOWED_SOURCES


def event_message(row, session, offset, uri, paused, counts):
    if row.get('type') != 'event_msg':
        return None, paused
    payload = row.get('payload', {})
    if payload.get('type') in ('user_message', 'agent_message'):
        counts['unsupported_legacy_message'] += 1
        return None, paused
    if payload.get('type') != 'item_completed':
        return None, paused
    item = payload.get('item', {})
    kind = item.get('type')
    if kind not in ('UserMessage', 'AgentMessage'):
        return None, paused
    if payload.get('thread_id') not in (None, session):
        counts['foreign_thread_message'] += 1
        return None, paused
    if kind == 'AgentMessage' and item.get('phase') not in ALLOWED_PHASES:
        counts['unsupported_assistant_phase'] += 1
        return None, paused
    mid = item.get('id')
    if not isinstance(mid, str) or not mid.strip():
        counts['message_without_source_id'] += 1
        return None, paused
    content = item.get('content')
    if not isinstance(content, list):
        counts['unsupported_content'] += 1
        return None, paused
    text_parts = [part['text'] for part in content
                  if isinstance(part, dict) and part.get('type') in ('text', 'Text')
                  and isinstance(part.get('text'), str)]
    if len(text_parts) != 1:
        counts['unsupported_multipart_or_nontext_message'] += 1
        return None, paused
    text = text_parts[0]
    if not text.strip():
        counts['empty_message'] += 1
        return None, paused
    if kind == 'UserMessage' and PRIVACY_OFF.search(text):
        counts['privacy_pause'] += 1
        return None, True
    if kind == 'UserMessage' and PRIVACY_ON.search(text):
        counts['privacy_resume'] += 1
        return None, False
    if paused:
        counts['privacy_paused_message'] += 1
        return None, paused
    if SECRET.search(text):
        counts['possible_secret_message_omitted'] += 1
        return None, paused
    if len(content) != 1:
        counts['media_omitted_text_retained'] += 1
    message = {'speaker': 'user' if kind == 'UserMessage' else 'assistant',
               'text': text, 'message_id': mid, 'source_url': uri + '#byte=' + str(offset)}
    if isinstance(row.get('timestamp'), str) and row['timestamp']:
        message['source_date'] = row['timestamp']
    counts['eligible_messages'] += 1
    return message, paused


def edge_hash(stream, offset):
    stream.seek(max(0, offset - 1024))
    return digest(stream.read(min(offset, 1024)))


def project_file(path, previous, max_bytes):
    counts = Counter()
    batches, batch, ids = [], [], set()
    path = Path(path).resolve()
    with path.open('rb') as stream:
        first_line = stream.readline()
        first = json.loads(first_line)
        meta, eligible = source_session(first)
        session = meta['id']
        header = digest(first_line)
        if not eligible:
            return meta, None, [], {'excluded_background_or_unknown_source': 1}
        offset = previous.get('offset', 0)
        paused = previous.get('privacy_paused', False)
        size = os.fstat(stream.fileno()).st_size
        reset = (previous.get('header_sha') != header or offset > size or
                 (offset and edge_hash(stream, offset) != previous.get('edge_sha')))
        if reset:
            offset, paused = 0, False
            if previous:
                counts['source_restarted'] += 1
        stream.seek(offset)
        start = offset
        while stream.tell() - start < max_bytes:
            location = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b'\n'):
                stream.seek(location)
                counts['partial_tail_deferred'] += 1
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                # Do not advance the durable offset past an unreadable entry.
                stream.seek(location)
                counts['malformed_line_blocking'] += 1
                break
            message, paused = event_message(row, session, location, path.as_uri(), paused, counts)
            # Break adjacency across any omitted visible message.
            visible = (row.get('type') == 'event_msg' and
                       row.get('payload', {}).get('type') == 'item_completed' and
                       row.get('payload', {}).get('item', {}).get('type') in ('UserMessage', 'AgentMessage'))
            if batch and ((visible and message is None) or
                          (message is not None and message['message_id'] in ids) or len(batch) >= 200):
                batches.append(batch)
                batch, ids = [], set()
            if message:
                batch.append(message)
                ids.add(message['message_id'])
            offset = stream.tell()
        if batch:
            batches.append(batch)
        checkpoint = {'offset': offset, 'header_sha': header,
                      'edge_sha': edge_hash(stream, offset), 'privacy_paused': paused,
                      'path': str(path), 'session_id': session,
                      'source_bytes_at_scan': size, 'checked_at': stamp(),
                      'last_scan_counts': dict(counts)}
        counts['bytes_read'] = offset - start
        counts['remaining_bytes'] = max(0, size - offset)
        return meta, checkpoint, batches, dict(counts)


def run(config, dry_run=False):
    folder = Path(config['capture_dir'])
    folder.mkdir(parents=True, exist_ok=True)
    with capture_lock(folder):
        state_path = folder/'state.json'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'version': VERSION, 'sessions': {}}
        if state.get('version') != VERSION:
            raise ValueError('Unsupported capture state version')
        report = {'version': VERSION, 'started_at': stamp(), 'dry_run': dry_run,
                  'counts': {}, 'errors': [], 'source_roots': config['source_roots']}
        counts = Counter()
        started = time.monotonic()
        files = []
        for root in config['source_roots']:
            root = Path(root)
            if root.exists():
                files.extend(root.rglob('*.jsonl'))
        if not files:
            report['errors'].append({'reason': 'No transcript files found in configured roots'})
        excluded = set(config.get('excluded_session_ids', []))
        # Oldest first so an initial backfill cannot starve old sessions.
        for path in sorted(set(files), key=lambda p: (p.stat().st_mtime, str(p))):
            if time.monotonic() - started > config.get('max_run_seconds', 45):
                counts['time_budget_reached'] += 1
                break
            counts['files_checked'] += 1
            try:
                with path.open('rb') as f:
                    meta, eligible = source_session(json.loads(f.readline()))
                sid = meta['id']
                if not eligible or sid in excluded:
                    counts['excluded_sessions'] += 1
                    continue
                meta, checkpoint, batches, scanned = project_file(
                    path, state['sessions'].get(sid, {}), config.get('max_bytes_per_session', 32*1024*1024))
                counts.update(scanned)
                counts['eligible_sessions'] += 1
                if checkpoint is None:
                    continue
                for messages in batches:
                    packet = {'format': 'threadsatchel/1', 'kind': 'excerpt',
                              'title': 'Codex history: ' + str(meta.get('cwd') or sid),
                              'conversation_id': sid, 'messages': messages}
                    if dry_run:
                        counts['projected_packets'] += 1
                        continue
                    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
                    packet_path = folder/'packets'/(digest(raw) + '.json')
                    packet_path.parent.mkdir(parents=True, exist_ok=True)
                    if packet_path.exists():
                        if packet_path.read_bytes() != raw:
                            raise ValueError('Packet content verification failed')
                    else:
                        temp = packet_path.with_name('.incoming-' + uuid4().hex + '.json.part')
                        with temp.open('xb') as f:
                            f.write(raw)
                            f.flush()
                            os.fsync(f.fileno())
                        if temp.read_bytes() != raw:
                            raise ValueError('Packet read-back failed')
                        # Exclusive lock covers this directory. Windows rename does not overwrite.
                        os.rename(temp, packet_path)
                    result = import_file(packet_path, Path(config['db_path']))
                    for key in ('added_revisions', 'reused_messages', 'repeated_packets', 'linked_entities'):
                        counts[key] += result[key]
                    counts['import_warnings'] += len(result['warnings'])
                    counts['uncertain_matches'] += len(result['uncertain'])
                if not dry_run:
                    # Commit to the existing importer first; crash/retry is idempotent.
                    state['sessions'][sid] = checkpoint
                    dump_atomic(state_path, state)
            except Exception as exc:
                report['errors'].append({'path': str(path), 'reason': type(exc).__name__ + ': ' + str(exc)})
        report['counts'] = dict(counts)
        report['finished_at'] = stamp()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if not dry_run:
            dump_atomic(folder/'status.json', report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'codex-capture-config.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    try:
        result = run(config, args.dry_run)
    except (OSError, ValueError) as exc:
        result = {'finished_at': stamp(), 'errors': [{'reason': type(exc).__name__ + ': ' + str(exc)}]}
        # Never include transcript text in failure output.
        if sys.stdout:
            print(json.dumps(result))
        raise SystemExit(1)
    if sys.stdout:
        print(json.dumps(result, indent=2))
    raise SystemExit(1 if result['errors'] or result['counts'].get('malformed_line_blocking') else 0)


if __name__ == '__main__':
    main()
