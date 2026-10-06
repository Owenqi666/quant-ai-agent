"""Independent monthly experiments with immutable inputs and fenced publication."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import re
import stat
import threading
import time

from .db import connect, transaction
from .execution_lifecycle import _inventory
from .research_protocols import ResearchProtocols
from .service import ServiceError, now, uid
from ..storage import atomic_json, digest, json_text, read_json

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_ATTEMPTS = 3
PHASES = ('preparing', 'executing', 'verifying', 'publishing')
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
ACTIVE = {'running', 'cancelling'}
SUMMARY_COLUMNS = 'id,protocol_id,created_at,updated_at,status,config,attempt_count,attempt_id,error,phase'


def _loads(text, limit=MAX_RECORD_BYTES):
    if not isinstance(text, str) or len(text.encode()) > limit:
        raise ServiceError('Stored monthly JSON exceeds its limit', 409)
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON field')
            value[key] = item
        return value
    def invalid(_):
        raise ValueError('Nonfinite JSON number')
    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
        json_text(result)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise ServiceError('Stored monthly JSON is invalid', 409) from exc


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ServiceError('Invalid monthly resource identity', 422)
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ServiceError('Expected a SHA256 digest', 422)
    return value


def _request(operation, key, payload):
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise ServiceError('Idempotency key must contain 1..128 characters', 422)
    try:
        key.encode()
        return digest({'operation': operation, 'payload': payload})
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ServiceError('Request must contain finite valid JSON text', 422) from exc


def _receipt(connection, operation, key, fingerprint):
    row = connection.execute('SELECT * FROM monthly_receipts WHERE operation=? AND idempotency_key=?', (operation, key)).fetchone()
    if row is None:
        return None
    if row['request_digest'] != fingerprint:
        raise ServiceError('Idempotency key belongs to a different monthly request', 409)
    value = _loads(row['response'])
    if (not isinstance(value, dict) or digest(value) != row['response_digest'] or value.get('experiment_id') != row['experiment_id']):
        raise ServiceError('Stored monthly request receipt is invalid', 409)
    return value


def _record_receipt(connection, operation, key, fingerprint, experiment_id, value, *, encoded=None, response_digest=None):
    connection.execute('INSERT INTO monthly_receipts VALUES (?,?,?,?,?,?,?)',
                       (operation, key, fingerprint, encoded if encoded is not None else json_text(value),
                        response_digest if response_digest is not None else digest(value), experiment_id, now()))


def _summary(row):
    value = {key: row[key] for key in SUMMARY_COLUMNS.split(',')}
    value['config'] = _loads(value['config'])
    return value


def _files(output):
    value = _inventory(output)
    if any(stat.S_ISREG(item[2]) and item[3] != 1 for item in value.values()):
        raise ServiceError('Monthly artifacts may not be hard linked', 409)
    return value


class MonthlyExperiments:
    def __init__(self, store):
        self.store = store

    def _row(self, connection, identity):
        _id(identity)
        row = connection.execute('SELECT * FROM monthly_experiments WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ServiceError('Monthly experiment not found', 404)
        return dict(row)

    def _fetch(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            return self._row(connection, identity)

    def _event(self, connection, identity, kind, payload=None, attempt_id=None):
        connection.execute('INSERT INTO monthly_events(experiment_id,attempt_id,kind,created_at,payload) VALUES (?,?,?,?,?)',
                           (identity, attempt_id, kind, now(), json_text(payload or {})))

    def _path(self, identity, attempt_id):
        base = self.store.root / 'monthly' / _id(identity) / 'attempts' / _id(attempt_id)
        if base.resolve() != base:
            raise ServiceError('Monthly attempt path is unsafe', 409)
        return base

    def _input(self, row):
        try:
            value = _loads(row['input'], MAX_INPUT_BYTES)
            if (not isinstance(value, dict) or set(value) != {'schema_version', 'protocol', 'config', 'bundle'}
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or digest(value) != row['input_digest'] or value['protocol'] != _loads(row['protocol'])
                    or value['config'] != _loads(row['config']) or value['protocol']['id'] != row['protocol_id']):
                raise ValueError('Frozen input differs from its experiment')
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError('Frozen monthly input failed integrity verification', 409) from exc

    def _owned(self, connection, identity, worker_id, attempt_id):
        row = self._row(connection, identity)
        attempt = connection.execute('SELECT * FROM monthly_attempts WHERE id=?', (_id(attempt_id),)).fetchone()
        if (row['worker_id'] != worker_id or row['attempt_id'] != attempt_id or row['status'] not in ACTIVE
                or attempt is None or attempt['experiment_id'] != identity or attempt['worker_id'] != worker_id
                or attempt['status'] != 'running'):
            raise ServiceError('Worker no longer owns this monthly attempt', 409)
        return row

    def create(self, protocol_id, protocol_digest, config, idempotency_key):
        from ..monthly_evaluation import validate_config, demo_bundle
        _hash(protocol_digest)
        try:
            config = validate_config(config)
        except ValueError as exc:
            raise ServiceError(str(exc), 422) from exc
        fingerprint = _request('create', idempotency_key, {'protocol_id': protocol_id, 'protocol_digest': protocol_digest, 'config': config})
        with closing(connect(self.store.db_path)) as connection:
            replay = _receipt(connection, 'create', idempotency_key, fingerprint)
        if replay is not None:
            return replay
        protocol = ResearchProtocols(self.store).get(protocol_id)
        if protocol['digest'] != protocol_digest:
            raise ServiceError('Selected research protocol digest changed', 412)
        try:
            bundle = demo_bundle(config, protocol['config'])
            frozen = {'schema_version': 1, 'protocol': protocol, 'config': config, 'bundle': bundle}
            payload = json_text(frozen)
        except ValueError as exc:
            raise ServiceError(str(exc), 422) from exc
        if len(payload.encode()) > MAX_INPUT_BYTES:
            raise ServiceError('Monthly frozen input exceeds 64 MiB', 413)
        input_digest = digest(frozen)
        with transaction(self.store.db_path) as connection:
            replay = _receipt(connection, 'create', idempotency_key, fingerprint)
            if replay is not None:
                return replay
            if ResearchProtocols(self.store)._load(connection, protocol_id, {}) != protocol:
                raise ServiceError('Research protocol changed during monthly preparation', 409)
            identity, created = uid(), now()
            connection.execute('INSERT INTO monthly_experiments(id,protocol_id,protocol,config,input,input_digest,created_at,updated_at,status) VALUES (?,?,?,?,?,?,?,?,?)',
                               (identity, protocol_id, json_text(protocol), json_text(config), payload, input_digest, created, created, 'queued'))
            self._event(connection, identity, 'queued',
                        {'domain_submit_effect_key': idempotency_key} if idempotency_key.startswith('domain-effect-') else None)
            value = {'experiment_id': identity}
            _record_receipt(connection, 'create', idempotency_key, fingerprint, identity, value)
            from .domain_research_jobs import bind_monthly_effect, domain_execution_remaining_seconds
            bind_monthly_effect(self.store, connection, identity, idempotency_key)
            if idempotency_key.startswith('domain-effect-'):
                domain_execution_remaining_seconds(self.store, identity, connection=connection)
            return value

    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ServiceError('Monthly pagination requires limit 1..100 and offset 0..100000', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM monthly_experiments').fetchone()[0]
            rows = connection.execute(f'SELECT {SUMMARY_COLUMNS} FROM monthly_experiments ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            return {'items': [_summary(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def status(self, identity):
        _id(identity)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            row = connection.execute(f'SELECT {SUMMARY_COLUMNS} FROM monthly_experiments WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ServiceError('Monthly experiment not found', 404)
            event = connection.execute('SELECT MAX(id) FROM monthly_events WHERE experiment_id=?', (identity,)).fetchone()[0]
            value = {key: row[key] for key in ('id', 'status', 'attempt_id', 'attempt_count', 'phase')}
            return {**value, 'change_token': digest({'experiment': dict(row), 'event': event}), 'integrity_checked': False}

    def _verified(self, row):
        from ..monthly_workflow import verify
        self._input(row)
        output = self._path(row['id'], row['attempt_id']) / 'output'
        try:
            inventory = _files(output)
            checked = verify(output, expected_input_digest=row['input_digest'])
            result = read_json(output / 'result.json')
            if (checked.get('verified') is not True or checked['result_digest'] != digest(result)
                    or checked['input_digest'] != row['input_digest']
                    or (result['status'] != 'blocked' and checked['reference_passed'] is not True)
                    or (row['result_digest'] is not None and checked['result_digest'] != row['result_digest'])
                    or _files(output) != inventory):
                raise ValueError('Monthly result identity or verification changed')
            if row['status'] == 'completed':
                with closing(connect(self.store.db_path)) as connection:
                    self._published_attempt(connection, row, checked)
            return result, checked, inventory
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ServiceError('Monthly artifacts failed integrity verification', 409) from exc

    def _published_attempt(self, connection, row, checked):
        attempt = connection.execute('SELECT * FROM monthly_attempts WHERE id=?', (row['attempt_id'],)).fetchone()
        if (row['result_digest'] != checked['result_digest'] or row['verification'] is None
                or _loads(row['verification']) != checked or attempt is None
                or attempt['experiment_id'] != row['id'] or attempt['worker_id'] != row['worker_id']
                or attempt['number'] != row['attempt_count'] or attempt['status'] != 'completed'
                or attempt['result_digest'] != checked['result_digest'] or attempt['verification'] is None
                or _loads(attempt['verification']) != checked):
            raise ServiceError('Monthly published attempt metadata differs from verified output', 409)

    def _saved(self, row, *, report=False):
        try:
            value = _loads(row['payload'])
            fields = {'id', 'experiment_id', 'attempt_id', 'result_digest', 'created_at', 'digest'}
            fields |= {'result', 'reviews', 'protocol', 'markdown'} if report else {'verdict', 'note', 'source'}
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError('Frozen record fields differ')
            body = {key: item for key, item in value.items() if key != 'digest'}
            if (value['digest'] != row['digest'] or digest(body) != row['digest']
                    or any(value[key] != row[key] for key in ('id', 'experiment_id', 'attempt_id', 'result_digest', 'created_at'))):
                raise ValueError('Frozen record identity differs')
            if report and (digest(value['result']) != value['result_digest'] or not isinstance(value['markdown'], str)):
                raise ValueError('Frozen report result differs')
            return value
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError('Frozen monthly record failed integrity verification', 409) from exc

    def get(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            row = self._row(connection, identity)
            frozen = self._input(row)
            attempts = [dict(item) for item in connection.execute('SELECT id,number,status,started_at,finished_at,error FROM monthly_attempts WHERE experiment_id=? ORDER BY number', (identity,))]
            reviews = [self._saved(item) for item in connection.execute('SELECT * FROM monthly_reviews WHERE experiment_id=? ORDER BY created_at,id', (identity,))]
            reports = [dict(item) for item in connection.execute('SELECT id,created_at,digest,attempt_id,result_digest FROM monthly_reports WHERE experiment_id=? ORDER BY created_at,id', (identity,))]
            result, verification, target = None, {'verified': False, 'result_digest': None, 'input_digest': None, 'reference_passed': None}, None
            if row['status'] == 'completed':
                result, verification, _ = self._verified(row)
                target = {'attempt_id': row['attempt_id'], 'result_digest': verification['result_digest']}
            return {'experiment': _summary(row), 'protocol': frozen['protocol'], 'attempts': attempts,
                    'result': result, 'verification': verification, 'review_target': target, 'reviews': reviews, 'reports': reports}

    def cancel(self, identity, expected_attempt_id):
        with transaction(self.store.db_path) as connection:
            row = self._row(connection, identity)
            if row['attempt_id'] != expected_attempt_id:
                raise ServiceError('Monthly cancellation targets a different attempt', 412)
            if row['status'] in {'queued', 'running'}:
                status = 'cancelled' if row['status'] == 'queued' else 'cancelling'
                connection.execute('UPDATE monthly_experiments SET status=?,updated_at=? WHERE id=?', (status, now(), identity))
                self._event(connection, identity, status, attempt_id=row['attempt_id'])
        return {'experiment_id': identity}

    def retry(self, identity, expected_attempt_id, idempotency_key):
        _id(expected_attempt_id)
        fingerprint = _request('retry', idempotency_key, {'experiment_id': identity, 'expected_attempt_id': expected_attempt_id})
        with transaction(self.store.db_path) as connection:
            replay = _receipt(connection, 'retry', idempotency_key, fingerprint)
            if replay is not None:
                return replay
            row = self._row(connection, identity)
            if row['attempt_id'] != expected_attempt_id:
                raise ServiceError('Monthly retry targets a different attempt', 412)
            if row['status'] not in {'failed', 'cancelled', 'interrupted'} or row['attempt_count'] >= MAX_ATTEMPTS:
                raise ServiceError('Monthly experiment cannot be retried or reached its three-attempt limit', 409)
            connection.execute("UPDATE monthly_experiments SET status='queued',worker_id=NULL,heartbeat=NULL,error=NULL,phase=NULL,result_digest=NULL,verification=NULL,updated_at=? WHERE id=?", (now(), identity))
            self._event(connection, identity, 'retry_queued', attempt_id=expected_attempt_id)
            value = {'experiment_id': identity}
            _record_receipt(connection, 'retry', idempotency_key, fingerprint, identity, value)
            return value

    def cancel_requested(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT status FROM monthly_experiments WHERE id=?', (_id(identity),)).fetchone()
            if row is None:
                raise ServiceError('Monthly experiment not found', 404)
            return row['status'] in {'cancelling', 'cancelled'}

    def heartbeat(self, worker_id, identity, attempt_id):
        with transaction(self.store.db_path) as connection:
            stamp = time.time()
            changed = connection.execute("UPDATE monthly_experiments SET heartbeat=? WHERE id=? AND worker_id=? AND attempt_id=? AND status IN ('running','cancelling')", (stamp, identity, worker_id, attempt_id)).rowcount
            if changed:
                connection.execute('INSERT INTO workers VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen', (worker_id, stamp))
            return bool(changed)

    @contextmanager
    def heartbeat_scope(self, identity, worker_id, attempt_id):
        stop = threading.Event()
        def pulse():
            while not stop.wait(.5):
                try:
                    if not self.heartbeat(worker_id, identity, attempt_id):
                        break
                except Exception:
                    # The inherited flock, never a heartbeat timeout, authorizes recovery.
                    import logging
                    logging.getLogger('paper_alpha.monthly').exception('monthly_heartbeat_failed')
        self.heartbeat(worker_id, identity, attempt_id)
        thread = threading.Thread(target=pulse, name='monthly-heartbeat', daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(16)

    def record_phase(self, identity, worker_id, attempt_id, phase):
        if phase not in PHASES:
            raise ServiceError('Unknown monthly execution phase', 422)
        with transaction(self.store.db_path) as connection:
            row = self._owned(connection, identity, worker_id, attempt_id)
            if row['phase'] != phase:
                connection.execute('UPDATE monthly_experiments SET phase=?,updated_at=? WHERE id=?', (phase, now(), identity))
                self._event(connection, identity, 'execution_phase', {'phase': phase}, attempt_id)

    def claim(self, worker_id):
        with transaction(self.store.db_path) as connection:
            selected = connection.execute("SELECT id FROM monthly_experiments WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if selected is None:
                return None
            row = self._row(connection, selected['id'])
            attempt_id, stamp, number = uid(), now(), row['attempt_count'] + 1
            if number > MAX_ATTEMPTS:
                raise ServiceError('Monthly attempt limit exceeded', 409)
            connection.execute("INSERT INTO monthly_attempts(id,experiment_id,number,worker_id,status,started_at) VALUES (?,?,?,?,'running',?)", (attempt_id, row['id'], number, worker_id, stamp))
            connection.execute("UPDATE monthly_experiments SET status='running',attempt_count=?,attempt_id=?,worker_id=?,heartbeat=?,updated_at=?,phase='preparing',error=NULL,result_digest=NULL,verification=NULL WHERE id=?", (number, attempt_id, worker_id, time.time(), stamp, row['id']))
            self._event(connection, row['id'], 'execution_phase', {'phase': 'preparing'}, attempt_id)
        with self.heartbeat_scope(row['id'], worker_id, attempt_id):
            try:
                from .domain_research_jobs import domain_execution_remaining_seconds
                remaining = domain_execution_remaining_seconds(self.store, row['id'])
                if remaining is not None and remaining <= 0:
                    raise ServiceError('Original domain wall deadline elapsed while queued', 409)
                folder = self._path(row['id'], attempt_id)
                frozen = self._input(row)
                folder.mkdir(parents=True, exist_ok=False)
                atomic_json(folder / 'input.json', frozen)
                if digest(read_json(folder / 'input.json')) != row['input_digest']:
                    raise ValueError('Materialized monthly input digest differs')
                if self.cancel_requested(row['id']):
                    self.finish(row['id'], worker_id, attempt_id, 'cancelled')
                    return None
                with closing(connect(self.store.db_path)) as connection:
                    self._owned(connection, row['id'], worker_id, attempt_id)
            except (OSError, ValueError, ServiceError) as exc:
                self.finish(row['id'], worker_id, attempt_id, 'failed', error='Input preparation failed: ' + str(exc))
                return None
        return {'kind': 'monthly', 'id': row['id'], 'attempt_id': attempt_id,
                'input_path': str(folder / 'input.json'), 'output_dir': str(folder / 'output'),
                'max_seconds': min(600, remaining) if remaining is not None else 600}

    def finish(self, identity, worker_id, attempt_id, status, error=None):
        if status not in TERMINAL:
            raise ServiceError('Invalid monthly terminal state', 422)
        with closing(connect(self.store.db_path)) as connection:
            row = self._owned(connection, identity, worker_id, attempt_id)
        verification, files = None, None
        with self.heartbeat_scope(identity, worker_id, attempt_id):
            if status == 'completed' and row['status'] != 'cancelling':
                try:
                    self.record_phase(identity, worker_id, attempt_id, 'verifying')
                    _, verification, files = self._verified(row)
                    self.record_phase(identity, worker_id, attempt_id, 'publishing')
                except (ValueError, OSError, ServiceError) as exc:
                    status, error, verification = 'failed', 'Output verification failed: ' + str(exc), None
            with transaction(self.store.db_path) as connection:
                current = self._owned(connection, identity, worker_id, attempt_id)
                if verification:
                    try:
                        from .domain_research_jobs import domain_execution_remaining_seconds
                        remaining = domain_execution_remaining_seconds(self.store, identity, connection=connection)
                        if remaining is not None and remaining <= 0:
                            raise ServiceError('Original domain wall deadline elapsed before publication', 409)
                        if (current['input_digest'] != row['input_digest'] or current['input'] != row['input']
                                or _files(self._path(identity, attempt_id) / 'output') != files):
                            raise ValueError('Monthly input or output changed before publication')
                    except (ValueError, OSError, ServiceError) as exc:
                        status, error, verification = 'failed', 'Publication verification failed: ' + str(exc), None
                if current['status'] == 'cancelling':
                    status, error, verification = 'cancelled', 'Cancellation requested', None
                stamp, error = now(), self.store._safe_error(error)
                result_digest = verification['result_digest'] if verification else None
                encoded = json_text(verification) if verification else None
                connection.execute('UPDATE monthly_attempts SET status=?,finished_at=?,error=?,result_digest=?,verification=? WHERE id=?', (status, stamp, error, result_digest, encoded, attempt_id))
                connection.execute('UPDATE monthly_experiments SET status=?,updated_at=?,error=?,phase=NULL,result_digest=?,verification=? WHERE id=?', (status, stamp, error, result_digest, encoded, identity))
                self._event(connection, identity, status, {'error': error, 'verified': bool(verification)}, attempt_id)
        return {'experiment_id': identity}

    def recover_stale(self, stale_seconds=0):
        recovered = []
        with transaction(self.store.db_path) as connection:
            rows = connection.execute("SELECT id,attempt_id,status FROM monthly_experiments WHERE status IN ('running','cancelling') AND (heartbeat IS NULL OR heartbeat<?)", (time.time() - stale_seconds,)).fetchall()
            for row in rows:
                status = 'cancelled' if row['status'] == 'cancelling' else 'interrupted'
                stamp, error = now(), 'Worker lost; partial files retained. Explicit retry required.'
                connection.execute('UPDATE monthly_experiments SET status=?,updated_at=?,error=?,phase=NULL,result_digest=NULL,verification=NULL WHERE id=?', (status, stamp, error, row['id']))
                connection.execute('UPDATE monthly_attempts SET status=?,finished_at=?,error=? WHERE id=?', (status, stamp, error, row['attempt_id']))
                self._event(connection, row['id'], 'worker_lost', {'status': status}, row['attempt_id'])
                recovered.append(row['id'])
        return recovered

    def _target(self, identity, attempt_id, result_digest):
        _id(attempt_id)
        _hash(result_digest)
        row = self._fetch(identity)
        if row['attempt_id'] != attempt_id or row['result_digest'] != result_digest:
            raise ServiceError('Monthly request targets a different result', 412)
        if row['status'] != 'completed':
            raise ServiceError('Only completed verified monthly results may be reviewed or reported', 409)
        result, verified, files = self._verified(row)
        return row, result, verified, files

    def _fence(self, connection, row, files):
        if self._row(connection, row['id']) != row or _files(self._path(row['id'], row['attempt_id']) / 'output') != files:
            raise ServiceError('Monthly result changed during verification', 409)
        self._published_attempt(connection, row, _loads(row['verification']))

    def create_review(self, identity, attempt_id, result_digest, verdict, note, source, idempotency_key):
        if (not isinstance(verdict, str) or verdict not in {'accepted', 'needs_revision', 'rejected'}
                or not isinstance(source, str) or source not in {'human', 'automation'}):
            raise ServiceError('Invalid monthly review verdict or source', 422)
        if not isinstance(note, str) or len(note) > 4000:
            raise ServiceError('Monthly review note must contain at most 4000 characters', 422)
        request = {'experiment_id': identity, 'attempt_id': attempt_id, 'result_digest': result_digest, 'verdict': verdict, 'note': note, 'source': source}
        fingerprint = _request('review', idempotency_key, request)
        with closing(connect(self.store.db_path)) as connection:
            replay = _receipt(connection, 'review', idempotency_key, fingerprint)
        if replay is not None:
            return replay
        row, _, _, files = self._target(identity, attempt_id, result_digest)
        with transaction(self.store.db_path) as connection:
            replay = _receipt(connection, 'review', idempotency_key, fingerprint)
            if replay is not None:
                return replay
            self._fence(connection, row, files)
            value = {**request, 'id': uid(), 'created_at': now()}
            value['digest'] = digest(value)
            connection.execute('INSERT INTO monthly_reviews VALUES (?,?,?,?,?,?,?)', (value['id'], identity, attempt_id, result_digest, json_text(value), value['digest'], value['created_at']))
            self._event(connection, identity, 'review_recorded', {'review_id': value['id']}, attempt_id)
            _record_receipt(connection, 'review', idempotency_key, fingerprint, identity, value)
            return value

    def create_report(self, identity, attempt_id, result_digest, idempotency_key):
        from ..monthly_workflow import render_report
        request = {'experiment_id': identity, 'attempt_id': attempt_id, 'result_digest': result_digest}
        fingerprint = _request('report', idempotency_key, request)
        with closing(connect(self.store.db_path)) as connection:
            replay = _receipt(connection, 'report', idempotency_key, fingerprint)
        if replay is not None:
            return replay
        row, result, _, files = self._target(identity, attempt_id, result_digest)
        with closing(connect(self.store.db_path)) as connection:
            review_rows = [dict(item) for item in connection.execute('SELECT * FROM monthly_reviews WHERE experiment_id=? AND attempt_id=? AND result_digest=? ORDER BY created_at,id', (identity, attempt_id, result_digest))]
        reviews = [self._saved(item) for item in review_rows]
        markdown = render_report(result) + '\n\n## Frozen reviews\n\n'
        markdown += '\n'.join('- ' + json.dumps({key: review[key] for key in ('id', 'verdict', 'source', 'note')}, ensure_ascii=False) for review in reviews) if reviews else 'No review existed when this report was frozen.'
        value = {**request, 'id': uid(), 'created_at': now(), 'result': result, 'reviews': reviews, 'protocol': _loads(row['protocol']), 'markdown': markdown + '\n'}
        value['digest'] = digest(value)
        payload = json_text(value)
        response_digest = digest(value)
        if len(payload.encode()) > MAX_RECORD_BYTES:
            raise ServiceError('Frozen monthly report exceeds 128 MiB', 413)
        with transaction(self.store.db_path) as connection:
            replay = _receipt(connection, 'report', idempotency_key, fingerprint)
            if replay is not None:
                return replay
            self._fence(connection, row, files)
            current_reviews = [dict(item) for item in connection.execute('SELECT * FROM monthly_reviews WHERE experiment_id=? AND attempt_id=? AND result_digest=? ORDER BY created_at,id', (identity, attempt_id, result_digest))]
            if current_reviews != review_rows:
                raise ServiceError('Reviews changed while freezing monthly report; retry the request', 409)
            connection.execute('INSERT INTO monthly_reports VALUES (?,?,?,?,?,?,?)', (value['id'], identity, attempt_id, result_digest, payload, value['digest'], value['created_at']))
            self._event(connection, identity, 'report_frozen', {'report_id': value['id']}, attempt_id)
            _record_receipt(connection, 'report', idempotency_key, fingerprint, identity, value,
                            encoded=payload, response_digest=response_digest)
            return value

    def get_report(self, identity, report_id):
        _id(identity)
        _id(report_id)
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT * FROM monthly_reports WHERE id=? AND experiment_id=?', (report_id, identity)).fetchone()
            if row is None:
                raise ServiceError('Monthly report not found', 404)
            return self._saved(row, report=True)
