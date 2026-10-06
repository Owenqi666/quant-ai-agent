"""Durable attempt ownership around file work, with phase-aware heartbeats.

File copying and hashing run outside SQLite writer transactions. Interrupted
files remain attached to a recorded attempt; startup recovery marks it interrupted
and requires an explicit new attempt rather than guessing publication succeeded.
"""
from contextlib import contextmanager
from itertools import chain
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time

from .db import transaction
from ..evidence import sha256
from ..storage import atomic_json, digest, json_text, read_json
from ..workflow import verify_run

LOG = logging.getLogger('paper_alpha.lifecycle')
PHASES = ('preparing', 'executing', 'verifying', 'publishing')
HEARTBEAT_SECONDS = .5
_SCOPES = threading.local()


def _owned(store, connection, run_id, worker_id, attempt_id):
    from .service import ServiceError
    run = store._one(connection, 'runs', run_id)
    if (run['worker_id'] != worker_id or run['attempt_id'] != attempt_id
            or run['status'] not in {'running', 'cancelling'}):
        raise ServiceError('Worker no longer owns this active attempt', 409)
    attempt = store._one(connection, 'attempts', attempt_id)
    if attempt['run_id'] != run_id:
        raise ServiceError('Attempt does not belong to this run', 409)
    return run


def _touch(store, run_id, worker_id, attempt_id):
    """Only heartbeat columns change; do not repeatedly deserialize large state."""
    with transaction(store.db_path) as connection:
        stamp = time.time()
        changed = connection.execute("UPDATE runs SET heartbeat=? WHERE id=? AND worker_id=? AND attempt_id=? AND status IN ('running','cancelling')",
                                     (stamp, run_id, worker_id, attempt_id)).rowcount
        if not changed:
            return False
        connection.execute('INSERT INTO workers VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen',
                           (worker_id, stamp))
    return True


@contextmanager
def heartbeat_scope(store, run_id, worker_id, attempt_id):
    """Keep supervision alive during blocking input I/O and output verification."""
    key = (id(store), run_id, worker_id, attempt_id)
    active = getattr(_SCOPES, 'active', set())
    if key in active:
        yield
        return
    stop = threading.Event()
    def pulse():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                if not _touch(store, run_id, worker_id, attempt_id):
                    return
            except Exception:
                # A busy/unavailable database does not authorize another worker;
                # the inherited process lock remains the execution authority.
                LOG.exception('heartbeat_failed run=%s attempt=%s', run_id, attempt_id)
    _touch(store, run_id, worker_id, attempt_id)
    _SCOPES.active = active | {key}
    thread = threading.Thread(target=pulse, name='attempt-heartbeat', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # SQLite's existing busy timeout is 15 seconds. Joining prevents a late
        # heartbeat from outliving a finished attempt or a closed test workspace.
        thread.join(16)
        _SCOPES.active = active


def record_phase(store, run_id, worker_id, attempt_id, phase):
    from .service import ServiceError
    if phase not in PHASES:
        raise ServiceError('Unknown execution phase', 422)
    with transaction(store.db_path) as connection:
        _owned(store, connection, run_id, worker_id, attempt_id)
        previous = connection.execute("SELECT payload FROM events WHERE run_id=? AND attempt_id=? AND kind='execution_phase' ORDER BY id DESC LIMIT 1",
                                      (run_id, attempt_id)).fetchone()
        if previous is None or json.loads(previous['payload']).get('phase') != phase:
            store._event(connection, run_id, 'execution_phase', {'phase': phase}, attempt_id)
        stamp = time.time()
        connection.execute('INSERT INTO workers VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen',
                           (worker_id, stamp))
        connection.execute('UPDATE runs SET heartbeat=? WHERE id=?', (stamp, run_id))


class _Cancelled(Exception):
    pass


def _guard(store, run_id, worker_id, attempt_id):
    from .service import ServiceError
    run = store._fetch('runs', run_id)
    if run['worker_id'] != worker_id or run['attempt_id'] != attempt_id or run['status'] not in {'running', 'cancelling'}:
        raise ServiceError('Worker no longer owns this active attempt', 409)
    if run['status'] == 'cancelling':
        raise _Cancelled('Cancellation requested')
    return run


def _preparation_failed(store, run_id, worker_id, attempt_id, error, *, cancelled=False):
    from .service import now
    with transaction(store.db_path) as connection:
        run = _owned(store, connection, run_id, worker_id, attempt_id)
        status = 'cancelled' if cancelled or run['status'] == 'cancelling' else 'failed'
        error, finished = store._safe_error(error), now()
        connection.execute('UPDATE attempts SET status=?,finished_at=?,error=? WHERE id=?', (status, finished, error, attempt_id))
        connection.execute('UPDATE runs SET status=?,finished_at=?,error=?,state=NULL,verification=NULL WHERE id=?',
                           (status, finished, error, run_id))
        store._event(connection, run_id, 'materialization_failed' if status == 'failed' else 'cancelled',
                     {'error': error, 'phase': 'preparing'}, attempt_id)


def claim(store, worker_id):
    from .service import loads, now, uid
    with transaction(store.db_path) as connection:
        row = connection.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return None
        run = dict(row)
        revision = store._one(connection, 'revisions', run['revision_id'])
        research = store._one(connection, 'researches', run['research_id'])
        paper = store._one(connection, 'papers', research['paper_id'])
        dataset = store._one(connection, 'datasets', research['dataset_id'])
        from .research_guard import assert_revision_safe, record_event
        from .service import ServiceError
        try:
            assert_revision_safe(connection, dataset, revision)
        except ServiceError as exc:
            # A pre-migration queued revision can contain a now-visible legacy
            # reservation conflict. Refuse execution before creating any
            # attempt or copying inputs, preserving its original identity.
            connection.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE id=?",
                               (now(), store._safe_error(exc), run['id']))
            store._event(connection, run['id'], 'temporal_guard_blocked',
                         {'error': store._safe_error(exc), 'revision_id': revision['id']})
            return None
        attempt_id, started = uid(), now()
        folder = store.root / 'runs' / run['id'] / 'attempts' / attempt_id
        output = folder / 'output'
        number = run['attempt_count'] + 1
        # Persist ownership before even mkdir: every partial directory belongs
        # to an attempt visible after process death and SQLite rollback.
        connection.execute("INSERT INTO attempts(id,run_id,number,worker_id,status,started_at,output_dir) VALUES (?,?,?,?,'running',?,?)",
                           (attempt_id, run['id'], number, worker_id, started, str(output)))
        connection.execute("UPDATE runs SET status='running',attempt_count=?,worker_id=?,attempt_id=?,heartbeat=?,started_at=?,finished_at=NULL,error=NULL,verification=NULL,state=NULL WHERE id=?",
                           (number, worker_id, attempt_id, time.time(), started, run['id']))
        store._event(connection, run['id'], 'started', {'attempt_number': number}, attempt_id)
        record_event(connection, revision['id'], run['id'], 'started')
        store._event(connection, run['id'], 'execution_phase', {'phase': 'preparing'}, attempt_id)
    with heartbeat_scope(store, run['id'], worker_id, attempt_id):
        try:
            _guard(store, run['id'], worker_id, attempt_id)
            folder.mkdir(parents=True, exist_ok=False)
            if sha256(paper['pdf_path']) != paper['sha256']:
                raise ValueError('Stored PDF checksum mismatch')
            _guard(store, run['id'], worker_id, attempt_id)
            metadata = loads(dataset['metadata'])
            if digest({'data': sha256(dataset['data_path']), 'metadata': read_json(dataset['metadata_path'])}) != dataset['sha256']:
                raise ValueError('Stored dataset checksum mismatch')
            if read_json(dataset['metadata_path']) != metadata:
                raise ValueError('Stored dataset metadata differs from registered version')
            _guard(store, run['id'], worker_id, attempt_id)
            shutil.copyfile(paper['pdf_path'], folder / 'paper.pdf')
            atomic_json(folder / 'paper.json', loads(paper['document']))
            _guard(store, run['id'], worker_id, attempt_id)
            shutil.copyfile(dataset['data_path'], folder / 'market.csv')
            atomic_json(folder / 'metadata.json', metadata)
            # Verify copied bytes as well as source bytes: an external source
            # edit during copying must not become an executable frozen input.
            if (sha256(folder / 'paper.pdf') != paper['sha256']
                    or digest({'data': sha256(folder / 'market.csv'), 'metadata': metadata}) != dataset['sha256']):
                raise ValueError('Copied research inputs differ from registered versions')
            _guard(store, run['id'], worker_id, attempt_id)
            raw = {**loads(revision['task']), 'schema_version': 1, 'title': research['title'],
                   'paper': 'paper.json', 'paper_pdf': 'paper.pdf', 'data': 'market.csv', 'data_metadata': 'metadata.json'}
            atomic_json(folder / 'task.json', raw)
            run = _guard(store, run['id'], worker_id, attempt_id)
        except _Cancelled as exc:
            _preparation_failed(store, run['id'], worker_id, attempt_id, str(exc), cancelled=True)
            return None
        except (OSError, ValueError) as exc:
            # A changed owner is rejected, never published as a new worker's
            # failure; _preparation_failed also fences the exact attempt.
            _preparation_failed(store, run['id'], worker_id, attempt_id, str(exc))
            return None
    return {**store._run_public(run), 'attempt_id': attempt_id,
            'task_path': str(folder / 'task.json'), 'output_dir': str(output)}


def _export(path, content):
    """Publish one complete export file; a killed publisher may leave a temp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.pending', dir=path.parent)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _stamp(path):
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError('Publication inventory contains a symlink or unsupported file')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _inventory(base):
    """Bounded stat fence around hashing; excludes atime changed by our reads."""
    base = Path(base).absolute()
    if base.resolve() != base or not base.is_dir():
        raise ValueError('Publication inventory directory is missing or unsafe')
    result, total = {}, 0
    for path in chain((base,), base.rglob('*')):
        stamp = _stamp(path)
        if stat.S_ISREG(stamp[2]):
            total += stamp[4]
        if len(result) >= 4096 or total > 512 * 1024 * 1024:
            raise ValueError('Publication inventory exceeds the 4096-entry/512-MiB bound')
        result[str(path.relative_to(base))] = stamp
    return result


def _check_publication_files(output, source_files, export_files):
    if _inventory(output) != source_files:
        raise ValueError('Verified output source changed before publication')
    if _inventory(output.parent / 'exports') != export_files:
        raise ValueError('Verified export files changed before publication')


def finish(store, run_id, worker_id, attempt_id, status, error=None, verification=None, timings=None):
    from .service import REPO, ServiceError, TERMINAL, now, uid
    started = time.monotonic()
    if status not in TERMINAL:
        raise ServiceError('Invalid terminal worker state', 422)
    # Validate before phase or file writes, even for cancelled attempts.
    with transaction(store.db_path) as connection:
        run = _owned(store, connection, run_id, worker_id, attempt_id)
    output = store.root / 'runs' / run_id / 'attempts' / attempt_id / 'output'
    state, checked, registered = None, None, []
    source_files, export_files = None, None
    with heartbeat_scope(store, run_id, worker_id, attempt_id):
        if status in {'completed', 'failed'} and output.is_dir() and run['status'] != 'cancelling':
            try:
                record_phase(store, run_id, worker_id, attempt_id, 'verifying')
                source_files = _inventory(output)
                for path in output.rglob('*'):
                    if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
                        raise ValueError('Attempt contains unsafe artifact path')
                manifest, candidate_state = read_json(output / 'manifest.json'), read_json(output / 'state.json')
                referenced = list(manifest.get('snapshot_files', {})) + list(candidate_state.get('tool_artifacts', {}))
                for candidate in candidate_state.get('candidates', []):
                    referenced.extend(candidate.get('artifacts', {}))
                if any(Path(name).is_absolute() or '..' in Path(name).parts for name in referenced):
                    raise ValueError('Attempt has invalid artifact references')
                checked = verify_run(output)
                store._bind_output(run, candidate_state, manifest, output)
                state = candidate_state
                if status == 'completed' and checked['status'] != 'completed':
                    raise ValueError('Engine did not finish successfully')
                _guard(store, run_id, worker_id, attempt_id)
                record_phase(store, run_id, worker_id, attempt_id, 'publishing')
                saved_exports = {}
                for path in sorted(output.rglob('*')):
                    relative = path.relative_to(output)
                    if not path.is_file() or not (str(relative) in {'state.json', 'report.md', 'manifest.json', 'events.jsonl'} or relative.parts[0] in {'inputs', 'candidates'}):
                        continue
                    _guard(store, run_id, worker_id, attempt_id)
                    exported = output.parent / 'exports' / relative
                    store._safe_artifact_path(exported, output.parent / 'exports')
                    content = path.read_bytes()
                    if path.suffix in {'.json', '.jsonl', '.md', '.txt'}:
                        content = content.replace(str(store.root).encode(), b'[workbench]').replace(str(REPO).encode(), b'[project]')
                    _export(exported, content)
                    expected = hashlib.sha256(content).hexdigest()
                    saved = _stamp(exported)
                    if sha256(exported) != expected or _stamp(exported) != saved:
                        raise ValueError('Export differs from its verified source projection')
                    saved_exports[str(relative)] = saved
                    registered.append((uid(), run_id, attempt_id, str(relative), str(exported), saved[4], expected))
                export_files = _inventory(output.parent / 'exports')
                actual_exports = {name: stamp for name, stamp in export_files.items() if stat.S_ISREG(stamp[2])}
                if actual_exports != saved_exports:
                    raise ValueError('Export inventory changed during publication preparation')
                _check_publication_files(output, source_files, export_files)
            except _Cancelled:
                status, error, state, checked, registered = 'cancelled', 'Cancellation requested', None, None, []
            except (OSError, ValueError, KeyError, TypeError) as exc:
                status, error, state, checked, registered = 'failed', 'Output verification failed: ' + store._safe_error(exc), None, None, []
        elif status == 'completed' and run['status'] != 'cancelling':
            status, error = 'failed', 'Completed attempt has no verifiable output'
        with transaction(store.db_path) as connection:
            run = _owned(store, connection, run_id, worker_id, attempt_id)
            if status == 'completed':
                from .research_jobs import execution_remaining_seconds
                try:
                    remaining = execution_remaining_seconds(store, run_id)
                    deadline_error = ('Research job wall time budget exhausted before publication'
                                      if remaining is not None and remaining <= 0 else None)
                except ServiceError as exc:
                    deadline_error = 'Research job budget integrity failed: ' + store._safe_error(exc)
                if deadline_error:
                    status, error, state, checked, registered = ('failed',
                        deadline_error, None, None, [])
            if checked:
                try:
                    # Cryptographic checks ran without a writer lock. Stat and
                    # inventory equality fence ordinary file edits until commit;
                    # later external edits remain detectable by normal reads.
                    _check_publication_files(output, source_files, export_files)
                except (OSError, ValueError) as exc:
                    status, error, state, checked, registered = ('failed',
                        'Output publication failed: ' + store._safe_error(exc), None, None, [])
            if run['status'] == 'cancelling':
                status, error, state, checked, registered = 'cancelled', 'Cancellation requested', None, None, []
            finished = now()
            error = store._safe_error(error)
            connection.execute('UPDATE runs SET status=?,finished_at=?,error=?,verification=?,state=? WHERE id=?',
                               (status, finished, error, json_text(checked) if checked else None, json_text(state) if state else None, run_id))
            connection.execute('UPDATE attempts SET status=?,finished_at=?,error=?,state_digest=?,verification=? WHERE id=?',
                               (status, finished, error, digest(state) if state else None, json_text(checked) if checked else None, attempt_id))
            connection.executemany('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)', registered)
            if state:
                store._engine_events(connection, run_id, attempt_id, state)
            if timings is not None:
                store._event(connection, run_id, 'attempt_timing', timings, attempt_id)
            store._event(connection, run_id, status, {'error': error, 'verified': bool(checked),
                'output_verification_seconds': time.monotonic() - started}, attempt_id)
    return store.get_run(run_id)
