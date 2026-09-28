"""Optional, bounded CPU embeddings over a dedicated local llama.cpp server.

Importing this module performs no I/O and requires only Python's standard library.
The caller must explicitly enable the feature, and must close clients it creates.
The embedding endpoint is deliberately separate from the robot's chat service.
"""
from __future__ import annotations

import atexit
from array import array
import hashlib
import heapq
import http.client
import ipaddress
from itertools import chain
import json
import math
import os
import socket
import struct
import sys
from pathlib import Path
import stat
import subprocess
import threading
import time
from functools import lru_cache
from urllib.parse import urlsplit


MAX_TEXT_BYTES = 1600
MAX_BATCH_SIZE = 32
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_VECTOR_DIMENSIONS = 4096
QUERY_PREFIX = ('Instruct: Given a search query, retrieve relevant passages from '
                'previous conversations that answer the query\nQuery: ')
PROTOCOL_VERSION = 'qwen3-last-l2-query-instruct-v1'


class EmbeddingError(RuntimeError):
    """Safe error text: never includes source text, HTTP bodies, or credentials."""


class EmbeddingDisabled(EmbeddingError):
    pass


class _Unavailable(EmbeddingError):
    pass


class _Loading(_Unavailable):
    pass


def _endpoint(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise EmbeddingError('Embedding endpoint must be a literal loopback HTTP URL')
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or '')
        port = 80 if parsed.port is None else parsed.port
    except ValueError:
        raise EmbeddingError('Embedding endpoint must be a literal loopback HTTP URL') from None
    if (parsed.scheme != 'http' or not address.is_loopback or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/', '/v1', '/v1/')
            or parsed.query or parsed.fragment or '%' in (parsed.hostname or '')
            or not 1 <= port <= 65535):
        raise EmbeddingError('Embedding endpoint must be a literal loopback HTTP URL')
    if port == 8090:
        raise EmbeddingError('Port 8090 is reserved for the separate chat service')
    return str(address), port


def _local_file(value, root, label):
    if not isinstance(value, (str, os.PathLike)) or not str(value):
        raise EmbeddingError(label + ' is not configured')
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve(strict=True)
        if str(path).startswith(('\\\\', '//')) or not stat.S_ISREG(path.stat().st_mode):
            raise OSError()
    except (OSError, ValueError, RuntimeError):
        raise EmbeddingError(label + ' must be an existing local regular file') from None
    return path


@lru_cache(maxsize=8)
def _file_hash(path, size, modified_ns, changed_ns):
    """Cache by file identity; a model replacement cannot reuse the old digest."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            part = stream.read(1024 * 1024)
            if not part:
                break
            digest.update(part)
    return digest.hexdigest()


def normalize_vector(vector, dimensions=None):
    """Validate a numeric sequence and return a finite, nonzero unit vector."""
    if (not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= MAX_VECTOR_DIMENSIONS
            or (dimensions is not None and len(vector) != dimensions)):
        raise EmbeddingError('Invalid embedding vector dimensions')
    values = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingError('Embedding vector contains a nonnumeric value')
        try:
            number = float(value)
        except (OverflowError, ValueError):
            raise EmbeddingError('Embedding vector contains an invalid number') from None
        if not math.isfinite(number):
            raise EmbeddingError('Embedding vector contains a nonfinite value')
        values.append(number)
    scale = max(abs(v) for v in values)
    if not scale:
        raise EmbeddingError('Embedding vector is zero')
    # Scaling prevents overflow/underflow in the norm for otherwise finite values.
    values = [v / scale for v in values]
    length = math.sqrt(math.fsum(v * v for v in values))
    return [v / length for v in values]


@lru_cache(maxsize=1)
def _optional_numpy():
    """Load acceleration only when ranking is requested; absence is supported."""
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def _numpy_scores(query, rows, numpy):
    """Score bounded batches without BLAS thread pools or a full archive matrix."""
    query_array = numpy.asarray(query, dtype=numpy.float64)
    dimensions = len(query)
    iterator = iter(rows)
    while True:
        identifiers, vectors = [], []
        for _ in range(256):
            try:
                identifier, vector = next(iterator)
            except StopIteration:
                break
            if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
                raise EmbeddingError('Invalid embedding vector dimensions')
            identifiers.append(identifier)
            vectors.append(vector)
        if not vectors:
            return
        # JSON numbers are exact int/float. C-level map/set iteration checks the
        # entire batch cheaply without letting NumPy coerce bools or strings.
        if not set(map(type, chain.from_iterable(vectors))).issubset({int, float}):
            vectors = [normalize_vector(vector, dimensions) for vector in vectors]
        try:
            matrix = numpy.asarray(vectors, dtype=numpy.float64)
        except (ValueError, TypeError, OverflowError):
            raise EmbeddingError('Embedding vector contains an invalid number') from None
        if not numpy.isfinite(matrix).all():
            raise EmbeddingError('Embedding vector contains a nonfinite value')
        scale = numpy.max(numpy.abs(matrix), axis=1)
        if numpy.any(scale == 0):
            raise EmbeddingError('Embedding vector is zero')
        matrix /= scale[:, None]
        length = numpy.sqrt(numpy.einsum('ij,ij->i', matrix, matrix, optimize=False))
        matrix /= length[:, None]
        scores = numpy.einsum('ij,j->i', matrix, query_array, optimize=False)
        for identifier, score in zip(identifiers, scores):
            yield identifier, float(score)


def _stdlib_scores(query, rows):
    for identifier, vector in rows:
        vector = normalize_vector(vector, len(query))
        yield identifier, math.fsum(a * b for a, b in zip(query, vector))


def cosine_top_k(query_vector, rows, k=20):
    """Return [(id, cosine_score)] from iterable [(id, vector)], best first.

    Input vectors need not already be normalized. Invalid vectors raise
    EmbeddingError so the caller can fall back to lexical search. Ties retain
    input order. Optional NumPy uses batches of at most 256 vectors; without it,
    the standard-library fallback scores one row at a time. Neither implementation
    builds an archive-sized matrix, and both validate every vector.
    IDs can be any object; they are never compared or interpreted here.
    """
    if isinstance(k, bool) or not isinstance(k, int) or not 0 <= k <= 1000:
        raise EmbeddingError('Embedding result limit must be an integer from 0 to 1000')
    if not k:
        return []
    query = normalize_vector(query_vector)
    numpy = _optional_numpy()
    scores = _stdlib_scores(query, rows) if numpy is None else _numpy_scores(query, rows, numpy)
    best = []
    for position, (identifier, score) in enumerate(scores):
        score = max(-1.0, min(1.0, score))
        item = (score, -position, identifier)
        if len(best) < k:
            heapq.heappush(best, item)
        elif item[:2] > best[0][:2]:
            heapq.heapreplace(best, item)
    return [(identifier, score) for score, _, identifier in sorted(best, reverse=True)]


def pack_vector(vector):
    """Versioned little-endian float32 storage; never deserialize executable data."""
    values = normalize_vector(vector)
    return b'TSV1' + struct.pack('<' + 'f' * len(values), *values)


def unpack_vector(value):
    if isinstance(value, str):  # Old caches remain readable and migrate in bounded passes.
        return normalize_vector(json.loads(value))
    if not isinstance(value, bytes) or value[:4] != b'TSV1' or (len(value)-4) % 4:
        raise EmbeddingError('Invalid stored embedding')
    count = (len(value)-4)//4
    if not 1 <= count <= MAX_VECTOR_DIMENSIONS:
        raise EmbeddingError('Invalid stored embedding dimensions')
    return normalize_vector(struct.unpack('<' + 'f'*count, value[4:]))


class VectorIndex:
    """Immutable, validated derived matrix, reusable until its SQLite generation changes."""
    def __init__(self, rows):
        identifiers, values = [], array('f')
        dimensions = None
        self.numpy = _optional_numpy()
        for identifier, stored in rows:
            if self.numpy is not None and isinstance(stored,bytes):
                if stored[:4]!=b'TSV1' or (len(stored)-4)%4 or not 1<=(len(stored)-4)//4<=MAX_VECTOR_DIMENSIONS:
                    raise EmbeddingError('Invalid stored embedding')
                vector=array('f');vector.frombytes(stored[4:])
                if sys.byteorder!='little':vector.byteswap()
            else:
                vector = unpack_vector(stored)
            if dimensions is not None and len(vector) != dimensions:
                raise EmbeddingError('Inconsistent stored embedding dimensions')
            dimensions = len(vector)
            identifiers.append(identifier)
            values.extend(vector)
            if len(values)*values.itemsize > 128*1024*1024:
                raise EmbeddingError('Derived vector matrix exceeds the 128 MiB budget')
        self.identifiers = tuple(identifiers)
        if identifiers and self.numpy is not None:
            self.vectors = self.numpy.frombuffer(values, dtype=self.numpy.float32).reshape(len(identifiers),dimensions)
            if not self.numpy.isfinite(self.vectors).all():
                raise EmbeddingError('Invalid stored embedding numbers')
            norms=self.numpy.sqrt(self.numpy.einsum('ij,ij->i',self.vectors,self.vectors,optimize=False))
            if self.numpy.any(norms==0) or not self.numpy.isfinite(norms).all():
                raise EmbeddingError('Invalid stored embedding norm')
            self.vectors/=norms[:,None]
            self.vectors.flags.writeable = False
        else:
            self.vectors = values
        self.dimensions = dimensions

    def top_k(self, query_vector, k=20, groups=None, allowed=None):
        query = normalize_vector(query_vector)
        if not self.identifiers:
            return []
        if len(query) != self.dimensions:
            raise EmbeddingError('Invalid query embedding dimensions')
        if self.numpy is not None:
            scores = self.numpy.einsum('ij,j->i', self.vectors,
                self.numpy.asarray(query, dtype=self.numpy.float32), optimize=False)
        else:
            scores = (math.fsum(a*b for a,b in zip(query,self.vectors[start:start+self.dimensions]))
                      for start in range(0,len(self.vectors),self.dimensions))
        # Keep the best passage per memory before the bounded result selection.
        best = {}
        for position, (identifier, score) in enumerate(zip(self.identifiers, scores)):
            if allowed is not None and identifier not in allowed:
                continue
            key = groups.get(identifier, identifier) if groups is not None else identifier
            item = (max(-1.0, min(1.0, float(score))), -position, identifier)
            if key not in best or item[:2] > best[key][:2]:
                best[key] = item
        return [(identifier, score) for score, _, identifier in heapq.nlargest(k, best.values())]


def bounded_json_request(host, port, method, route, body, timeout, max_bytes, authorization=None):
    """One wall-clock deadline, including slow headers and trickled response bodies."""
    deadline = time.monotonic() + max(0.001, timeout)
    conn = http.client.HTTPConnection(host, port, timeout=max(0.001, timeout))
    sock = None
    timer = None
    try:
        conn.connect()
        sock = conn.sock
        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(max(0.001, deadline-time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        headers={'Content-Type':'application/json', 'Accept':'application/json', 'Connection':'close'}
        if authorization is not None:
            headers['Authorization']='Bearer '+authorization
        conn.request(method, route, body=body,headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise EmbeddingError('Local endpoint returned HTTP ' + str(response.status))
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise EmbeddingError('Encoded local responses are prohibited')
        length = response.getheader('Content-Length')
        if length is not None and not 0 <= int(length) <= max_bytes:
            raise EmbeddingError('Local response exceeds byte budget')
        pieces, received = [], 0
        while not response.isclosed():
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Local request deadline exceeded')
            sock.settimeout(remaining)
            piece = response.read1(min(65536, max_bytes+1-received))
            if not piece:
                break
            pieces.append(piece)
            received += len(piece)
            if received > max_bytes:
                raise EmbeddingError('Local response exceeds byte budget')
        if time.monotonic() >= deadline:
            raise TimeoutError('Local request deadline exceeded')
        return json.loads(b''.join(pieces),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


class _WindowsJob:
    """Kill our child on handle close, including abrupt parent termination."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if (not self.handle or not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.api.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise EmbeddingError('Could not bind embedding server to its owner process')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class EmbeddingClient:
    """An explicitly enabled local embedding client with lazy server ownership.

    Config: enabled (must be True), embeddings (default True when enabled),
    embedding_endpoint (default loopback:8091),
    embedding_model_path, llama_server_path, embedding_threads (default 4),
    embedding_timeout_seconds (default 30). Paths may be relative to root.
    An existing endpoint is used only when its model alias matches our model
    fingerprint. close() terminates only a server started by this instance.
    """
    def __init__(self, config: dict, root: Path):
        self.config = dict(config)
        self.root = Path(root)
        self._enabled = (self.config.get('enabled') is True
                         and self.config.get('embeddings', True) is True)
        self._lock = threading.RLock()
        self._process = None
        self._job = None
        self._model_path = None
        self._fingerprint = None
        self._dimensions = None
        self._ready = False
        self._closed = False
        self._registered = False
        # Disabled construction is inert, even with missing or malformed settings.
        if self._enabled:
            self.host, self.port = _endpoint(self.config.get(
                'embedding_endpoint', 'http://127.0.0.1:8091'))
            threads = self.config.get('embedding_threads', 4)
            timeout = self.config.get('embedding_timeout_seconds', 30)
            if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 32:
                raise EmbeddingError('Embedding threads must be an integer from 1 to 32')
            if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                    or not math.isfinite(timeout) or not 1 <= timeout <= 120):
                raise EmbeddingError('Embedding timeout must be from 1 to 120 seconds')
            self.threads, self.timeout = threads, float(timeout)

    def _check_enabled(self):
        if not self._enabled:
            raise EmbeddingDisabled('Local AI embeddings are disabled')
        if self._closed:
            raise EmbeddingError('Embedding client is closed')

    @property
    def model_fingerprint(self):
        """SHA256 identity of model contents and embedding preprocessing."""
        self._check_enabled()
        with self._lock:
            if self._fingerprint is None:
                self._model_path = _local_file(self.config.get('embedding_model_path'),
                                               self.root, 'Embedding model')
                info = self._model_path.stat()
                try:
                    digest = _file_hash(str(self._model_path), info.st_size,
                                        info.st_mtime_ns, info.st_ctime_ns)
                    after = self._model_path.stat()
                except OSError:
                    raise EmbeddingError('Could not read embedding model') from None
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise EmbeddingError('Embedding model changed while computing its identity')
                identity = PROTOCOL_VERSION + '\n' + QUERY_PREFIX + '\n' + digest
                self._fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
            return self._fingerprint

    @property
    def model_alias(self):
        return 'threadsatchel-' + self.model_fingerprint

    def _request(self, method, route, payload=None, timeout=None):
        """Direct HTTP: no proxy handling, DNS names, redirects, or response logging."""
        budget = self.timeout if timeout is None else min(self.timeout, timeout)
        deadline = time.monotonic() + budget
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode('utf-8')
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise EmbeddingError('Embedding request is too large')
        conn = http.client.HTTPConnection(self.host, self.port, timeout=max(0.01, budget))
        try:
            try:
                conn.connect()
            except (ConnectionRefusedError, TimeoutError):
                raise _Unavailable('Embedding server is not running') from None
            conn.request(method, route, body=body,
                         headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                  'Connection': 'close'})
            conn.sock.settimeout(max(0.01, deadline - time.monotonic()))
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise EmbeddingError('Embedding endpoint redirects are prohibited')
            if response.status == 503:
                raise _Loading('Embedding server is loading or unavailable')
            if response.status != 200:
                raise EmbeddingError('Embedding endpoint returned HTTP ' + str(response.status))
            content_length = response.getheader('Content-Length')
            if content_length is not None:
                try:
                    length = int(content_length)
                except ValueError:
                    raise EmbeddingError('Invalid embedding response length') from None
                if not 0 <= length <= MAX_RESPONSE_BYTES:
                    raise EmbeddingError('Embedding response is too large')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise EmbeddingError('Encoded embedding responses are not supported')
            chunks, received = [], 0
            # read1 returns at most one socket read; refresh the remaining deadline.
            # The response retains the socket after HTTPConnection closes its reference.
            response_socket = response.fp.raw._sock
            while not response.isclosed():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EmbeddingError('Embedding request timed out')
                response_socket.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise EmbeddingError('Embedding response is too large')
            try:
                return json.loads(b''.join(chunks).decode('utf-8'),
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (UnicodeError, ValueError, RecursionError):
                raise EmbeddingError('Embedding endpoint returned invalid JSON') from None
        except ConnectionRefusedError:
            raise _Unavailable('Embedding server is not running') from None
        except (OSError, http.client.HTTPException):
            raise EmbeddingError('Could not complete local embedding request') from None
        finally:
            conn.close()

    def _probe(self, timeout=None):
        response = self._request('GET', '/v1/models', timeout=timeout)
        if not isinstance(response, dict) or not isinstance(response.get('data'), list):
            raise EmbeddingError('Invalid embedding model discovery response')
        if not any(isinstance(row, dict) and row.get('id') == self.model_alias
                   for row in response['data']):
            raise EmbeddingError('Loopback endpoint is serving a different embedding model')

    def _launch(self):
        binary = _local_file(self.config.get('llama_server_path'), self.root, 'llama-server')
        command = [str(binary), '--model', str(self._model_path), '--alias', self.model_alias,
                   '--embedding', '--pooling', 'last', '--ctx-size', '2048',
                   '--batch-size', '2048', '--ubatch-size', '2048', '--threads', str(self.threads),
                   '--threads-batch', str(self.threads), '--n-gpu-layers', '0', '--device', 'none',
                   '--no-kv-offload', '--fit', 'off', '--host', self.host,
                   '--port', str(self.port), '--parallel', '1',
                   '--threads-http', '2', '--offline', '--no-webui', '--no-slots',
                   '--timeout', str(math.ceil(self.timeout))]
        # Ambient llama options must not enable remote RPC, model downloads, or GPU offload.
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(('LLAMA_ARG_', 'LLAMA_API_KEY'))}
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, cwd=str(binary.parent), env=environment)
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        else:
            kwargs['start_new_session'] = True
        try:
            self._process = subprocess.Popen(command, **kwargs)
            if os.name == 'nt':
                self._job = _WindowsJob(self._process)
        except (OSError, EmbeddingError):
            self._stop_owned()
            raise EmbeddingError('Could not start the dedicated CPU embedding server') from None
        if not self._registered:
            atexit.register(self.close)
            self._registered = True

    def ensure_ready(self, allow_launch=True):
        """Check identity or start a local CPU server; wait at most the timeout.

        allow_launch=False performs one bounded probe and never starts a server.
        Use it before embed() on interactive queries to avoid cold starts.
        """
        self._check_enabled()
        if not isinstance(allow_launch, bool):
            raise EmbeddingError('allow_launch must be a boolean')
        with self._lock:
            alias = self.model_alias  # Validate local model before any network access.
            del alias
            if self._ready and (self._process is None or self._process.poll() is None):
                return
            deadline = time.monotonic() + self.timeout
            loading = False
            try:
                self._probe(timeout=min(2, self.timeout))
                self._ready = True
                return
            except _Loading:
                loading = True
                if not allow_launch:
                    raise
            except _Unavailable:
                if not allow_launch:
                    raise
            if self._process is not None and self._process.poll() is not None:
                self._stop_owned()
            if self._process is None and not loading:
                self._launch()
            try:
                while time.monotonic() < deadline:
                    if self._process is not None and self._process.poll() is not None:
                        raise EmbeddingError('Dedicated CPU embedding server exited during startup')
                    try:
                        self._probe(timeout=min(2, max(0.01, deadline - time.monotonic())))
                        self._ready = True
                        return
                    except _Unavailable:
                        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                raise EmbeddingError('Dedicated CPU embedding server startup timed out')
            except Exception:
                self._stop_owned()
                raise

    def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        """Embed 1..32 strings, each <=1600 UTF-8 bytes; [] returns [] inertly.

        Query instructions are added only with query=True. Results are checked
        for count, distinct indexes, dimensions, and finite nonzero numbers,
        reordered to input order, then normalized to unit length.
        """
        self._check_enabled()
        if not isinstance(texts, list) or len(texts) > MAX_BATCH_SIZE or not isinstance(query, bool):
            raise EmbeddingError('Invalid embedding input batch')
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise EmbeddingError('Embedding inputs must be nonempty strings')
            try:
                size = len(text.encode('utf-8'))
            except UnicodeError:
                raise EmbeddingError('Embedding inputs must be valid UTF-8') from None
            if size > MAX_TEXT_BYTES:
                raise EmbeddingError('Embedding input exceeds the UTF-8 byte limit')
        if not texts:
            return []
        with self._lock:
            self.ensure_ready()
            inputs = [QUERY_PREFIX + text if query else text for text in texts]
            try:
                response = self._request('POST', '/v1/embeddings',
                                         {'model': self.model_alias, 'input': inputs,
                                          'encoding_format': 'float'})
            except EmbeddingError:
                self._ready = False
                raise
            if (not isinstance(response, dict) or not isinstance(response.get('data'), list)
                    or len(response['data']) != len(texts)
                    or response.get('model', self.model_alias) != self.model_alias):
                raise EmbeddingError('Embedding response does not match the request')
            vectors, dimensions = {}, self._dimensions
            for row in response['data']:
                if (not isinstance(row, dict) or type(row.get('index')) is not int
                        or not 0 <= row['index'] < len(texts) or row['index'] in vectors):
                    raise EmbeddingError('Embedding response has invalid or duplicate indexes')
                vector = normalize_vector(row.get('embedding'), dimensions)
                dimensions = len(vector)
                vectors[row['index']] = vector
            self._dimensions = dimensions
            return [vectors[index] for index in range(len(texts))]

    def _stop_owned(self):
        self._ready = False
        process, self._process = self._process, None
        try:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                except OSError:
                    # A concurrent normal exit can race terminate(); the job
                    # remains our final guarantee that no Windows child survives.
                    if process.poll() is None:
                        raise EmbeddingError('Could not stop the owned embedding server') from None
        finally:
            if self._job is not None:
                self._job.close()
                self._job = None

    def close(self):
        """Release only owned server resources; safe to call repeatedly."""
        with self._lock:
            self._stop_owned()
            self._closed = True
            if self._registered:
                atexit.unregister(self.close)
                self._registered = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
