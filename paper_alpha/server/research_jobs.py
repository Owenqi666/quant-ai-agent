"""Durable provider-free research orchestration using the existing Store/worker.

Only application-created jobs authorize effects. Every bounded transition and
its Store mutation key are committed before dispatch. The same transition key
recovers its original effect; an uncertain response never creates a new key.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
import sqlite3
import stat
import time

from .db import connect, transaction
from .service import ServiceError, now
from .research_jobs_schema import (ResearchJobCreate, ResearchJob, ResearchJobSummary,
                                   ResearchJobAdvance, JobStep)
from ..contracts import Task
from ..evidence import sha256, match_evidence, verify_paper
from ..evaluation import FIELDS
from ..expressions import (validate_expression, repair_expression, ExpressionError, OPERATOR_SEMANTICS_VERSION,
                           _FUNCTION_ARITY, _ALIASES)
from ..storage import digest, json_text
from ..workflow import verify_candidate_origin

SCHEMA = """
CREATE TABLE research_jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,research_id TEXT NOT NULL REFERENCES researches(id),charged_count INTEGER NOT NULL,ledger_digest TEXT NOT NULL,submit_effect_key TEXT NOT NULL UNIQUE);
CREATE INDEX research_jobs_created ON research_jobs(created_at,id);
CREATE TABLE research_job_steps(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES research_jobs(id),sequence INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(job_id,sequence),UNIQUE(job_id,idempotency_key));
"""
MAX_BODY_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 128 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
TERMINAL = {'completed', 'blocked', 'failed', 'cancelled', 'exhausted'}
NEXT = {'validate': 'created', 'commit': 'validated', 'submit': 'committed',
        'observe': 'submitted', 'complete': 'observed'}


class JobServiceError(ServiceError):
    def __init__(self, message, status=409, *, code, retryable=False):
        super().__init__(message, status)
        self.code, self.retryable = code, retryable


def _error(code, message, retryable=False):
    return {'code': code, 'message': message, 'retryable': retryable}


def _bounded(value, maximum):
    text = json_text(value)
    if len(text.encode('utf-8')) > maximum:
        raise JobServiceError('Research job payload exceeds its byte bound', 413, code='PAYLOAD_TOO_LARGE')
    return text


def _parse(text, maximum):
    def constant(_):
        raise ValueError('Nonfinite stored number')
    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError('Duplicate stored key')
            out[key] = value
        return out
    value = json.loads(text, parse_constant=constant, object_pairs_hook=pairs)
    if _bounded(value, maximum) != text:
        raise ValueError('Stored JSON is not canonical')
    return value


def _identity(identity):
    import re
    if not isinstance(identity, str) or not re.fullmatch('research_job_[0-9a-f]{64}', identity):
        raise JobServiceError('Invalid research job identity', 422, code='ARGUMENTS_INVALID')


def _wall_seconds(created_at):
    return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds())


def execution_remaining_seconds(store, run_id):
    """Worker-only cap, including a submitted effect not yet acknowledged in its job.

Uses the saved submit effect key rather than trusting a post-effect job update.
None means an ordinary historic/application run, unaffected by this job budget.
"""
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        run = connection.execute('SELECT idempotency_key FROM runs WHERE id=?', (run_id,)).fetchone()
        if run is None:
            return None
        row = connection.execute('SELECT id FROM research_jobs WHERE submit_effect_key=?', (run['idempotency_key'],)).fetchone()
        if row is None:
            if run['idempotency_key'].startswith('job-effect-'):
                raise JobServiceError('Owned run lost its research job binding', code='LEDGER_INTEGRITY')
            return None
        job = ResearchJobs(store)._load(connection, row['id'])
        submit = [step for step in job['steps'] if step['action'] == 'submit' and step['effect_key'] == run['idempotency_key']]
        if not submit:
            raise JobServiceError('Owned run has no durably charged submit transition', code='LEDGER_INTEGRITY')
        return max(0.0, job['budget']['max_seconds'] - _wall_seconds(job['created_at']))


class ResearchJobs:
    def __init__(self, store):
        self.store = store

    def _source(self, research_id, revision_id):
        research = self.store._fetch('researches', research_id)
        revision = self.store._revision(self.store._fetch('revisions', revision_id))
        if revision['research_id'] != research_id or digest(revision['task']) != revision['digest']:
            raise JobServiceError('Base revision is not a verified member of this research', code='SOURCE_INTEGRITY')
        paper = self.store._fetch('papers', research['paper_id'])
        dataset = self.store._fetch('datasets', research['dataset_id'])
        source = {'research': {key: research[key] for key in ('id', 'title', 'paper_id', 'dataset_id')},
                  'revision': revision, 'paper_sha256': paper['sha256'],
                  'paper_document_digest': digest(json.loads(paper['document'])),
                  'dataset_sha256': dataset['sha256'], 'dataset_metadata_digest': digest(json.loads(dataset['metadata'])),
                  'data_file_sha256': sha256(dataset['data_path']), 'metadata_file_sha256': sha256(dataset['metadata_path'])}
        if sha256(paper['pdf_path']) != paper['sha256']:
            raise JobServiceError('Frozen paper PDF changed', code='SOURCE_INTEGRITY')
        return source

    def _check_source(self, body):
        request = body['request']
        if self._source(request['research_id'], request['base_revision_id']) != body['source']:
            raise JobServiceError('Frozen research source changed', code='SOURCE_INTEGRITY')

    def _context(self, source):
        base = source['revision']['task']
        evidence = [{**item, 'quote': item['quote'][:2000]} for item in base['evidence'][:20]]
        candidates = deepcopy(base['candidates'][:20])
        assessments = self.store._read('SELECT r.id,r.run_id,r.candidate_id,r.verdict,r.source,r.assessment '
                                      'FROM reviews r WHERE r.revision_id=? ORDER BY r.created_at DESC LIMIT 10',
                                      (source['revision']['id'],))
        for item in assessments:
            item['assessment'] = json.loads(item['assessment']) if item['assessment'] else None
        value = {'source_digest': digest(source), 'paper_id': source['research']['paper_id'],
                 'dataset_id': source['research']['dataset_id'], 'base_revision_id': source['revision']['id'],
                 'fields': sorted(set(FIELDS) | {'returns'}), 'operator_semantics_version': OPERATOR_SEMANTICS_VERSION,
                 'operators': [{'name': name, 'arity': arity} for name, arity in sorted(_FUNCTION_ARITY.items())],
                 'documented_aliases': _ALIASES,
                 'operator_scope': 'Existing AST registry only; aliases may normalize. No field/window substitution.',
                 'evidence': evidence, 'base_candidates': candidates, 'assessments': assessments,
                 'evaluation': base['evaluation'], 'engine_budget': base['budget'],
                 'omissions': {'raw_market_rows': 'All market data omitted, including reserved final test.',
                               'full_paper': 'Only bounded existing evidence snippets are exposed.',
                               'evidence_count': max(0, len(base['evidence']) - len(evidence)),
                               'quotes_over_2000_characters': sum(len(e['quote']) > 2000 for e in base['evidence'][:20]),
                               'candidate_count': max(0, len(base['candidates']) - len(candidates)),
                               'review_limit': 10},
                 'semantic_fidelity': 'unverified', 'provider_connected': False}
        # Large assessments are explicitly omitted rather than silently overflowing.
        if len(json_text(value).encode('utf-8')) > MAX_CONTEXT_BYTES:
            value['assessments'] = []
            value['omissions']['assessments'] = 'Bounded projection exceeded 128 KiB; inspect exact source review separately.'
        return _bounded(value, MAX_CONTEXT_BYTES)

    def create(self, research_id, base_revision_id, draft, budget, idempotency_key, note='',
               allow_partial_execution=False, method_status='resolved', unresolved_rules=None):
        try:
            request = ResearchJobCreate.model_validate(dict(research_id=research_id, base_revision_id=base_revision_id,
                draft=draft, budget=budget, idempotency_key=idempotency_key, note=note,
                allow_partial_execution=allow_partial_execution, method_status=method_status,
                unresolved_rules=unresolved_rules or [])).model_dump()
            if not idempotency_key.strip():
                raise ValueError('Blank key')
            _bounded(request, MAX_BODY_BYTES // 2)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise JobServiceError('Invalid bounded research job request', 422, code='ARGUMENTS_INVALID') from exc
        fingerprint = digest(request)
        with closing(connect(self.store.db_path)) as connection:
            prior = connection.execute('SELECT id,request_digest FROM research_jobs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if prior:
                if prior['request_digest'] != fingerprint:
                    raise JobServiceError('Job key has another request', code='IDEMPOTENCY_CONFLICT')
                return self._load(connection, prior['id'])
        source = self._source(research_id, base_revision_id)
        if self.store._fetch('researches', research_id)['latest_revision_id'] != base_revision_id:
            raise JobServiceError('Job must begin from the current revision', code='BASE_REVISION_CHANGED')
        body = {'request': request, 'source': source, 'context_json': self._context(source)}
        payload = _bounded(body, MAX_BODY_BYTES)
        identity, created = 'research_job_' + fingerprint, now()
        with transaction(self.store.db_path) as connection:
            prior = connection.execute('SELECT id,request_digest FROM research_jobs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if prior:
                if prior['request_digest'] != fingerprint:
                    raise JobServiceError('Job key has another request', code='IDEMPOTENCY_CONFLICT')
                return self._load(connection, prior['id'])
            connection.execute('INSERT INTO research_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identity, payload, digest(body), created, digest({'id': identity, 'created_at': created, 'digest': digest(body)}),
                 idempotency_key, fingerprint, research_id, 0, digest([]),
                 'job-effect-' + digest({'job_id': identity, 'action': 'submit'})))
            return self._load(connection, identity)

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM research_jobs WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise JobServiceError('Research job not found', 404, code='JOB_NOT_FOUND')
        try:
            body = _parse(row['payload'], MAX_BODY_BYTES)
            request = ResearchJobCreate.model_validate(body['request']).model_dump()
            if (digest(request) != row['request_digest'] or row['id'] != 'research_job_' + digest(request)
                    or digest(body) != row['digest'] or row['research_id'] != request['research_id']
                    or row['idempotency_key'] != request['idempotency_key']
                    or row['submit_effect_key'] != 'job-effect-' + digest({'job_id': identity, 'action': 'submit'})
                    or row['metadata_digest'] != digest({'id': identity, 'created_at': row['created_at'], 'digest': row['digest']})):
                raise ValueError('Job metadata differs')
            rows = connection.execute('SELECT * FROM research_job_steps WHERE job_id=? ORDER BY sequence', (identity,)).fetchall()
            if (len(rows) != row['charged_count'] or row['ledger_digest'] != digest([r['digest'] for r in rows])
                    or sum(len(r['payload'].encode('utf-8')) for r in rows) > MAX_LEDGER_BYTES):
                raise ValueError('Ledger anchor differs')
            steps = []
            for index, item in enumerate(rows, 1):
                step = JobStep.model_validate(_parse(item['payload'], MAX_BODY_BYTES)).model_dump()
                if (step['sequence'] != index or item['sequence'] != index or step['id'] != item['id']
                        or item['digest'] != digest(step) or item['request_digest'] != step['request_digest']
                        or step['request_digest'] != digest({'action': step['action']})
                        or step['idempotency_key'] != item['idempotency_key']
                        or step['id'] != 'job_step_' + digest({'job_id': identity, 'key': item['idempotency_key']})
                        or step['effect_key'] != 'job-effect-' + digest({'job_id': identity, 'action': step['action']})):
                    raise ValueError('Step metadata differs')
                if (step['status'] == 'running') != (step['finished_at'] is None):
                    raise ValueError('Step completion differs')
                steps.append(step)
            # One safety cancellation receipt is allowed after budget closure;
            # it authorizes stopping existing work, never another computation.
            if len(steps) > request['budget']['max_steps'] and not (len(steps) == request['budget']['max_steps'] + 1 and steps[-1]['action'] == 'cancel'):
                raise ValueError('Step budget changed')
            state, reason, revision_id, run_id, checks, result_json = 'created', None, None, None, [], None
            for step in steps:
                if step['status'] != 'running':
                    state = step['next_state']
                    if step['error'] and state in TERMINAL:
                        reason = step['error']['code']
                    output = json.loads(step['output_json']) if step['output_json'] else {}
                    revision_id = output.get('revision_id', revision_id)
                    run_id = output.get('run_id', run_id)
                    checks = output.get('candidate_checks', checks)
                    result_json = output.get('result_json', result_json)
            failures = sum(s['status'] in {'failed', 'interrupted'} for s in steps)
            elapsed = _wall_seconds(row['created_at'])
            if state not in TERMINAL and not any(s['status'] == 'running' for s in steps) and (
                    len(steps) >= request['budget']['max_steps'] or failures >= request['budget']['max_failures']
                    or elapsed >= request['budget']['max_seconds']):
                state, reason = 'exhausted', 'BUDGET_EXHAUSTED'
            value = {'id': identity, 'digest': row['digest'], 'created_at': row['created_at'],
                     'research_id': request['research_id'], 'base_revision_id': request['base_revision_id'],
                     'paper_id': body['source']['research']['paper_id'], 'dataset_id': body['source']['research']['dataset_id'],
                     'source_digest': digest(body['source']), 'budget': request['budget'],
                     'usage': {'steps': len(steps), 'failures': failures, 'elapsed_seconds': elapsed},
                     'state': state, 'stop_reason': reason, 'revision_id': revision_id, 'run_id': run_id,
                     'provider_connected': False, 'semantic_fidelity': 'unverified', 'draft': request['draft'],
                     'allow_partial_execution': request['allow_partial_execution'], 'context_json': body['context_json'],
                     'candidate_checks': checks, 'steps': steps, 'result_json': result_json}
            ResearchJob.model_validate(value)
            return value
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise JobServiceError('Research job ledger failed integrity verification', code='LEDGER_INTEGRITY') from exc

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._load(connection, identity)

    def list(self, limit=20, offset=0, research_id=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise JobServiceError('Invalid job page', 422, code='ARGUMENTS_INVALID')
        where, args = (' WHERE research_id=?', (research_id,)) if research_id else ('', ())
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            count = connection.execute('SELECT COUNT(*) FROM research_jobs' + where, args).fetchone()[0]
            rows = connection.execute('SELECT id FROM research_jobs' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
            fields = set(ResearchJobSummary.model_fields)
            return {'items': [{k: v for k, v in self._load(connection, r['id']).items() if k in fields} for r in rows],
                    'total': count, 'limit': limit, 'offset': offset}

    @contextmanager
    def _lock(self, identity):
        descriptor = os.open(self.store.root / ('.' + identity + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise JobServiceError('Job lock is not regular', code='LEDGER_INTEGRITY')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobServiceError('Job has an active transition; replay the same key', code='JOB_BUSY', retryable=True) from exc
            yield
        finally:
            os.close(descriptor)

    def _body(self, identity):
        row = self.store._fetch('research_jobs', identity)
        return _parse(row['payload'], MAX_BODY_BYTES)

    def _validate(self, body):
        request, source = body['request'], body['source']
        if request['method_status'] != 'resolved' or request['unresolved_rules']:
            return 'blocked', _error('RULES_UNRESOLVED', 'Resolve declared method rules before executing this draft.'), {}
        base = source['revision']['task']
        task = self.store._task({**request['draft'], 'evaluation': base['evaluation'], 'budget': base['budget']},
                                source['research']['title'], source['research']['dataset_id'])
        parsed = Task.parse({**task, 'schema_version': 1, 'title': source['research']['title'],
                             'paper': 'paper.json', 'paper_pdf': 'paper.pdf', 'data': 'market.csv', 'data_metadata': 'metadata.json'})
        paper_row = self.store._fetch('papers', source['research']['paper_id'])
        paper = json.loads(paper_row['document'])
        verify_paper(paper, paper_row['pdf_path'])
        for item in parsed.evidence:
            match_evidence(item, paper)
        evidence = {e.id: e for e in parsed.evidence}
        hypotheses = {h.id: h for h in parsed.hypotheses}
        base_hypotheses = {h['id']: h for h in base['hypotheses']}
        for hypothesis in parsed.hypotheses:
            prior = base_hypotheses.get(hypothesis.id)
            if hypothesis.attribution == 'paper_original':
                fields = ('claim', 'evidence_ids', 'required_fields', 'signal_direction', 'assumptions')
                if not prior or prior['attribution'] != 'paper_original' or any(getattr(hypothesis, field) != prior[field] for field in fields):
                    raise JobServiceError('New or changed original-paper assertions must first be maintained explicitly in a base revision; automated changes declare modification/conjecture.', 422, code='ATTRIBUTION_INVALID')
            if hypothesis.mechanism_attribution == 'paper_original' and (not prior or prior['mechanism_attribution'] != 'paper_original'
                    or hypothesis.economic_mechanism != prior['economic_mechanism']):
                raise JobServiceError('A new or changed mechanism cannot inherit paper-original attribution', 422, code='ATTRIBUTION_INVALID')
        fields = set(FIELDS) | {'returns'}
        checks, accepted = [], []
        for candidate in parsed.candidates:
            check = {'candidate_id': candidate.id, 'status': 'ready', 'code': 'VALIDATED', 'message': 'Static checks only; semantic review remains unverified.',
                     'submitted_expression': candidate.expression, 'accepted_expression': candidate.expression, 'normalization': 'unchanged'}
            try:
                hypothesis = hypotheses[candidate.hypothesis_id]
                verify_candidate_origin(candidate, hypothesis, evidence)
                expression = candidate.expression
                try:
                    validated = validate_expression(expression, fields)
                except ExpressionError as exc:
                    normalized = repair_expression(expression, exc)
                    if not normalized:
                        raise
                    validated = validate_expression(normalized, fields)
                    expression = validated['canonical_expression']
                    check.update(accepted_expression=expression, normalization='documented_operator_alias')
                missing = sorted(set(hypothesis.required_fields) - fields)
                if missing:
                    raise ExpressionError('Required fields are unavailable: ' + ', '.join(missing), 'required_field_missing')
                undeclared = sorted(set(validated['used_fields']) - set(hypothesis.required_fields))
                if undeclared:
                    raise ExpressionError('Expression fields absent from declared hypothesis requirements: ' + ', '.join(undeclared), 'undeclared_field')
                accepted.append({**task['candidates'][len(checks)], 'expression': expression})
            except (ValueError, SyntaxError) as exc:
                check.update(status='blocked', code=getattr(exc, 'code', 'ATTRIBUTION_INVALID'), message=str(exc)[:2000],
                             accepted_expression=None, normalization='excluded')
            checks.append(check)
        if not accepted or (len(accepted) != len(checks) and not request['allow_partial_execution']):
            return 'blocked', _error('CANDIDATES_BLOCKED', 'No authorized complete legal candidate set; retained diagnostics require explicit correction or partial authorization.'), {'candidate_checks': checks}
        task['candidates'] = accepted
        from .preflight import inspect_task
        metadata = json.loads(self.store._fetch('datasets', source['research']['dataset_id'])['metadata'])
        preflight = inspect_task(task, metadata)
        if any(item['status'] == 'blocked' for item in preflight['candidates']):
            return 'blocked', _error('PREFLIGHT_BLOCKED', 'Candidate has no usable validation rows; no window substitution performed.'), {'candidate_checks': checks, 'preflight': preflight}
        from .research_guard import ResearchGuard
        guard = ResearchGuard(self.store).check(source['research']['dataset_id'], task)
        if not guard['allowed']:
            return 'blocked', _error('TEMPORAL_GUARD', guard['reason']), {'candidate_checks': checks, 'guard': guard}
        return 'validated', None, {'task': task, 'candidate_checks': checks, 'preflight': preflight,
                                   'attribution_scope': 'Exact formula and declarations checked; economic/prose semantic fidelity unverified.'}

    def _validated_task(self, job):
        step = next(s for s in job['steps'] if s['action'] == 'validate' and s['status'] == 'completed')
        return json.loads(step['output_json'])['task']

    def _effect_exists(self, step):
        if step['action'] == 'commit':
            return bool(self.store._read("SELECT 1 FROM workflow_mutation_receipts WHERE operation='revision.create' AND idempotency_key=?", (step['effect_key'],)))
        if step['action'] == 'submit':
            return bool(self.store._read('SELECT 1 FROM runs WHERE idempotency_key=?', (step['effect_key'],)))
        return False

    def _dispatch(self, job, body, step, *, recovery=False):
        action = step['action']
        if action == 'cancel':
            if job['run_id']:
                status = self.store.get_run_status(job['run_id'])['status']
                if status in {'queued', 'running', 'cancelling', 'cancelled'}:
                    self.store.cancel_run(job['run_id'])
            return 'cancelled', None, {'run_id': job['run_id'], 'reason': 'Explicit application/user cancellation.'}
        existing = self._effect_exists(step)
        if not existing:
            self._check_source(body)
            if _wall_seconds(job['created_at']) >= job['budget']['max_seconds']:
                return 'exhausted', _error('BUDGET_EXHAUSTED', 'Original wall-clock job deadline elapsed; no new effects authorized.'), {}
        if action == 'validate':
            return self._validate(body)
        if action == 'commit':
            revision = self.store.create_revision(job['research_id'], job['base_revision_id'], self._validated_task(job),
                                                  body['request']['note'], step['effect_key'])
            return 'committed', None, {'revision_id': revision['id'], 'revision_digest': revision['digest']}
        if action == 'submit':
            run = self.store.submit_run(job['revision_id'], 'normalized_fixed', step['effect_key'])
            return 'submitted', None, {'run_id': run['id'], 'revision_id': run['revision_id'], 'mode': run['mode']}
        if action == 'observe':
            run = self.store.get_run_status(job['run_id'])
            if run['status'] == 'completed':
                return 'observed', None, {'run_id': run['id'], 'run_status': run['status'], 'attempt_id': run['attempt_id']}
            if run['status'] in {'failed', 'interrupted', 'cancelled'}:
                return 'failed', _error('EXECUTION_' + run['status'].upper(), 'Actual worker execution stopped; no automatic new attempt authorized.'), {'run_id': run['id'], 'run_status': run['status']}
            return 'submitted', None, {'run_id': run['id'], 'run_status': run['status'], 'integrity_checked': False}
        if action == 'complete':
            run = self.store.get_run(job['run_id'])
            if run['status'] != 'completed' or not (run['verification'] or {}).get('verified'):
                return 'blocked', _error('RESULT_UNVERIFIED', 'Exact run result failed integrity verification.'), {'run_id': run['id']}
            if not any(item['status'] == 'evaluated' for item in run['state']['candidates']):
                return 'blocked', _error('RESULT_NOT_EVALUABLE', 'No candidate produced an actual evaluated result.'), {'run_id': run['id']}
            return 'completed', None, {'run_id': run['id'], 'result_json': _bounded(run, MAX_BODY_BYTES // 2),
                                       'result_digest': digest(run), 'semantic_fidelity': 'unverified'}
        raise JobServiceError('Action is not exposed', 422, code='ACTION_DENIED')

    def _save_step(self, connection, identity, step):
        payload = _bounded(step, MAX_BODY_BYTES)
        connection.execute('UPDATE research_job_steps SET payload=?,digest=? WHERE id=?', (payload, digest(step), step['id']))
        values = [r[0] for r in connection.execute('SELECT digest FROM research_job_steps WHERE job_id=? ORDER BY sequence', (identity,))]
        connection.execute('UPDATE research_jobs SET charged_count=?,ledger_digest=? WHERE id=?', (len(values), digest(values), identity))

    def _finish_step(self, identity, job, body, step, recovery=False):
        started = time.monotonic()
        try:
            state, error, output = self._dispatch(job, body, step, recovery=recovery)
        except sqlite3.OperationalError:
            if step['action'] in {'commit', 'submit', 'cancel'}:
                # The Store may have committed and lost its acknowledgement.
                # Keep the charged original effect pending until reconciled.
                raise JobServiceError('Store effect completion is uncertain; replay the original transition key.',
                                      503, code='EFFECT_UNCERTAIN', retryable=True)
            state, error, output = job['state'], _error('STORE_BUSY', 'Store temporarily unavailable; replay effects with the original key.', True), {}
        except (ServiceError, ValueError, OSError) as exc:
            state, error, output = 'blocked', _error(getattr(exc, 'code', 'DRAFT_INVALID'), self.store._safe_error(exc), False), {}
        # A Store mutation can finish beyond deadline. Record its exact identity
        # before stopping future work, rather than concealing a committed effect.
        if not error and step['action'] != 'cancel' and _wall_seconds(job['created_at']) >= job['budget']['max_seconds']:
            state, error = 'exhausted', _error('BUDGET_EXHAUSTED', 'Effect receipt retained; original wall-clock deadline elapsed.')
        step = {**step, 'status': 'failed' if error else 'completed', 'finished_at': now(),
                'elapsed_seconds': step['elapsed_seconds'] + max(0.0, time.monotonic() - started),
                'next_state': state, 'error': error, 'output_json': _bounded(output, MAX_BODY_BYTES // 2)}
        with transaction(self.store.db_path) as connection:
            self._load(connection, identity)
            previous = connection.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM research_job_steps WHERE job_id=? AND id<>?', (identity, step['id'])).fetchone()[0]
            if previous + len(json_text(step).encode('utf-8')) + (job['budget']['max_steps'] - step['sequence']) * 8192 > MAX_LEDGER_BYTES:
                step.update(status='failed', next_state='blocked', error=_error('LEDGER_TOO_LARGE', 'Job storage budget exhausted.'), output_json=None)
            self._save_step(connection, identity, step)
        return step

    def advance(self, identity, action, idempotency_key):
        _identity(identity)
        try:
            request = ResearchJobAdvance.model_validate({'action': action, 'idempotency_key': idempotency_key}).model_dump()
            if not idempotency_key.strip():
                raise ValueError('Blank key')
        except (ValueError, TypeError) as exc:
            raise JobServiceError('Invalid closed job action', 422, code='ARGUMENTS_INVALID') from exc
        fingerprint = digest({'action': action})
        with self._lock(identity):
            job, body = self.get(identity), self._body(identity)
            for running in [s for s in job['steps'] if s['status'] == 'running']:
                # Any caller first reconciles the original effect; never start
                # another effect under a fresh key while completion is unknown.
                if action == 'cancel' and running['action'] != 'cancel' and not self._effect_exists(running):
                    # A cancellation must not dispatch an effect that never
                    # happened merely to reconcile an abandoned request.
                    stopped = {**running, 'status': 'interrupted', 'finished_at': now(),
                               'error': _error('CANCELLED_BEFORE_EFFECT', 'Explicit cancellation interrupted the pending transition without creating its effect.'),
                               'output_json': json_text({})}
                    with transaction(self.store.db_path) as connection:
                        self._load(connection, identity)
                        self._save_step(connection, identity, stopped)
                else:
                    self._finish_step(identity, job, body, running, recovery=True)
                job = self.get(identity)
            with transaction(self.store.db_path) as connection:
                job = self._load(connection, identity)
                prior = next((s for s in job['steps'] if s['idempotency_key'] == idempotency_key), None)
                if prior:
                    if prior['request_digest'] != fingerprint:
                        raise JobServiceError('Transition key has another action', code='IDEMPOTENCY_CONFLICT')
                    return prior
                if action == 'cancel' and len(job['steps']) >= job['budget']['max_steps'] + 1:
                    raise JobServiceError('The one safety cancellation receipt is already reserved; replay its original key.',
                                          429, code='STOP_BUDGET_EXHAUSTED')
                if job['state'] in TERMINAL and not (action == 'cancel' and job['state'] not in {'completed', 'cancelled'}):
                    raise JobServiceError('Research job is closed', 429 if job['state'] == 'exhausted' else 409, code='JOB_' + job['state'].upper())
                if action != 'cancel' and NEXT.get(action) != job['state']:
                    raise JobServiceError('Action does not match durable job state', code='ACTION_NOT_ALLOWED')
                step_id = 'job_step_' + digest({'job_id': identity, 'key': idempotency_key})
                step = {'id': step_id, 'sequence': len(job['steps']) + 1, 'action': action,
                        'idempotency_key': idempotency_key, 'request_digest': fingerprint,
                        'effect_key': 'job-effect-' + digest({'job_id': identity, 'action': action}),
                        'status': 'running', 'started_at': now(), 'finished_at': None, 'elapsed_seconds': 0.0,
                        'next_state': job['state'], 'error': None, 'output_json': None}
                connection.execute('INSERT INTO research_job_steps VALUES(?,?,?,?,?,?,?)',
                    (step_id, identity, step['sequence'], idempotency_key, fingerprint, json_text(step), digest(step)))
                self._save_step(connection, identity, step)
            return self._finish_step(identity, job, body, step)

    def cancel(self, identity, idempotency_key):
        return self.advance(identity, 'cancel', idempotency_key)

    def markdown(self, identity):
        job = self.get(identity)
        # Wall-clock usage shown by get/list is live. Exports derive elapsed time
        # exclusively from frozen ledger timestamps so repeated export is stable.
        end = job['steps'][-1]['finished_at'] if job['steps'] else job['created_at']
        if end is None:
            raise JobServiceError('Finish or reconcile the running transition before immutable export', code='JOB_BUSY', retryable=True)
        job['usage']['elapsed_seconds'] = max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(job['created_at'])).total_seconds())
        if job['state'] == 'exhausted' and job['usage']['steps'] < job['budget']['max_steps'] and job['usage']['failures'] < job['budget']['max_failures']:
            job['usage']['elapsed_seconds'] = float(job['budget']['max_seconds'])
        if job['state'] not in TERMINAL:
            raise JobServiceError('Immutable export requires a terminal job', code='EXPORT_NOT_FROZEN')
        return '# Provider-free research execution\n\nAutomation draft; semantic fidelity remains unverified.\n\n```json\n' + json_text(job) + '```\n'
