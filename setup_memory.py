"""Initialize an archive and generate machine-local capture configuration."""
import argparse,json,os,socket
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-only',action='store_true',help='Queue on this computer for a central archive')
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    args=parser.parse_args()
    mode='export' if args.export_only else 'local'
    target=ROOT/('capture-'+mode+'.json')
    if target.exists():raise SystemExit('Configuration already exists; inspect/edit it instead of overwriting.')
    if not args.export_only:
        from server import initialize
        from import_memory import migrate
        initialize();migrate(ROOT/'memory.sqlite3')
    config={'source_roots':[str((args.codex_home/n).resolve()) for n in ('sessions','archived_sessions')],
            'capture_dir':str(ROOT/('queue' if args.export_only else 'capture')),
            'db_path':str(ROOT/'memory.sqlite3'),'export_only':args.export_only,
            'source_host':socket.gethostname(),'allow_empty':True,
            'excluded_session_ids':[],'max_run_seconds':35,'max_bytes_per_session':33554432}
    target.write_text(json.dumps(config,indent=2),encoding='utf-8')
    print('Created '+str(target))
    print('Run: python codex_capture_export.py --config '+str(target))
if __name__=='__main__':main()
