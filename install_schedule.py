"""Opt-in scheduler for capture, export, sync, or a two-minute inbox sweep."""
import argparse,base64,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=('local','export','sync','inbox'))
    args=p.parse_args()
    script='inbox_sweep.py' if args.mode=='inbox' else 'sync_codex_capture.py' if args.mode=='sync' else 'codex_capture_export.py'
    config=ROOT/('remote-capture-config.json' if args.mode=='sync' else 'capture-'+args.mode+'.json')
    if args.mode=='inbox':
        if not (ROOT/'memory.sqlite3').is_file():raise SystemExit('Initialize the existing archive first.')
        (ROOT/'inbox').mkdir(exist_ok=True)
    elif not config.exists():raise SystemExit('Create configuration first: '+str(config))
    extra=[] if args.mode in ('sync','inbox') else ['--config',str(config)]
    interval=2 if args.mode=='inbox' else 1
    name='ThreadSatchel-'+{'local':'CodexCapture','export':'CodexExport','sync':'RemoteCodexSync','inbox':'InboxSweep'}[args.mode]
    if os.name=='nt':
        exe=Path(sys.executable).with_name('pythonw.exe')
        if not exe.exists():exe=Path(sys.executable)
        def q(s):return chr(39)+str(s).replace(chr(39),chr(39)*2)+chr(39)
        commandline=subprocess.list2cmdline([str(ROOT/script),*extra])
        ps='$ErrorActionPreference='+q('Stop')+'; $name='+q(name)+'; '
        ps+='if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw '+q('Task already exists; inspect it before replacing')+'}; '
        ps+='$a=New-ScheduledTaskAction -Execute '+q(exe)+' -Argument '+q(commandline)+' -WorkingDirectory '+q(ROOT)+'; '
        ps+='$u=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name; '
        ps+='$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes('+str(interval)+') -RepetitionInterval (New-TimeSpan -Minutes '+str(interval)+'); '
        ps+='$l=New-ScheduledTaskTrigger -AtLogOn -User $u; $p=New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited; '
        ps+='$s=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; '
        ps+='Register-ScheduledTask -TaskName $name -Action $a -Trigger @($t,$l) -Principal $p -Settings $s -ErrorAction Stop | Out-Null; Start-ScheduledTask -TaskName $name -ErrorAction Stop'
        subprocess.run(['powershell','-NoProfile','-EncodedCommand',base64.b64encode(ps.encode('utf-16-le')).decode()],check=True)
    elif sys.platform.startswith('linux'):
        unit='threadsatchel-'+args.mode
        directory=Path.home()/'.config/systemd/user';directory.mkdir(parents=True,exist_ok=True)
        service=directory/(unit+'.service');timer=directory/(unit+'.timer')
        if service.exists() or timer.exists():raise SystemExit('Unit already exists; inspect before replacing')
        def quote(s):
            return chr(34)+str(s).replace('\\','\\\\').replace(chr(34),'\\'+chr(34)).replace('%','%%')+chr(34)
        command=' '.join(quote(s) for s in [sys.executable,ROOT/script,*extra])
        service.write_text('[Unit]\nDescription=ThreadSatchel '+args.mode+'\n[Service]\nType=oneshot\nExecStart='+command+'\nTimeoutStartSec=240\nNice=10\n')
        timer.write_text('[Unit]\nDescription=ThreadSatchel periodic timer\n[Timer]\nOnBootSec=60\nOnUnitInactiveSec='+str(interval*60)+'\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','enable','--now',unit+'.timer'],check=True)
        subprocess.run(['systemctl','--user','start',unit+'.service'],check=True)
    else:raise SystemExit('Use your operating system scheduler to run the documented command once per minute.')
    print('Installed '+name+'. Runs with your user permissions; no SSH keys or security settings changed.')
if __name__=='__main__':main()
