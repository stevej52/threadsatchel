import base64
import json
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch
from pathlib import Path
from connector.connector import Archive, Diagnostics, OperationError, encode, error_detail, main, run_job, secure_path, safe_name, sha

class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'handoffs').mkdir()
        self.imports=[]
        self.archive=Archive(self.root,self.root/'state',importer=self.importer)
    def tearDown(self): self.temp.cleanup()
    def importer(self,packet,delivery):
        self.imports.append((packet,delivery))
        return {'saved':True,'sha256':'synthetic'}
    def args(self): return {'request_key':'test-save-1','name':'Test Handoff','instructions':'Exact instructions\nÎ©\n','files':[{'name':'sample.bin','base64':base64.b64encode(b'\x00\xffEXACT\r\n').decode()}]}
    def test_save_retry_and_bytes(self):
        args=self.args()
        result=self.archive.execute('save_handoff',args)
        self.assertEqual(result,self.archive.execute('save_handoff',args))
        self.assertEqual(len(self.imports),1)
        output=self.archive.read_file('Test Handoff/v1/sample.bin',0,4)
        self.assertEqual(base64.b64decode(output['base64']),b'\x00\xffEX')
        self.assertFalse(output['eof'])
        self.assertEqual(self.archive.get_handoff('Test Handoff')['instructions'][0]['text'],args['instructions'])
    def test_new_version_preserves_old(self):
        a=self.args(); self.archive.execute('save_handoff',a)
        a={**a,'request_key':'test-save-2','instructions':'New instructions'}
        self.assertEqual(self.archive.execute('save_handoff',a)['version'],'v2')
        self.assertEqual((self.root/'handoffs/Test Handoff/v1/instructions.md').read_text(encoding='utf-8'),'Exact instructions\nÎ©\n')
    def test_conflicting_key_rejected(self):
        a=self.args(); self.archive.execute('save_handoff',a)
        with self.assertRaises(ValueError): self.archive.execute('save_handoff',{**a,'instructions':'Changed'})
    def test_legacy_read(self):
        (self.root/'handoffs/File-Handoff-1.md').write_text('Legacy instructions')
        result=self.archive.get_handoff('File Handoff 1')
        self.assertEqual(result['instructions'][0]['text'],'Legacy instructions')
    def test_traversal_and_hidden_rejected(self):
        for path in ('../config.json','/etc/passwd','C:/secret','folder/../../secret','.incoming/test','folder\\file'):
            with self.subTest(path=path),self.assertRaises(ValueError): secure_path(self.root/'handoffs',path)
    def test_reserved_name_and_duplicate_files(self):
        for name in ('NUL','CON.txt','../bad','bad.','name:stream'):
            with self.subTest(name=name),self.assertRaises(ValueError): safe_name(name)
        args=self.args(); args['files']=[{'name':'instructions.md','base64':'YQ=='}]
        with self.assertRaises(ValueError): self.archive.execute('save_handoff',args)
    def test_no_arbitrary_execution(self):
        with self.assertRaises(ValueError): self.archive.execute('run_command',{'command':'anything'})
    def test_paginated_listing_delegates_readonly_without_import(self):
        self.archive._reader = Mock()
        page = {'records': [{'id': 'synthetic-id'}], 'total_count': 101, 'next_cursor': 'cursor'}
        self.archive._reader.list_memories.return_value = page
        self.assertEqual(self.archive.execute('list_memories', {'limit': 100, 'cursor': 'opaque'}), page)
        self.archive._reader.list_memories.assert_called_once_with(limit=100, cursor='opaque')
        self.assertEqual(self.imports, [])
    def test_import_failure_then_retry_no_extra_version(self):
        args=self.args()
        self.archive._importer=lambda p,d: (_ for _ in ()).throw(ValueError('Synthetic failure'))
        with self.assertRaises(ValueError): self.archive.execute('save_handoff',args)
        self.archive._importer=self.importer
        self.assertEqual(self.archive.execute('save_handoff',args)['version'],'v1')
        self.assertFalse((self.root/'handoffs/Test Handoff/v2').exists())
    def test_packet_save_idempotent(self):
        args={'request_key':'packet-1','packet':{'format':'threadsatchel/1','kind':'note','messages':[{'text':'SYNTHETIC test'}]}}
        self.archive.execute('save_memory',args); self.archive.execute('save_memory',args)
        self.assertEqual(len(self.imports),1)
    def test_reject_oversized_handoff(self):
        args=self.args(); args['files']=[{'name':'oversize.bin','base64':base64.b64encode(b'x'*(1024*1024+1)).decode()}]
        with self.assertRaises(ValueError): self.archive.execute('save_handoff',args)

    def job(self, args=None, action='save_memory', operation='a'*64):
        args = args or {'request_key':'SECRET_REQUEST_KEY','packet':{'format':'threadsatchel/1','kind':'note','messages':[{'speaker':'user','text':'PRIVATE_PACKET_TEXT'}]}}
        payload=encode({'action':action,'args':args})
        return {'id':operation,'payload':payload.decode(),'sha256':sha(payload),'lease':'SECRET_LEASE'}

    def real_import_archive(self):
        (self.root/'inbox').mkdir(exist_ok=True)
        archive=Archive(self.root,self.root/'state')
        archive._credential_guard=Mock()
        archive._credential_guard.SECRET.search.return_value=None
        archive._credential_guard.contains_possible_credential.return_value=False
        return archive

    def valid_receipt(self, **updates):
        packet=json.loads(self.job()['payload'])['args']['packet']
        identity=sha(b'save_memory\nSECRET_REQUEST_KEY')
        receipt={'file':str((self.root/'inbox'/('plugin-'+identity+'.json')).resolve()),'sha256':sha(encode(packet)),
                 'added_revisions':1,'reused_messages':0,'linked_entities':0,'repeated_packets':0,'uncertain':[],'warnings':[]}
        receipt.update(updates)
        return receipt

    def test_importer_detail_persisted_without_sensitive_output(self):
        archive=self.real_import_archive()
        run=Mock(returncode=1, stdout=encode({'error':'PRIVATE_PACKET_TEXT SECRET_TOKEN', 'error_detail':{'code':'invalid_text','message':'SECRET_TOKEN','location':'$.messages[0].text'}}), stderr=b'SECRET_TOKEN PRIVATE_STDERR')
        job=self.job()
        with patch('connector.connector.subprocess.run', return_value=run):
            result,action=run_job(archive,job)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_detail']['code'],'invalid_text')
        self.assertEqual(result['error_detail']['location'],'$.messages[0].text')
        self.assertTrue(result['error_detail']['original_retained'])
        self.assertEqual(result['error_detail']['sha256'],sha(next((self.root/'inbox').glob('*.json')).read_bytes()))
        self.assertFalse((self.root/'state'/('a'*64+'.json')).exists())
        self.assertEqual(list((self.root/'state/writes').glob('*')),[])
        disk=(self.root/'state/connector-events.jsonl').read_text('utf-8')+(self.root/'state/failures'/('a'*64+'.json')).read_text('utf-8')+json.dumps(result)
        for secret in ('PRIVATE_PACKET_TEXT','SECRET_TOKEN','PRIVATE_STDERR','SECRET_REQUEST_KEY','SECRET_LEASE'):
            self.assertNotIn(secret,disk)
        self.assertEqual(archive.execute('recent_save_errors',{})['errors'][0]['operation_id'],'a'*64)

    def test_failed_operation_reexecutes_then_receipt_prevents_duplicates(self):
        job=self.job()
        self.archive._importer=Mock(side_effect=[ValueError('PRIVATE_PACKET_TEXT'),{'saved':True,'sha256':'synthetic'}])
        self.assertFalse(run_job(self.archive,job)[0]['ok'])
        self.assertTrue(run_job(self.archive,job)[0]['ok'])
        self.assertTrue(run_job(self.archive,job)[0]['ok'])
        self.assertEqual(self.archive._importer.call_count,2)
        failure=self.archive.execute('recent_save_errors',{})['errors'][0]
        self.assertIn('resolved_at',failure)
        self.assertEqual(failure['error_detail']['code'],'invalid_request')

    def test_timeout_retains_packet_and_reports_uncertain(self):
        archive=self.real_import_archive()
        failure=subprocess.TimeoutExpired(['SECRET_CMD'],120,output=b'PRIVATE_PACKET_TEXT',stderr=b'SECRET_TOKEN')
        with patch('connector.connector.subprocess.run',side_effect=failure):
            result,_=run_job(archive,self.job())
        self.assertEqual(result['error_detail']['code'],'importer_timeout')
        self.assertEqual(result['error_detail']['outcome'],'uncertain')
        self.assertTrue(result['error_detail']['retryable'])
        self.assertEqual(len(list((self.root/'inbox').glob('*.json'))),1)
        self.assertNotIn('SECRET_CMD',json.dumps(result))

    def test_success_with_warnings_exposed_and_cached(self):
        archive=self.real_import_archive()
        receipt=self.valid_receipt(has_warnings=True,import_status='imported_with_warnings',warnings=[{'code':'extra_fields_preserved','location':'$','field_count':1}])
        with patch('connector.connector.subprocess.run',return_value=Mock(returncode=0,stdout=encode(receipt),stderr=b'')) as run:
            result,_=run_job(archive,self.job())
            repeated,_=run_job(archive,self.job())
        self.assertTrue(result['ok'])
        self.assertTrue(result['result']['has_warnings'])
        self.assertEqual(result['result']['import_status'],'imported_with_warnings')
        self.assertEqual(result,repeated)
        self.assertEqual(run.call_count,1)

    def test_failure_survives_gateway_completion_failure(self):
        config={'archive':str(self.root),'key':'SECRET_KEY','access_token':'SECRET_TOKEN','endpoint':'https://example.invalid/api/agent'}
        (self.root/'memory.sqlite3').touch()
        config_path=self.root/'config.dpapi';config_path.write_bytes(b'encrypted')
        job=self.job(); self.archive._importer=Mock(side_effect=ValueError('PRIVATE_PACKET_TEXT'))
        with patch('connector.connector.sys.argv',['connector','--once','--config',str(config_path)]), \
             patch('connector.connector.load_config',return_value=config), \
             patch('connector.connector.Archive',return_value=self.archive), \
             patch('connector.connector.post',side_effect=[{'job':job},urllib.error.URLError('SECRET_TOKEN')]):
            main()
        failures=self.archive.recent_save_errors()['errors']
        self.assertEqual(len(failures),1)
        events=(self.root/'state/connector-events.jsonl').read_text('utf-8')
        self.assertIn('complete_failed',events)
        self.assertIn('invalid_request',events)
        for secret in ('SECRET_KEY','SECRET_TOKEN','PRIVATE_PACKET_TEXT','SECRET_REQUEST_KEY'):
            self.assertNotIn(secret,events)

    def test_diagnostics_are_bounded_and_hide_read_failures(self):
        diagnostics=self.archive.diagnostics
        with patch('connector.connector.EVENT_MAX_BYTES',400),patch('connector.connector.MAX_FAILURE_FILES',3):
            for number in range(12):
                diagnostics.record(f'{number:064x}','save_memory','failed',error_detail(ValueError('SECRET')))
        self.assertLessEqual(len(list(diagnostics.failures.glob('*.json'))),3)
        self.assertLessEqual(len(list(diagnostics.state.glob('connector-events.jsonl*'))),4)
        diagnostics.record('f'*64,'get_memory','failed',error_detail(ValueError('SECRET')))
        self.assertTrue(all(x['action']=='save_memory' for x in self.archive.recent_save_errors()['errors']))
        for limit in (0,21,True,'2'):
            with self.assertRaises(ValueError):self.archive.recent_save_errors(limit)

    def test_distinct_sanitized_error_categories(self):
        for exc,code in ((PermissionError('SECRET'),'file_access'),(sqlite3.DatabaseError('SECRET'),'database_error'),(urllib.error.URLError('SECRET'),'connectivity')):
            with self.subTest(code=code):
                detail=error_detail(exc)
                self.assertEqual(detail['code'],code)
                self.assertNotIn('SECRET',json.dumps(detail))

    def test_untrusted_importer_fields_and_legacy_errors_are_not_forwarded(self):
        archive=self.real_import_archive()
        for detail in ({'code':'invalid_text','message':'SECRET','location':'$.SECRET','line':'SECRET'},
                       {'code':['SECRET'],'message':'SECRET'},None):
            with self.subTest(detail=detail):
                run=Mock(returncode=1,stdout=encode({'error':'SECRET','error_detail':detail}),stderr=b'SECRET')
                with patch('connector.connector.subprocess.run',return_value=run):
                    result,_=run_job(archive,self.job())
                self.assertFalse(result['ok'])
                self.assertNotIn('SECRET',json.dumps(result))
                self.assertNotIn('location',result['error_detail'])

    def test_unreadable_success_receipt_is_uncertain_not_cached(self):
        archive=self.real_import_archive()
        with patch('connector.connector.subprocess.run',return_value=Mock(returncode=0,stdout=b'SECRET broken-json',stderr=b'')):
            result,_=run_job(archive,self.job())
        self.assertEqual(result['error_detail']['code'],'importer_protocol')
        self.assertEqual(result['error_detail']['outcome'],'uncertain')
        self.assertEqual(list((self.root/'state/writes').glob('*')),[])
        self.assertNotIn('SECRET',json.dumps(result))

    def test_exit_zero_requires_committed_matching_receipt(self):
        archive=self.real_import_archive()
        invalid=[{}, {'error':'SECRET'}, self.valid_receipt(error='SECRET'), self.valid_receipt(sha256='0'*64),
                 self.valid_receipt(file=str(self.root/'other.json')),self.valid_receipt(added_revisions=-1),
                 self.valid_receipt(added_revisions=True),self.valid_receipt(added_revisions=0),self.valid_receipt(warnings='bad')]
        for receipt in invalid:
            with self.subTest(receipt=receipt),patch('connector.connector.subprocess.run',return_value=Mock(returncode=0,stdout=encode(receipt),stderr=b'')):
                result,_=run_job(archive,self.job())
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_detail']['code'],'importer_protocol')
            self.assertEqual(result['error_detail']['outcome'],'uncertain')
            self.assertEqual(list((self.root/'state/writes').glob('*')),[])
        with patch('connector.connector.subprocess.run',return_value=Mock(returncode=0,stdout=encode(self.valid_receipt(added_revisions=0,repeated_packets=1)),stderr=b'')):
            self.assertTrue(run_job(archive,self.job())[0]['ok'])

    def test_oversized_committed_write_returns_compact_success(self):
        self.archive._importer=Mock(return_value={'saved':True,'delivery':'synthetic.json','sha256':'a'*64,'has_warnings':True,'import_status':'imported_with_warnings',
                                                  'import_result':{'added_revisions':1,'reused_messages':0,'linked_entities':0,'repeated_packets':0,'warnings':['x'*3000000],'uncertain':[]}})
        result,_=run_job(self.archive,self.job())
        repeated,_=run_job(self.archive,self.job())
        self.assertTrue(result['ok'])
        self.assertTrue(result['result']['response_compacted'])
        self.assertEqual(result['result']['import_result']['added_revisions'],1)
        self.assertEqual(result['result']['import_result']['warning_count'],1)
        self.assertEqual(result,repeated)
        self.assertEqual(self.archive._importer.call_count,1)
        self.assertGreater(next((self.root/'state/writes').glob('*.json')).stat().st_size,3000000)

    def test_oversized_read_still_reports_limit(self):
        self.archive._reader=Mock()
        self.archive._reader.get_memory.return_value={'text':'x'*3000000}
        result,_=run_job(self.archive,self.job({'id':'synthetic'},'get_memory'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_detail']['code'],'result_too_large')

    def test_nested_extra_credential_held_before_import(self):
        archive=self.real_import_archive()
        packet={'format':'threadsatchel/1','kind':'note','messages':[{'text':'SYNTHETIC'}],'extras':{'nested':['SYNTHETIC_SECRET_VALUE']}}
        archive._credential_guard.contains_possible_credential.side_effect=lambda value:value['extras']['nested']==['SYNTHETIC_SECRET_VALUE']
        with patch('connector.connector.subprocess.run') as run:
            result,_=run_job(archive,self.job({'request_key':'credential-test','packet':packet}))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_detail']['code'],'possible_credential_review_required')
        self.assertTrue(result['error_detail']['original_retained'])
        archive._credential_guard.contains_possible_credential.assert_called_once_with(packet)
        archive._credential_guard.SECRET.search.assert_called_once_with(encode(packet).decode())
        run.assert_not_called()
        self.assertEqual(json.loads(next((self.root/'inbox').glob('*.json')).read_bytes()),packet)
        self.assertNotIn('SYNTHETIC_SECRET_VALUE',(self.root/'state/connector-events.jsonl').read_text('utf-8'))

if __name__=='__main__': unittest.main()
