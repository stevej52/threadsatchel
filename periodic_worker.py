"""Optional foreground supervisor for capture, inbox imports, and a local reader."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from codex_capture import CaptureLockBusy, capture_lock, dump_atomic, run as capture
from inbox_sweep import sweep


def maintenance(root, *, capture_due, sweep_due, daemon=False):
    """Run each due job independently and publish only state/error types."""
    state = {}
    def run(name, action):
        try:
            result = action()
            state[name] = result.get('state', 'ok') if isinstance(result, dict) else 'ok'
        except Exception as error:
            state[name] = type(error).__name__

    if daemon:
        def start_reader():
            import server_readonly
            if Path(server_readonly.DB).resolve().parent != root:
                raise ValueError('Reader archive differs from worker archive')
            if not server_readonly._ensure_daemon_started():
                raise RuntimeError('Reader unavailable')
        run('reader', start_reader)
    config = root / 'capture-local.json'
    if capture_due and config.is_file():
        run('capture', lambda: capture(json.loads(config.read_text(encoding='utf-8-sig'))))
    if sweep_due:
        def scan():
            from ai_memory import load_config
            cfg = load_config(root)
            if cfg['enabled'] and (cfg.get('vision_upload_dirs') or cfg.get('vision_canonical_dir')):
                from upload_vision import scan_once
                return scan_once(root, cfg)
            return {'state':'off'}
        run('vision', scan)
        run('inbox', lambda: sweep(root))
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--daemon', action='store_true', help='Also supervise the authenticated shared reader.')
    parser.add_argument('--once', action='store_true', help='Run one pass and exit.')
    args = parser.parse_args()
    root = args.root.resolve()
    state_dir = root / 'capture-state' / 'periodic-worker'
    try:
        with capture_lock(state_dir):
            next_capture = next_sweep = 0
            status = {}
            while True:
                now = time.monotonic()
                capture_due, sweep_due = now >= next_capture, now >= next_sweep
                status.update(maintenance(root, capture_due=capture_due, sweep_due=sweep_due,
                                          daemon=args.daemon))
                status['updated_at'] = datetime.now(timezone.utc).isoformat()
                dump_atomic(state_dir / 'status.json', status)
                if args.once:
                    return 0
                if capture_due:
                    next_capture = now + 60
                if sweep_due:
                    next_sweep = now + 120
                time.sleep(10)
    except CaptureLockBusy:
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
