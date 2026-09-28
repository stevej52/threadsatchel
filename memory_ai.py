"""Explicit on/off controls and bounded optional-Qwen processing. No paid APIs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from ai_memory import (archive_connect, cache_connect, diagnostic_history, load_config, process,
                       project_brief, record_diagnostic, status, validate_chunk_ids)
from codex_capture import capture_lock, dump_atomic

ROOT=Path(__file__).resolve().parent


def retry_held(root,chunk_ids=None):
    """Queue one owner-requested retry; holds/errors remain until validated success."""
    selected=validate_chunk_ids(chunk_ids)
    runtime=Path(root)/'.ai-cache'
    with capture_lock(runtime),closing(cache_connect(root,True)) as cache,cache:
        counts={}
        restriction=' AND id IN ('+','.join('?' for _ in selected)+')' if selected else ''
        for stage,attempts,flag in [('analysis','attempts','analysis_retry_requested'),
                ('embedding','embedding_attempts','embedding_retry_requested')]:
            rows=cache.execute('SELECT id,'+attempts+' FROM chunks WHERE '+attempts+'>=3'+restriction,selected or []).fetchall()
            for row in rows:
                cache.execute('UPDATE chunks SET '+flag+'=1 WHERE id=?',(row['id'],))
                record_diagnostic(cache,row['id'],stage,'retry_requested',row[attempts],'retry_requested',{'count':row[attempts]})
            counts[stage+'_chunks']=len(rows)
    return dict(state='retry_scheduled',**counts)


def set_enabled(root, enabled):
    config=load_config(root)
    path=root/'memory-ai.json'
    if path.exists():
        backup=root/'backups'/'qwen-config'
        backup.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,backup/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json'))
    config['enabled']=enabled
    dump_atomic(path,config)
    return dict(enabled=enabled,configuration=str(path))


def embeddings_daemon(root):
    """Optional CPU worker, limited to a local child we own; switch-off stops it."""
    from ai_embeddings import EmbeddingClient
    runtime=root/'.ai-cache'/'embedding-owner'
    runtime.mkdir(parents=True,exist_ok=True)
    with capture_lock(runtime):
        client=None
        try:
            config=load_config(root)
            if not config['enabled'] or not config['embeddings']:return
            client=EmbeddingClient(config,root)
            client.ensure_ready()
            while load_config(root)['enabled'] and load_config(root)['embeddings']:
                time.sleep(3)
                client.ensure_ready()
        finally:
            if client:client.close()


def install_schedule(root):
    if os.name!='nt':
        raise ValueError('Use your user scheduler for process every five minutes and embeddings while enabled')
    python=Path(sys.executable).with_name('pythonw.exe')
    if not python.exists():python=Path(sys.executable)
    def q(s):return "'"+str(s).replace("'","''")+"'"
    for mode,task,minutes in [('embeddings','ThreadSatchel-AIEmbeddings',5),('process','ThreadSatchel-QwenMemory',5)]:
        args=subprocess.list2cmdline([str(root/'memory_ai.py'),mode])
        script="$ErrorActionPreference='Stop'; $name="+q(task)+'; '
        script+="if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw 'Task exists; inspect before changing it'}; "
        script+='$a=New-ScheduledTaskAction -Execute '+q(python)+' -Argument '+q(args)+' -WorkingDirectory '+q(root)+'; '
        script+='$u=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name; '
        script+='$p=New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited; '
        script+='$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes('+str(minutes)+') -RepetitionInterval (New-TimeSpan -Minutes '+str(minutes)+'); '
        script+='$l=New-ScheduledTaskTrigger -AtLogOn -User $u; '
        limit='([TimeSpan]::Zero)' if mode=='embeddings' else '(New-TimeSpan -Minutes 4)'
        script+='$s=New-ScheduledTaskSettingsSet -Priority 10 -MultipleInstances IgnoreNew -ExecutionTimeLimit '+limit+' -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; '
        script+='Register-ScheduledTask -TaskName $name -Action $a -Trigger @($t,$l) -Principal $p -Settings $s | Out-Null; Start-ScheduledTask -TaskName $name'
        import base64
        subprocess.run(['powershell','-NoProfile','-EncodedCommand',base64.b64encode(script.encode('utf-16-le')).decode()],check=True)
    return dict(installed=['ThreadSatchel-AIEmbeddings','ThreadSatchel-QwenMemory'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['on','off','status','process','brief','embeddings','install-schedule','retry-held','diagnostics'])
    parser.add_argument('project',nargs='?',default='general')
    parser.add_argument('--max-chunks',type=int,default=None)
    parser.add_argument('--budget-seconds',type=int,default=None)
    parser.add_argument('--embeddings-only',action='store_true')
    parser.add_argument('--chunk-id',action='append',dest='chunk_ids',help='Repeat to select exact existing SHA-256 chunk IDs')
    parser.add_argument('--recover-analysis',action='store_true',help='For selected queued held retries only: constrain quotes to exact source choices')
    args=parser.parse_args()
    try:
        if args.action in ('on','off'):
            result=set_enabled(ROOT,args.action=='on')
        elif args.action=='install-schedule':result=install_schedule(ROOT)
        elif args.action=='retry-held':result=retry_held(ROOT,chunk_ids=args.chunk_ids)
        elif args.action=='diagnostics':result={'events':diagnostic_history(ROOT,chunk_ids=args.chunk_ids)}
        elif args.action=='embeddings':
            embeddings_daemon(ROOT);result={'state':'stopped'}
        elif args.action=='process':
            config=load_config(ROOT)
            if args.max_chunks is None and not args.embeddings_only and not args.chunk_ids:
                process(ROOT,max_records=64,budget_seconds=40,embeddings_only=True)
            result=process(ROOT,max_records=args.max_chunks or (len(args.chunk_ids) if args.chunk_ids else config['max_chunks_per_pass']),
                budget_seconds=args.budget_seconds or config['budget_seconds'],embeddings_only=args.embeddings_only,
                chunk_ids=args.chunk_ids,recover_analysis=args.recover_analysis)
        elif args.action=='status':result=status(ROOT,None)
        elif not load_config(ROOT)['enabled']:result=project_brief(ROOT,None,args.project)
        else:
            with closing(archive_connect(ROOT)) as db:
                result=project_brief(ROOT,db,args.project)
        print(json.dumps(result,ensure_ascii=True))
    except Exception as error:
        # Fixed type only; model input/output and configuration secrets are never logged.
        result={'state':'failed','error_type':type(error).__name__,'at':datetime.now(timezone.utc).isoformat()}
        runtime=ROOT/'.ai-cache'
        runtime.mkdir(exist_ok=True)
        dump_atomic(runtime/'last-error.json',result)
        print(json.dumps(result))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
