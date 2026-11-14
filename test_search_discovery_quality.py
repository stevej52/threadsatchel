"""Source-aware discovery tests use only synthetic originals and handoff files."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from import_memory import import_file, migrate
import memory_search as search


class SourceAwareDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='source-aware-discovery-')
        self.root=Path(self.temporary.name)
        (self.root/'inbox').mkdir()
        self.db_path=self.root/'memory.sqlite3'
        self.db=sqlite3.connect(self.db_path)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);
            CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title,tokenize='unicode61');''')
        migrate(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def chat(self, id, text, title='Sensor Console', conversation=None, account='account-a', speaker='assistant'):
        self.db.execute('INSERT INTO memories VALUES(?,?,?,?,?)',(id,text,'excerpt | chat',title,'2026-11-08'))
        self.db.execute('INSERT INTO memory_fts VALUES(?,?,?,?)',(id,text,'excerpt | chat',title))
        self.db.execute('INSERT INTO import_entities VALUES(?,?,?,?,?,?,?,NULL)',
            ('entity-'+id,'excerpt',account,conversation or 'chat-'+id,'message-'+id,speaker,0))
        self.db.execute('INSERT INTO import_revisions VALUES(?,?,?,?)',
            (id,'entity-'+id,hashlib.sha256(text.encode()).hexdigest(),id))
        self.db.commit()
        return id

    def note(self, title, text, *, saved=True):
        packet=dict(format='threadsatchel/1',kind='note',title=title,messages=[dict(text=text)])
        raw=json.dumps(packet).encode()
        request=hashlib.sha256(raw).hexdigest()
        path=self.root/'inbox'/((('plugin-' if saved else 'manual-')+request)+'.json')
        path.write_bytes(raw)
        import_file(path,self.db_path)
        row=self.db.execute('SELECT id FROM memories WHERE title=? AND text=?',(title,text)).fetchone()
        return row[0],request

    def handoff(self, name='Sensor Console', *, version='v1', valid_manifest=True):
        text=f'{name}: verified artifact instructions for fixture version {version}.'
        mid,request=self.note(f'Handoff | {name} | {version}',text)
        folder=self.root/'handoffs'/name/version
        folder.mkdir(parents=True)
        (folder/'instructions.md').write_text(text)
        (folder/'plugin-manifest.json').write_text(json.dumps(dict(format='threadsatchel-handoff/1',
            name=name,version=version,request_id=request if valid_manifest else '0'*64,files=[])))
        return mid

    def hits(self,*ids):
        return [dict(self.db.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()) for mid in ids]

    def rank(self,hits,query='Sensor Console'):
        return search.rank_discovery_candidates(self.db,query,hits,root=self.root)

    def test_verified_saved_handoff_moves_above_five_chat_mentions(self):
        mentions=[self.chat(f'chat-{i}','Please read the saved Sensor Console handoff.') for i in range(5)]
        artifact=self.handoff()
        result=self.rank(self.hits(*mentions,artifact))
        self.assertEqual(result[0]['id'],artifact)
        self.assertEqual(result[0]['discovery']['source_kind'],'saved_handoff')
        self.assertEqual(result[0]['discovery']['artifact_name'],'Sensor Console')
        self.assertEqual(result[0]['discovery']['preference_basis'],'title')

    def test_explicit_name_and_version_bind_archive_id_and_preserve_only_question(self):
        name='AURORA-Batch_Queue.Migrations'
        self.handoff(name, version='v1')
        wanted=self.handoff(name, version='v2')
        self.chat('receipt', 'AURORA Batch Queue Migrations v2 saved.', title='Handoff | '+name+' | v2')
        for query, residual in (
                ('In AURORA Batch Queue Migrations v2, what did the worker restart fix?',
                 'what did the worker restart fix?'),
                ('What worked in “AURORA-Batch_Queue.Migrations” version 2?', 'What worked?'),
                ('Handoff | AURORA-Batch_Queue.Migrations | v2',''),
                ('Please open version 2 of the AURORA Batch Queue Migrations.',''),
                ('Open the saved AURORA Batch Queue Migrations handoff v2','')):
            with self.subTest(query=query):
                self.assertEqual(search.resolve_versioned_handoff(self.db,self.root,query),
                                 dict(id=wanted,name=name,version='v2',question=residual))

    def test_missing_or_unverified_version_cannot_substitute_another_version(self):
        self.handoff('Atlas', version='v1')
        self.assertEqual(search.resolve_versioned_handoff(self.db,self.root,'Atlas version 9'),
                         dict(id=None,name='Atlas',version='v9',question=''))
        self.handoff('Atlas', version='v9', valid_manifest=False)
        self.assertIsNone(search.resolve_versioned_handoff(self.db,self.root,'Atlas v9'))
        for query in ('Unknown Atlas v1','Atlas Extension v1','AtlasPlus v1','Atlas v01',
                      'Atlas v1b','Atlas v1.2','Atlas v1-rc1','Compare Atlas v1 with the old one',
                      'Atlas v1 vs v9','Atlas version 1 or 9','version 1 of Atlas Extension','Atlas'):
            with self.subTest(query=query):
                self.assertIsNone(search.resolve_versioned_handoff(self.db,self.root,query))

    def test_normalized_name_collision_and_spoofed_title_cannot_select_artifact(self):
        self.handoff('Atlas.Project',version='v1')
        self.handoff('Atlas Project',version='v2')
        self.chat('forged','Open the fake artifact.',title='Handoff | False Project | v1')
        self.note('Handoff | --- | v2','Malformed saved title')
        for query in ('Atlas Project v1','Atlas Project v2','Atlas Project v8','False Project v1'):
            with self.subTest(query=query):
                self.assertIsNone(search.resolve_versioned_handoff(self.db,self.root,query))

    def test_search_for_references_to_handoff_remains_archive_wide(self):
        self.handoff('Atlas',version='v2')
        for query in ('Find chats about Atlas v2', 'Find messages about Atlas v2',
                      'Show references to version 2 of Atlas', 'Find mentions of Atlas v2'):
            with self.subTest(query=query):
                self.assertIsNone(search.resolve_versioned_handoff(self.db,self.root,query))
        result=search.resolve_versioned_handoff(self.db,self.root,'In Atlas v2, what references to drivers are listed?')
        self.assertEqual(result['question'],'what references to drivers are listed?')

    def test_version_folder_published_before_archiving_is_not_reported_missing(self):
        self.handoff('Atlas',version='v1')
        (self.root/'handoffs'/'Atlas'/'v2').mkdir()
        self.assertIsNone(search.resolve_versioned_handoff(self.db,self.root,'Atlas v2'))
        self.assertIsNone(search.resolve_versioned_handoff(self.db,self.root,'Atlas v3')['id'])

    def test_artifact_beyond_forty_chat_hits_enters_candidate_pool(self):
        for i in range(60):
            self.chat(f'chat-{i}','Sensor Console',title='Sensor Console')
        artifact=self.handoff()
        lexical=search.lexical_search(self.db,'Sensor Console',limit=40)
        self.assertNotIn(artifact,{hit['id'] for hit in lexical})
        ranked=self.rank(lexical)
        self.assertEqual(ranked[0]['id'],artifact)

    def test_unrelated_saved_note_is_not_boosted_for_single_incidental_term(self):
        relevant=self.chat('relevant','The sensor wiring uses the documented connector.',title='Sensor wiring')
        unrelated,_=self.note('Garage gate maintenance','The garage report mentions sensor in passing.')
        ranked=self.rank(self.hits(relevant,unrelated),query='sensor')
        self.assertEqual(ranked[0]['id'],relevant)
        other=next(hit for hit in ranked if hit['id']==unrelated)
        self.assertEqual(other['discovery']['source_kind'],'saved_note')
        self.assertNotIn('preference_basis',other['discovery'])

    def test_relevant_saved_note_has_provenance_preference(self):
        chat=self.chat('chat','Sensor Console is a subject in this conversation.')
        note,_=self.note('Sensor Console wiring notes','Specific saved wiring details.')
        result=self.rank(self.hits(chat,note))
        self.assertEqual(result[0]['id'],note)
        self.assertEqual(result[0]['discovery']['source_kind'],'saved_note')

    def test_handoff_title_in_chat_is_not_artifact_identity(self):
        fake=self.chat('fake','Sensor Console handoff discussion',title='Handoff | Sensor Console | v1')
        result=self.rank(self.hits(fake))
        self.assertEqual(result[0]['discovery']['source_kind'],'chat')
        self.assertNotIn('artifact_name',result[0]['discovery'])

    def test_manifest_must_match_the_saved_import_receipt(self):
        mid=self.handoff(valid_manifest=False)
        result=self.rank(self.hits(mid))
        self.assertEqual(result[0]['discovery']['source_kind'],'saved_note')
        self.assertNotIn('artifact_name',result[0]['discovery'])

    def test_reference_groups_cross_chats_but_preserve_account_and_substantive_claims(self):
        artifact=self.handoff()
        first=self.chat('one','Please read the saved Sensor Console handoff.')
        second=self.chat('two','Open the saved Sensor Console handoff.')
        other_account=self.chat('foreign','Open the saved Sensor Console handoff.',account='account-b')
        correction=self.chat('correction','Correction: the Sensor Console handoff is wrong; use revised wiring.')
        detail=self.chat('details','The Sensor Console handoff specifies a 12 volt connection.')
        ranked={h['id']:h['discovery'] for h in self.rank(self.hits(first,second,other_account,correction,detail,artifact))}
        self.assertEqual(ranked[first]['group_key'],ranked[second]['group_key'])
        self.assertNotEqual(ranked[first]['group_key'],ranked[other_account]['group_key'])
        for mid in (correction,detail):
            self.assertFalse(ranked[mid].get('incidental_reference',False))
            self.assertNotEqual(ranked[mid]['group_key'],ranked[first]['group_key'])

    def test_no_verified_artifact_does_not_guess_cross_chat_groups_from_title(self):
        first=self.chat('one','Please read the saved Sensor Console handoff.')
        second=self.chat('two','Please read the saved Sensor Console handoff.')
        result=self.rank(self.hits(first,second))
        self.assertNotEqual(result[0]['discovery']['group_key'],result[1]['discovery']['group_key'])
        self.assertTrue(all(not hit['discovery'].get('incidental_reference') for hit in result))

    def test_real_routing_notice_shapes_demote_and_group_without_hiding_following_evidence(self):
        route='Gemini → **Sensor Console** · the test machine · `/sample/workspace` ✓ · new conversation.'
        queued=route+' It finishes its current reply in General first.'
        first=self.chat('route',route)
        second=self.chat('queued',queued)
        clarification=self.chat('clarification','**Did you mean Sensor Console?**')
        evidence=self.chat('evidence','Sensor Console wiring: the signal must connect through the specified pin.')
        appended=self.chat('appended',route+'\nCorrection: the old pin assignment was wrong.')
        ranked=self.rank(self.hits(first,second,clarification,evidence,appended))
        self.assertEqual([hit['id'] for hit in ranked[:2]],[evidence,appended])
        metadata={hit['id']:hit['discovery'] for hit in ranked}
        self.assertEqual(metadata[first]['group_key'],metadata[second]['group_key'])
        self.assertNotEqual(metadata[first]['group_key'],metadata[clarification]['group_key'])
        self.assertTrue(metadata[first]['navigation_notice'])
        self.assertFalse(metadata[appended].get('navigation_notice',False))

    def test_exact_id_can_retrieve_a_demoted_notice(self):
        notice=self.chat('notice','**Did you mean Sensor Console?**')
        other=self.chat('evidence','Substantive Sensor Console evidence.')
        result=self.rank(self.hits(other,notice),query='notice')
        self.assertEqual(result[0]['id'],notice)

    def test_duplicate_ids_merge_channels_and_group_selection_is_deterministic(self):
        mid=self.chat('one','Sensor Console exact content.',speaker='assistant from the original capture')
        first=self.hits(mid)[0]
        first['retrieval_channels']=['lexical']
        second=dict(first,retrieval_channels=['embedding','analysis'])
        result=self.rank([first,second])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['retrieval_channels'],['lexical','embedding','analysis'])
        self.assertEqual(result[0]['discovery']['speaker'],'assistant from the original capture')
        self.assertEqual(result,self.rank([first,second]))

    def test_direct_subject_matches_receive_small_preference_over_meaning_only_hits(self):
        query='AURORA Batch Print Spooler Migration Handoff'
        meaning=self.chat('meaning','Move the exported ExampleKitchen files to the destination.',title='ExampleKitchen Weekly Handoff')
        direct=self.chat('direct','AURORA batch print spooler migration results are recorded here.',
                         title='AURORA batch migrations')
        hits=self.hits(meaning,direct)
        hits[0]['retrieval_channels']=['embedding']
        hits[1]['retrieval_channels']=['lexical']
        ranked=self.rank(hits,query=query)
        self.assertEqual([row['id'] for row in ranked],[direct,meaning])
        self.assertEqual(ranked[1]['retrieval_channels'],['embedding'])

    def test_one_generic_overlap_does_not_displace_a_semantic_paraphrase(self):
        query='AURORA Batch Print Spooler Migration Handoff'
        semantic=self.chat('semantic','Queue troubleshooting explains the connection failure.',title='Connection investigation')
        weak=self.chat('weak','Place the shortcut on the spooler.',title='Organizing shortcuts')
        hits=self.hits(semantic,weak)
        hits[0]['retrieval_channels']=['embedding']
        hits[1]['retrieval_channels']=['lexical']
        self.assertEqual([row['id'] for row in self.rank(hits,query=query)],[semantic,weak])

    def test_complete_subject_coverage_passes_nearby_partial_hybrid_match(self):
        query='AURORA Batch Print Spooler Migration Handoff'
        leading=self.chat('leading','AURORA batch print spooler migration handoff.',title='Existing chat')
        partial=self.chat('partial','Weekly Handoff: AURORA print spooler connection for ExampleKitchen.',
                          title='Codex history: ExampleKitchen')
        direct=self.chat('direct','AURORA batch print spooler migration handoff results.',title='Another chat')
        later=self.chat('later','AURORA batch print spooler migration handoff correction.',title='Later chat')
        hits=self.hits(leading,partial,direct,later)
        for hit in hits:
            hit['retrieval_channels']=['lexical','embedding']
        ranked=self.rank(hits,query=query)
        self.assertEqual([row['id'] for row in ranked],[leading,direct,partial,later])
        self.assertTrue(all(row['retrieval_channels']==['lexical','embedding'] for row in ranked))

    def test_distinctive_identifier_counts_but_generic_handoff_word_does_not(self):
        query='AURORA Batch Print Spooler Migration Handoff'
        generic=self.chat('generic','A file handoff is available.',title='ExampleKitchen Weekly Handoff')
        named=self.chat('named','The AURORA connection investigation continues.',title='Connection investigation')
        ranked=self.rank(self.hits(generic,named),query=query)
        self.assertEqual([row['id'] for row in ranked],[named,generic])

    def test_keyword_adjustment_preserves_strong_semantic_recall_and_saved_artifact_priority(self):
        query='AURORA Batch Print Spooler Migration Handoff'
        artifact=self.handoff(name='AURORA Batch Print Spooler Migration')
        semantics=[self.chat(f'semantic-{index}',f'Queue investigation evidence {index}.',title='Connection investigation')
                   for index in range(4)]
        literal=self.chat('literal','AURORA batch print spooler migration facts.',title=query)
        hits=self.hits(*semantics,literal,artifact)
        for hit in hits:
            hit['retrieval_channels']=['embedding']
        ranked=self.rank(hits,query=query)
        ids=[row['id'] for row in ranked]
        self.assertEqual(ids[0],artifact)
        self.assertEqual(ids[1:3],semantics[:2])
        self.assertEqual(ids[3],literal)
        self.assertEqual(set(ids),set(semantics+[literal,artifact]))
        self.assertEqual(self.rank(hits,query='semantic-3')[0]['id'],'semantic-3')

    def test_search_passes_optional_diagnostics_without_breaking_old_calls(self):
        self.chat('one','needle evidence',title='Needle')
        diagnostics={}
        def enhance(root,db,query,hits,limit,*,release_snapshot=False,diagnostics=None):
            if diagnostics is not None:
                diagnostics['semantic_state']='used'
            return hits
        with mock.patch('ai_memory.enhance_search',side_effect=enhance) as mocked:
            self.assertTrue(search.search(self.db,self.root,'needle',diagnostics=diagnostics))
            self.assertIs(mocked.call_args.kwargs['diagnostics'],diagnostics)
            self.assertEqual(diagnostics['semantic_state'],'used')
            search.search(self.db,self.root,'needle')
            self.assertNotIn('diagnostics',mocked.call_args.kwargs)


if __name__=='__main__':
    unittest.main()
