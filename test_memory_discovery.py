"""Broader discovery and timeline tests use synthetic, temporary sources only."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import memory_budget as budgets
import memory_retrieval as retrieval
from import_memory import migrate


def use_test_encoding_when_offline(test):
    """Exercise selection, identity and pagination without downloading a BPE.

    The byte-bound path has separate tests. Release checks with verified vocab
    files use both actual encoders, including real transport-token assertions.
    """
    if not budgets._encoders():
        class FixtureEncoding:
            def encode(self, value, **kwargs):
                return range((len(value.encode('utf-8'))+3)//4)
        test.enterContext(mock.patch.object(budgets, '_encoders', return_value=[FixtureEncoding()]))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        use_test_encoding_when_offline(self)
        self.temporary = tempfile.TemporaryDirectory(prefix='memory-discovery-')
        self.root = Path(self.temporary.name)
        self.db = sqlite3.connect(self.root/'memory.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);
            CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title,tokenize='unicode61');
        ''')
        migrate(self.root/'memory.sqlite3')
        self.hybrid = mock.patch('memory_search.search', return_value=[])
        self.hybrid.start()

    def tearDown(self):
        self.hybrid.stop()
        self.db.close()
        self.temporary.cleanup()

    def add(self, id, text, *, title='Source', conversation=None, account='account-a',
            order=None, date='2026-11-08T12:00:00Z', entity=None, canonical=None):
        self.db.execute('INSERT INTO memories VALUES(?,?,?,?,?)', (id,text,'fixture',title,date))
        self.db.execute('INSERT INTO memory_fts VALUES(?,?,?,?)', (id,text,'fixture',title))
        if conversation is not None:
            eid = entity or 'entity-'+id
            linked = eid if canonical and canonical != id else None
            if linked:
                eid = 'alias-entity-'+id
            self.db.execute('INSERT OR IGNORE INTO import_entities VALUES(?,?,?,?,?,?,?,?)',
                (eid,'excerpt',account,conversation,'message-'+id,'assistant',order,linked))
            self.db.execute('INSERT INTO import_revisions VALUES(?,?,?,?)',
                (id,eid,retrieval.source_revision(text),canonical or id))
        self.db.commit()

    def resolve(self, result, item):
        return budgets.resolve_reference(self.root, result['lookup_id'], item['ref'])

    def ids(self, result, key='hits'):
        return [self.resolve(result,item)['memory_id'] for item in result[key]]

    def assert_bounded(self, result):
        self.assertLessEqual(budgets.transport_cost(result), result['budget']['charged'])
        self.assertGreaterEqual(result['budget']['remaining'],0)
        self.assertLessEqual(result['budget']['charged'],2000)

    def test_default_ten_choices_are_not_crowded_by_first_rich_record(self):
        for index in range(15):
            self.add(f'm{index:02d}', f'needle option {index} has exact evidence',title=f'Option {index}')
        huge=dict(summary='long '*2000,conversation_id='conversation',evidence=[dict(quote='x'*2000)])
        with mock.patch.object(retrieval,'_catalogue',return_value=huge) as catalogue, \
             mock.patch.object(retrieval,'_conversation',return_value=huge) as conversation, \
             mock.patch('ai_memory.note_retrieved') as priority:
            result=retrieval.compact_search(self.db,self.root,'needle')
        # Message/record timestamps are now part of every choice. Preserve the
        # navigation cap instead of dropping dates or crowding out evidence.
        self.assertGreaterEqual(len(result['hits']),8)
        self.assertLessEqual(len(result['hits']),10)
        self.assertEqual(len(result['hits'])+result['omitted_hits'],10)
        self.assertEqual(len(set(self.ids(result))),len(result['hits']))
        catalogue.assert_not_called()
        conversation.assert_not_called()
        priority.assert_not_called()
        self.assertTrue(all('revision' not in item and 'id' not in item for item in result['hits']))
        self.assert_bounded(result)

    def test_twenty_requested_choices_preserve_evidence_budget(self):
        for index in range(25):
            self.add(f'm{index:02d}',f'needle choice {index}',title=f'Choice {index}')
        result=retrieval.compact_search(self.db,self.root,'needle',limit=20)
        self.assertGreaterEqual(len(result['hits']),8)
        self.assertLessEqual(len(result['hits']),20)
        self.assertEqual(result['candidate_count'],20)
        self.assertEqual(len(set(self.ids(result))),len(result['hits']))
        self.assertEqual(len(result['hits'])+result['omitted_hits'],20)
        self.assert_bounded(result)
        self.assertLessEqual(result['budget']['charged'],960)

    def test_canonical_aliases_deduplicate_but_different_revisions_survive(self):
        original='needle baseline'
        self.add('original',original,conversation='chat',order=0)
        self.add('alias',original,conversation='chat',order=0,entity='entity-original',canonical='original')
        self.add('revised','needle Correction: the baseline was wrong',conversation='chat',order=0,entity='entity-original')
        for index in range(8):
            self.add(f'other-{index}',f'needle distinct option {index}',conversation='chat',order=index+1)
        result=retrieval.compact_search(self.db,self.root,'needle',limit=10)
        ids=self.ids(result)
        self.assertEqual(len(ids),10)
        self.assertIn('revised',ids)
        self.assertEqual(len({'original','alias'} & set(ids)),1)

    def test_changed_alias_text_is_not_hidden_by_stale_canonical_pointer(self):
        self.add('original','needle original',conversation='chat',order=0)
        self.add('alias','needle distinct changed text',conversation='chat',order=0,
                 entity='entity-original',canonical='original')
        result=retrieval.compact_search(self.db,self.root,'needle')
        self.assertEqual(set(self.ids(result)),{'original','alias'})

    def test_ref_resolves_exact_unicode_range_and_legacy_id_alias(self):
        text='🌊 before\r\nneedle e\u0301 漢字\r\nafter'
        self.add('unicode',text)
        found=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        ref=found['hits'][0]['ref']
        self.assertRegex(ref,r'^r[0-9a-f]{12}$')
        start,end=text.index('needle'),text.index('after')
        sliced=retrieval.get_memory_slice(self.db,self.root,id=ref,lookup_id=found['lookup_id'],
                                           start_char=start,end_char=end)
        self.assertEqual(sliced['text'],text[start:end])
        self.assertEqual(sliced['revision'],retrieval.source_revision(text))
        self.assert_bounded(sliced)

    def test_ref_cannot_resolve_under_another_valid_lookup(self):
        self.add('a','alpha needle')
        self.add('b','beta marker')
        first=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        second=retrieval.compact_search(self.db,self.root,'marker',limit=1)
        self.assertNotEqual(first['hits'][0]['ref'],second['hits'][0]['ref'])
        with self.assertRaisesRegex(ValueError,'Unknown or expired'):
            retrieval.get_memory_slice(self.db,self.root,ref=first['hits'][0]['ref'],lookup_id=second['lookup_id'])
        with self.assertRaisesRegex(ValueError,'lookup_id'):
            retrieval.get_memory_slice(self.db,self.root,ref=first['hits'][0]['ref'])

    def test_explicit_ref_and_literal_original_id_collision_are_unambiguous(self):
        self.add('original','needle original evidence')
        found=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        ref=found['hits'][0]['ref']
        self.add(ref,'a distinct literal original ID')
        with budgets.lookup_budget(self.root,lookup_id=found['lookup_id']) as budget:
            budget.authorize(ref)
            budget.finish(dict(status='fixture_scope_authorized'))
        actual=retrieval.get_memory_slice(self.db,self.root,id=ref,lookup_id=found['lookup_id'])
        referenced=retrieval.get_memory_slice(self.db,self.root,ref=ref,lookup_id=found['lookup_id'])
        self.assertEqual(actual['text'],'a distinct literal original ID')
        self.assertEqual(referenced['text'],'needle original evidence')

    def test_stale_ref_rejects_get_peek_and_timeline(self):
        self.add('m','needle old evidence',conversation='chat',order=0)
        found=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        ref=found['hits'][0]['ref']
        self.db.execute('UPDATE memories SET text=? WHERE id=?',('replacement evidence','m'))
        self.db.commit()
        for operation in (retrieval.get_memory_slice,retrieval.peek_memory,retrieval.timeline_memory):
            result=operation(self.db,self.root,ref=ref,lookup_id=found['lookup_id'])
            self.assertEqual(result['status'],'stale_revision')
            self.assertNotIn('text',result)
            self.assertNotIn('items',result)

    def test_timeline_surfaces_later_correction_and_isolates_account_and_conversation(self):
        self.add('before','Original context',conversation='chat',order=0)
        self.add('anchor','needle claimed setting is 10',conversation='chat',order=1)
        self.add('correction','Correction: the earlier setting is wrong; use 12.',conversation='chat',order=2)
        self.add('after','This remains a reported result.',conversation='chat',order=3)
        self.add('other-chat','Correction: unrelated conversation',conversation='other',order=2)
        self.add('other-account','Correction: unrelated account',conversation='chat',account='account-b',order=2)
        found=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        timeline=retrieval.timeline_memory(self.db,self.root,ref=found['hits'][0]['ref'],
            lookup_id=found['lookup_id'],before=1,after=2)
        self.assertEqual(self.ids(timeline,'items'),['before','anchor','correction','after'])
        self.assertEqual(timeline['order_basis'],'source_order')
        correction=next(row for row in timeline['items'] if row.get('notice')=='correction_mention')
        self.assertEqual(self.resolve(timeline,correction)['memory_id'],'correction')
        exact=retrieval.get_memory_slice(self.db,self.root,ref=correction['ref'],lookup_id=timeline['lookup_id'])
        self.assertEqual(exact['text'],'Correction: the earlier setting is wrong; use 12.')
        self.assertEqual(timeline['labels'],'source_mentions_not_verified_truth')
        self.assertLessEqual(sum(result['budget']['charged'] for result in (found,timeline,exact)),2000)
        self.assert_bounded(timeline)

    def test_timeline_record_dates_normalize_timezone_offsets(self):
        self.add('early','early',conversation='chat',date='2026-11-08T10:00:00+02:00')
        self.add('anchor','middle',conversation='chat',date='2026-11-08T09:00:00Z')
        self.add('late','late',conversation='chat',date='2026-11-08T09:00:00-02:00')
        result=retrieval.timeline_memory(self.db,self.root,id='anchor',before=1,after=1)
        self.assertEqual(self.ids(result,'items'),['early','anchor','late'])
        self.assertEqual(result['order_basis'],'record_date')
        self.assertIn('import dates',result['ordering_note'])

    def test_correction_preview_gets_its_own_location_ref_without_changing_search_ref(self):
        self.add('anchor','needle earlier proposal',conversation='chat',order=0)
        text='needle opening context. '+'middle '*50+'Correction: the earlier claim was wrong.'
        self.add('later',text,conversation='chat',order=1)
        found=retrieval.compact_search(self.db,self.root,'needle',limit=2)
        references={self.resolve(found,item)['memory_id']:item['ref'] for item in found['hits']}
        old=self.resolve(found,dict(ref=references['later']))
        timeline=retrieval.timeline_memory(self.db,self.root,ref=references['anchor'],
            lookup_id=found['lookup_id'],before=0,after=1)
        correction=next(item for item in timeline['items'] if item.get('notice')=='correction_mention')
        current=self.resolve(timeline,correction)
        self.assertNotEqual(correction['ref'],references['later'])
        self.assertEqual(current['start_char'],text.index('Correction'))
        self.assertEqual(self.resolve(found,dict(ref=references['later'])),old)
        peek=retrieval.peek_memory(self.db,self.root,ref=correction['ref'],
                                  lookup_id=found['lookup_id'],before=0,after=0)
        self.assertEqual(peek['text'],'Correction')

    def test_timeline_without_conversation_does_not_guess_from_similar_title(self):
        self.add('plain','needle',title='Same title')
        self.add('other','Correction: another conversation',title='Same title',conversation='chat',order=0)
        result=retrieval.timeline_memory(self.db,self.root,id='plain')
        self.assertEqual(result['status'],'timeline_unavailable')
        self.assertNotIn('items',result)

    def test_omitted_rows_do_not_publish_hidden_refs_and_fallback_is_bounded(self):
        for index in range(20):
            self.add(f'm{index}', 'needle '+('🌊漢字'*20),title='🌊'*30)
        with mock.patch.object(budgets,'_encoders',return_value=[]):
            result=retrieval.compact_search(self.db,self.root,'needle',limit=20)
            self.assert_bounded(result)
        cache=budgets.cache_connect(self.root)
        try:
            count=cache.execute('SELECT COUNT(*) FROM lookup_refs WHERE lookup_id=?',(result['lookup_id'],)).fetchone()[0]
            self.assertEqual(count,len(result['hits']))
            self.assertEqual(result['omitted_hits'],20-count)
        finally:
            cache.close()

    def test_expired_lookup_removes_reference_rows(self):
        self.add('m','needle')
        found=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        cache=budgets.cache_connect(self.root)
        with cache:
            cache.execute('UPDATE lookups SET created=0 WHERE id=?',(found['lookup_id'],))
        cache.close()
        with self.assertRaisesRegex(ValueError,'Unknown or expired'):
            budgets.resolve_reference(self.root,found['lookup_id'],found['hits'][0]['ref'])
        cache=budgets.cache_connect(self.root)
        self.assertEqual(cache.execute('SELECT COUNT(*) FROM lookup_refs').fetchone()[0],0)
        cache.close()

    def test_selected_outcome_card_retains_exact_quotes_not_invented_stages(self):
        text='The attempt failed. Correction: use the tested setting.'
        self.add('m',text)
        card=dict(stages={
            'result':dict(label='corrected_as_reported',quote='Correction',start_char=20,end_char=30,chunk_id='c'),
            'decision':dict(label='proposed',quote='invented quote',start_char=0,end_char=10,chunk_id='c')})
        with mock.patch('memory_catalogue.catalogue_for_record',return_value=dict(outcome_card=card)):
            result=retrieval.get_memory_slice(self.db,self.root,id='m')
        outcome=result['catalogue']['outcome_card']
        self.assertEqual(set(outcome['stages']),{'result'})
        self.assertTrue(outcome['interpretation'])
        self.assertTrue(outcome['partial'])


if __name__=='__main__':
    unittest.main()
