"""Pull only filtered Codex packets from enrolled machines and commit on Jedi."""
import base64, hashlib, json, os, sys, subprocess
from pathlib import Path
from uuid import uuid4
from import_memory import import_file
from codex_capture import capture_lock, dump_atomic, stamp
ROOT = Path(__file__).resolve().parent
FOLDER = ROOT/'remote-capture'
def run():
    FOLDER.mkdir(exist_ok=True)
    report = {'started_at':stamp(),'machines':{},'errors':[]}
    config = json.loads((ROOT/'remote-capture-config.json').read_text(encoding='utf-8'))
    with capture_lock(FOLDER):
        for machine in config['machines']:
            name, home = machine['name'], machine['capture_home']
            stats = {'packets':0,'added_revisions':0,'reused_messages':0,'repeated_packets':0,'warnings':0,'uncertain':0,'linked_entities':0,'ambiguous_excerpt_packets':0}
            report['machines'][name] = stats
            try:
                def call(code):
                    args=machine['ssh_argv']
                    if not isinstance(args,list) or not args or not all(isinstance(x,str) for x in args):raise ValueError('ssh_argv must be an argument list')
                    process=subprocess.run(args,input=code,capture_output=True,text=True,encoding='utf-8',timeout=90)
                    if process.returncode:raise RuntimeError('SSH command failed with exit '+str(process.returncode)+': '+process.stderr[-1200:])
                    return process.stdout
                prefix = 'import sys,json;sys.path.insert(0,'+repr(home)+');import capture_transport as t;'
                bundle = json.loads(call(prefix+'print(json.dumps(t.pending('+repr(home+'/queue')+')))'))
                stats['capture_status'] = bundle['status']
                committed=[]
                for packet in bundle['packets']:
                    raw=base64.b64decode(packet['data'],validate=True)
                    sha=hashlib.sha256(raw).hexdigest()
                    if sha != packet['sha']: raise ValueError('Transfer hash mismatch')
                    directory=FOLDER/name/'packets'; directory.mkdir(parents=True,exist_ok=True)
                    path=directory/(sha+'.json')
                    if path.exists():
                        if path.read_bytes()!=raw: raise ValueError('Existing packet differs')
                    else:
                        temp=directory/('.incoming-'+uuid4().hex+'.json.part')
                        with temp.open('xb') as f:
                            f.write(raw);f.flush();os.fsync(f.fileno())
                        if temp.read_bytes()!=raw: raise ValueError('Packet verification failed')
                        os.rename(temp,path)
                    result=import_file(path,ROOT/'memory.sqlite3')
                    for key in ('added_revisions','reused_messages','repeated_packets'): stats[key]+=result[key]
                    stats['warnings']+=len(result['warnings']);stats['uncertain']+=len(result['uncertain'])
                    stats['linked_entities']+=result['linked_entities'];stats['ambiguous_excerpt_packets']=max(stats['ambiguous_excerpt_packets'],result.get('ambiguous_excerpt_packets',0))
                    stats['packets']+=1;committed.append(sha)
                if committed:
                    receipt=json.loads(call(prefix+'print(json.dumps(t.acknowledge('+repr(home+'/queue')+','+repr(committed)+')))'))
                    stats.update(receipt)
            except Exception as exc:
                report['errors'].append({'machine':name,'reason':type(exc).__name__+': '+str(exc)[-1800:]})
        report['finished_at']=stamp()
        dump_atomic(FOLDER/'status.json',report)
    return report
if __name__=='__main__':
    result=run()
    if sys.stdout: print(json.dumps(result,indent=2))
    sys.exit(1 if result['errors'] else 0)
