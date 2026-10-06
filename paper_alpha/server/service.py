"""Versioned research, fenced worker transitions and explicit review feedback."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from itertools import chain
from datetime import datetime, timezone
import hashlib
import fcntl
import json
from pathlib import Path
import shutil
import stat
import time
import uuid

from pypdf import PdfReader

from .db import connect, initialize, transaction
from .maintenance import workspace_lease
from . import mutations
from .research_assessments import assessment_summary, validate_assessment
from .regression import compare_contract, freeze_contract, recorded_semantics, verify_candidate
from ..contracts import Task
from ..evidence import ingest_pdf, sha256
from ..evaluation import validate_config
from ..storage import atomic_json, digest, json_text, read_json
from ..workflow import verify_run

REPO = Path(__file__).resolve().parents[2]
TASK_KEYS = {'evidence', 'hypotheses', 'candidates', 'evaluation', 'budget'}
TERMINAL = {'completed', 'failed', 'interrupted', 'cancelled'}
MAX_ATTEMPTS = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


def loads(value):
    return json.loads(value) if value is not None else None


class ServiceError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.db_path = self.root / 'workbench.sqlite3'
        with workspace_lease(self.root):
            if (self.root / '.restore-incomplete').exists():
                raise RuntimeError('Workspace restore is incomplete; restore into a new directory')
            initialize(self.db_path)
            previous = self._read("SELECT value FROM settings WHERE key='historical_workspace_roots'")
            roots = loads(previous[0]['value']) if previous else []
            self._redacted_roots = sorted({str(self.root), *roots}, key=len, reverse=True)
            self.example_dataset_id = self._seed_dataset()

    def _read(self, sql, values=()):
        connection = connect(self.db_path)
        try:
            return [dict(row) for row in connection.execute(sql, values).fetchall()]
        finally:
            connection.close()

    @staticmethod
    def _one(connection, table, identity):
        # Table names are constants exclusively supplied by our own methods.
        row = connection.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ServiceError(f'{table} item not found', 404)
        return dict(row)

    def _fetch(self, table, identity):
        connection = connect(self.db_path)
        try:
            return self._one(connection, table, identity)
        finally:
            connection.close()

    def _safe_error(self, error):
        if error is None:
            return None
        return self._public(str(error))[:2000]

    @staticmethod
    def _event(connection, run_id, kind, payload=None, attempt_id=None):
        connection.execute('INSERT INTO events(run_id,attempt_id,kind,created_at,payload) VALUES (?,?,?,?,?)',
                           (run_id, attempt_id, kind, now(), json_text(payload or {})))

    def _seed_dataset(self):
        fixture = REPO / 'examples/alpha101'
        metadata = read_json(fixture / 'metadata.json')
        fingerprint = digest({'data': sha256(fixture / 'market.csv'), 'metadata': metadata})
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'paper-alpha:dataset:' + fingerprint))
        folder = self.root / 'datasets' / identity
        with transaction(self.db_path) as connection:
            existing = connection.execute('SELECT id FROM datasets WHERE id=?', (identity,)).fetchone()
            if not existing:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(fixture / 'market.csv', folder / 'market.csv')
                atomic_json(folder / 'metadata.json', metadata)
                connection.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?)',
                    (identity, 'Synthetic OHLCV demonstration', fingerprint, json_text(metadata),
                     str(folder / 'market.csv'), str(folder / 'metadata.json')))
        return identity

    def list_datasets(self):
        return [{'id': r['id'], 'title': r['title'], 'sha256': r['sha256'],
                 'version': loads(r['metadata'])['version'], 'data_kind': loads(r['metadata'])['data_kind'],
                 'fields': ['open', 'high', 'low', 'close', 'volume', 'returns'],
                 'metadata': loads(r['metadata'])} for r in self._read('SELECT * FROM datasets ORDER BY id')]

    def get_dataset(self, dataset_id):
        self._fetch('datasets', dataset_id)
        result = next(item for item in self.list_datasets() if item['id'] == dataset_id)
        sources = self._read("SELECT id,input_digest,latest_validation_attempt_id FROM dataset_imports WHERE registered_dataset_id=? AND status='registered' ORDER BY created_at", (dataset_id,))
        return self._public({**result, 'registration': {'kind': 'validated_import' if sources else 'legacy_registration',
                                                       'imports': sources}})

    def add_paper(self, content: bytes, title: str):
        if not title.strip() or len(title) > 200:
            raise ServiceError('Paper title must contain 1..200 characters', 422)
        if not 0 < len(content) <= 16 * 1024 * 1024 or not content.startswith(b'%PDF-'):
            raise ServiceError('Upload must be a PDF no larger than 16 MiB', 422)
        fingerprint = hashlib.sha256(content).hexdigest()
        identity = uid()
        folder = self.root / 'papers' / identity
        folder.mkdir(parents=True)
        pdf = folder / 'paper.pdf'
        pdf.write_bytes(content)
        try:
            reader = PdfReader(pdf)
            if reader.is_encrypted or not 0 < len(reader.pages) <= 500:
                raise ValueError('PDF must be unencrypted and contain 1..500 pages')
            document = ingest_pdf(pdf, identity, title.strip(), 'uploaded-local-document', fingerprint)
            if sum(len(page['text']) for page in document['pages']) > 2_000_000:
                raise ValueError('Extracted PDF text exceeds 2 million characters')
            with transaction(self.db_path) as connection:
                existing = connection.execute('SELECT id FROM papers WHERE sha256=?', (fingerprint,)).fetchone()
                if existing:
                    shutil.rmtree(folder)
                    return self.get_paper(existing['id'])
                connection.execute('INSERT INTO papers VALUES (?,?,?,?,?,?)',
                    (identity, title.strip(), fingerprint, now(), json_text(document), str(pdf)))
            return self.get_paper(identity)
        except Exception as exc:
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('PDF cannot be ingested: ' + self._safe_error(exc), 422) from exc

    def list_papers(self):
        return self._read('SELECT id,title,sha256,created_at FROM papers ORDER BY created_at DESC')

    def get_paper(self, paper_id):
        row = self._fetch('papers', paper_id)
        document = loads(row.pop('document'))
        row.pop('pdf_path')
        return {**row, 'pages': document['pages'], 'extraction': document['extraction']}

    def paper_path(self, paper_id):
        row = self._fetch('papers', paper_id)
        path = Path(row['pdf_path'])
        if not path.is_file() or sha256(path) != row['sha256']:
            raise ServiceError('Stored paper digest mismatch', 409)
        return path

    def _task(self, task, title, dataset_id):
        if not isinstance(task, dict) or set(task) - TASK_KEYS - {'schema_version', 'title'}:
            raise ServiceError('Task accepts evidence, hypotheses, candidates, evaluation and budget only', 422)
        if 'schema_version' in task and task['schema_version'] != 1:
            raise ServiceError('Unsupported task schema', 422)
        clean = {key: deepcopy(value) for key, value in task.items() if key in TASK_KEYS}
        if len(json_text(clean).encode()) > 1024 * 1024:
            raise ServiceError('Task exceeds 1 MiB', 413)
        raw = {**clean, 'schema_version': 1, 'title': title, 'paper': 'paper.json',
               'paper_pdf': 'paper.pdf', 'data': 'market.csv', 'data_metadata': 'metadata.json'}
        try:
            parsed = Task.parse(raw)
            if len(parsed.candidates) > parsed.budget.max_candidates:
                raise ValueError('Candidate count exceeds max_candidates budget')
            if any(len(c.expression) > 2048 for c in parsed.candidates):
                raise ValueError('Expression exceeds 2048 characters')
            metadata = loads(self._fetch('datasets', dataset_id)['metadata'])
            clean['evaluation'] = validate_config(clean['evaluation'], metadata['calendar_dates'])
            assets = len(metadata['universe'])
            if assets < 3 or clean['evaluation']['min_assets'] > assets:
                raise ValueError('Research evaluation requires at least three assets and min_assets within this dataset universe')
            dates, bounds = metadata['calendar_dates'], clean['evaluation']['splits']['validation']
            if (dates.index(bounds['end']) - dates.index(bounds['start']) - 1) * assets > 100_000:
                raise ValueError('Research validation exceeds the 100,000 evaluation-cell limit')
            clean['budget'] = parsed.to_dict()['budget']
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Invalid task: ' + str(exc), 422) from exc
        return clean

    @staticmethod
    def _revision(row):
        result = dict(row)
        result['task'] = loads(result['task'])
        return result

    def create_research(self, title, paper_id, dataset_id, task, idempotency_key=None):
        key = self._mutation_key(idempotency_key)
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ServiceError('Research title must contain 1..200 characters', 422)
        operation = 'research.create'
        fingerprint = mutations.request_digest(operation, {
            'title': title, 'paper_id': paper_id, 'dataset_id': dataset_id, 'task': task})
        with transaction(self.db_path) as connection:
            replay = self._mutation_replay(connection, operation, key, fingerprint)
            if replay is not None:
                return replay
            self._one(connection, 'papers', paper_id)
            task = self._task(task, title, dataset_id)
            identity, revision_id, created = uid(), uid(), now()
            connection.execute('INSERT INTO researches VALUES (?,?,?,?,?,?)',
                               (identity, title.strip(), paper_id, dataset_id, created, revision_id))
            connection.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?,?)',
                               (revision_id, identity, 1, json_text(task), 'Initial revision', digest(task), created))
            from .research_guard import register_revision
            register_revision(connection, self._one(connection, 'datasets', dataset_id),
                              self._one(connection, 'revisions', revision_id))
            # Construct the same public detail inside the write transaction, so
            # a concurrent revision cannot change the creation acknowledgement.
            response = self._research_detail(connection, identity)
            mutations.record(connection, operation, key, fingerprint, response)
        return response

    def list_researches(self):
        return self._read('SELECT * FROM researches ORDER BY created_at DESC')

    def get_research(self, research_id):
        with closing(connect(self.db_path)) as connection:
            return self._research_detail(connection, research_id)

    def _research_detail(self, connection, research_id):
        row = self._one(connection, 'researches', research_id)
        row['revisions'] = [self._revision(r) for r in connection.execute(
            'SELECT * FROM revisions WHERE research_id=? ORDER BY number', (research_id,))]
        from .preflight import inspect_task
        metadata = loads(self._one(connection, 'datasets', row['dataset_id'])['metadata'])
        row['preflight'] = inspect_task(row['revisions'][-1]['task'], metadata)
        return row

    def create_revision(self, research_id, base_revision_id, task, note='', idempotency_key=None):
        key = self._mutation_key(idempotency_key)
        fingerprint = mutations.request_digest('revision.create', {
            'research_id': research_id, 'base_revision_id': base_revision_id, 'task': task, 'note': note})
        with closing(connect(self.db_path)) as connection:
            replay = self._mutation_replay(connection, 'revision.create', key, fingerprint)
            if replay is not None:
                return replay
        research = self._fetch('researches', research_id)
        clean = self._task(task, research['title'], research['dataset_id'])
        if not isinstance(note, str) or len(note) > 4000:
            raise ServiceError('Revision note exceeds limit', 422)
        with transaction(self.db_path) as connection:
            replay = self._mutation_replay(connection, 'revision.create', key, fingerprint)
            if replay is not None:
                return replay
            research = self._one(connection, 'researches', research_id)
            if research['latest_revision_id'] != base_revision_id:
                raise ServiceError('Research changed; reload the latest revision', 409)
            previous = self._one(connection, 'revisions', base_revision_id)
            revision = {'id': uid(), 'research_id': research_id, 'number': previous['number'] + 1,
                        'task': json_text(clean), 'note': note, 'digest': digest(clean), 'created_at': now()}
            connection.execute('INSERT INTO revisions VALUES (:id,:research_id,:number,:task,:note,:digest,:created_at)', revision)
            from .research_guard import register_revision
            register_revision(connection, self._one(connection, 'datasets', research['dataset_id']), revision)
            connection.execute('UPDATE researches SET latest_revision_id=? WHERE id=?', (revision['id'], research_id))
            result = self._revision(revision)
            mutations.record(connection, 'revision.create', key, fingerprint, result)
        return result

    def seed_example(self):
        with (self.root / '.example-import.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._seed_example_locked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _seed_example_locked(self):
        existing = self._read("SELECT value FROM settings WHERE key='alpha101_research'")
        if existing:
            research = self.get_research(existing[0]['value'])
            return {'research_id': research['id'], 'revision_id': research['revisions'][0]['id']}
        paper = self.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), '101 Formulaic Alphas')
        task = read_json(REPO / 'examples/alpha101/task.json')
        research = self.create_research(task['title'], paper['id'], self.example_dataset_id,
                                        {key: task[key] for key in TASK_KEYS})
        with transaction(self.db_path) as connection:
            connection.execute("INSERT OR IGNORE INTO settings VALUES ('alpha101_research',?)", (research['id'],))
            identity = connection.execute("SELECT value FROM settings WHERE key='alpha101_research'").fetchone()[0]
        research = self.get_research(identity)
        return {'research_id': identity, 'revision_id': research['revisions'][0]['id']}

    @staticmethod
    def _run_public(row):
        return {key: row[key] for key in ('id', 'revision_id', 'research_id', 'status', 'mode',
                'created_at', 'started_at', 'finished_at', 'error', 'attempt_count')}

    def submit_run(self, revision_id, mode='agent', idempotency_key=None):
        if mode not in {'agent', 'fixed', 'normalized_fixed'}:
            raise ServiceError('Unknown execution mode', 422)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ServiceError('Provide a nonempty idempotency key up to 128 characters', 422)
        request_digest = digest({'revision_id': revision_id, 'mode': mode})
        with transaction(self.db_path) as connection:
            existing = connection.execute('SELECT * FROM runs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                if existing['request_digest'] != request_digest:
                    raise ServiceError('Idempotency key was already used for a different request', 409)
                return self._run_public(dict(existing))
            revision = self._one(connection, 'revisions', revision_id)
            research = self._one(connection, 'researches', revision['research_id'])
            from .research_guard import assert_revision_safe, record_event
            assert_revision_safe(connection, self._one(connection, 'datasets', research['dataset_id']), revision)
            identity = uid()
            connection.execute('INSERT INTO runs(id,revision_id,research_id,mode,status,created_at,idempotency_key,request_digest) VALUES (?,?,?,?,?,?,?,?)',
                (identity, revision_id, revision['research_id'], mode, 'queued', now(), idempotency_key, request_digest))
            self._event(connection, identity, 'queued', {'revision_id': revision_id, 'mode': mode})
            record_event(connection, revision_id, identity, 'submitted')
            result = self._one(connection, 'runs', identity)
        return self._run_public(result)

    def list_runs(self):
        return [self._run_public(row) for row in self._read('SELECT * FROM runs ORDER BY created_at DESC')]

    def _status_snapshot(self, connection, row):
        result = self._run_public(row)
        run_id = row['id']
        latest = connection.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
        reviews = list(connection.execute('SELECT COUNT(*),MAX(rowid) FROM reviews WHERE run_id=?', (run_id,)).fetchone())
        artifacts = connection.execute('SELECT COUNT(*),MAX(rowid) FROM artifacts WHERE run_id=?', (run_id,)).fetchone()
        phase = connection.execute("SELECT payload FROM events WHERE run_id=? AND attempt_id=? AND kind='execution_phase' ORDER BY id DESC LIMIT 1",
                                   (run_id, row['attempt_id'])).fetchone()
        phase = loads(phase['payload']).get('phase') if phase and row['status'] in {'running', 'cancelling'} else None
        token = digest({'run': result, 'attempt_id': row['attempt_id'], 'event': latest,
                        'reviews': reviews, 'artifacts': list(artifacts)})
        return {**result, 'attempt_id': row['attempt_id'], 'phase': phase,
                'change_token': token, 'integrity_checked': False}

    def get_run_status(self, run_id):
        # No saved engine state or filesystem reads on this unverified probe.
        with closing(connect(self.db_path)) as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT id,revision_id,research_id,status,mode,created_at,started_at,finished_at,error,attempt_count,attempt_id FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ServiceError('runs item not found', 404)
            return self._public(self._status_snapshot(connection, dict(row)))

    def get_run(self, run_id):
        # Bind detail and its polling token to one consistent WAL read snapshot.
        with closing(connect(self.db_path)) as connection:
            connection.execute('BEGIN')
            row = self._one(connection, 'runs', run_id)
            result = self._run_public(row)
            original = loads(row['state'])
            state = deepcopy(original)
            if state:
                state['task'] = {key: value for key, value in state.get('task', {}).items()
                                 if key not in {'paper', 'paper_pdf', 'data', 'data_metadata'}}
                for candidate in state.get('candidates', []):
                    candidate.get('result', {}).pop('daily', None)
            verification = loads(row['verification'])
            if verification and verification.get('verified'):
                try:
                    self._check_integrity(connection, row)
                except ServiceError as exc:
                    verification = {'verified': False, 'error': str(exc)}
            verified = state and verification and verification.get('verified') and row['status'] in {'completed', 'failed'}
            result.update(state=state, verification=verification,
                reviews=[self._review_public(dict(item)) for item in connection.execute('SELECT * FROM reviews WHERE run_id=? ORDER BY created_at', (run_id,))],
                review_targets=[{'candidate_id': candidate['id'], 'attempt_id': row['attempt_id'],
                    'result_digest': digest(candidate)} for candidate in original['candidates']] if verified else [],
                regression_target={'attempt_id': row['attempt_id'], 'result_digest': digest(original)} if verified else None,
                status_token=self._status_snapshot(connection, row)['change_token'],
                attempts=[dict(item) for item in connection.execute('SELECT id,number,worker_id,status,started_at,finished_at,error,state_digest FROM attempts WHERE run_id=? ORDER BY number', (run_id,))])
        return self._public(result)

    def _public(self, value):
        if isinstance(value, dict):
            return {key: self._public(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._public(item) for item in value]
        if isinstance(value, str):
            for root in self._redacted_roots:
                value = value.replace(root, '[workbench]')
            return value.replace(str(REPO), '[project]')
        return value

    def events(self, run_id, after=0):
        self._fetch('runs', run_id)
        return self._public([{**r, 'sequence': r['id'], 'payload': loads(r['payload'])} for r in self._read(
            'SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 1000', (run_id, after))])

    def event_page(self, run_id, after=0, limit=200, through=None):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ServiceError('Use a nonnegative event cursor and a page size of 1..1000', 422)
        if through is not None and (type(through) is not int or through < 0):
            raise ServiceError('Event watermark must be nonnegative', 422)
        connection = connect(self.db_path)
        try:
            # One deferred read transaction binds metadata and rows to the same
            # WAL snapshot without holding the writer lock during a page read.
            connection.execute('BEGIN')
            self._one(connection, 'runs', run_id)
            latest = connection.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
            watermark = latest if through is None else through
            if watermark > latest or after > watermark:
                raise ServiceError('Event cursor exceeds this run snapshot; restart synchronization', 409)
            rows = connection.execute('SELECT * FROM events WHERE run_id=? AND id>? AND id<=? ORDER BY id LIMIT ?',
                                      (run_id, after, watermark, limit + 1)).fetchall()
            total = connection.execute('SELECT COUNT(*) FROM events WHERE run_id=? AND id<=?', (run_id, watermark)).fetchone()[0]
            selected = rows[:limit]
            return self._public({'run_id': run_id,
                'items': [{**dict(r), 'sequence': r['id'], 'payload': loads(r['payload'])} for r in selected],
                'next_cursor': selected[-1]['id'] if selected else after,
                'has_more': len(rows) > limit, 'high_watermark': watermark, 'total_records': total})
        finally:
            connection.rollback()
            connection.close()

    def heartbeat(self, worker_id, run_id=None):
        engine_state = None
        attempt_id = None
        if run_id:
            run = self._fetch('runs', run_id)
            attempt_id = run['attempt_id']
            if attempt_id and run['worker_id'] == worker_id:
                attempt = self._fetch('attempts', attempt_id)
                path = Path(attempt['output_dir']) / 'state.json'
                if path.is_file():
                    try:
                        engine_state = read_json(path)
                    except (OSError, ValueError):
                        pass
        with transaction(self.db_path) as connection:
            connection.execute('INSERT INTO workers VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen',
                               (worker_id, time.time()))
            if run_id is not None:
                changed = connection.execute("UPDATE runs SET heartbeat=? WHERE id=? AND worker_id=? AND status IN ('running','cancelling')",
                                             (time.time(), run_id, worker_id)).rowcount
                if changed and engine_state is not None:
                    current = self._one(connection, 'runs', run_id)
                    if current['attempt_id'] == attempt_id:
                        connection.execute('UPDATE runs SET state=? WHERE id=?', (json_text(engine_state), run_id))
                        self._engine_events(connection, run_id, attempt_id, engine_state)

    def _engine_events(self, connection, run_id, attempt_id, state):
        cursor = connection.execute("SELECT MAX(json_extract(payload,'$.engine_sequence')) FROM events WHERE attempt_id=? AND kind='engine_event'", (attempt_id,)).fetchone()[0] or 0
        for event in state.get('events', []):
            if event.get('sequence', 0) > cursor:
                self._event(connection, run_id, 'engine_event', {'engine_sequence': event['sequence'], 'event': event}, attempt_id)

    def worker_health(self):
        rows = self._read('SELECT MAX(last_seen) AS last_seen FROM workers')
        stamp = rows[0]['last_seen']
        return {'online': stamp is not None and time.time() - stamp < 30,
                'last_seen': datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else None}

    def claim(self, worker_id):
        from .execution_lifecycle import claim
        return claim(self, worker_id)

    def cancel_requested(self, run_id):
        return self._fetch('runs', run_id)['status'] in {'cancelling', 'cancelled'}

    def cancel_run(self, run_id):
        with transaction(self.db_path) as connection:
            row = self._one(connection, 'runs', run_id)
            if row['status'] == 'queued':
                connection.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE id=?", (now(), run_id))
                self._event(connection, run_id, 'cancelled')
            elif row['status'] == 'running':
                connection.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run_id,))
                self._event(connection, run_id, 'cancel_requested', attempt_id=row['attempt_id'])
            elif row['status'] not in {'cancelling', 'cancelled'}:
                raise ServiceError('Only queued or running experiments can be cancelled', 409)
        return self.get_run(run_id)

    def retry_run(self, run_id):
        with transaction(self.db_path) as connection:
            row = self._one(connection, 'runs', run_id)
            if row['status'] not in {'failed', 'interrupted'}:
                raise ServiceError('Only failed or interrupted experiments can be retried', 409)
            if row['attempt_count'] >= MAX_ATTEMPTS:
                raise ServiceError('Experiment reached the three-attempt limit', 409)
            from .research_guard import assert_revision_safe, record_event
            revision = self._one(connection, 'revisions', row['revision_id'])
            research = self._one(connection, 'researches', row['research_id'])
            assert_revision_safe(connection, self._one(connection, 'datasets', research['dataset_id']), revision)
            connection.execute("UPDATE runs SET status='queued',worker_id=NULL,heartbeat=NULL,error=NULL,finished_at=NULL,state=NULL,verification=NULL WHERE id=?", (run_id,))
            self._event(connection, run_id, 'retry_queued', {'previous_attempt_id': row['attempt_id']})
            record_event(connection, row['revision_id'], run_id, 'retried')
        return self.get_run(run_id)

    def recover_stale(self, stale_seconds=30):
        recovered = []
        with transaction(self.db_path) as connection:
            rows = connection.execute("SELECT * FROM runs WHERE status IN ('running','cancelling') AND (heartbeat IS NULL OR heartbeat<?)", (time.time() - stale_seconds,)).fetchall()
            for row in rows:
                status = 'cancelled' if row['status'] == 'cancelling' else 'interrupted'
                error = 'Worker heartbeat expired; prior output retained. Explicit retry required.'
                finished = now()
                connection.execute('UPDATE runs SET status=?,error=?,finished_at=? WHERE id=?', (status, error, finished, row['id']))
                connection.execute('UPDATE attempts SET status=?,error=?,finished_at=? WHERE id=?', (status, error, finished, row['attempt_id']))
                self._event(connection, row['id'], 'worker_lost', {'status': status}, row['attempt_id'])
                recovered.append(row['id'])
        return recovered

    def _bind_output(self, run, state, manifest, output):
        revision = self._fetch('revisions', run['revision_id'])
        research = self._fetch('researches', run['research_id'])
        paper = self._fetch('papers', research['paper_id'])
        dataset = self._fetch('datasets', research['dataset_id'])
        expected_task = {**loads(revision['task']), 'schema_version': 1, 'title': research['title'],
                         'paper': 'paper.json', 'paper_pdf': 'paper.pdf',
                         'data': 'market.csv', 'data_metadata': 'metadata.json'}
        if state.get('task') != expected_task:
            raise ValueError('Executed task does not match the queued immutable revision')
        signature = manifest.get('signature', {})
        if state.get('mode') != run['mode'] or signature.get('mode') != run['mode']:
            raise ValueError('Executed policy does not match the queued mode')
        inputs = output / 'inputs'
        if sha256(inputs / 'paper.pdf') != paper['sha256'] or read_json(inputs / 'paper.json') != loads(paper['document']):
            raise ValueError('Executed paper does not match the registered document')
        metadata = read_json(inputs / 'metadata.json')
        market_hash = sha256(inputs / 'market.csv')
        if metadata != loads(dataset['metadata']) or digest({'data': market_hash, 'metadata': metadata}) != dataset['sha256']:
            raise ValueError('Executed data does not match the registered dataset version')
        expected_inputs = {key: sha256(inputs / name) for key, name in {
            'paper': 'paper.json', 'paper_pdf': 'paper.pdf', 'data': 'market.csv', 'data_metadata': 'metadata.json'}.items()}
        if signature.get('inputs') != expected_inputs or signature.get('task_sha256') != sha256(inputs / 'task.json'):
            raise ValueError('Manifest signature does not match the executed inputs')

    def finish(self, run_id, worker_id, attempt_id, status, error=None, verification=None, timings=None):
        from .execution_lifecycle import finish
        return finish(self, run_id, worker_id, attempt_id, status, error, verification, timings)

    def list_artifacts(self, run_id):
        self._fetch('runs', run_id)
        return self._read('SELECT id,attempt_id,name,size,sha256 FROM artifacts WHERE run_id=? ORDER BY attempt_id,name', (run_id,))

    def artifact_path(self, run_id, artifact_id):
        artifact = self._fetch('artifacts', artifact_id)
        if artifact['run_id'] != run_id:
            raise ServiceError('Artifact not found in this experiment', 404)
        path = Path(artifact['path'])
        attempt = self._fetch('attempts', artifact['attempt_id'])
        base = self.root / 'runs' / run_id / 'attempts' / attempt['id'] / 'exports'
        self._safe_artifact_path(path, base)
        if not path.is_file() or path.stat().st_size != artifact['size'] or sha256(path) != artifact['sha256']:
            raise ServiceError('Artifact was modified after verification', 409)
        return path, artifact['name']

    def report_path(self, run_id):
        run = self._fetch('runs', run_id)
        rows = self._read("SELECT id FROM artifacts WHERE run_id=? AND attempt_id=? AND name='report.md'", (run_id, run['attempt_id']))
        if not rows:
            raise ServiceError('No verified report is available for the latest attempt', 404)
        return self.artifact_path(run_id, rows[0]['id'])[0]

    @staticmethod
    def _safe_artifact_path(path, base):
        # Reject symlinks in parent directories too, not only the final file.
        if base.resolve() != base.absolute() or path.is_symlink() or not path.resolve().is_relative_to(base.absolute()):
            raise ServiceError('Artifact path is invalid', 409)

    def _check_integrity(self, connection, run):
        attempt = self._one(connection, 'attempts', run['attempt_id'])
        base = self.root / 'runs' / run['id'] / 'attempts' / attempt['id']
        output = base / 'output'
        artifacts = connection.execute('SELECT * FROM artifacts WHERE attempt_id=?', (attempt['id'],)).fetchall()
        if not artifacts:
            raise ServiceError('Verified artifact inventory is missing', 409)
        try:
            self._safe_artifact_path(output / 'state.json', output)
            state = read_json(output / 'state.json')
            if digest(state) != attempt['state_digest'] or digest(loads(run['state'])) != attempt['state_digest']:
                raise ServiceError('Engine result changed after verification', 409)
            for artifact in artifacts:
                path = Path(artifact['path'])
                self._safe_artifact_path(path, base / 'exports')
                if not path.is_file() or sha256(path) != artifact['sha256']:
                    raise ServiceError('Verified artifact was modified; review is blocked', 409)
            manifest_export = next((a for a in artifacts if a['name'] == 'manifest.json'), None)
            if not manifest_export or self._public(read_json(output / 'manifest.json')) != read_json(manifest_export['path']):
                raise ServiceError('Engine manifest changed after verification', 409)
            verify_run(output)
            self._bind_output(run, state, read_json(output / 'manifest.json'), output)
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Engine snapshot was modified; verification is blocked', 409) from exc

    def _verified_state(self, connection, run_id):
        run = self._one(connection, 'runs', run_id)
        if run['status'] not in {'completed', 'failed'} or not loads(run['verification']) or not loads(run['verification']).get('verified'):
            raise ServiceError('A terminal, verified experiment is required', 409)
        self._check_integrity(connection, run)
        return run, loads(run['state'])

    @staticmethod
    def _review_public(review):
        assessment = loads(review.get('assessment'))
        return {**review, 'assessment': assessment,
                'assessment_summary': assessment_summary(assessment, review['source'])}

    @staticmethod
    def _mutation_key(value):
        try:
            return mutations.validate_key(value)
        except ValueError as exc:
            raise ServiceError(str(exc), 422) from exc

    @staticmethod
    def _mutation_replay(connection, operation, key, fingerprint):
        try:
            return mutations.replay(connection, operation, key, fingerprint)
        except mutations.ReceiptError as exc:
            raise ServiceError(str(exc), 409) from exc

    def _review_rows(self, connection, run_id, attempt_id, review_id=None):
        """Small publication fence plus exact saved state; no result-file hashing."""
        run = self._one(connection, 'runs', run_id)
        revision = self._one(connection, 'revisions', run['revision_id'])
        research = self._one(connection, 'researches', run['research_id'])
        rows = {
            'run': {key: run[key] for key in ('id', 'revision_id', 'research_id', 'mode', 'status', 'attempt_id', 'state', 'verification')},
            'attempt': self._one(connection, 'attempts', attempt_id),
            'revision': revision,
            'research': {key: research[key] for key in ('id', 'paper_id', 'dataset_id')},
            'paper': self._one(connection, 'papers', research['paper_id']),
            'dataset': self._one(connection, 'datasets', research['dataset_id']),
            'artifacts': [dict(row) for row in connection.execute(
                'SELECT * FROM artifacts WHERE attempt_id=? ORDER BY id', (attempt_id,))],
        }
        if review_id:
            rows['review'] = self._one(connection, 'reviews', review_id)
        return rows

    def _review_files(self, run_id, attempt_id):
        """Detect ordinary local file changes around full cryptographic verification.

        Include inode/ctime, parent directories and the file inventory so replacing
        bytes and restoring mtime cannot pass. This is not hostile-filesystem or
        cryptographic attestation; external changes after publication remain
        detectable by the normal result/download checks.
        """
        base = self.root / 'runs' / run_id / 'attempts' / attempt_id
        self._safe_artifact_path(base, self.root / 'runs')
        stamps, total = {}, 0
        try:
            for path in chain((base,), base.rglob('*')):
                info = path.lstat()
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise ServiceError('Review source contains an unsupported file', 409)
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                if len(stamps) >= 4096 or total > 512 * 1024 * 1024:
                    raise ServiceError('Review source exceeds bounded verification inventory', 413)
                stamps[str(path.relative_to(base))] = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                                     info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except OSError as exc:
            raise ServiceError('Review source changed or became unavailable', 409) from exc
        return stamps

    def _assert_review_snapshot(self, connection, rows, files):
        run_id, attempt_id = rows['run']['id'], rows['attempt']['id']
        current = self._review_rows(connection, run_id, attempt_id, rows.get('review', {}).get('id'))
        if current != rows or self._review_files(run_id, attempt_id) != files:
            raise ServiceError('Review source changed during verification; refresh before submitting', 409)

    def create_review(self, run_id, candidate_id, verdict, category, note, source='legacy_unknown', assessment=None, idempotency_key=None,
                      expected_attempt_id=None, expected_result_digest=None):
        key = self._mutation_key(idempotency_key)
        if source not in {'human', 'automation', 'imported', 'legacy_unknown'}:
            raise ServiceError('Invalid declared review source', 422)
        if verdict not in {'accepted', 'needs_changes', 'rejected'} or category not in {'evidence', 'hypothesis', 'implementation', 'data', 'evaluation', 'other'}:
            raise ServiceError('Invalid review classification', 422)
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ServiceError('Provide a review note of 1..4000 characters', 422)
        if assessment is not None:
            try:
                assessment = validate_assessment(assessment)
            except ValueError as exc:
                raise ServiceError(str(exc), 422) from exc
        try:
            expected_target = mutations.validate_review_target(expected_attempt_id, expected_result_digest, assessment)
        except ValueError as exc:
            raise ServiceError(str(exc), 422) from exc
        operation = 'review.create'
        payload = {
            'run_id': run_id, 'candidate_id': candidate_id, 'verdict': verdict,
            'category': category, 'note': note, 'source': source, 'assessment': assessment}
        # Omitted and explicit-null target fields share the pre-target receipt
        # digest; existing callers and receipts keep their original meaning.
        if expected_target is not None:
            payload.update(expected_target)
        fingerprint = mutations.request_digest(operation, payload)
        with closing(connect(self.db_path)) as connection:
            connection.execute('BEGIN')
            replay = self._mutation_replay(connection, operation, key, fingerprint)
            if replay is not None:
                return replay
            initial = self._one(connection, 'runs', run_id)
            if initial['status'] not in {'completed', 'failed'} or not (loads(initial['verification']) or {}).get('verified'):
                raise ServiceError('A terminal, verified experiment is required', 409)
            rows = self._review_rows(connection, run_id, initial['attempt_id'])
            files = self._review_files(run_id, initial['attempt_id'])
            run, state = self._verified_state(connection, run_id)
            candidate = next((c for c in state['candidates'] if c['id'] == candidate_id), None)
            if candidate is None:
                raise ServiceError('Candidate does not belong to this experiment', 422)
            if expected_target is not None and (expected_attempt_id != run['attempt_id']
                                                or expected_result_digest != digest(candidate)):
                raise ServiceError('Review target changed; refresh the verified experiment before reviewing', 412)
            if assessment is not None and (assessment['expected_attempt_id'] != run['attempt_id']
                                           or assessment['expected_result_digest'] != digest(candidate)):
                raise ServiceError('Assessment target changed; refresh the verified experiment before reviewing', 409)
            review = {'id': uid(), 'run_id': run_id, 'revision_id': run['revision_id'], 'attempt_id': run['attempt_id'],
                      'candidate_id': candidate_id, 'verdict': verdict, 'category': category, 'note': note,
                      'result_digest': digest(candidate), 'created_at': now(), 'source': source,
                      'assessment': json_text(assessment) if assessment is not None else None}
        with transaction(self.db_path) as connection:
            replay = self._mutation_replay(connection, operation, key, fingerprint)
            if replay is not None:
                return replay
            self._assert_review_snapshot(connection, rows, files)
            connection.execute('INSERT INTO reviews(id,run_id,revision_id,attempt_id,candidate_id,verdict,category,note,result_digest,created_at,source,assessment) VALUES (:id,:run_id,:revision_id,:attempt_id,:candidate_id,:verdict,:category,:note,:result_digest,:created_at,:source,:assessment)', review)
            self._event(connection, run_id, 'review_recorded', {'review_id': review['id'], 'candidate_id': candidate_id}, run['attempt_id'])
            response = self._review_public(review)
            mutations.record(connection, operation, key, fingerprint, response)
        return response

    def approve_case(self, review_id, expected_status, note, idempotency_key=None):
        key = self._mutation_key(idempotency_key)
        if expected_status not in {'evaluated', 'blocked', 'failed', 'rejected', 'not_evaluable', 'budget_stopped'}:
            raise ServiceError('Invalid expected candidate status', 422)
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ServiceError('Provide the regression approval rationale', 422)
        operation = 'case.approve'
        fingerprint = mutations.request_digest(operation, {
            'review_id': review_id, 'expected_status': expected_status, 'note': note})
        with closing(connect(self.db_path)) as connection:
            connection.execute('BEGIN')
            replay = self._mutation_replay(connection, operation, key, fingerprint)
            if replay is not None:
                return replay
            review = self._one(connection, 'reviews', review_id)
            run = self._one(connection, 'runs', review['run_id'])
            research = self._one(connection, 'researches', run['research_id'])
            # A review binds a particular attempt. Retrying its run cannot
            # silently transfer that approval to the newest result.
            attempt = self._one(connection, 'attempts', review['attempt_id'])
            rows = self._review_rows(connection, run['id'], attempt['id'], review_id)
            files = self._review_files(run['id'], attempt['id'])
            if (attempt['run_id'] != run['id'] or review['revision_id'] != run['revision_id']
                    or not (loads(attempt['verification']) or {}).get('verified')):
                raise ServiceError('Reviewed attempt is not verified', 409)
            output = self.root / 'runs' / run['id'] / 'attempts' / attempt['id'] / 'output'
            try:
                source_state = read_json(output / 'state.json')
                historical_run = {**run, 'attempt_id': attempt['id'], 'state': json_text(source_state)}
                self._check_integrity(connection, historical_run)
                source_candidate = next(c for c in source_state['candidates'] if c['id'] == review['candidate_id'])
                if digest(source_candidate) != review['result_digest']:
                    raise ValueError('Reviewed candidate digest changed')
            except (OSError, ValueError, StopIteration) as exc:
                raise ServiceError('Reviewed attempt no longer matches its frozen result', 409) from exc
            contract = self._contract(connection, review['revision_id'], review['candidate_id'], output)
            case = {'id': uid(), 'review_id': review_id, 'research_id': research['id'], 'paper_id': research['paper_id'],
                    'dataset_id': research['dataset_id'], 'candidate_id': review['candidate_id'], 'expected_status': expected_status,
                    'note': note, 'created_at': now(), 'version': 2, 'approved': 1,
                    'contract': json_text(contract), 'contract_digest': digest(contract)}
        with transaction(self.db_path) as connection:
            replay = self._mutation_replay(connection, operation, key, fingerprint)
            if replay is not None:
                return replay
            self._assert_review_snapshot(connection, rows, files)
            connection.execute('INSERT INTO regression_cases(id,review_id,research_id,paper_id,dataset_id,candidate_id,expected_status,note,created_at,version,approved,contract,contract_digest) VALUES (:id,:review_id,:research_id,:paper_id,:dataset_id,:candidate_id,:expected_status,:note,:created_at,:version,:approved,:contract,:contract_digest)', case)
            response = {**case, 'approved': True, 'contract': contract}
            mutations.record(connection, operation, key, fingerprint, response)
        return response

    def _contract(self, connection, revision_id, candidate_id, output):
        revision = self._one(connection, 'revisions', revision_id)
        research = self._one(connection, 'researches', revision['research_id'])
        paper = self._one(connection, 'papers', research['paper_id'])
        dataset = self._one(connection, 'datasets', research['dataset_id'])
        try:
            contract = freeze_contract(loads(revision['task']), candidate_id, paper['sha256'],
                                       dataset['sha256'], loads(dataset['metadata']))
            # Interpret the immutable engine snapshot, never today's imported
            # constants when reviewing an earlier attempt.
            contract.update(recorded_semantics(Path(output)))
            return contract
        except (ValueError, KeyError, OSError) as exc:
            raise ServiceError('Cannot freeze candidate compatibility contract: ' + self._safe_error(exc), 422) from exc

    def list_cases(self):
        return [{**row, 'approved': bool(row['approved']), 'contract': loads(row['contract'])}
                for row in self._read('SELECT * FROM regression_cases ORDER BY created_at DESC')]

    def run_regression_check(self, run_id, case_ids, idempotency_key=None,
                             expected_attempt_id=None, expected_result_digest=None):
        key = self._mutation_key(idempotency_key)
        try:
            target = mutations.validate_review_target(expected_attempt_id, expected_result_digest)
        except ValueError as exc:
            raise ServiceError(str(exc), 422) from exc
        fingerprint = mutations.request_digest('regression.check', {
            'run_id': run_id, 'case_ids': case_ids, 'expected_attempt_id': expected_attempt_id,
            'expected_result_digest': expected_result_digest})
        if not isinstance(case_ids, list) or not 1 <= len(case_ids) <= 100 or len(set(case_ids)) != len(case_ids):
            raise ServiceError('Choose 1..100 unique regression cases', 422)
        started = time.monotonic()
        # WAL read snapshot permits queue/heartbeat writes during PDF extraction
        # and independent numerical recomputation. Publish only after rechecking
        # the exact result, case rows and immutable artifacts under the writer fence.
        with closing(connect(self.db_path)) as connection:
            connection.execute('BEGIN')
            replay = self._mutation_replay(connection, 'regression.check', key, fingerprint)
            if replay is not None:
                return replay
            initial = self._one(connection, 'runs', run_id)
            if initial['status'] not in {'completed', 'failed'} or not initial['attempt_id']:
                raise ServiceError('A verified terminal result is required', 409)
            rows = self._review_rows(connection, run_id, initial['attempt_id'])
            files = self._review_files(run_id, initial['attempt_id'])
            run, state = self._verified_state(connection, run_id)
            if target and (run['attempt_id'] != expected_attempt_id or digest(state) != expected_result_digest):
                raise ServiceError('Regression target changed; reload the exact result before submitting', 412)
            research = self._one(connection, 'researches', run['research_id'])
            candidates = {c['id']: c for c in state['candidates']}
            attempt = self._one(connection, 'attempts', run['attempt_id'])
            task = loads(self._one(connection, 'revisions', run['revision_id'])['task'])
            verified_candidates = {}
            results = []
            case_snapshot = {}
            for identity in case_ids:
                case = self._one(connection, 'regression_cases', identity)
                case_snapshot[identity] = case
                differences = []
                for binding_key in ('research_id', 'paper_id', 'dataset_id'):
                    expected = research['id'] if binding_key == 'research_id' else research[binding_key]
                    if case[binding_key] != expected:
                        differences.append(binding_key)
                if case['candidate_id'] not in candidates:
                    differences.append('candidate_id')
                if not case['contract']:
                    differences.append('legacy_case_requires_reapproval')
                elif digest(loads(case['contract'])) != case['contract_digest']:
                    raise ServiceError('Frozen regression contract digest does not match', 409)
                elif not differences:
                    try:
                        current_contract = self._contract(connection, run['revision_id'], case['candidate_id'], attempt['output_dir'])
                    except ServiceError:
                        differences.append('recorded_scientific_semantics_unavailable')
                    else:
                        differences.extend(compare_contract(loads(case['contract']), current_contract))
                compatible = not differences
                actual = candidates.get(case['candidate_id'], {}).get('status')
                checks = []
                if compatible:
                    checks.append({'name': 'candidate_status', 'outcome': 'passed' if actual == case['expected_status'] else 'failed',
                                   'reason': 'Observed terminal status compared with explicitly approved expectation',
                                   'expected': case['expected_status'], 'actual': actual})
                    if actual == 'evaluated':
                        if case['candidate_id'] not in verified_candidates:
                            verified_candidates[case['candidate_id']] = verify_candidate(Path(attempt['output_dir']), case['candidate_id'], task)
                        checks.extend(verified_candidates[case['candidate_id']]['checks'])
                outcome = ('not_comparable' if not compatible else 'failed' if any(c['outcome'] == 'failed' for c in checks)
                           else 'not_comparable' if any(c['outcome'] == 'not_comparable' for c in checks) else 'passed')
                results.append({'case_id': identity, 'candidate_id': case['candidate_id'], 'expected_status': case['expected_status'],
                                'actual_status': actual, 'compatible': compatible, 'passed': outcome == 'passed', 'outcome': outcome,
                                'differences': differences, 'checks': checks,
                                'reason_code': 'scientific_contract_mismatch' if not compatible else 'verification_failed' if outcome == 'failed' else 'reference_not_comparable' if outcome == 'not_comparable' else 'passed',
                                'reason': 'Scientific contract matches; inspect individual checks' if compatible else 'Incompatible contract: ' + ', '.join(differences),
                                'scope': 'Status, literal evidence and independent fixture numerical checks' if actual == 'evaluated' else 'Status only; no numerical or semantic validity claim'})
            check = {'id': uid(), 'run_id': run_id, 'attempt_id': run['attempt_id'], 'revision_id': run['revision_id'],
                     'result_digest': digest(state), 'created_at': now(), 'scope': 'Frozen scientific compatibility; literal evidence and supported numerical reference checks for evaluated outputs; economic fidelity requires human review',
                     'outcome': 'failed' if any(r['outcome'] == 'failed' for r in results) else 'not_comparable' if any(r['outcome'] == 'not_comparable' for r in results) else 'passed',
                     'passed': all(r['passed'] for r in results), 'results': results}
        compute_seconds = time.monotonic() - started
        with transaction(self.db_path) as connection:
            write_started = time.monotonic()
            replay = self._mutation_replay(connection, 'regression.check', key, fingerprint)
            if replay is not None:
                return replay
            self._assert_review_snapshot(connection, rows, files)
            if any(self._one(connection, 'regression_cases', identity) != value for identity, value in case_snapshot.items()):
                raise ServiceError('Regression inputs changed during verification; rerun the check', 409)
            check['timing'] = {'compute_and_read_seconds':compute_seconds,
                'total_before_publish_seconds': time.monotonic() - started,
                'write_transaction_before_publish_seconds': time.monotonic() - write_started,
                'scope': 'PDF/reference recomputation outside writer lock; publication fences exact attempt/result/case rows and verified file stat inventory. Durations measured before final insert/commit.'}
            connection.execute('INSERT INTO regression_checks VALUES (?,?,?,?)', (check['id'], run_id, check['created_at'], json_text(check)))
            mutations.record(connection, 'regression.check', key, fingerprint, check)
            self._event(connection, run_id, 'regression_checked', {'check_id': check['id'], 'passed': check['passed']}, run['attempt_id'])
        return check

    def list_checks(self):
        return [loads(row['payload']) for row in self._read('SELECT payload FROM regression_checks ORDER BY created_at DESC')]
