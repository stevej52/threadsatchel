"""Bounded synthetic capture tests; fixtures stay in temporary isolated databases."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import codex_capture as cap

def row(kind, payload):
    return {'timestamp': '2000-01-01T00:00:00Z', 'type': kind, 'payload': payload}

def meta(source='vscode', thread='user'):
    return row('session_meta', {'id': 'SYNTHETIC-session', 'source': source,
        'thread_source': thread, 'cwd': 'SYNTHETIC-project'})

def msg(mid, text, kind='UserMessage', phase=None):
    item = {'type': kind, 'id': mid, 'content': [{'type': 'text' if kind == 'UserMessage' else 'Text', 'text': text}]}
    if phase is not None:
        item['phase'] = phase
    return row('event_msg', {'type': 'item_completed', 'thread_id': 'SYNTHETIC-session', 'item': item})

def data(rows):
    return b''.join(json.dumps(x).encode('utf-8') + b'\n' for x in rows)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-SYNTHETIC-capture-')
        self.root = Path(self.temp.name)
        self.source = self.root/'sessions'
        self.source.mkdir()
        self.path = self.source/'SYNTHETIC.jsonl'
        self.db = self.root/'memory.sqlite3'
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
            c.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)")
        self.cfg = {'source_roots': [str(self.source)], 'capture_dir': str(self.root/'capture'),
                    'db_path': str(self.db), 'max_run_seconds': 30}

    def tearDown(self):
        self.temp.cleanup()

    def texts(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            return [x[0] for x in c.execute('SELECT text FROM memories ORDER BY rowid')]

    def run_capture(self):
        result = cap.run(self.cfg)
        self.assertEqual(result['errors'], [])
        return result

    def test_projection_and_repeat(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC user'),
            row('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'HIDDEN injected text'}]}),
            row('event_msg', {'type': 'item_completed', 'item': {'type': 'Reasoning', 'raw_content': ['HIDDEN reasoning']}}),
            msg('a1', 'SYNTHETIC answer', 'AgentMessage', 'final_answer'),
            msg('a2', 'HIDDEN analysis', 'AgentMessage', 'analysis')]))
        self.assertEqual(self.run_capture()['counts']['added_revisions'], 2)
        self.assertEqual(self.run_capture()['counts'].get('added_revisions', 0), 0)
        self.assertEqual(self.texts(), ['SYNTHETIC user', 'SYNTHETIC answer'])

    def test_partial_tail_then_append(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC first')]))
        self.run_capture()
        encoded = data([msg('a1', 'SYNTHETIC later', 'AgentMessage')])
        with self.path.open('ab') as f:
            f.write(encoded[:30])
        self.assertEqual(self.run_capture()['counts']['partial_tail_deferred'], 1)
        with self.path.open('ab') as f:
            f.write(encoded[30:])
        self.assertEqual(self.run_capture()['counts']['added_revisions'], 1)
        self.assertEqual(len(self.texts()), 2)

    def test_lost_checkpoint_replay(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC persisted')]))
        self.run_capture()
        (self.root/'capture'/'state.json').unlink()
        self.assertEqual(self.run_capture()['counts']['repeated_packets'], 1)
        self.assertEqual(len(self.texts()), 1)

    def test_failed_import_does_not_advance(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC retry')]))
        with patch.object(cap, 'import_file', side_effect=RuntimeError('SYNTHETIC import failure')):
            result = cap.run(self.cfg)
        self.assertEqual(len(result['errors']), 1)
        state = json.loads((self.root/'capture'/'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['sessions'], {})
        self.assertEqual(self.run_capture()['counts']['added_revisions'], 1)

    def test_background_excluded(self):
        self.path.write_bytes(data([meta({'subagent': {'other': 'guardian'}}, 'guardian_review'),
                                   msg('u1', 'SYNTHETIC internal')]))
        self.assertEqual(self.run_capture()['counts']['excluded_sessions'], 1)
        self.assertEqual(self.texts(), [])

    def test_secrets_and_privacy_pause(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC before'),
            msg('u2', 'password=synthetic-secret-value'),
            msg('u3', 'Do not save this conversation'), msg('a1', 'SYNTHETIC private', 'AgentMessage')]))
        self.run_capture()
        with self.path.open('ab') as f:
            f.write(data([msg('u4', 'SYNTHETIC still private'), msg('u5', '/memory on'), msg('u6', 'SYNTHETIC after')]))
        self.run_capture()
        self.assertEqual(self.texts(), ['SYNTHETIC before', 'SYNTHETIC after'])

    def test_malformed_line_blocks(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC before')]) + b'broken\n' + data([msg('u2', 'SYNTHETIC after')]))
        result = self.run_capture()
        self.assertEqual(result['counts']['malformed_line_blocking'], 1)
        self.assertEqual(self.texts(), ['SYNTHETIC before'])

    def test_truncation_recovery(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC old text padded ' * 10)]))
        self.run_capture()
        self.path.write_bytes(data([meta(), msg('u2', 'SYNTHETIC replacement')]))
        result = self.run_capture()
        self.assertEqual(result['counts']['source_restarted'], 1)
        self.assertEqual(len(self.texts()), 2)

    def test_multimodal_and_unknown_parts(self):
        image = msg('u1', 'SYNTHETIC image caption')
        image['payload']['item']['content'].append({'type': 'image', 'url': 'SYNTHETIC image'})
        multi = msg('u2', 'SYNTHETIC first part')
        multi['payload']['item']['content'].append({'type': 'text', 'text': 'SYNTHETIC second part'})
        self.path.write_bytes(data([meta(), image, multi]))
        result = self.run_capture()
        self.assertEqual(result['counts']['media_omitted_text_retained'], 1)
        self.assertEqual(result['counts']['unsupported_multipart_or_nontext_message'], 1)
        self.assertEqual(self.texts(), ['SYNTHETIC image caption'])

    def test_dry_run_no_database_write(self):
        self.path.write_bytes(data([meta(), msg('u1', 'SYNTHETIC dry')]))
        before = self.db.read_bytes()
        result = cap.run(self.cfg, True)
        self.assertEqual(result['counts']['eligible_messages'], 1)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.root/'capture'/'state.json').exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
