"""Shared local loopback daemon (loopback only) for ThreadSatchel read-only MCP tools.

Holds the prewarmed source/vector cache once in RAM across all local and SSH
stdio MCP sessions (Claude, Codex, Gemini) and the plugin connector.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server_readonly  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

HOST = '127.0.0.1'
from reader_transport import daemon_port, proof, reader_key
PORT = daemon_port()

ALLOWED_TOOLS = {
    'search_memory': server_readonly.search_memory,
    'list_memories': server_readonly.list_memories,
    'get_memory': server_readonly.get_memory,
    'peek_memory': server_readonly.peek_memory,
    'timeline_memory': server_readonly.timeline_memory,
    'get_project_brief': server_readonly.get_project_brief,
    'memory_ai_status': server_readonly.memory_ai_status,
    'list_handoffs': server_readonly.list_handoffs,
    'get_handoff': server_readonly.get_handoff,
    'read_handoff_file': server_readonly.read_handoff_file,
}

_prewarm_state = {'state': 'starting', 'running': True}
_prewarm_lock = threading.RLock()


def _prewarm_loop(stop_event: threading.Event):
    try:
        import ai_memory as ai
        from memory_prewarm import warm_once
        from memory_resources import ResourceMonitor, resource_reason
        monitor = ResourceMonitor()
        monitor.sample()
        if stop_event.wait(3):
            return
        while not stop_event.is_set():
            try:
                config = ai.load_config(ROOT)
                if not config.get('enabled'):
                    with _prewarm_lock:
                        _prewarm_state.update(state='off', running=True)
                else:
                    sample = monitor.sample()
                    reason = resource_reason(config, sample)
                    if reason is not None:
                        with _prewarm_lock:
                            _prewarm_state.update(state='deferred_busy', running=True)
                    elif config.get('prewarm_enabled'):
                        if config.get('embeddings'):
                            from ai_embeddings import _optional_numpy
                            _optional_numpy()
                        report = warm_once(ROOT, config)
                        with _prewarm_lock:
                            _prewarm_state.clear()
                            _prewarm_state.update(report, running=True)
            except Exception:
                with _prewarm_lock:
                    _prewarm_state.update(state='unavailable', running=True)
            stop_event.wait(30)
    except Exception:
        with _prewarm_lock:
            _prewarm_state.update(state='unavailable', running=False)


class DaemonHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        return  # Silence stdio/stderr logging in background daemon

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            nonce = self.headers.get('X-Challenge', '')
            if len(nonce) != 40 or any(c not in '0123456789abcdef' for c in nonce):
                self._send_json(403, {'ok': False})
                return
            self._send_json(200, {'proof': proof(self.server.reader_key, b'health', nonce.encode())})
            return
        self._send_json(404, {'ok': False, 'error': 'not_found'})

    def do_POST(self):
        if self.path != '/rpc':
            self._send_json(404, {'ok': False, 'error': 'not_found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 262144:
                self._send_json(400, {'ok': False, 'error_type': 'ValueError', 'message': 'Invalid request size'})
                return
            raw = self.rfile.read(length)
            expected = proof(self.server.reader_key, b'rpc', raw)
            if not hmac.compare_digest(expected, self.headers.get('X-Request-Proof', '')):
                self._send_json(403, {'ok': False})
                return
            req = json.loads(raw.decode('utf-8'))
            tool = req.get('tool')
            args = req.get('args') or {}
            if tool not in ALLOWED_TOOLS or not isinstance(args, dict):
                self._send_json(400, {'ok': False, 'error_type': 'ValueError', 'message': 'Unsupported tool or arguments'})
                return
            fn = ALLOWED_TOOLS[tool]
            result = fn(**args)
            if tool == 'memory_ai_status' and isinstance(result, dict):
                with _prewarm_lock:
                    result['prewarm'] = dict(_prewarm_state)
            self._send_json(200, {'ok': True, 'result': result})
        except ToolError as exc:
            self._send_json(200, {'ok': False, 'error_type': 'ToolError', 'message': str(exc)})
        except Exception as exc:
            self._send_json(200, {'ok': False, 'error_type': type(exc).__name__, 'message': str(exc)})


def main():
    os.environ['THREADSATCHEL_READER_WORKER'] = '1'
    from codex_capture import capture_lock, CaptureLockBusy
    key = reader_key(ROOT)
    try:
        with capture_lock(ROOT / '.reader-state'):
            with ThreadingHTTPServer((HOST, PORT), DaemonHandler) as server:
                server.reader_key = key
                stop_event = threading.Event()
                worker = threading.Thread(target=_prewarm_loop, args=(stop_event,), daemon=True)
                worker.start()
                try:
                    server.serve_forever(poll_interval=0.5)
                finally:
                    stop_event.set()
    except CaptureLockBusy:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
