"""Isolated synthetic regression tests; never imports fixtures into the live collection."""
import asyncio
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from import_memory import import_file, migrate

ROOT = Path(__file__).resolve().parent

async def tool(client, name, **args):
    result = await client.call_tool(name,args)
    assert not result.is_error, result
    values = [json.loads(c.text) for c in result.content if c.type=='text']
    if name=='search_memory':
        return values
    value=values[0]
    return value['result'] if isinstance(value,dict) and set(value)=={'result'} else value

async def run():
    passed=[]
    with tempfile.TemporaryDirectory(prefix='threadsatchel-synthetic-') as temp:
        root=Path(temp)
        dbpath=root/'memory.sqlite3'
        for name in ('server.py','server_readonly.py','import_metadata.py','memory_search.py','ai_memory.py'):
            shutil.copy2(ROOT/name,root/name)
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
            db.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title,tokenize='unicode61')")
            db.execute("INSERT INTO memories VALUES('legacy-id','Synthetic legacy text','synthetic legacy',NULL,'2000-01-01')")
            db.execute("INSERT INTO memory_fts VALUES('legacy-id','Synthetic legacy text','synthetic legacy',NULL)")
        backup=migrate(dbpath)
        with closing(sqlite3.connect(backup)) as db, db:
            assert db.execute('SELECT id FROM memories').fetchall()==[('legacy-id',)]
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='import_schema'").fetchone() is None
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        passed.append('Consistent pre-schema backup and legacy ID preservation')
        def packet(name,conv,messages,**kw):
            path=root/name
            path.write_text(json.dumps(dict(format='threadsatchel/1',kind='excerpt',title='SYNTHETIC TEST',conversation_id=conv,messages=messages,**kw)),encoding='utf-8')
            return path
        a=packet('excerpt.json','synthetic-conversation',[dict(text='SYNTHETIC amberquartz request',speaker='user'),dict(text='SYNTHETIC amberquartz answer',speaker='assistant')])
        first=import_file(a,dbpath)
        assert first['added_revisions']==2
        params=StdioServerParameters(command=sys.executable,args=[str(root/'server_readonly.py')])
        async with Client(params) as client:
            assert sorted(t.name for t in (await client.list_tools()).tools)==['get_memory','get_project_brief','memory_ai_status','search_memory']
            hits=await tool(client,'search_memory',query='amberquartz',full=True)
            assert len(hits)==2
            saved=hits[0]['id']
            assert (await tool(client,'get_memory',id=saved))['import_metadata']['kind']=='excerpt'
        renamed=root/'renamed.json'
        shutil.copy2(a,renamed)
        assert import_file(renamed,dbpath)['added_revisions']==0
        passed.append('Excerpt import and real read-only MCP search/get; renamed repeat adds zero')
        # Synthetic export follows graph-shaped conversations.json, including a branch.
        ms=[('prefix','system','SYNTHETIC preface'),('u','user','SYNTHETIC amberquartz request'),('a','assistant','SYNTHETIC amberquartz answer'),('suffix','user','SYNTHETIC followup')]
        mapping={}
        parent=None
        for key,role,text in ms:
            mapping[key]=dict(parent=parent,message=dict(id=key,author=dict(role=role),create_time=1700000000,content=dict(content_type='text',parts=[text])))
            parent=key
        z=root/'SYNTHETIC-export.zip'
        with zipfile.ZipFile(z,'w') as archive:
            archive.writestr('conversations.json',json.dumps([dict(id='synthetic-conversation',title='SYNTHETIC TEST',mapping=mapping,current_node='suffix')]))
        merged=import_file(z,dbpath)
        assert merged['added_revisions']==2 and merged['reused_messages']==2, merged
        assert import_file(z,dbpath)['added_revisions']==0
        passed.append('Longer synthetic ZIP adds only two missing messages and binds actual IDs')
        edit=packet('edit.json','synthetic-conversation',[dict(text='SYNTHETIC amberquartz edited answer',message_id='a',speaker='assistant',source_date='2024-01-02T03:04:05Z')])
        assert import_file(edit,dbpath)['added_revisions']==1
        separate=packet('separate.json','synthetic-other',[dict(text='SYNTHETIC amberquartz request',message_id='u',speaker='user')])
        assert import_file(separate,dbpath)['added_revisions']==1
        p=packet('repeat1.json','synthetic-repeat',[dict(text='SYNTHETIC echo')])
        import_file(p,dbpath)
        p=packet('repeat2.json','synthetic-repeat',[dict(text='SYNTHETIC echo'),dict(text='SYNTHETIC echo')])
        r=import_file(p,dbpath)
        assert r['added_revisions']==2 and r['uncertain'],r
        passed.append('Edits retain both revisions; separate conversations and ambiguous repeated phrases stay separate')
        # A verified later identity/order bridges two formerly uncertain records.
        p=packet('bridge1.json','synthetic-bridge',[dict(text='SYNTHETIC bridgeword',source_order=4)])
        import_file(p,dbpath)
        with closing(sqlite3.connect(dbpath)) as db, db:
            old_id=db.execute("SELECT id FROM memories WHERE text='SYNTHETIC bridgeword'").fetchone()[0]
        p=packet('bridge2.json','synthetic-bridge',[dict(text='SYNTHETIC bridgeword',message_id='bridge-message')])
        assert import_file(p,dbpath)['added_revisions']==1
        p=packet('bridge3.json','synthetic-bridge',[dict(text='SYNTHETIC bridgeword',message_id='bridge-message',source_order=4)])
        r=import_file(p,dbpath)
        assert r['linked_entities']==1 and r['added_revisions']==0,r
        async with Client(params) as client:
            assert len(await tool(client,'search_memory',query='bridgeword'))==1
            old=await tool(client,'get_memory',id=old_id)
            assert old['id']==old_id and old['import_metadata']['canonical_memory_id']!=old_id
            assert len(old['import_metadata']['provenance'])==3
            hits=await tool(client,'search_memory',query='amberquartz',full=True)
            assert len(hits)==4,hits
            assert (await tool(client,'get_memory',id=saved))['id']==saved
            edit_hit=next(h for h in hits if 'edited' in h['text'])
            assert len(edit_hit['import_metadata']['revision_ids'])==2
            assert edit_hit['import_metadata']['provenance'][0]['message']['source_date']=='2024-01-02T03:04:05Z'
            assert (await tool(client,'get_memory',id='legacy-id'))==dict(id='legacy-id',text='Synthetic legacy text',source='synthetic legacy',title=None,created_at='2000-01-01')
        assert import_file(renamed,dbpath)['added_revisions']==0
        passed.append('MCP restart, repeat safety, canonical linking, original IDs and provenance preserved')
        plain=root/'plain.md'
        original=b'\xef\xbb\xbf# SYNTHETIC note\r\nKeep bytes exactly.\r\n'
        plain.write_bytes(original)
        r=import_file(plain,dbpath)
        copy=root/'plain-renamed.txt'
        copy.write_bytes(original)
        assert import_file(copy,dbpath)['added_revisions']==0
        assert import_file(plain,dbpath,kind='summary')['added_revisions']==1
        with closing(sqlite3.connect(dbpath)) as db, db:
            assert db.execute('SELECT raw FROM import_objects WHERE sha256=?',(r['sha256'],)).fetchone()[0]==original
        passed.append('Plain Markdown/TXT, BOM/CRLF bytes unchanged, renamed repeat, summaries labeled separately')
        concurrent=packet('concurrent.json','synthetic-concurrent',[dict(text='SYNTHETIC concurrency')])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:import_file(concurrent,dbpath),range(4)))
        assert sum(r['added_revisions'] for r in results)==1
        passed.append('Four concurrent imports add exactly one search entry')
        fail=packet('atomic.json','synthetic-atomic',[dict(text='SYNTHETIC atomic first'),dict(text='SYNTHETIC fail transaction')])
        with closing(sqlite3.connect(dbpath)) as db, db:
            before=db.execute('SELECT count(*) FROM import_objects').fetchone()[0]
            db.execute("CREATE TRIGGER fail_test BEFORE INSERT ON memories WHEN new.text='SYNTHETIC fail transaction' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        try:
            import_file(fail,dbpath)
            raise AssertionError('Failure expected')
        except sqlite3.IntegrityError:
            pass
        with closing(sqlite3.connect(dbpath)) as db, db:
            assert db.execute('SELECT count(*) FROM import_objects').fetchone()[0]==before
            assert not db.execute("SELECT 1 FROM memories WHERE text='SYNTHETIC atomic first'").fetchone()
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
            assert not db.execute('SELECT id FROM memory_fts GROUP BY id HAVING count(*)>1').fetchall()
        passed.append('Forced mid-import failure rolls back bytes, records and FTS; integrity and foreign keys pass')
        writable=StdioServerParameters(command=sys.executable,args=[str(root/'server.py')])
        async with Client(writable) as client:
            assert sorted(t.name for t in (await client.list_tools()).tools)==['get_memory','get_project_brief','memory_ai_status','search_memory','store_memory']
            stored=await tool(client,'store_memory',text='SYNTHETIC writabletool',source='SYNTHETIC TEST')
            assert (await tool(client,'get_memory',id=stored['id']))==stored
            keyed=await tool(client,'store_memory',text='SYNTHETIC keyedretry',source='SYNTHETIC TEST',idempotency_key='synthetic-client:write-1')
            replay=await tool(client,'store_memory',text='SYNTHETIC keyedretry',source='SYNTHETIC TEST',idempotency_key='synthetic-client:write-1')
            assert replay==keyed
            assert len(await tool(client,'search_memory',query='keyedretry'))==1
            assert len(await tool(client,'search_memory',query='amberquartz'))==4
        passed.append('Existing writable MCP tools and store contract preserved; explicit keyed retries return one searchable record')
    report=dict(status='PASS',tests=passed,fixtures='Clearly labeled synthetic data; isolated temporary database removed',actual_export_validation='PENDING: real ChatGPT ZIP not available')
    (ROOT/'incremental-test-results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    asyncio.run(run())

