"""Synthetic checks for complete, bounded, read-only archive traversal."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from memory_listing import _decode, _encode, list_records
import test_memory_search


class ListingTests(unittest.TestCase):
    setUp = test_memory_search.SearchTests.setUp
    add = test_memory_search.SearchTests.add
    packet = test_memory_search.SearchTests.packet

    def populate(self, count):
        for n in reversed(range(count)):
            self.add(f'id-{n:04d}', 'SYNTHETIC original ' + str(n))

    def test_complete_traversal_retries_and_new_arrivals(self):
        self.populate(257)
        expected = [r[0] for r in self.db.execute('SELECT id FROM memories ORDER BY id')]
        first = list_records(self.db)
        self.assertEqual(first['returned_count'], 100)
        self.assertEqual(first['total_count'], 257)
        self.add('id-0000-new', 'Arrived after scan began')
        second = list_records(self.db, cursor=first['next_cursor'])
        self.assertEqual(second, list_records(self.db, cursor=first['next_cursor']))
        third = list_records(self.db, cursor=second['next_cursor'])
        pages = [first, second, third]
        ids = [r['id'] for p in pages for r in p['records']]
        self.assertEqual(ids, expected)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([p['returned_count'] for p in pages], [100, 100, 57])
        self.assertEqual(third['returned_total'], 257)
        self.assertFalse(third['has_more'])
        self.assertIsNone(third['next_cursor'])
        self.assertEqual(list_records(self.db)['total_count'], 258)
        self.assertFalse(self.db.in_transaction)

    def test_empty_exact_page_and_changed_page_size(self):
        page = list_records(self.db)
        self.assertEqual(page['records'], [])
        self.assertEqual(page['total_count'], 0)
        self.assertIsNone(page['next_cursor'])
        self.populate(100)
        self.assertIsNone(list_records(self.db)['next_cursor'])
        first = list_records(self.db, limit=1)
        last = list_records(self.db, limit=99, cursor=first['next_cursor'])
        self.assertEqual(last['returned_total'], 100)
        self.assertIsNone(last['next_cursor'])

    def test_membership_removal_replacement_and_invalid_position_fail(self):
        self.populate(4)
        cursor = list_records(self.db, limit=1)['next_cursor']
        altered = _decode(cursor)
        altered['seen'] = 2
        with self.assertRaisesRegex(ValueError, 'cursor'):
            list_records(self.db, cursor=_encode(altered))
        self.db.execute("UPDATE memories SET id='different' WHERE id='id-0003'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'membership changed'):
            list_records(self.db, cursor=cursor)
        self.db.execute("DELETE FROM memories WHERE id='different'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'membership changed'):
            list_records(self.db, cursor=cursor)

    def test_input_validation_and_bounded_previews(self):
        for limit in (True, False, None, '100', 1.5, 0, -1, 101):
            with self.assertRaises(ValueError):
                list_records(self.db, limit=limit)
        for cursor in ('', '!', 'x' * 4097, 'e30', 'W10', 42, True, '{}'):
            with self.assertRaises(ValueError):
                list_records(self.db, cursor=cursor)
        original = 'SYNTHETIC café\r\n' * 100000
        self.add('huge', original, title='t' * 2000, source='s' * 2000)
        record = list_records(self.db)['records'][0]
        self.assertEqual(record['text'], original[:600])
        self.assertTrue(record['text_truncated'])
        self.assertEqual(len(record['title']), 240)
        self.assertEqual(len(record['source']), 240)
        self.assertEqual(self.db.execute('SELECT text FROM memories').fetchone()[0], original)

    def test_original_records_not_chunks_or_collapsed_revisions(self):
        self.packet('original.json', [dict(text='SYNTHETIC original', message_id='m1')])
        self.packet('edited.json', [dict(text='SYNTHETIC changed', message_id='m1')])
        self.db.execute('CREATE TABLE chunks(id TEXT)')
        self.db.executemany('INSERT INTO chunks VALUES (?)', [(str(i),) for i in range(20)])
        self.db.commit()
        page = list_records(self.db)
        self.assertEqual(page['total_count'], 2)
        self.assertEqual({r['text'] for r in page['records']}, {'SYNTHETIC original', 'SYNTHETIC changed'})

    def test_both_endpoints_readonly_and_caller_transaction(self):
        import server
        import server_readonly
        self.populate(3)
        before = self.db.total_changes
        for endpoint in (server, server_readonly):
            with patch.object(endpoint, 'DB', self.path):
                page = endpoint.list_memories(limit=2)
                final = endpoint.list_memories(cursor=page['next_cursor'])
                self.assertEqual(final['returned_total'], 3)
                self.assertEqual(endpoint.get_memory(final['records'][0]['id'])['text'],
                                 final['records'][0]['text'])
        with patch.object(server_readonly, 'DB', self.path), closing(server_readonly.connect()) as db:
            self.assertEqual(list_records(db)['total_count'], 3)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM memories')
        self.db.execute('BEGIN')
        list_records(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(before, self.db.total_changes)


if __name__ == '__main__':
    unittest.main()
