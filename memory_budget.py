"""Serialized response and persistent per-lookup quotas; never writes originals.

The fallback counts UTF-8 bytes of JSON (an upper bound for byte-token BPEs).
An installed tiktoken encoding is used only when its vocabulary is already local;
no download or package installation is attempted. Transport framing is reserved
separately. Exhausted lookups raise, rather than returning unmetered data.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import types
from uuid import uuid4

MAX_BUDGET = 2000
MIN_BUDGET = 512
WRAPPER_RESERVE = 256
TRANSPORT_ID_RESERVE = 64
LOOKUP_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_LOOKUPS = 4096
REFERENCE_PATTERN = re.compile(r'r[0-9a-f]{12}\Z')
_ENCODERS = []
_ENCODER_CHECKED = False


def serialized(value):
    # Count pretty ASCII JSON too: MCP transports can pretty-print or escape it.
    return json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False)


def _encoders():
    global _ENCODERS, _ENCODER_CHECKED
    if not _ENCODER_CHECKED:
        _ENCODER_CHECKED = True
        try:
            import tiktoken
            import tiktoken_ext.openai_public as public
            roots = [Path(__file__).resolve().parent / '.tokenizer-cache']
            if os.environ.get('TIKTOKEN_CACHE_DIR'):
                roots.insert(0, Path(os.environ['TIKTOKEN_CACHE_DIR']))

            def local_ranks(url, expected_hash=None):
                names = [url.rsplit('/', 1)[-1], hashlib.sha1(url.encode()).hexdigest()]
                for root in roots:
                    for name in names:
                        path = root / name
                        if not path.is_file():
                            continue
                        data = path.read_bytes()
                        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
                            continue
                        return {base64.b64decode(token): int(rank)
                                for line in data.splitlines() if line
                                for token, rank in [line.split()]}
                raise FileNotFoundError('Tokenizer vocabulary is not provisioned locally')

            for name in ('o200k_base', 'cl100k_base'):
                try:
                    constructor = getattr(public, name)
                    # Clone only the official constructor's globals. Its loader
                    # can read verified local bytes only, never the network.
                    globals_copy = dict(constructor.__globals__, load_tiktoken_bpe=local_ranks)
                    local_constructor = types.FunctionType(constructor.__code__, globals_copy,
                        constructor.__name__, constructor.__defaults__, constructor.__closure__)
                    _ENCODERS.append(tiktoken.Encoding(**local_constructor()))
                except (OSError, ValueError, AttributeError, TypeError):
                    pass
        except (ImportError, AttributeError, OSError):
            pass
    # Using only one vocabulary is not a verified bound for the other.
    return _ENCODERS if len(_ENCODERS) == 2 else []


def count_method():
    return 'max_o200k_cl100k_json' if _encoders() else 'json_utf8_byte_upper_bound'


def token_count(value):
    variants = [serialized(value), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)]
    encoders = _encoders()
    if encoders:
        try:
            # Archived text may literally contain model control-token spellings.
            # They are ordinary text in this JSON payload.
            return max(len(encoder.encode(text, disallowed_special=()))
                       for encoder in encoders for text in variants)
        except ValueError:
            # Some valid inputs exhaust the BPE regex engine's backtracking
            # stack. A UTF-8 byte upper bound still lets the caller shrink or
            # refuse that candidate and count its final reply normally.
            pass
    return max(len(text.encode('utf-8')) for text in variants)


def fits(value, budget_tokens=MAX_BUDGET, reserve=WRAPPER_RESERVE):
    """Stateless guard for legacy/connector envelopes; includes wrapper reserve."""
    return token_count(value) + reserve <= validate_budget(budget_tokens)


def transport_template(value):
    """Match the connector/gateway completed-operation response envelope."""
    return dict(operation_id='', status='completed', ok=True, result=value,
                response_budget=dict(v=1, used_tokens=9999))


def transport_cost(value):
    # The real operation ID replaces the empty string; reserve its worst case.
    return token_count(transport_template(value)) + TRANSPORT_ID_RESERVE


def validate_budget(value):
    if type(value) is not int or not MIN_BUDGET <= value <= MAX_BUDGET:
        raise ValueError('budget_tokens must be an integer from 512 to 2000')
    return value


def cache_connect(root):
    folder = Path(root) / '.retrieval-cache'
    folder.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(folder / 'index.sqlite3', timeout=15)
    cache.row_factory = sqlite3.Row
    cache.execute('PRAGMA busy_timeout=15000')
    cache.executescript('''
        CREATE TABLE IF NOT EXISTS lookups(
            id TEXT PRIMARY KEY, capacity INTEGER NOT NULL, used INTEGER NOT NULL,
            scope TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS lookup_sources(
            lookup_id TEXT NOT NULL, memory_id TEXT NOT NULL,
            PRIMARY KEY(lookup_id,memory_id));
        CREATE TABLE IF NOT EXISTS lookup_refs(
            lookup_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            memory_id TEXT NOT NULL, revision TEXT NOT NULL,
            start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
            ref TEXT NOT NULL UNIQUE,
            PRIMARY KEY(lookup_id,ordinal), UNIQUE(lookup_id,memory_id,revision,start_char,end_char));
    ''')
    if 'ref' not in {row[1] for row in cache.execute('PRAGMA table_info(lookup_refs)')}:
        cache.execute('ALTER TABLE lookup_refs ADD COLUMN ref TEXT')
    with cache:
        for row in cache.execute('SELECT lookup_id,ordinal FROM lookup_refs WHERE ref IS NULL').fetchall():
            cache.execute('UPDATE lookup_refs SET ref=? WHERE lookup_id=? AND ordinal=?',
                          ('r'+uuid4().hex[:12], row[0], row[1]))
        cache.execute('CREATE UNIQUE INDEX IF NOT EXISTS lookup_ref_token ON lookup_refs(ref)')
    indexes = cache.execute("SELECT name FROM pragma_index_list('lookup_refs') WHERE \"unique\"=1").fetchall()
    old_location_key = any([row['name'] for row in cache.execute('SELECT name FROM pragma_index_info(?)',
        (index['name'],))] == ['lookup_id','memory_id','revision'] for index in indexes)
    if old_location_key:
        # Preserve already-issued references while allowing a correction preview
        # to have its own location ref within the same immutable source revision.
        with cache:
            cache.execute('BEGIN IMMEDIATE')
            cache.execute('''CREATE TABLE lookup_refs_next(
                lookup_id TEXT NOT NULL,ordinal INTEGER NOT NULL,memory_id TEXT NOT NULL,revision TEXT NOT NULL,
                start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,ref TEXT NOT NULL UNIQUE,
                PRIMARY KEY(lookup_id,ordinal),UNIQUE(lookup_id,memory_id,revision,start_char,end_char))''')
            cache.execute('INSERT INTO lookup_refs_next SELECT lookup_id,ordinal,memory_id,revision,start_char,end_char,ref FROM lookup_refs')
            cache.execute('DROP TABLE lookup_refs')
            cache.execute('ALTER TABLE lookup_refs_next RENAME TO lookup_refs')
    with cache:
        cache.execute('DELETE FROM lookups WHERE created<?', (time.time()-LOOKUP_TTL_SECONDS,))
        cache.execute('DELETE FROM lookups WHERE id IN '
            '(SELECT id FROM lookups ORDER BY created DESC,id DESC LIMIT -1 OFFSET ?)', (MAX_LOOKUPS,))
        cache.execute('DELETE FROM lookup_sources WHERE lookup_id NOT IN (SELECT id FROM lookups)')
        cache.execute('DELETE FROM lookup_refs WHERE lookup_id NOT IN (SELECT id FROM lookups)')
    return cache


class Budget:
    def __init__(self, cache, capacity, lookup_id=None, memory_id=None):
        self.cache = cache
        self.capacity = validate_budget(capacity)
        if lookup_id is not None and (not isinstance(lookup_id, str) or
                                     not re.fullmatch(r'[0-9a-f]{32}', lookup_id)):
            raise ValueError('Invalid lookup_id')
        self.id = lookup_id or uuid4().hex
        cache.execute('BEGIN IMMEDIATE')
        if lookup_id is None:
            cache.execute('INSERT INTO lookups VALUES(?,?,0,?,?)',
                          (self.id, capacity, '', time.time()))
            cache.execute('DELETE FROM lookups WHERE id IN '
                '(SELECT id FROM lookups ORDER BY created DESC,id DESC LIMIT -1 OFFSET ?)', (MAX_LOOKUPS,))
            cache.execute('DELETE FROM lookup_sources WHERE lookup_id NOT IN (SELECT id FROM lookups)')
            cache.execute('DELETE FROM lookup_refs WHERE lookup_id NOT IN (SELECT id FROM lookups)')
            self.used = 0
            if memory_id is not None:
                self.authorize(memory_id)
        else:
            row = cache.execute('SELECT * FROM lookups WHERE id=?', (lookup_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown lookup_id; begin a new lookup')
            self.capacity = min(capacity, row['capacity'])
            self.used = row['used']
            if memory_id is not None and cache.execute(
                    'SELECT 1 FROM lookup_sources WHERE lookup_id=? AND memory_id=?',
                    (self.id, memory_id)).fetchone() is None:
                raise ValueError('Memory is outside this lookup; begin a new lookup')
        self._references = {}
        self._next_ordinal = cache.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM lookup_refs '
                                           'WHERE lookup_id=?', (self.id,)).fetchone()[0]

    @property
    def available(self):
        return max(0, self.capacity - self.used - TRANSPORT_ID_RESERVE)

    def authorize(self, memory_id):
        self.cache.execute('INSERT OR IGNORE INTO lookup_sources VALUES(?,?)',
                           (self.id, memory_id))

    def reference(self, memory_id, revision, start_char=0, end_char=0):
        """Reserve a short navigation ref; only published refs become resolvable."""
        key = (memory_id, revision, start_char, end_char)
        if key in self._references:
            return self._references[key]['ref']
        row = self.cache.execute('SELECT * FROM lookup_refs WHERE lookup_id=? AND memory_id=? AND revision=? '
                                 'AND start_char=? AND end_char=?',
                                 (self.id, memory_id, revision, start_char, end_char)).fetchone()
        if row is not None:
            value = dict(row, published=True)
        else:
            ref = 'r'+uuid4().hex[:12]
            while (any(value['ref'] == ref for value in self._references.values()) or
                   self.cache.execute('SELECT 1 FROM lookup_refs WHERE ref=?', (ref,)).fetchone()):
                ref = 'r'+uuid4().hex[:12]
            value = dict(ordinal=self._next_ordinal, memory_id=memory_id, revision=revision,
                         start_char=start_char, end_char=end_char,
                         ref=ref, published=False)
            self._next_ordinal += 1
        self._references[key] = value
        return value['ref']

    def publish_reference(self, ref):
        value = next(value for value in self._references.values() if value['ref'] == ref)
        if not value['published']:
            self.cache.execute('INSERT INTO lookup_refs(lookup_id,ordinal,memory_id,revision,start_char,end_char,ref) '
                               'VALUES(?,?,?,?,?,?,?)',
                (self.id, value['ordinal'], value['memory_id'], value['revision'],
                 value['start_char'], value['end_char'], value['ref']))
            value['published'] = True
        self.authorize(value['memory_id'])

    def envelope(self, payload):
        result = dict(payload, lookup_id=self.id)
        # Fixed-width pessimistic values make fitting independent of the final
        # decimal lengths. Count the budget metadata as part of the response.
        result['budget'] = dict(limit=self.capacity, charged=9999,
                                remaining=9999, method=count_method())
        return result

    def fits(self, payload, maximum=None):
        result = self.envelope(payload)
        if maximum is not None and token_count(result) > maximum:
            return False
        return transport_cost(result) <= self.capacity - self.used

    def finish(self, payload):
        if not self.fits(payload):
            raise ValueError('Lookup budget exhausted; additional retrieval requires a separately authorized lookup')
        result = self.envelope(payload)
        charge = transport_cost(result)
        result['budget']['charged'] = charge
        result['budget']['remaining'] = self.capacity - self.used - charge
        assert transport_cost(result) <= charge
        self.cache.execute('UPDATE lookups SET used=used+? WHERE id=?', (charge, self.id))
        self.cache.commit()
        return result


@contextmanager
def lookup_budget(root, capacity=MAX_BUDGET, lookup_id=None, memory_id=None):
    cache = cache_connect(root)
    try:
        yield Budget(cache, capacity, lookup_id, memory_id)
    finally:
        if cache.in_transaction:
            cache.rollback()
        cache.close()


def resolve_reference(root, lookup_id, ref, *, optional=False):
    """Resolve only a previously returned ref from this exact unexpired lookup."""
    if not isinstance(lookup_id, str) or not re.fullmatch(r'[0-9a-f]{32}', lookup_id):
        raise ValueError('A valid lookup_id is required with ref')
    match = REFERENCE_PATTERN.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise ValueError('Invalid memory ref')
    cache = cache_connect(root)
    try:
        row = cache.execute('SELECT r.* FROM lookup_refs r JOIN lookups l ON l.id=r.lookup_id '
            'WHERE r.lookup_id=? AND r.ref=?', (lookup_id, ref)).fetchone()
        if row is None and not optional:
            raise ValueError('Unknown or expired memory ref for this lookup')
        return dict(row) if row is not None else None
    finally:
        cache.close()
