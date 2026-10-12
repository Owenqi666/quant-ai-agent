"""Persistent provider-free control for monthly fixtures and author diagnostics.

Scientific stops never acquire portfolio execution authority. Monthly effects
reuse the existing queue and engine; their original receipt, separate binding,
and queue event anchor independently identify the parent budget.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
import sqlite3
import stat
import time

from .db import connect, transaction
from .service import ServiceError, now
from .domain_research_jobs_schema import (DomainJobCreate, DomainJobAdvance, DomainJobStep,
                                         DomainResearchJob, DomainJobSummary)
from .research_jobs import JobServiceError
from ..storage import digest, json_text

SCHEMA = """
CREATE TABLE domain_research_jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,source_kind TEXT NOT NULL,charged_count INTEGER NOT NULL,ledger_digest TEXT NOT NULL,submit_effect_key TEXT NOT NULL UNIQUE);
CREATE INDEX domain_research_jobs_created ON domain_research_jobs(created_at,id);
CREATE TABLE domain_research_job_steps(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES domain_research_jobs(id),sequence INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(job_id,sequence),UNIQUE(job_id,idempotency_key));
CREATE TABLE domain_monthly_effects(experiment_id TEXT PRIMARY KEY REFERENCES monthly_experiments(id),job_id TEXT NOT NULL UNIQUE REFERENCES domain_research_jobs(id),effect_key TEXT NOT NULL UNIQUE);
"""
MAX_BODY = 512 * 1024
MAX_STEP = 3 * 1024 * 1024
MAX_RESULT = 2 * 1024 * 1024
MAX_LEDGER = 32 * 1024 * 1024
TERMINAL = {'completed', 'blocked', 'failed', 'exhausted', 'cancelled'}
NEXT = {'validate': 'created', 'submit': 'validated', 'observe': 'submitted', 'complete': 'observed'}
PREFIX = 'domain-effect-'


def _error(code, message, retryable=False):
    return {'code': code, 'message': message, 'retryable': retryable}


def _bounded(value, maximum):
    text = json_text(value)
    if len(text.encode('utf-8')) > maximum:
        raise JobServiceError('Domain job payload exceeds its byte bound', 413, code='PAYLOAD_TOO_LARGE')
    return text


def _parse(text, maximum):
    if not isinstance(text, str) or len(text.encode('utf-8')) > maximum:
        raise ValueError('Stored JSON exceeds its byte bound')
    def constant(_):
        raise ValueError('Nonfinite stored number')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate stored field')
            result[key] = value
        return result
    value = json.loads(text, parse_constant=constant, object_pairs_hook=pairs)
    if _bounded(value, maximum) != text:
        raise ValueError('Noncanonical stored JSON')
    return value


def _identity(identity):
    import re
    if not isinstance(identity, str) or not re.fullmatch(r'domain_job_[0-9a-f]{64}', identity):
        raise JobServiceError('Invalid domain job identity', 422, code='ARGUMENTS_INVALID')


def _elapsed(created_at):
    return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds())


def _effect(identity, action):
    return PREFIX + digest({'job_id': identity, 'action': action})


def bind_monthly_effect(store, connection, experiment_id, effect_key):
    """Called within the original monthly-create transaction, not caller authority."""
    if not effect_key.startswith(PREFIX):
        return
    row = connection.execute('SELECT id FROM domain_research_jobs WHERE submit_effect_key=?', (effect_key,)).fetchone()
    if row is None:
        raise JobServiceError('Unknown domain submit effect', code='PARENT_INTEGRITY')
    job = DomainResearchJobs(store)._load(connection, row['id'])
    if job['source_kind'] != 'monthly_fixture' or not any(s['action'] == 'submit' and s['effect_key'] == effect_key for s in job['steps']):
        raise JobServiceError('Domain effect lacks a durably charged submit', code='PARENT_INTEGRITY')
    if _elapsed(job['created_at']) >= job['budget']['max_seconds']:
        raise JobServiceError('Original domain deadline elapsed before queue insertion', code='BUDGET_EXHAUSTED')
    connection.execute('INSERT INTO domain_monthly_effects VALUES(?,?,?)', (experiment_id, job['id'], effect_key))
    connection.execute('INSERT INTO monthly_events(experiment_id,kind,created_at,payload) VALUES(?,?,?,?)',
                       (experiment_id, 'domain_parent_bound', now(), json_text({'job_id': job['id'], 'effect_key': effect_key})))


def domain_execution_remaining_seconds(store, experiment_id, *, connection=None):
    """Verify every parent anchor before calculation/publication; ordinary jobs return None."""
    if connection is None:
        with closing(connect(store.db_path)) as opened:
            opened.execute('BEGIN')
            return domain_execution_remaining_seconds(store, experiment_id, connection=opened)
    binding = connection.execute('SELECT * FROM domain_monthly_effects WHERE experiment_id=?', (experiment_id,)).fetchone()
    markers = connection.execute("SELECT payload FROM monthly_events WHERE experiment_id=? AND kind='domain_parent_bound'", (experiment_id,)).fetchall()
    origins = connection.execute("SELECT payload FROM monthly_events WHERE experiment_id=? AND kind='queued'", (experiment_id,)).fetchall()
    receipts = connection.execute("SELECT * FROM monthly_receipts WHERE operation='create' AND experiment_id=? AND idempotency_key LIKE 'domain-effect-%'", (experiment_id,)).fetchall()
    try:
        owned_origins = []
        for item in origins:
            value = _parse(item['payload'], MAX_BODY)
            if not isinstance(value, dict):
                raise ValueError('Queue origin is not an object')
            if value.get('domain_submit_effect_key'):
                owned_origins.append(value)
        if not binding and not markers and not receipts and not owned_origins:
            # An acknowledged submit is another original reverse reference.
            # LIKE only narrows candidates; exact canonical parsing decides.
            rows = connection.execute('SELECT job_id,payload FROM domain_research_job_steps WHERE payload LIKE ? LIMIT 101', ('%' + experiment_id + '%',)).fetchall()
            if len(rows) > 100:
                raise ValueError('Reverse ownership lookup exceeds its bound')
            for saved in rows:
                step = _parse(saved['payload'], MAX_STEP)
                if not isinstance(step, dict):
                    raise ValueError('Reverse ownership step is not an object')
                output = _parse(step['output_json'], MAX_RESULT) if step.get('output_json') else {}
                if not isinstance(output, dict):
                    raise ValueError('Reverse ownership output is not an object')
                if step.get('action') == 'submit' and output.get('experiment_id') == experiment_id:
                    raise ValueError('Acknowledged domain effect lost all monthly parent anchors')
            return None
    except (ValueError, TypeError, KeyError, ServiceError) as exc:
        raise JobServiceError('Monthly origin binding failed integrity verification', code='PARENT_INTEGRITY') from exc
    try:
        if binding is None or len(markers) != 1 or len(receipts) != 1 or len(owned_origins) != 1:
            raise ValueError('Missing or repeated original parent anchor')
        marker = _parse(markers[0]['payload'], MAX_BODY)
        if (marker != {'job_id': binding['job_id'], 'effect_key': binding['effect_key']}
                or receipts[0]['idempotency_key'] != binding['effect_key']
                or owned_origins[0] != {'domain_submit_effect_key': binding['effect_key']}):
            raise ValueError('Parent anchors disagree')
        service = DomainResearchJobs(store)
        job = service._load(connection, binding['job_id'])
        request = service._body(connection, job['id'])['request']['source']
        if job['source_kind'] != 'monthly_fixture' or binding['effect_key'] != _effect(job['id'], 'submit'):
            raise ValueError('Wrong source or effect')
        submit = [s for s in job['steps'] if s['action'] == 'submit' and s['effect_key'] == binding['effect_key']]
        if len(submit) != 1:
            raise ValueError('No original charged submit')
        from .monthly_experiments import _receipt, _request
        expected = _request('create', binding['effect_key'], {'protocol_id': request['protocol_id'], 'protocol_digest': request['protocol_digest'], 'config': request['config']})
        if _receipt(connection, 'create', binding['effect_key'], expected) != {'experiment_id': experiment_id}:
            raise ValueError('Original receipt changed')
        row = connection.execute('SELECT protocol_id,config,protocol FROM monthly_experiments WHERE id=?', (experiment_id,)).fetchone()
        if row is None or row['protocol_id'] != request['protocol_id'] or json.loads(row['config']) != request['config'] or json.loads(row['protocol']) != service._body(connection, job['id'])['source']['protocol']:
            raise ValueError('Frozen experiment source differs')
        if job['experiment_id'] and job['experiment_id'] != experiment_id:
            raise ValueError('Acknowledged experiment differs')
        return max(0.0, job['budget']['max_seconds'] - _elapsed(job['created_at']))
    except (ValueError, TypeError, KeyError, ServiceError) as exc:
        raise JobServiceError('Monthly parent binding failed integrity verification', code='PARENT_INTEGRITY') from exc


class DomainResearchJobs:
    def __init__(self, store):
        self.store = store

    def _source(self, request):
        if request['kind'] == 'monthly_fixture':
            from .research_protocols import ResearchProtocols
            from ..monthly_evaluation import validate_config
            protocol = ResearchProtocols(self.store).get(request['protocol_id'])
            if protocol['digest'] != request['protocol_digest'] or validate_config(request['config']) != request['config']:
                raise JobServiceError('Selected protocol/config changed', code='SOURCE_INTEGRITY')
            return {'protocol': protocol, 'config': request['config'], 'data_kind': 'controlled_fixture', 'source_id': 'fictional-monthly-portfolios-v1'}
        from .author_studies import AuthorStudies
        study = AuthorStudies(self.store).get(request['study_id'])
        if study['digest'] != request['study_digest']:
            raise JobServiceError('Selected author study changed', code='SOURCE_INTEGRITY')
        return {'study': study}

    def create(self, source, budget, idempotency_key, note=''):
        try:
            request = DomainJobCreate.model_validate(dict(source=source, budget=budget, idempotency_key=idempotency_key, note=note)).model_dump()
            if not idempotency_key.strip():
                raise ValueError('Blank key')
            _bounded(request, MAX_BODY // 2)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise JobServiceError('Invalid closed domain job request', 422, code='ARGUMENTS_INVALID') from exc
        fingerprint = digest(request)
        with closing(connect(self.store.db_path)) as connection:
            replay = self._replay(connection, idempotency_key, fingerprint)
            if replay:
                return replay
        frozen = self._source(request['source'])
        context = {'source': frozen, 'provider_connected': False, 'semantic_fidelity': 'unverified',
                   'scope': 'Monthly engine controlled fixture only; author studies are aggregate diagnostics without portfolio execution.',
                   'omissions': {'raw_source': 'No raw MAT or arbitrary files are exposed.', 'market_labels': 'No actual market data.'}}
        body = {'request': request, 'source': frozen, 'context_json': _bounded(context, MAX_BODY // 2)}
        payload = _bounded(body, MAX_BODY)
        identity, created = 'domain_job_' + fingerprint, now()
        with transaction(self.store.db_path) as connection:
            replay = self._replay(connection, idempotency_key, fingerprint)
            if replay:
                return replay
            connection.execute('INSERT INTO domain_research_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identity, payload, digest(body), created, digest({'id': identity, 'created_at': created, 'digest': digest(body)}),
                 idempotency_key, fingerprint, request['source']['kind'], 0, digest([]), _effect(identity, 'submit')))
            return self._load(connection, identity)

    def _replay(self, connection, key, fingerprint):
        row = connection.execute('SELECT id,request_digest FROM domain_research_jobs WHERE idempotency_key=?', (key,)).fetchone()
        if row:
            if row['request_digest'] != fingerprint:
                raise JobServiceError('Domain job key belongs to another request', code='IDEMPOTENCY_CONFLICT')
            return self._load(connection, row['id'])
        return None

    def _body(self, connection, identity):
        row = connection.execute('SELECT payload FROM domain_research_jobs WHERE id=?', (identity,)).fetchone()
        return _parse(row['payload'], MAX_BODY)

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM domain_research_jobs WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise JobServiceError('Domain job not found', 404, code='JOB_NOT_FOUND')
        try:
            body = _parse(row['payload'], MAX_BODY)
            if set(body) != {'request', 'source', 'context_json'}:
                raise ValueError('Body fields changed')
            request = DomainJobCreate.model_validate(body['request']).model_dump()
            if (digest(request) != row['request_digest'] or row['id'] != 'domain_job_' + digest(request)
                    or digest(body) != row['digest'] or row['source_kind'] != request['source']['kind']
                    or row['idempotency_key'] != request['idempotency_key'] or row['submit_effect_key'] != _effect(identity, 'submit')
                    or row['metadata_digest'] != digest({'id': identity, 'created_at': row['created_at'], 'digest': row['digest']})):
                raise ValueError('Job metadata changed')
            rows = connection.execute('SELECT * FROM domain_research_job_steps WHERE job_id=? ORDER BY sequence', (identity,)).fetchall()
            if (len(rows) != row['charged_count'] or row['ledger_digest'] != digest([r['digest'] for r in rows])
                    or sum(len(r['payload'].encode()) for r in rows) > MAX_LEDGER):
                raise ValueError('Ledger anchors differ')
            steps = []
            for index, saved in enumerate(rows, 1):
                step = DomainJobStep.model_validate(_parse(saved['payload'], MAX_STEP)).model_dump()
                if (step['sequence'] != index or saved['sequence'] != index or step['id'] != saved['id']
                        or step['id'] != 'domain_step_' + digest({'job_id': identity, 'key': step['idempotency_key']})
                        or digest(step) != saved['digest'] or step['idempotency_key'] != saved['idempotency_key']
                        or step['request_digest'] != saved['request_digest'] or step['request_digest'] != digest({'action': step['action']})
                        or step['effect_key'] != _effect(identity, step['action'])
                        or (step['status'] == 'running') != (step['finished_at'] is None)):
                    raise ValueError('Step identity changed')
                steps.append(step)
            if len(steps) > request['budget']['max_steps'] and not (len(steps) == request['budget']['max_steps'] + 1 and steps[-1]['action'] == 'cancel'):
                raise ValueError('Original step budget changed')
            state, reason, experiment_id, attempt_id, result_json = 'created', None, None, None, None
            for step in steps:
                if step['status'] != 'running':
                    state = step['next_state']
                    if step['error'] and state in TERMINAL:
                        reason = step['error']['code']
                    output = _parse(step['output_json'], MAX_RESULT) if step['output_json'] else {}
                    if not isinstance(output, dict):
                        raise ValueError('Step output is not an object')
                    experiment_id = output.get('experiment_id', experiment_id)
                    attempt_id = output.get('attempt_id', attempt_id)
                    result_json = output.get('result_json', result_json)
            elapsed, failures = _elapsed(row['created_at']), sum(s['status'] in {'failed', 'interrupted'} for s in steps)
            if state not in TERMINAL and not any(s['status'] == 'running' for s in steps) and (
                    len(steps) >= request['budget']['max_steps'] or failures >= request['budget']['max_failures'] or elapsed >= request['budget']['max_seconds']):
                state, reason = 'exhausted', 'BUDGET_EXHAUSTED'
            source_id = request['source'].get('protocol_id', request['source'].get('study_id'))
            value = {'id': identity, 'digest': row['digest'], 'created_at': row['created_at'], 'source_kind': request['source']['kind'],
                     'source_id': source_id, 'source_digest': digest(body['source']), 'source': request['source'], 'note': request['note'],
                     'budget': request['budget'], 'usage': {'steps': len(steps), 'failures': failures, 'elapsed_seconds': elapsed},
                     'state': state, 'stop_reason': reason, 'experiment_id': experiment_id, 'attempt_id': attempt_id,
                     'context_json': body['context_json'], 'steps': steps, 'result_json': result_json,
                     'provider_connected': False, 'semantic_fidelity': 'unverified'}
            DomainResearchJob.model_validate(value)
            return value
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise JobServiceError('Domain job ledger failed integrity verification', code='LEDGER_INTEGRITY') from exc

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._load(connection, identity)

    def list(self, limit=20, offset=0, source_kind=None):
        if (type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000
                or source_kind not in {None, 'monthly_fixture', 'author_study_diagnostic'}):
            raise JobServiceError('Invalid domain job page', 422, code='ARGUMENTS_INVALID')
        where, args = (' WHERE source_kind=?', (source_kind,)) if source_kind else ('', ())
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM domain_research_jobs' + where, args).fetchone()[0]
            rows = connection.execute('SELECT id FROM domain_research_jobs' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
            fields = set(DomainJobSummary.model_fields)
            return {'items': [{k: v for k, v in self._load(connection, r['id']).items() if k in fields} for r in rows], 'total': total, 'limit': limit, 'offset': offset}

    @contextmanager
    def _lock(self, identity):
        descriptor = os.open(self.store.root / ('.' + identity + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise JobServiceError('Domain job lock is not regular', code='LEDGER_INTEGRITY')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobServiceError('Domain job has an active transition; replay the same key', code='JOB_BUSY', retryable=True) from exc
            yield
        finally:
            os.close(descriptor)

    def _monthly_effect(self, job, step):
        from .monthly_experiments import _receipt, _request
        source = job['source']
        if source['kind'] != 'monthly_fixture' or step['action'] != 'submit':
            return None
        fingerprint = _request('create', step['effect_key'], {'protocol_id': source['protocol_id'], 'protocol_digest': source['protocol_digest'], 'config': source['config']})
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            value = _receipt(connection, 'create', step['effect_key'], fingerprint)
            if value:
                domain_execution_remaining_seconds(self.store, value['experiment_id'], connection=connection)
            return value

    def _dispatch(self, job, body, step):
        from .monthly_experiments import MonthlyExperiments
        action, monthly = step['action'], MonthlyExperiments(self.store)
        if action == 'cancel':
            identity = job['experiment_id']
            if identity:
                status = monthly.status(identity)
                if job['attempt_id'] is not None and status['attempt_id'] != job['attempt_id']:
                    raise JobServiceError('Cancellation targets a different monthly attempt', code='ATTEMPT_CHANGED')
                # A first attempt may be claimed since submit. Pin that original
                # attempt, but never adopt a later manually retried computation.
                if status['attempt_count'] > 1:
                    raise JobServiceError('Cancellation cannot adopt a retried attempt', code='ATTEMPT_CHANGED')
                if status['status'] in {'queued', 'running', 'cancelling', 'cancelled'}:
                    monthly.cancel(identity, status['attempt_id'])
                return 'cancelled', None, {'experiment_id': identity, 'attempt_id': status['attempt_id']}
            return 'cancelled', None, {'experiment_id': None}
        existing = self._monthly_effect(job, step)
        if not existing:
            if self._source(body['request']['source']) != body['source']:
                raise JobServiceError('Frozen domain source changed', code='SOURCE_INTEGRITY')
            if _elapsed(job['created_at']) >= job['budget']['max_seconds']:
                return 'exhausted', _error('BUDGET_EXHAUSTED', 'Original domain wall deadline elapsed.'), {}
        if action == 'validate':
            if job['source_kind'] == 'author_study_diagnostic':
                result = body['source']['study']['result']
                insufficient = result['summary']['status'] == 'screen_blocked'
                return 'blocked', _error('DATA_INSUFFICIENT' if insufficient else 'AUTHOR_METHOD_UNRESOLVED',
                    'Predeclared sample threshold failed.' if insufficient else 'Counts passed; author portfolio method/universe/holding labels remain unexecuted.'), {
                    'study_id': job['source']['study_id'], 'study_digest': job['source']['study_digest'],
                    'diagnostic_json': _bounded(result, MAX_BODY // 2), 'execution_ready': False,
                    'verification_scope': 'aggregate_consistency_only', 'raw_source_reverified': False}
            protocol = body['source']['protocol']
            if protocol['config']['mode'] != 'project':
                return 'blocked', _error('RULES_UNRESOLVED', 'Selected paper method is unresolved; no monthly experiment was created.'), {'unresolved': protocol['unresolved']}
            return 'validated', None, {'protocol_id': protocol['id'], 'protocol_digest': protocol['digest'], 'data_kind': 'controlled_fixture',
                                      'paper_unresolved': protocol['unresolved'], 'method_scope': 'Explicit project conventions; paper ambiguities remain unresolved.'}
        if action == 'submit':
            source = job['source']
            value = existing or monthly.create(source['protocol_id'], source['protocol_digest'], source['config'], step['effect_key'])
            return 'submitted', None, value
        if action == 'observe':
            status = monthly.status(job['experiment_id'])
            if status['attempt_count'] > 1 or (job['attempt_id'] and status['attempt_id'] != job['attempt_id']):
                raise JobServiceError('Observed experiment changed its original attempt', code='ATTEMPT_CHANGED')
            output = {'experiment_id': job['experiment_id'], 'attempt_id': status['attempt_id'], 'experiment_status': status['status']}
            if status['status'] == 'completed':
                return 'observed', None, output
            if status['status'] in {'failed', 'cancelled', 'interrupted'}:
                return 'failed', _error('EXECUTION_' + status['status'].upper(), 'Original monthly attempt stopped; no retry is authorized.'), output
            return 'submitted', None, output
        if action == 'complete':
            value = monthly.get(job['experiment_id'])
            if value['experiment']['attempt_id'] != job['attempt_id'] or value['experiment']['attempt_count'] != 1:
                raise JobServiceError('Completed result has another attempt', code='ATTEMPT_CHANGED')
            if value['experiment']['status'] != 'completed' or not value['verification']['verified'] or value['result'] is None:
                return 'blocked', _error('RESULT_UNVERIFIED', 'Original monthly result cannot be verified.'), {'experiment_id': job['experiment_id']}
            if value['result']['status'] not in {'evaluated', 'partial'}:
                return 'blocked', _error('RESULT_NOT_EVALUABLE', 'No usable controlled fixture portfolio result.'), {'experiment_id': job['experiment_id']}
            # Exact result only; mutable new reviews/reports are not silently
            # pulled into the finished job. Existing controls snapshot them.
            exact = {k: value[k] for k in ('experiment', 'protocol', 'result', 'verification', 'review_target')}
            return 'completed', None, {'experiment_id': job['experiment_id'], 'attempt_id': job['attempt_id'],
                                      'result_json': _bounded(exact, MAX_RESULT // 2), 'result_digest': digest(exact)}
        raise JobServiceError('Action is not authorized', 422, code='ACTION_DENIED')

    def _save_step(self, connection, identity, step):
        payload = _bounded(step, MAX_STEP)
        connection.execute('UPDATE domain_research_job_steps SET payload=?,digest=? WHERE id=?', (payload, digest(step), step['id']))
        values = [r[0] for r in connection.execute('SELECT digest FROM domain_research_job_steps WHERE job_id=? ORDER BY sequence', (identity,))]
        connection.execute('UPDATE domain_research_jobs SET charged_count=?,ledger_digest=? WHERE id=?', (len(values), digest(values), identity))

    def _finish_step(self, identity, job, body, step):
        started = time.monotonic()
        try:
            state, error, output = self._dispatch(job, body, step)
        except sqlite3.OperationalError as exc:
            if step['action'] in {'submit', 'cancel'}:
                raise JobServiceError('Monthly effect response uncertain; reconcile original key.', 503, code='EFFECT_UNCERTAIN', retryable=True) from exc
            state, error, output = job['state'], _error('STORE_BUSY', 'Store unavailable; bounded new action may retry.', True), {}
        except (ServiceError, ValueError, OSError) as exc:
            state, error, output = 'blocked', _error(getattr(exc, 'code', 'DOMAIN_INVALID'), self.store._safe_error(exc)), {}
        if not error and step['action'] != 'cancel' and _elapsed(job['created_at']) >= job['budget']['max_seconds']:
            state, error = 'exhausted', _error('BUDGET_EXHAUSTED', 'Original effect receipt retained; total wall deadline elapsed.')
        step = {**step, 'status': 'failed' if error else 'completed', 'finished_at': now(),
                'elapsed_seconds': step['elapsed_seconds'] + max(0.0, time.monotonic() - started),
                'next_state': state, 'error': error, 'output_json': _bounded(output, MAX_RESULT)}
        with transaction(self.store.db_path) as connection:
            self._load(connection, identity)
            previous = connection.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM domain_research_job_steps WHERE job_id=? AND id<>?', (identity, step['id'])).fetchone()[0]
            if previous + len(json_text(step).encode()) + max(0, job['budget']['max_steps'] - step['sequence']) * 8192 > MAX_LEDGER:
                step.update(status='failed', next_state='blocked', error=_error('LEDGER_TOO_LARGE', 'Original ledger storage bound exhausted.'), output_json=None)
            self._save_step(connection, identity, step)
        return step

    def advance(self, identity, action, idempotency_key):
        _identity(identity)
        try:
            DomainJobAdvance.model_validate({'action': action, 'idempotency_key': idempotency_key})
            if not idempotency_key.strip():
                raise ValueError('Blank key')
        except (ValueError, TypeError) as exc:
            raise JobServiceError('Invalid closed domain action', 422, code='ARGUMENTS_INVALID') from exc
        fingerprint = digest({'action': action})
        with self._lock(identity):
            job = self.get(identity)
            with closing(connect(self.store.db_path)) as connection:
                body = self._body(connection, identity)
            for running in [s for s in job['steps'] if s['status'] == 'running']:
                if action == 'cancel' and running['action'] != 'cancel' and not self._monthly_effect(job, running):
                    stopped = {**running, 'status': 'interrupted', 'finished_at': now(),
                               'error': _error('CANCELLED_BEFORE_EFFECT', 'Pending transition stopped without creating its missing effect.'), 'output_json': json_text({})}
                    with transaction(self.store.db_path) as connection:
                        self._load(connection, identity)
                        self._save_step(connection, identity, stopped)
                else:
                    self._finish_step(identity, job, body, running)
                job = self.get(identity)
            with transaction(self.store.db_path) as connection:
                job = self._load(connection, identity)
                prior = next((s for s in job['steps'] if s['idempotency_key'] == idempotency_key), None)
                if prior:
                    if prior['request_digest'] != fingerprint:
                        raise JobServiceError('Transition key has another action', code='IDEMPOTENCY_CONFLICT')
                    return prior
                if action == 'cancel' and len(job['steps']) >= job['budget']['max_steps'] + 1:
                    raise JobServiceError('One safety-stop receipt is already reserved; replay its key.', 429, code='STOP_BUDGET_EXHAUSTED')
                if job['state'] in TERMINAL and not (action == 'cancel' and job['state'] not in {'completed', 'cancelled'}):
                    raise JobServiceError('Domain job is closed', 429 if job['state'] == 'exhausted' else 409, code='JOB_' + job['state'].upper())
                if action != 'cancel' and NEXT.get(action) != job['state']:
                    raise JobServiceError('Action does not match durable state', code='ACTION_NOT_ALLOWED')
                step_id = 'domain_step_' + digest({'job_id': identity, 'key': idempotency_key})
                step = {'id': step_id, 'sequence': len(job['steps']) + 1, 'action': action, 'idempotency_key': idempotency_key,
                        'request_digest': fingerprint, 'effect_key': _effect(identity, action), 'status': 'running', 'started_at': now(),
                        'finished_at': None, 'elapsed_seconds': 0.0, 'next_state': job['state'], 'error': None, 'output_json': None}
                connection.execute('INSERT INTO domain_research_job_steps VALUES(?,?,?,?,?,?,?)',
                                   (step_id, identity, step['sequence'], idempotency_key, fingerprint, json_text(step), digest(step)))
                self._save_step(connection, identity, step)
            return self._finish_step(identity, job, body, step)

    def cancel(self, identity, idempotency_key):
        return self.advance(identity, 'cancel', idempotency_key)

    def markdown(self, identity):
        job = self.get(identity)
        end = job['steps'][-1]['finished_at'] if job['steps'] else job['created_at']
        if end is None:
            raise JobServiceError('Reconcile running transition before export', code='JOB_BUSY', retryable=True)
        if job['state'] not in TERMINAL:
            raise JobServiceError('Immutable export requires terminal job', code='EXPORT_NOT_FROZEN')
        job['usage']['elapsed_seconds'] = max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(job['created_at'])).total_seconds())
        if job['state'] == 'exhausted' and job['usage']['steps'] < job['budget']['max_steps'] and job['usage']['failures'] < job['budget']['max_failures']:
            job['usage']['elapsed_seconds'] = float(job['budget']['max_seconds'])
        return '# Bounded domain research execution\n\nProvider-free; controlled fixtures / aggregate diagnostics only. Semantic fidelity is unverified.\n\n```json\n' + json_text(job) + '\n```\n'
