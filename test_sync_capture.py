"""Exercise generic transport using an isolated local Python process."""
import json,sqlite3,tempfile,shutil,sys,base64,hashlib
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import sync_codex_capture as sync
def run():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);node=root/'node';node.mkdir();(node/'queue/packets').mkdir(parents=True)
        shutil.copy2(Path(__file__).with_name('capture_transport.py'),node/'capture_transport.py')
        packet={'format':'threadsatchel/1','kind':'excerpt','conversation_id':'SYNTHETIC-remote','messages':[{'speaker':'user','message_id':'u1','text':'SYNTHETIC remote transfer'}]}
        raw=json.dumps(packet).encode();sha=hashlib.sha256(raw).hexdigest();queued=node/'queue/packets'/(sha+'.json');queued.write_bytes(raw)
        (root/'remote-capture-config.json').write_text(json.dumps({'machines':[{'name':'test','capture_home':str(node),'ssh_argv':[sys.executable,'-']}]}))
        with closing(sqlite3.connect(root/'memory.sqlite3')) as db,db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
        with patch.object(sync,'ROOT',root),patch.object(sync,'FOLDER',root/'remote-capture'):
            first=sync.run();assert not first['errors'];assert first['machines']['test']['added_revisions']==1
            assert not queued.exists()
            queued.write_bytes(raw)
            again=sync.run();assert not again['errors'];assert again['machines']['test']['added_revisions']==0
            assert again['machines']['test']['repeated_packets']==1;assert not queued.exists()
            queued.write_bytes(raw)
            with patch.object(sync,'import_file',side_effect=RuntimeError('SYNTHETIC import failure')):
                failed=sync.run();assert failed['errors'];assert queued.exists()
    print('PASS: central commit before acknowledgement, retry deduplication, failed import retains source queue')
if __name__=='__main__':run()
