"""Pull only filtered Codex packets from enrolled machines and commit in the central archive."""
import base64, hashlib, json, os, sys, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from import_memory import import_file
from codex_capture import capture_lock, dump_atomic, stamp
ROOT = Path(__file__).resolve().parent
FOLDER = ROOT/'remote-capture'
RETRY_BASE_SECONDS = 10 * 60
RETRY_MAX_SECONDS = 60 * 60

def retry_stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
def run():
    FOLDER.mkdir(exist_ok=True)
    report = {'started_at':stamp(),'machines':{},'errors':[]}
    config = json.loads((ROOT/'remote-capture-config.json').read_text(encoding='utf-8'))
    with capture_lock(FOLDER):
        retry_path = FOLDER/'retry-state.json'
        retries = json.loads(retry_path.read_text(encoding='utf-8')) if retry_path.exists() else {}
        if not isinstance(retries, dict):
            raise ValueError('Invalid remote-sync retry state')
        for machine in config['machines']:
            name, home = machine['name'], machine['capture_home']
            stats = {'packets':0,'added_revisions':0,'reused_messages':0,'repeated_packets':0,'warnings':0,'uncertain':0,'linked_entities':0,'ambiguous_excerpt_packets':0}
            report['machines'][name] = stats
            retry = retries.get(name, {})
            if retry.get('next_attempt', 0) > time.time():
                stats.update(sync_state='waiting_to_retry', consecutive_failures=retry['failures'],
                             next_retry_at=retry_stamp(retry['next_attempt']))
                report['errors'].append({'machine':name, 'reason':retry['last_error'],
                                         'deferred':True, 'next_retry_at':stats['next_retry_at']})
                continue
            try:
                def call(code):
                    args=machine['ssh_argv']
                    if not isinstance(args,list) or not args or not all(isinstance(x,str) for x in args):raise ValueError('ssh_argv must be an argument list')
                    process=subprocess.run(args,input=code,capture_output=True,text=True,encoding='utf-8',timeout=90,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
                retries.pop(name, None)
                stats['sync_state'] = 'succeeded'
            except Exception as exc:
                reason = type(exc).__name__+': '+str(exc)[-1800:]
                failures = retry.get('failures', 0) + 1
                delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** min(failures - 1, 6))
                retry = {'failures':failures, 'next_attempt':time.time() + delay, 'last_error':reason}
                retries[name] = retry
                stats.update(sync_state='failed', consecutive_failures=failures,
                             next_retry_at=retry_stamp(retry['next_attempt']))
                report['errors'].append({'machine':name, 'reason':reason,
                                         'next_retry_at':stats['next_retry_at']})
            # Save after each machine, so an interrupted run retains its retry delay.
            dump_atomic(retry_path, retries)
        report['finished_at']=stamp()
        dump_atomic(FOLDER/'status.json',report)
    return report
if __name__=='__main__':
    result=run()
    if sys.stdout: print(json.dumps(result,indent=2))
    sys.exit(1 if result['errors'] else 0)
