"""Whole ZIP reconciliation regression; isolated synthetic databases."""
import json,sqlite3,tempfile,zipfile
from pathlib import Path
from contextlib import closing
from import_memory import import_file
for ambiguous in (False,True):
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);dbp=root/'memory.sqlite3'
        with closing(sqlite3.connect(dbp)) as db,db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
        ms=[{'speaker':'user','text':'SYNTHETIC uniquely detailed request about archival imports and source provenance. '*3},{'speaker':'assistant','text':'SYNTHETIC detailed answer about safe reconciliation and retaining original imported bytes. '*3}]
        p=root/'excerpt.json';p.write_text(json.dumps({'format':'threadsatchel/1','kind':'excerpt','messages':ms}))
        import_file(p,dbp)
        mapping={str(i):{'parent':str(i-1) if i else None,'message':{'id':str(i),'author':{'role':m['speaker']},'content':{'content_type':'text','parts':[m['text']]}}} for i,m in enumerate(ms)}
        convs=[{'id':'A','mapping':mapping,'current_node':'1'}]
        if ambiguous:convs.append({'id':'B','mapping':mapping,'current_node':'1'})
        z=root/'export.zip'
        with zipfile.ZipFile(z,'w') as f:f.writestr('conversations.json',json.dumps(convs))
        result=import_file(z,dbp)
        assert result['linked_entities']==(0 if ambiguous else 2),result
        assert result['ambiguous_excerpt_packets']==int(ambiguous),result
        assert import_file(z,dbp)['added_revisions']==0
        with closing(sqlite3.connect(dbp)) as db:
            assert db.execute('SELECT count(*) FROM memory_fts').fetchone()[0]==(6 if ambiguous else 2)
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
print('PASS: whole export links unique unidentified overlap; complete-export ambiguity preserved and flagged; reimport adds zero')
