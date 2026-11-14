"""Optional in-process RAM preparation. No inference, network, archive writes or service."""
from contextlib import closing, contextmanager
from pathlib import Path
import threading
import time

import ai_memory as ai


@contextmanager
def _phase(report, name):
    started = time.monotonic()
    try:
        yield
    finally:
        report[name + '_ms'] = round((time.monotonic() - started) * 1000, 2)


def warm_once(root, config=None, *, budget_seconds=None):
    """Prepare existing sources, vector matrices and evidence-checked project briefs.

    The report contains counts only. Context stays in bounded process RAM and is
    served through the existing search/get_project_brief interfaces. It is never
    exported to a new file or presented as a new authoritative decision.
    """
    started = time.monotonic()
    config = ai.load_config(root) if config is None else config
    report = dict(state='off', source_records=0, vector_chunks=0, prepared_projects=0,
                  elapsed_ms=0)
    if not config.get('enabled') or not config.get('prewarm_enabled', False):
        return report
    budget = config.get('prewarm_budget_seconds', 3) if budget_seconds is None else budget_seconds
    budget = max(.1, min(float(budget), 15))
    deadline = started + budget
    report['budget_seconds'] = budget
    try:
        with closing(ai.archive_connect(root)) as db:
            with _phase(report, 'sources'):
                records, version = ai.cached_sources(root, db, config, deadline=deadline)
                ai.source_maps(root, records, version, config, deadline=deadline)
                report['source_records'] = len(records)
            ai._check_read_deadline(deadline)
            # Existing stored model identities are sufficient to warm matrices.
            # Retrieval still verifies its actual model identity before using one;
            # prewarming need not hash a large model or contact a model endpoint.
            if config.get('embeddings'):
                with _phase(report, 'vectors'):
                    generation = ai.cache_generation(root, deadline=deadline)
                    with closing(ai.cache_connect(root, deadline=deadline)) as cache:
                        versions = [r[0] for r in cache.execute('SELECT embedding_version FROM chunks '
                            'WHERE vector IS NOT NULL AND embedding_version IS NOT NULL '
                            'GROUP BY embedding_version ORDER BY count(*) DESC LIMIT 2')]
                    for model_version in versions:
                        ai._check_read_deadline(deadline)
                        # Refresh changed generations before request-side reuse
                        # expires; unchanged generations still reuse the matrix.
                        index, _, _ = ai.cached_vector_index(root, generation, model_version,
                                                            deadline=deadline, allow_snapshot_reuse=False)
                        report['vector_chunks'] += len(index.identifiers)
            projects = list(config.get('projects', {}))
            if config.get('include_unassigned', True) and 'general' not in projects:
                projects.append('general')
            with _phase(report, 'briefs'):
                for project in projects[:max(1, min(int(config.get('prewarm_max_projects', 8)), 32))]:
                    ai._check_read_deadline(deadline)
                    brief = ai.project_brief(root, db, project, deadline=deadline)
                    if brief.get('generated'):
                        report['prepared_projects'] += 1
            ai._check_read_deadline(deadline)
            report['state'] = 'ready'
    except TimeoutError:
        report['state'] = 'budget_exhausted'
    except Exception:
        # Never print private source text, credential/path-bearing exceptions or
        # diagnostics to stdout: this runs inside a stdio MCP server.
        report['state'] = 'budget_exhausted' if time.monotonic() >= deadline else 'unavailable'
    report['elapsed_ms'] = round((time.monotonic() - started) * 1000, 2)
    return report


class PrewarmWorker:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.last = dict(state='starting')
        self.thread = threading.Thread(target=self._run, name='memory-cache-prewarm', daemon=True)

    def _set(self, report):
        with self.lock:
            self.last = report

    def _run(self):
        try:
            from memory_resources import ResourceMonitor, resource_reason
            monitor = ResourceMonitor()
            config = ai.load_config(self.root)
            monitor.sample()  # Establish the first CPU measurement without work.
            if self.stop_event.wait(config.get('prewarm_startup_delay_seconds', 5)):
                return
            while not self.stop_event.is_set():
                config = ai.load_config(self.root)
                if not config.get('enabled') or not config.get('prewarm_enabled', False):
                    ai.close_cached_readers(self.root)
                    self._set(dict(state='off'))
                else:
                    sample = monitor.sample()
                    reason = resource_reason(config, sample)
                    if reason == 'resource_unavailable':
                        self._set(dict(state='resource_sample_pending'))
                    elif reason is not None:
                        self._set(dict(state='deferred_busy'))
                    else:
                        # Import acceleration only in this background worker after
                        # resource checks, outside request/cache locks and the warm
                        # pass budget. Foreground queries never wait on an import.
                        if config.get('embeddings'):
                            from ai_embeddings import _optional_numpy
                            _optional_numpy()
                        self._set(warm_once(self.root, config))
                self.stop_event.wait(max(15, config.get('prewarm_interval_seconds', 60)))
        except Exception:
            self._set(dict(state='unavailable'))

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def status(self):
        with self.lock:
            return dict(self.last, running=self.thread.is_alive())


_workers = {}
_workers_lock = threading.RLock()


def start(root):
    """Start at most one bounded worker inside this MCP process, only if enabled."""
    root = Path(root).resolve()
    try:
        config = ai.load_config(root)
        if not config.get('enabled') or not config.get('prewarm_enabled', False):
            return None
        with _workers_lock:
            previous = _workers.get(str(root))
            if previous is not None and previous.thread.is_alive():
                return previous
            worker = PrewarmWorker(root)
            _workers[str(root)] = worker
            worker.thread.start()
            return worker
    except Exception:
        return None


def status(root):
    with _workers_lock:
        worker = _workers.get(str(Path(root).resolve()))
        return worker.status() if worker is not None else dict(state='not_started', running=False)
