"""Readable navigation must preserve source identity and evidence capacity."""
import unittest
import json
from unittest import mock
import test_memory_discovery as fixtures
import memory_retrieval as retrieval
import memory_budget as budgets


class PresentationTests(unittest.TestCase):
    setUp = fixtures.DiscoveryTests.setUp
    tearDown = fixtures.DiscoveryTests.tearDown
    add = fixtures.DiscoveryTests.add
    resolve = fixtures.DiscoveryTests.resolve
    ids = fixtures.DiscoveryTests.ids

    def message_metadata(self, id, date, packet='packet'):
        self.db.execute('INSERT OR IGNORE INTO import_packets VALUES(?,?,?)',
            (packet,json.dumps({'source_date':'2026-11-03T00:00:00Z'}),'2026-11-09'))
        self.db.execute('INSERT INTO import_items VALUES(?,?,?,?)',
            (packet,0,id,json.dumps({'source_date':date})))
        self.db.commit()

    def test_message_date_comes_from_message_not_chat_title_or_import(self):
        self.add('today','Needle evidence written today.',title='Chat started 11/3/2026',
                 conversation='chat',date='2026-11-09T10:00:00Z')
        self.message_metadata('today','2026-11-08T20:26:54.234Z')
        found=retrieval.compact_search(self.db,self.root,'needle')
        hit=found['hits'][0]
        self.assertEqual(hit['message_date'],'2026-11-08T20:26:54.234000Z')
        self.assertNotIn('record_date',hit)
        timeline=retrieval.timeline_memory(self.db,self.root,ref=hit['ref'],lookup_id=found['lookup_id'])
        self.assertEqual(timeline['items'][0]['message_date'],hit['message_date'])

    def test_missing_or_invalid_message_date_is_explicit_record_date(self):
        for date in (None,'not a date','2026-02-30','0001-01-01T00:00:00+14:00'):
            with self.subTest(date=date):
                self.db.execute('DELETE FROM import_items')
                if not self.db.execute("SELECT 1 FROM memories WHERE id='unknown'").fetchone():
                    self.add('unknown','Needle evidence.',conversation='chat',date='2026-11-09T10:00:00Z')
                self.message_metadata('unknown',date)
                hit=retrieval.compact_search(self.db,self.root,'needle')['hits'][0]
                self.assertEqual(hit['record_date'],'2026-11-09T10:00:00Z')
                self.assertNotIn('message_date',hit)

    def test_conflicting_dates_do_not_silently_pick_latest(self):
        self.add('conflict','Needle evidence.',conversation='chat')
        self.message_metadata('conflict','2026-11-06T10:00:00Z')
        self.message_metadata('conflict','2026-11-08T10:00:00Z',packet='reexport')
        hit=retrieval.compact_search(self.db,self.root,'needle')['hits'][0]
        self.assertNotIn('message_date',hit)
        self.assertEqual(hit['message_date_status'],'conflicting_sources')

    def test_equivalent_offsets_agree_but_distinct_subseconds_conflict(self):
        self.add('offset','Needle evidence.',conversation='chat')
        self.message_metadata('offset','2026-11-08T10:00:00.100Z')
        self.message_metadata('offset','2026-11-08T05:00:00.100-05:00',packet='offset-export')
        hit=retrieval.compact_search(self.db,self.root,'needle')['hits'][0]
        self.assertEqual(hit['message_date'],'2026-11-08T10:00:00.100000Z')
        self.message_metadata('offset','2026-11-08T10:00:00.900Z',packet='later-export')
        hit=retrieval.compact_search(self.db,self.root,'needle')['hits'][0]
        self.assertNotIn('message_date',hit)
        self.assertEqual(hit['message_date_status'],'conflicting_sources')

    def test_complete_title_and_complete_sentence_around_keyword(self):
        title='Sensor Console handoff and exact joystick configuration for Claude (Anthropic)'
        sentence='The Sensor Console handoff contains the tested deadband function and its configuration.'
        text='Earlier background. '+sentence+' Later unrelated context.'
        self.add('note',text,title=title)
        found=retrieval.compact_search(self.db,self.root,'deadband')
        self.assertEqual(found['hits'][0]['title'],title)
        self.assertEqual(found['hits'][0]['preview'],sentence)
        target=self.resolve(found,found['hits'][0])
        self.assertEqual(text[target['start_char']:target['end_char']],'deadband')

    def test_query_selects_verified_outcome_instead_of_a_repeated_question(self):
        question = 'Did the relay reset work?'
        answer = 'The relay remained offline. The repair was never confirmed.'
        middle = '\n' + ('Earlier unrelated details. ' * 25) + '\n'
        text = question + middle + answer
        self.add('relay', text, title='Relay troubleshooting')
        start = text.index(answer)
        evidence = dict(start_char=start, end_char=len(text), revision=retrieval.source_revision(text))
        candidate = dict(id='relay',text=question,retrieval_channels=['embedding','lexical'],
                         _semantic_passage=evidence)
        with mock.patch('memory_search.search', return_value=[candidate]):
            result = retrieval.compact_search(self.db,self.root,'Was the relay repair confirmed?')
        hit = result['hits'][0]
        self.assertEqual(hit['preview'],'The repair was never confirmed.')
        self.assertNotIn('_semantic_passage',hit)
        exact = retrieval.peek_memory(self.db,self.root,ref=hit['ref'],lookup_id=result['lookup_id'],
                                      before=0,after=0)
        self.assertEqual(exact['text'],hit['preview'])
        self.assertEqual(text[exact['start_char']:exact['end_char']],hit['preview'])

    def test_stale_semantic_passage_keeps_lexical_anchor(self):
        text = 'The motor stalled.\nThe replacement worked.'
        record = dict(id='motor',text=text)
        query = 'Which motor worked?'
        spans = [dict(start_char=4, end_char=9)]
        revision = retrieval.source_revision(text)
        for evidence in (None, dict(revision='old',start_char=19,end_char=len(text)),
                         dict(revision=revision,start_char=-2,end_char=3)):
            chosen = retrieval._select_passage(record,revision,query,{'_semantic_passage':evidence},spans)
            self.assertEqual(chosen,(4,9))

    def test_explicit_identifier_beats_semantic_paraphrase(self):
        text = 'Error code RELAY_42.\nThe other relay passed its test.'
        record = dict(id='relay',text=text)
        revision = retrieval.source_revision(text)
        hit = {'_semantic_passage':dict(start_char=text.index('The other'),end_char=len(text),revision=revision)}
        chosen = retrieval._select_passage(record,revision,'Show `RELAY_42`',hit,
                                           [dict(start_char=11,end_char=19)])
        self.assertEqual(text[slice(*chosen)], 'RELAY_42')

    def test_exact_heading_identifier_preserves_boundaries(self):
        text = '# RELAY_420 versus RELAY_42\nStatus: firmware staging is pending.'
        record = dict(id='relay',text=text)
        revision = retrieval.source_revision(text)
        spans = retrieval.lexical_spans(text,'RELAY_42',revision)
        chosen = retrieval._select_passage(record,revision,'Show `RELAY_42`',{},spans)
        self.assertEqual(chosen,(text.index('RELAY_42\n'),text.index('\n')))

    def test_named_version_returns_one_verbatim_answer_and_skips_archive_search(self):
        intro = 'Filter calibration v2\r\n' + ('Earlier troubleshooting. ' * 45)
        evidence = 'The filter adjustment restored flow. The valve still leaked.'
        text = intro + '\r\n\r\n' + evidence + '\r\n\r\nNEXT PROCEDURE'
        self.add('target', text, title='Handoff | Filter calibration | v2')
        self.add('receipt', 'Filter calibration v2 saved.', title='Chat')
        question = 'What did the filter adjustment restore and what still leaked?'
        target = dict(id='target',name='Filter calibration',version='v2',question=question)
        with mock.patch('memory_search.resolve_versioned_handoff', return_value=target), \
             mock.patch('memory_search.search') as broad:
            found = retrieval.compact_search(self.db,self.root,'In Filter calibration v2, '+question)
            broad.assert_not_called()
        self.assertEqual(len(found['hits']),1)
        hit = found['hits'][0]
        self.assertEqual((hit['source_kind'],hit['preview']),('saved_handoff',evidence))
        peek = retrieval.peek_memory(self.db,self.root,ref=hit['ref'],lookup_id=found['lookup_id'],before=0,after=0)
        self.assertEqual(peek['text'],evidence)
        self.assertLessEqual(found['budget']['charged'],960)

    def test_scoped_passage_bounds_and_metadata_fallback(self):
        text = '🔎 Header\n\n' + ('padding '*160) + 'the device failed after reset.\n'
        lo,hi,kind = retrieval._handoff_passage(text,'which device failed?')
        self.assertEqual(kind,'text')
        self.assertIn('device failed',text[lo:hi])
        self.assertLessEqual(hi-lo,240)
        for query in ('','nonexistentneedle'):
            lo,hi,kind = retrieval._handoff_passage(text,query)
            self.assertEqual((text[lo:hi],kind),('🔎 Header','metadata'))

    def test_unrelated_status_line_does_not_displace_subject_evidence(self):
        text = 'The wheel deadband now filters stick drift.\nStatus: the printer calibration passed.'
        record = dict(id='joystick',text=text)
        revision = retrieval.source_revision(text)
        hit = {'_semantic_passage':dict(start_char=text.index('Status:'),end_char=len(text),revision=revision)}
        chosen = retrieval._select_passage(record,revision,'What happened to the wheel deadband on the joystick?',hit,
                                           [dict(start_char=4,end_char=9)])
        self.assertIn('stick drift',retrieval._sentence_preview(text,chosen[0]))

    def test_long_sentence_excerpt_is_marked_and_does_not_cut_words(self):
        for separator in (' ','\t','\u2003'):
            text=(('ordinary'+separator)*50)+'specialword'+separator+(('following'+separator)*50)
            preview=retrieval._sentence_preview(text,text.index('specialword'),120)
            self.assertIn('specialword',preview)
            self.assertTrue(preview.startswith('…') or preview.endswith('…'))
            self.assertTrue(all(word in {'ordinary','specialword','following'} for word in preview.strip('…').split()))

    def test_full_flag_still_reserves_reading_capacity(self):
        for i in range(15):
            self.add(f'm{i}',f'Needle choice {i} contains useful evidence.',title=f'Choice {i}')
        found=retrieval.compact_search(self.db,self.root,'needle',limit=20,full=True)
        self.assertTrue(found['full_requested'])
        self.assertLessEqual(found['budget']['charged'],960)

    def test_handoff_heading_preview_uses_original_status_sentence(self):
        text=('AURORA BATCH PRINT SPOOLER MIGRATION HANDOFF — 2030-01-20\n'
              'Requested pickup name: AURORA-Batch-Print-Migration\n'
              'Prepared for the troubleshooting team.\n'
              'Status: the spooler queue drained and all test pages printed.\n\n'
              'START HERE\nA second printer is available in the test fixture.\n')
        self.add('note',text,title='Handoff | AURORA-Batch-Print-Migration | v1')
        found=retrieval.compact_search(self.db,self.root,'AURORA Batch Print Spooler Migration Handoff')
        hit=found['hits'][0]
        self.assertEqual(hit['preview'],'Status: the spooler queue drained and all test pages printed.')
        target=self.resolve(found,hit)
        self.assertEqual(text[target['start_char']:target['end_char']],'Status:')
        peek=retrieval.peek_memory(self.db,self.root,ref=hit['ref'],lookup_id=found['lookup_id'],before=0,after=0)
        self.assertEqual(peek['text'],'Status:')

    def test_map_after_search_preserves_capacity_for_a_verified_block(self):
        text='HEADER NEEDLE\nStatus: the configuration is captured.\n\n'+('Source line with exact evidence.\n'*800)
        self.add('long',text,title='Needle handoff with the full descriptive title')
        found=retrieval.compact_search(self.db,self.root,'needle')
        mapped=retrieval.get_memory_slice(self.db,self.root,ref=found['hits'][0]['ref'],lookup_id=found['lookup_id'])
        self.assertEqual(mapped['kind'],'map')
        self.assertLessEqual(mapped['budget']['charged'],450)
        self.assertTrue(mapped['blocks'])
        self.assertIsNotNone(mapped['next_start_char'])
        block=mapped['blocks'][0]
        exact=retrieval.get_memory_slice(self.db,self.root,ref=found['hits'][0]['ref'],lookup_id=found['lookup_id'],block_id=block['block_id'])
        self.assertEqual(exact['status'],'ok')
        self.assertEqual(exact['text'],text[block['start_char']:block['end_char']])

    def test_same_text_deduplicates_but_corrections_remain_distinct(self):
        text='The needle component uses the verified source configuration. '+('Detailed context. '*6)
        self.add('copy-a',text,conversation='one')
        self.add('copy-b',text,conversation='two')
        self.add('correction','Correction: the needle component requires a different configuration.',conversation='three')
        found=retrieval.compact_search(self.db,self.root,'needle')
        self.assertEqual(len(found['hits']),2)
        group=next(hit['group'] for hit in found['hits'] if 'group' in hit)
        self.assertEqual(group['matches'],2)
        self.assertEqual(group['chats'],2)
        self.assertIn('correction',self.ids(found))

    def test_generic_identical_short_messages_do_not_merge_contexts(self):
        self.add('one','Yes.',title='Needle discussion',conversation='one')
        self.add('two','Yes.',title='Needle discussion',conversation='two')
        found=retrieval.compact_search(self.db,self.root,'Needle')
        self.assertEqual(len(found['hits']),2)

    def test_navigation_preserves_reading_budget_and_full_speaker(self):
        for i in range(10):
            self.add(f'm{i}',f'The needle option {i} has different useful evidence.',title=f'Option {i}',conversation=f'chat{i}',order=0)
        self.add('correction','Correction: needle option zero requires value twelve.',conversation='chat0',order=1)
        self.db.execute("UPDATE import_entities SET speaker='Claude (Anthropic)' WHERE id='entity-correction'")
        self.db.commit()
        found=retrieval.compact_search(self.db,self.root,'needle')
        self.assertLessEqual(found['budget']['charged'],960)
        anchor=next(hit for hit in found['hits'] if self.resolve(found,hit)['memory_id']=='m0')
        timeline=retrieval.timeline_memory(self.db,self.root,ref=anchor['ref'],lookup_id=found['lookup_id'],before=0,after=1)
        self.assertLessEqual(timeline['budget']['charged'],550)
        self.assertGreaterEqual(timeline['budget']['remaining'],490)
        correction=next(item for item in timeline['items'] if item.get('notice')=='correction_mention')
        self.assertEqual(correction['speaker'],'Claude (Anthropic)')
        self.assertNotIn('title',correction)
        self.assertLessEqual(budgets.transport_cost(timeline),timeline['budget']['charged'])


if __name__=='__main__':
    unittest.main()
