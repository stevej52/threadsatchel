"""Cross-export overlap tests in temporary databases only."""
import json,sqlite3,tempfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from import_memory import import_file,merge_entity
from reconcile_imports import reconcile_anonymous
def run():
    for mode in ('unique','ambiguous','short','different_account'):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);dbp=root/'m.sqlite3'
            with closing(sqlite3.connect(dbp)) as db,db:
                db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
                db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
            texts=['Synthetic question describing the exact import requirement and machine configuration. '*2,'Synthetic answer describing matching source records and preserving all original provenance. '*2]
            if mode=='short':texts=['Yes','Okay']
            ms=[{'speaker':r,'text':x} for r,x in zip(('user','assistant'),texts)]
            def put(name,p):
                f=root/name;f.write_text(json.dumps(p),encoding='utf-8')
                with patch('reconcile_imports.reconcile_anonymous',return_value={'linked_entities':0,'ambiguous_excerpt_packets':0}):return import_file(f,dbp)
            p={'format':'threadsatchel/1','kind':'excerpt','messages':ms}
            if mode=='different_account':p['account_id']='account-A'
            put('unknown.json',p)
            known=dict(p,conversation_id='conversation-A',messages=[dict(m,message_id=str(i)) for i,m in enumerate(ms)])
            if mode=='different_account':known['account_id']='account-B'
            put('known.json',known)
            if mode=='ambiguous':put('other.json',dict(known,conversation_id='conversation-B'))
            with closing(sqlite3.connect(dbp)) as db,db:
                db.row_factory=sqlite3.Row
                before=db.execute('SELECT count(*) FROM memories').fetchone()[0]
                result=reconcile_anonymous(db,merge_entity)
                assert result['linked_entities']==(2 if mode=='unique' else 0),(mode,result)
                assert result['ambiguous_excerpt_packets']==(1 if mode=='ambiguous' else 0)
                assert db.execute('SELECT count(*) FROM memories').fetchone()[0]==before
                assert db.execute('SELECT count(*) FROM memory_fts').fetchone()[0]==before-result['linked_entities']
                assert reconcile_anonymous(db,merge_entity)['linked_entities']==0
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert put('renamed.json',p)['added_revisions']==0
    print('PASS: unidentified exact excerpt links once; ambiguous matches, short phrases and differing accounts remain separate; originals preserved; retry idempotent')
if __name__=='__main__':run()
