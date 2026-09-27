"""Isolated export/retry checks; no fixtures enter ThreadSatchel."""
import json,tempfile
from pathlib import Path
from unittest.mock import patch
import codex_capture_export as cap
import capture_transport as transport
from test_codex_capture import data,meta,msg
def run():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);src=root/'sessions';src.mkdir();p=src/'test.jsonl'
        p.write_bytes(data([meta(),msg('u1','Synthetic export request'),msg('a1','Synthetic export answer','AgentMessage')]))
        cfg={'source_roots':[str(src)],'capture_dir':str(root/'queue'),'export_only':True,'allow_empty':True,'source_host':'TEST'}
        r=cap.run(cfg);assert r['counts']['eligible_messages']==2 and not r['errors']
        one=transport.pending(root/'queue');assert len(one['packets'])==1
        cap.run(cfg);assert transport.pending(root/'queue')['packets']==one['packets']
        transport.acknowledge(root/'queue',[one['packets'][0]['sha']])
        cap.run(cfg);assert not transport.pending(root/'queue')['packets']
        try:transport.acknowledge(root/'queue',['../bad'])
        except ValueError:pass
        else:raise AssertionError('Unsafe ack accepted')
        p.write_bytes(data([meta(),msg('u1','Synthetic export request'),msg('a1','Synthetic export answer','AgentMessage'),msg('u2','Synthetic appended')]))
        assert cap.run(cfg)['counts']['eligible_messages']==1
        assert len(transport.pending(root/'queue')['packets'])==1
        cfg['source_roots']=[str(root/'absent')];assert not cap.run(cfg)['errors']
    print('PASS: durable queue, repeat before/after acknowledgement, append, empty roots, invalid acknowledgement')
if __name__=='__main__':run()
