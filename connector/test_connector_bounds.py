import base64
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import Mock, patch

from connector.connector import Archive, encode, public_result, response_tokens, run_job, sha


class ConnectorBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'handoffs').mkdir()
        self.archive = Archive(self.root, self.root / 'state', reader=Mock())

    def tearDown(self):
        self.temp.cleanup()

    def test_peek_delegates_without_starting_another_server(self):
        self.archive._reader.peek_memory.return_value = {'text': 'exact'}
        args = {'id': 'one', 'revision': 'a' * 64, 'match_id': 'match', 'lookup_id': 'lookup'}
        self.assertEqual(self.archive.execute('peek_memory', args), {'text': 'exact'})
        self.archive._reader.peek_memory.assert_called_once_with(**args)

    def test_lookup_reference_and_timeline_delegate_to_same_reader(self):
        args = {'ref': 'r' + 'a' * 12, 'lookup_id': 'b' * 32}
        for action in ('get_memory', 'peek_memory', 'timeline_memory'):
            with self.subTest(action=action):
                method = getattr(self.archive._reader, action)
                method.return_value = {'kind': action, 'lookup_id': args['lookup_id']}
                self.assertEqual(self.archive.execute(action, args), method.return_value)
                method.assert_called_once_with(**args)

    def test_handoff_pages_cover_all_names(self):
        for i in range(11):
            folder = self.root / 'handoffs' / ('handoff-%02d' % i) / 'v1'
            folder.mkdir(parents=True)
            (folder / 'instructions.md').write_text('source text')
        offset, names = 0, []
        while offset is not None:
            page = self.archive.execute('list_handoffs', {'offset': offset})
            names.extend(row['name'] for row in page['handoffs'])
            self.assertLessEqual(len(page['handoffs']), 3)
            self.assertLess(response_tokens(page), 1744)
            self.assertEqual(page['total_count'], 11)
            offset = page['next_offset']
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 11)

    def test_large_instructions_are_previewed_and_exact_bytes_continue(self):
        folder = self.root / 'handoffs' / 'large'
        folder.mkdir()
        original = ('first\r\nÎ©ðŸ˜€\n' * 900).encode('utf-8')
        path = folder / 'instructions.md'
        path.write_bytes(original)
        page = self.archive.get_handoff('large')
        preview = page['instructions'][0]
        self.assertTrue(preview['text_truncated'])
        self.assertEqual(preview['text'], original.decode('utf-8')[:200])
        combined, offset = bytearray(), 0
        while True:
            part = self.archive.read_file(preview['path'], offset, 1024)
            bounded = public_result({'ok': True, 'result': part}, 'read_handoff_file')
            self.assertLessEqual(response_tokens(bounded), 1872)
            combined.extend(base64.b64decode(part['base64']))
            if part['eof']:
                break
            self.assertGreater(part['next_offset'], offset)
            offset = part['next_offset']
        self.assertEqual(bytes(combined), original)

    def test_cached_pre_upgrade_result_cannot_bypass_budget(self):
        payload = encode({'action': 'get_memory', 'args': {'id': 'one'}})
        job = {'id': 'a' * 64, 'payload': payload.decode(), 'sha256': sha(payload)}
        original = {'ok': True, 'result': {'text': 'PRIVATE ' + ' '.join(str(i) for i in range(20000))}}
        stored = self.archive.state / (job['id'] + '.json')
        stored.write_bytes(encode(original))
        result, _ = run_job(self.archive, job)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_detail']['code'], 'result_too_large')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(json.loads(stored.read_text('utf-8')), original)
        self.archive._reader.get_memory.assert_not_called()

    def test_large_committed_save_receipt_keeps_success(self):
        result = public_result({'ok': True, 'result': {'saved': True, 'has_warnings': True,
            'import_status': 'imported_with_warnings', 'import_result': {'added_revisions': 1,
            'warnings': ['PRIVATE ' + ' '.join(str(i) for i in range(20000))]}}}, 'save_memory')
        self.assertTrue(result['ok'])
        self.assertTrue(result['result']['saved'])
        self.assertTrue(result['result']['has_warnings'])
        self.assertEqual(result['result']['import_result']['warning_count'], 1)
        self.assertLessEqual(response_tokens(result), 1872)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_real_core_boundary_includes_operation_envelope_with_byte_fallback(self):
        import memory_budget
        with patch.object(memory_budget, '_encoders', return_value=[]):
            with memory_budget.lookup_budget(self.root) as budget:
                payload = {'records': [{'id': 'record-%s' % i, 'revision': 'b' * 64,
                    'blocks': [{'id': 'block', 'start_char': 0, 'end_char': 120}]} for i in range(2)], 'text': ''}
                while budget.fits(dict(payload, text=payload['text'] + 'x')):
                    payload['text'] += 'x'
                core = budget.finish(payload)
            operation = '0123456789abcdef' * 4
            result = public_result({'ok': True, 'result': core}, 'get_memory', operation)
            final = dict(operation_id=operation, status='completed', **result)
            self.assertLessEqual(memory_budget.token_count(final), 2000)
            self.assertLessEqual(memory_budget.token_count(final), core['budget']['charged'])
            self.assertEqual(final['result'], core)

    def test_literal_control_tokens_are_metered_in_returned_json(self):
        import memory_budget
        text = 'Source <|endoftext|> and <|endofprompt|>. 漢字'
        operation = '1' * 64
        for encoders in [[], memory_budget._encoders()]:
            with self.subTest(bpe=bool(encoders)), patch.object(memory_budget, '_encoders', return_value=encoders):
                result = public_result({'ok': True, 'result': {'text': text}}, 'get_memory', operation)
                visible = dict(operation_id=operation, status='completed', **result)
                variants = [memory_budget.serialized(visible), json.dumps(visible, ensure_ascii=False, indent=2)]
                expected = (max(len(enc.encode_ordinary(v)) for enc in encoders for v in variants)
                            if encoders else max(len(v.encode('utf-8')) for v in variants))
                self.assertEqual(result['result']['text'], text)
                self.assertLessEqual(expected, result['response_budget']['used_tokens'])
                self.assertLessEqual(result['response_budget']['used_tokens'], 2000)

    def test_huge_word_rejected_even_if_the_bpe_regex_cannot_count_it(self):
        import memory_budget
        value = {'text': 'x' * 3_000_000}
        failing = Mock()
        failing.encode.side_effect = ValueError('Max stack size exceeded for backtracking')
        # Exercise a deterministic regex failure, byte mode, and real encoders
        # when the verified local vocabularies are available.
        for encoders in [[failing], [], memory_budget._encoders()]:
            with self.subTest(encoders=len(encoders)), patch.object(memory_budget, '_encoders', return_value=encoders):
                self.assertFalse(memory_budget.fits(value))
                count = memory_budget.token_count(value)
                self.assertGreater(count, 2000)
                if not encoders or encoders == [failing]:
                    self.assertEqual(count, len(memory_budget.serialized(value).encode('utf-8')))


if __name__ == '__main__':
    unittest.main()
