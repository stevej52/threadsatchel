"""Synthetic-only embedding transport, isolation, lifecycle, and ranking tests."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import ai_embeddings as embeddings


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-embedding-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'synthetic.gguf').write_bytes(b'synthetic-model-not-for-inference')
        (self.root / 'synthetic-server.exe').write_bytes(b'synthetic-server-not-executable')
        self.config = dict(enabled=True, embeddings=True, embedding_model_path='synthetic.gguf',
                           llama_server_path='synthetic-server.exe', embedding_timeout_seconds=2)
        self.requests = []
        self.status = 200
        self.response = {'data': [{'index': 0, 'embedding': [3.0, 4.0]}]}
        self.discovery = None
        self.content_length = None
        self.body_delay = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, content, status=200):
                body = json.dumps(content).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(owner.content_length or len(body)))
                if 300 <= status < 400:
                    self.send_header('Location', 'http://192.0.2.1/private')
                self.end_headers()
                if owner.body_delay:
                    time.sleep(owner.body_delay)
                try:
                    self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass

            def do_GET(self):
                owner.requests.append(('GET', self.path))
                discovery = owner.discovery
                if discovery is None:
                    discovery = {'data': [{'id': owner.alias}]}
                self.reply(discovery)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(('POST', self.path, payload))
                self.reply(owner.response, owner.status)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.config['embedding_endpoint'] = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = embeddings.EmbeddingClient(self.config, self.root)
        self.alias = self.client.model_alias
        self.addCleanup(self.client.close)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_disabled_is_inert_and_strict_boolean(self):
        with patch.object(embeddings, '_local_file') as files, \
                patch.object(embeddings.http.client, 'HTTPConnection') as http, \
                patch.object(embeddings.subprocess, 'Popen') as spawn:
            for config in ({}, {'enabled': False}, {'enabled': 'true'}, {'enabled': 1},
                           {'enabled': True, 'embeddings': False},
                           {'enabled': True, 'embeddings': 'true'}):
                client = embeddings.EmbeddingClient(config, self.root)
                with self.assertRaises(embeddings.EmbeddingDisabled):
                    client.embed(['synthetic'])
                with self.assertRaises(embeddings.EmbeddingDisabled):
                    client.ensure_ready()
                client.close()
            files.assert_not_called()
            http.assert_not_called()
            spawn.assert_not_called()

    def test_endpoint_rejects_remote_ambiguous_and_chat_urls(self):
        for endpoint in ('https://127.0.0.1:8091', 'http://localhost:8091',
                         'http://example.com', 'http://192.168.1.4:8091',
                         'http://127.0.0.1.evil.example:8091', 'http://2130706433:8091',
                         'http://127.1:8091', 'http://user:pass@127.0.0.1:8091',
                         'http://127.0.0.1:8091/remote', 'http://127.0.0.1:8091?q=1',
                         'http://127.0.0.1:8091#x', 'http://127.0.0.1:0',
                         'http://127.0.0.1:8090', ' http://127.0.0.1:8091',
                         'http://[::1%zone]:8091'):
            with self.subTest(endpoint=endpoint), self.assertRaises(embeddings.EmbeddingError):
                embeddings.EmbeddingClient(dict(self.config, embedding_endpoint=endpoint), self.root)
        self.assertEqual(embeddings._endpoint('http://[::1]:8091/v1'), ('::1', 8091))

    def test_order_normalization_query_prefix_and_proxy_bypass(self):
        self.response = {'data': [{'index': 1, 'embedding': [0.0, 9.0]},
                                  {'index': 0, 'embedding': [3.0, 4.0]}]}
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://192.0.2.1:1',
                                    'HTTPS_PROXY': 'http://192.0.2.1:1', 'NO_PROXY': ''}):
            result = self.client.embed(['synthetic first', 'synthetic second'], query=True)
        self.assertEqual(result, [[0.6, 0.8], [0.0, 1.0]])
        request = self.requests[-1]
        self.assertEqual(request[:2], ('POST', '/v1/embeddings'))
        self.assertEqual(request[2]['input'], [embeddings.QUERY_PREFIX + 'synthetic first',
                                              embeddings.QUERY_PREFIX + 'synthetic second'])
        self.response = {'data': [{'index': 0, 'embedding': [3, 4]}]}
        self.client.embed(['synthetic document'])
        self.assertEqual(self.requests[-1][2]['input'], ['synthetic document'])

    def test_redirect_response_never_followed(self):
        self.status = 307
        with self.assertRaisesRegex(embeddings.EmbeddingError, 'redirects'):
            self.client.embed(['synthetic'])
        self.assertEqual(len(self.requests), 2)

    def test_malformed_vectors_and_response_indexes_rejected(self):
        malformed = [None, [], [0, 0], [True, 0], ['1', 0], [None, 0],
                     [float('nan'), 1], [float('inf'), 1], [[1], 0], [1] * 4097]
        for vector in malformed:
            self.response = {'data': [{'index': 0, 'embedding': vector}]}
            with self.subTest(vector=str(vector)[:60]), self.assertRaises(embeddings.EmbeddingError):
                self.client.embed(['synthetic'])
        for response in ({'data': []}, {'data': [{}]}, {'data': [{'index': True, 'embedding': [1]}]},
                         {'data': [{'index': 1, 'embedding': [1]}]},
                         {'model': 'wrong', 'data': [{'index': 0, 'embedding': [1]}]}):
            self.response = response
            with self.assertRaises(embeddings.EmbeddingError):
                self.client.embed(['synthetic'])
        self.response = {'data': [{'index': 0, 'embedding': [1, 2]},
                                  {'index': 0, 'embedding': [2, 3]}]}
        with self.assertRaisesRegex(embeddings.EmbeddingError, 'indexes'):
            self.client.embed(['synthetic one', 'synthetic two'])

    def test_dimension_change_is_rejected(self):
        self.client.embed(['synthetic'])
        self.response = {'data': [{'index': 0, 'embedding': [1]}]}
        with self.assertRaisesRegex(embeddings.EmbeddingError, 'dimensions'):
            self.client.embed(['synthetic'])

    def test_input_and_response_bounds(self):
        with patch.object(self.client, 'ensure_ready') as ready:
            for value in (['x' * 1601], ['\u2603' * 534], ['x'] * 33, [''], ['\ud800'], 'text'):
                with self.assertRaises(embeddings.EmbeddingError):
                    self.client.embed(value)
            self.assertEqual(self.client.embed([]), [])
            ready.assert_not_called()
        self.client.ensure_ready()
        self.content_length = embeddings.MAX_RESPONSE_BYTES + 1
        with self.assertRaisesRegex(embeddings.EmbeddingError, 'too large'):
            self.client.embed(['synthetic'])

    def test_nonmatching_endpoint_never_launches(self):
        self.discovery = {'data': [{'id': 'other-server'}]}
        with patch.object(embeddings.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(embeddings.EmbeddingError, 'different'):
                self.client.ensure_ready()
            spawn.assert_not_called()

    def test_response_body_obeys_total_deadline(self):
        self.client.ensure_ready()
        self.client.timeout = 1
        self.body_delay = 1.5
        started = time.monotonic()
        with self.assertRaises(embeddings.EmbeddingError):
            self.client.embed(['synthetic'])
        self.assertLess(time.monotonic() - started, 1.4)

    def test_no_launch_mode_never_starts_a_cold_model(self):
        with patch.object(self.client, '_probe', side_effect=embeddings._Unavailable('offline')), \
                patch.object(embeddings.subprocess, 'Popen') as spawn:
            with self.assertRaises(embeddings.EmbeddingError):
                self.client.ensure_ready(allow_launch=False)
            spawn.assert_not_called()

    def test_windows_connect_timeout_is_unavailable_before_http(self):
        with patch.object(embeddings.http.client.HTTPConnection, 'connect', side_effect=TimeoutError):
            with self.assertRaises(embeddings._Unavailable):
                self.client._request('GET', '/v1/models')

    def test_existing_server_is_not_owned_or_stopped(self):
        with patch.object(embeddings.subprocess, 'Popen') as spawn:
            self.client.ensure_ready(allow_launch=False)
            self.client.close()
            self.client.close()
            spawn.assert_not_called()
        self.assertIsNone(self.client._process)

    def test_lazy_cpu_launch_and_owned_cleanup(self):
        class Process:
            def __init__(self):
                self.terminated = False
            def poll(self):
                return 0 if self.terminated else None
            def terminate(self):
                self.terminated = True
            def wait(self, timeout):
                return 0

        process = Process()
        with patch.object(self.client, '_probe', side_effect=[embeddings._Unavailable('offline'), None]), \
                patch.object(embeddings.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(embeddings, '_WindowsJob') as job, \
                patch.dict(os.environ, {'LLAMA_ARG_RPC': 'remote.example:5000',
                                        'LLAMA_ARG_HOST': '0.0.0.0', 'LLAMA_API_KEY': 'synthetic'}):
            self.client.ensure_ready()
            args, kwargs = spawn.call_args
            command = args[0]
            self.assertEqual(command[command.index('--n-gpu-layers') + 1], '0')
            self.assertEqual(command[command.index('--device') + 1], 'none')
            self.assertEqual(command[command.index('--host') + 1], '127.0.0.1')
            self.assertEqual(command[command.index('--pooling') + 1], 'last')
            self.assertIn('--offline', command)
            self.assertEqual(kwargs['stdout'], embeddings.subprocess.DEVNULL)
            self.assertNotIn('LLAMA_ARG_RPC', kwargs['env'])
            self.assertNotIn('LLAMA_API_KEY', kwargs['env'])
            if os.name == 'nt':
                self.assertEqual(kwargs['creationflags'], embeddings.subprocess.CREATE_NO_WINDOW)
            self.client.close()
            self.assertTrue(process.terminated)
            if os.name == 'nt':
                job.return_value.close.assert_called_once()

    def test_fingerprint_is_content_and_protocol_bound(self):
        first = self.client.model_fingerprint
        second = embeddings.EmbeddingClient(self.config, self.root)
        self.addCleanup(second.close)
        self.assertEqual(first, second.model_fingerprint)
        (self.root / 'synthetic.gguf').write_bytes(b'changed synthetic model')
        third = embeddings.EmbeddingClient(self.config, self.root)
        self.addCleanup(third.close)
        self.assertNotEqual(first, third.model_fingerprint)
        self.assertEqual(len(first), 64)

    def test_invalid_files_and_resource_settings(self):
        bad = embeddings.EmbeddingClient(dict(self.config, embedding_model_path='missing.gguf'), self.root)
        with patch.object(embeddings.http.client, 'HTTPConnection') as http:
            with self.assertRaises(embeddings.EmbeddingError):
                bad.ensure_ready()
            http.assert_not_called()
        for key, value in [('embedding_threads', 0), ('embedding_threads', True),
                           ('embedding_timeout_seconds', float('nan')),
                           ('embedding_timeout_seconds', 1000)]:
            with self.assertRaises(embeddings.EmbeddingError):
                embeddings.EmbeddingClient(dict(self.config, **{key: value}), self.root)


class RankingTests(unittest.TestCase):
    def test_cosine_ranking_scale_ties_and_limit(self):
        rows = [('opposite', [-1, 0]), ('side', [0, 2]), ('first', [10, 0]), ('second', [1, 0])]
        self.assertEqual(embeddings.cosine_top_k([2, 0], rows, k=3),
                         [('first', 1.0), ('second', 1.0), ('side', 0.0)])
        self.assertEqual(embeddings.cosine_top_k([2, 0], rows, k=0), [])

    def test_invalid_rows_are_not_silently_scored(self):
        for vector in ([0, 0], [1], [None, 1], [float('nan'), 1]):
            with self.assertRaises(embeddings.EmbeddingError):
                embeddings.cosine_top_k([1, 0], [('bad', vector)])
        self.assertAlmostEqual(sum(x*x for x in embeddings.normalize_vector([1e308, 1e308])), 1)
        self.assertAlmostEqual(sum(x*x for x in embeddings.normalize_vector([1e-320, 1e-320])), 1)

    def test_numpy_and_stdlib_match_and_validate_the_same_inputs(self):
        numpy = embeddings._optional_numpy()
        rows = [('opposite', [-1, 0]), ('side', [0, 2]), ('first', [10, 0]), ('second', [1, 0])]
        with patch.object(embeddings, '_optional_numpy', return_value=None):
            baseline = embeddings.cosine_top_k([1, 0], rows, k=4)
        if numpy is not None:
            self.assertEqual(embeddings.cosine_top_k([1, 0], rows, k=4), baseline)
        for backend in (None, numpy):
            with patch.object(embeddings, '_optional_numpy', return_value=backend):
                for vector in ([True, 0], ['1', 0], [None, 1], [float('inf'), 1],
                               [float('nan'), 1], [0, 0], [1], [10 ** 1000, 1]):
                    with self.assertRaises(embeddings.EmbeddingError):
                        embeddings.cosine_top_k([1, 0], [('bad', vector)])

    def test_numpy_batches_keep_stable_ties_and_bounded_order(self):
        rows = (({'position': n}, [1, 0]) for n in range(700))
        self.assertEqual([row['position'] for row, _ in embeddings.cosine_top_k([1, 0], rows, 20)],
                         list(range(20)))

    def test_synthetic_ranking_benchmark(self):
        # A deterministic synthetic microbenchmark; no archive data or inference.
        dimensions, count = 1024, 1000
        query = [math.sin(n) for n in range(dimensions)]
        rows = ((str(row), [math.sin(n + row / 10) for n in range(dimensions)])
                for row in range(count))
        started = time.perf_counter()
        result = embeddings.cosine_top_k(query, rows, 20)
        elapsed = time.perf_counter() - started
        self.assertEqual(result[0][0], '0')
        self.assertEqual(len(result), 20)
        print('Synthetic cosine benchmark: %d x %d in %.3fs' % (count, dimensions, elapsed))


@unittest.skipUnless(os.name == 'nt', 'Windows process-lifetime behavior')
class WindowsLifecycleTests(unittest.TestCase):
    def test_closing_job_stops_only_synthetic_owned_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            job = embeddings._WindowsJob(child)
            self.assertIsNone(child.poll())
            job.close()
            child.wait(timeout=3)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
