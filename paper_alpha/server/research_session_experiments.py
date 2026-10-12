"""Explicit, budgeted handoff to the existing ResearchJobs and Store executor.

Intent and effect receipts describe ownership only. Child state is always read
from ResearchJobs/Store, including while recovering a lost acknowledgement.
"""
from __future__ import annotations

from contextlib import closing, nullcontext
from copy import deepcopy
from datetime import timedelta

from ..storage import digest, json_text
from .db import connect, transaction
from .research_sessions import ResearchSessions, SessionError, _parse, _timestamp
from .research_session_experiments_schema import ExperimentCreate, ExperimentCreateV2, parse_experiment_create, SCHEMA
from .research_jobs_schema import ResearchJobCreate

PREFIX = 'session_experiment_'
MAX_BYTES = 1024 * 1024


def _exists(connection):
    present = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_session_experiments'").fetchone())
    if not present:
        version = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        if version and int(version['value']) >= 21:
            raise SessionError('Experiment schema is incomplete', code='EXPERIMENT_INTEGRITY')
    return present


def _json(value):
    result = json_text(value)
    if len(result.encode()) > MAX_BYTES:
        raise SessionError('Experiment provenance exceeds its byte limit', 413, code='PAYLOAD_TOO_LARGE')
    return result


class SessionExperiments:
    def __init__(self, store):
        self.store, self.sessions = store, ResearchSessions(store)

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM research_session_experiments WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise SessionError('Session experiment not found', 404, code='EXPERIMENT_NOT_FOUND')
        try:
            body = _parse(row['payload'], MAX_BYTES)
            request = parse_experiment_create(body['request']).model_dump()
            plan = body['plan']
            loaded = self.sessions._load(connection, row['session_id'])
            reservation = self.sessions._reservation(loaded, row['reservation_token'])
            if (digest(body) != row['digest'] or digest(request) != row['request_digest']
                    or identity != PREFIX + digest(request) or request['session_id'] != row['session_id']
                    or request['idempotency_key'] != row['idempotency_key']
                    or body['created_at'] != row['created_at'] or body['reservation_token'] != reservation['token']
                    or reservation['action'] != 'experiment' or reservation['action_digest'] != digest(request)
                    or reservation['effect_key'] != request['idempotency_key']
                    or body['bootstrap_effect_key'] != row['bootstrap_effect_key']
                    or body['job_effect_key'] != row['job_effect_key']
                    or body['bootstrap_effect_key'] != 'experiment-bootstrap-' + digest(request)
                    or body['job_effect_key'] != 'experiment-job-' + digest(request)
                    or plan['plan_digest'] != digest({k: v for k, v in plan.items() if k != 'plan_digest'})
                    or plan['task_digest'] != digest(plan['task'])
                    or plan['session_id'] != request['session_id']
                    or (request['schema_version'] == 1 and (plan['schema_version'] != 1
                        or plan['proposal_id'] != request['proposal_id']
                        or plan['accepted_response_digest'] != request['accepted_response_digest']))
                    or (request['schema_version'] == 2 and (plan['schema_version'] != 2
                        or plan['candidate_source'] != request['candidate_source']
                        or plan['proposal_id'] != request['candidate_source']['parent_proposal_id']
                        or plan['parent_response_digest'] != request['candidate_source']['parent_response_digest']
                        or plan['task_digest'] != request['candidate_source']['accepted_task_digest']))
                    or not any(e['kind'] == 'experiment_created' and e['operation_key'] == identity
                               and e['operation_digest'] == row['digest'] for e in loaded['events'])):
                raise ValueError('Experiment intent identity differs')
            return {'id': identity, 'row': dict(row), **body, 'request': request,
                    'loaded_session': loaded, 'reservation': reservation}
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, SessionError):
                raise
            raise SessionError('Experiment provenance failed verification', code='EXPERIMENT_INTEGRITY') from exc

    def _effect(self, connection, intent, kind):
        row = connection.execute('SELECT * FROM research_session_experiment_effects WHERE experiment_id=? AND kind=?',
                                 (intent['id'], kind)).fetchone()
        if row is None:
            expected_key = intent['bootstrap_effect_key'] if kind == 'bootstrap' else intent['job_effect_key'] if kind == 'job' else None
            if kind in {'revision', 'run'}:
                job_effect = self._effect(connection, intent, 'job')
                if job_effect:
                    expected_key = 'job-effect-' + digest({'job_id': job_effect['response']['id'], 'action': 'commit' if kind == 'revision' else 'submit'})
            dangling = connection.execute('SELECT 1 FROM research_session_experiment_resources WHERE experiment_id=? AND effect_key=?',
                                          (intent['id'], expected_key)).fetchone()
            if dangling or expected_key and any(e['kind'] == 'experiment_effect' and e['operation_key'] == expected_key for e in intent['loaded_session']['events']):
                raise SessionError('Experiment effect receipt is missing', code='EXPERIMENT_INTEGRITY')
            return None
        value = _parse(row['payload'], MAX_BYTES)
        if (digest(value) != row['digest'] or value['kind'] != kind or value['experiment_id'] != intent['id']
                or value['effect_key'] != row['effect_key']
                or not any(e['kind'] == 'experiment_effect' and e['operation_key'] == row['effect_key']
                           and e['operation_digest'] == row['digest'] for e in intent['loaded_session']['events'])):
            raise SessionError('Experiment effect is not anchored to its session', code='EXPERIMENT_INTEGRITY')
        return value

    def _record(self, connection, intent, kind, key, request, response, resources):
        value = {'experiment_id': intent['id'], 'kind': kind, 'effect_key': key,
                 'request': deepcopy(request), 'response': deepcopy(response), 'resources': resources}
        old = self._effect(connection, intent, kind)
        if old is not None:
            if old != value:
                raise SessionError('Experiment effect replay changed', code='EXPERIMENT_INTEGRITY')
            return
        encoded, fingerprint = _json(value), digest(value)
        connection.execute('INSERT INTO research_session_experiment_effects VALUES (?,?,?,?,?)',
                           (intent['id'], kind, key, encoded, fingerprint))
        for resource_kind, resource_id in resources:
            binding = {'kind': resource_kind, 'resource_id': resource_id, 'experiment_id': intent['id'],
                       'effect_key': key, 'effect_digest': fingerprint}
            connection.execute('INSERT INTO research_session_experiment_resources VALUES (?,?,?,?,?,?)',
                               (resource_kind, resource_id, intent['id'], key, _json(binding), digest(binding)))
        self.sessions._event(connection, intent['loaded_session'], 'experiment_effect', key, fingerprint,
                             receipt_digest=fingerprint)

    def _open(self, connection, intent):
        self.sessions._open(intent['loaded_session'])
        reservation = intent['reservation']
        if reservation['status'] == 'finished':
            raise SessionError('Experiment reservation has already ended', code='SESSION_CLOSED')

    def _bootstrap(self, connection, intent):
        plan = intent['plan']
        if plan['bootstrap_operation'] is None:
            source = plan['source_request']
            return {'research_id': source['research_id'], 'revision_id': source['base_revision_id'],
                    'receipt_digest': None}
        effect = self._effect(connection, intent, 'bootstrap')
        if effect is None:
            return None
        response = effect['response']
        if plan['bootstrap_operation'] == 'research.create':
            research_id, revision_id = response['id'], response['revisions'][0]['id']
        else:
            research_id, revision_id = response['research_id'], response['id']
        return {'research_id': research_id, 'revision_id': revision_id, 'receipt_digest': digest(response)}

    def _job_request(self, connection, intent):
        base = self._bootstrap(connection, intent)
        if base is None:
            raise SessionError('Bootstrap has not produced a real revision', code='BOOTSTRAP_REQUIRED')
        task = intent['plan']['task']
        return ResearchJobCreate.model_validate({'research_id': base['research_id'], 'base_revision_id': base['revision_id'],
            'draft': {k: deepcopy(task[k]) for k in ('evidence', 'hypotheses', 'candidates')},
            'budget': intent['request']['budget'], 'idempotency_key': intent['job_effect_key'],
            'note': intent['request']['note'], 'allow_partial_execution': False, 'method_status': 'resolved',
            'unresolved_rules': []}).model_dump()

    def get(self, identity):
        from .research_jobs import ResearchJobs
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            intent = self._load(connection, identity)
            effect = self._effect(connection, intent, 'job')
            job = ResearchJobs(self.store)._load(connection, effect['response']['id']) if effect else None
            return {'id': identity, 'request': deepcopy(intent['request']), 'request_digest': intent['row']['request_digest'],
                    'created_at': intent['created_at'], 'plan': deepcopy(intent['plan']),
                    'bootstrap': self._bootstrap(connection, intent), 'job': job,
                    'session': self.sessions._view(intent['loaded_session']), 'reservation': deepcopy(intent['reservation'])}


    def submitted_run(self, identity, job_id):
        """Read an exact existing submit effect; this grants no new Store effect."""
        from .research_jobs import ResearchJobs
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            intent = self._load(connection, identity)
            job_effect = self._effect(connection, intent, 'job')
            if job_effect is None or job_effect['response']['id'] != job_id:
                raise SessionError('Submitted run has a different experiment job', code='EXPERIMENT_INTEGRITY')
            children = intent['reservation']['children']
            if children != {'proposal_id': None, 'research_job_id': job_id, 'run_id': None}:
                raise SessionError('Submitted job differs from its parent reservation', code='CHILD_IDENTITY_MISMATCH')
            verify_experiment_child(self.store, connection, intent['loaded_session'], intent['reservation'], children)
            owner = _intent_by_resource(self.store, connection, 'job', job_id)
            if owner is None or owner['id'] != identity:
                raise SessionError('Submitted job ownership is missing', code='EXPERIMENT_INTEGRITY')
            job = ResearchJobs(self.store)._load(connection, job_id)
            actual_job = connection.execute('SELECT * FROM research_jobs WHERE id=?', (job_id,)).fetchone()
            key = actual_job['submit_effect_key']
            run = connection.execute('SELECT * FROM runs WHERE idempotency_key=?', (key,)).fetchone()
            effect = self._effect(connection, intent, 'run')
            if run is None:
                if effect is not None or job['run_id'] is not None:
                    raise SessionError('Saved submit effect lost its actual run', code='EXPERIMENT_INTEGRITY')
                return None
            submit = [step for step in job['steps'] if step['action'] == 'submit' and step['effect_key'] == key]
            expected = {'revision_id': job['revision_id'], 'mode': 'normalized_fixed'}
            run_owner = _intent_by_resource(self.store, connection, 'run', run['id'])
            if (len(submit) != 1 or submit[0]['status'] not in {'running', 'completed'}
                    or effect is None or effect['effect_key'] != key or effect['request'] != expected
                    or run_owner is None or run_owner['id'] != identity
                    or run['request_digest'] != digest(expected) or run['mode'] != expected['mode']
                    or run['revision_id'] != expected['revision_id'] or run['research_id'] != job['research_id']
                    or effect['response']['id'] != run['id'] or effect['response']['revision_id'] != run['revision_id']
                    or effect['response']['research_id'] != run['research_id'] or effect['response']['mode'] != run['mode']
                    or (job['run_id'] is not None and job['run_id'] != run['id'])):
                raise SessionError('Existing run differs from its anchored submit effect', code='EXPERIMENT_INTEGRITY')
            if submit[0]['status'] == 'completed':
                output = _parse(submit[0]['output_json'], MAX_BYTES)
                if output.get('run_id') != run['id'] or output.get('revision_id') != run['revision_id']:
                    raise SessionError('Submit receipt differs from its actual run', code='EXPERIMENT_INTEGRITY')
            return dict(run)

    def create(self, body):
        if isinstance(body, (ExperimentCreate, ExperimentCreateV2)):
            body = body.model_dump()
        request = parse_experiment_create(body).model_dump()
        fingerprint, identity = digest(request), PREFIX + digest(request)
        from .provider_experiment_source import prepare_experiment_source
        with transaction(self.store.db_path) as connection:
            old = connection.execute('SELECT id,request_digest FROM research_session_experiments WHERE idempotency_key=?',
                                     (request['idempotency_key'],)).fetchone()
            if old:
                if old['request_digest'] != fingerprint:
                    raise SessionError('Experiment key was used for another handoff', code='IDEMPOTENCY_CONFLICT')
                self._load(connection, old['id'])
            else:
                if request['schema_version'] == 2:
                    from .provider_experiment_source import prepare_corrected_experiment_source
                    plan = prepare_corrected_experiment_source(self.store, request['candidate_source'], connection=connection)
                else:
                    plan = prepare_experiment_source(self.store, request['proposal_id'], request['accepted_response_digest'], connection=connection)
                if plan['session_id'] != request['session_id']:
                    raise SessionError('Candidate belongs to another session', code='SOURCE_STALE')
                reserved = self.sessions.reserve(request['session_id'], 'experiment', request['idempotency_key'],
                    request['expected_session_digest'], action_digest=fingerprint, _connection=connection)
                loaded = self.sessions._load(connection, request['session_id'])
                if plan['source_digest'] != loaded['state']['current_source']['source_digest']:
                    raise SessionError('Accepted candidate is not from the current session source', code='SOURCE_STALE')
                value = {'request': request, 'plan': plan, 'reservation_token': reserved['reservation']['token'],
                         'created_at': self.sessions._now().isoformat(),
                         'bootstrap_effect_key': 'experiment-bootstrap-' + fingerprint,
                         'job_effect_key': 'experiment-job-' + fingerprint}
                connection.execute('INSERT INTO research_session_experiments VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (identity, request['session_id'], value['reservation_token'], request['idempotency_key'], fingerprint,
                     _json(value), digest(value), value['created_at'], value['bootstrap_effect_key'], value['job_effect_key']))
                self.sessions._event(connection, loaded, 'experiment_created', identity, digest(value), receipt_digest=digest(value))
        return self._ensure_created(identity)

    def _ensure_created(self, identity):
        from .research_jobs import ResearchJobs
        current = self.get(identity)
        plan = current['plan']
        if current['bootstrap'] is None:
            key = 'experiment-bootstrap-' + current['request_digest']
            if plan['bootstrap_operation'] == 'research.create':
                self.store.create_research(plan['title'], plan['paper_id'], plan['dataset_id'], plan['task'], key)
            else:
                self.store.create_revision(plan['destination']['research_id'], plan['destination']['base_revision_id'],
                                           plan['task'], current['request']['note'], key)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            intent = self._load(connection, identity)
            request = self._job_request(connection, intent)
        ResearchJobs(self.store).create(**request)
        return self.get(identity)

    def advance(self, identity, action, key):
        from .research_jobs import ResearchJobs
        current = self.get(identity)
        if current['job'] is None:
            current = self._ensure_created(identity)
        ResearchJobs(self.store).advance(current['job']['id'], action, key)
        return self.reconcile(identity)

    def cancel(self, identity, key):
        current = self.get(identity)
        from .research_session_execution import SessionExecution
        SessionExecution(self.store).cancel(current['session']['id'], current['session']['state_digest'], key)
        return self.reconcile(identity)

    def reconcile(self, identity):
        current = self.get(identity)
        job = current['job']
        if job:
            self._advance_source(identity)
            current = self.get(identity)
        job = current['job']
        if job and current['reservation']['outcome'] is None and job['state'] in {'completed', 'blocked', 'failed', 'cancelled', 'exhausted'}:
            unknown = any((step.get('error') or {}).get('code') in {'EXECUTION_INTERRUPTED', 'OUTCOME_UNKNOWN'} for step in job['steps'])
            outcome = {'status': 'interrupted' if unknown else 'completed' if job['state'] == 'completed' else 'failed',
                       'code': 'OUTCOME_UNKNOWN' if unknown else 'EXPERIMENT_' + job['state'].upper(),
                       'result_digest': digest(_parse(job['result_json'], MAX_BYTES)) if job['result_json'] else None}
            self.sessions.finish(current['session']['id'], current['reservation']['token'], outcome)
        elif job is None and current['reservation']['outcome'] is None and current['session']['state'] in {'cancelled', 'stopped', 'exhausted'}:
            self.sessions.finish(current['session']['id'], current['reservation']['token'],
                {'status': 'failed', 'code': 'EXPERIMENT_STOPPED_BEFORE_JOB', 'result_digest': None})
        return self.get(identity)

    def _advance_source(self, identity, *, _connection=None):
        """A saved owned commit may be acknowledged even after cancellation."""
        from .research_jobs import ResearchJobs
        from .provider_context import _provider_source
        with (transaction(self.store.db_path) if _connection is None else nullcontext(_connection)) as connection:
            intent = self._load(connection, identity)
            effect = self._effect(connection, intent, 'revision')
            job_effect = self._effect(connection, intent, 'job')
            if effect is None or job_effect is None:
                return
            job = ResearchJobs(self.store)._load(connection, job_effect['response']['id'])
            step = next((s for s in job['steps'] if s['action'] == 'commit' and s['status'] != 'running'
                         and s['output_json'] and _parse(s['output_json'], MAX_BYTES).get('revision_id') == effect['response']['id']), None)
            if step is None:
                return
            loaded = intent['loaded_session']
            key = 'experiment-source-' + intent['row']['request_digest']
            if any(e['kind'] == 'source_advanced' and e['operation_key'] == key for e in loaded['events']):
                return
            revision = effect['response']
            actual = _provider_source(self.store, revision['research_id'], revision['id'])
            loaded['state']['current_source'] = {'kind': 'research_revision', 'research_id': revision['research_id'],
                'revision_id': revision['id'], 'revision_digest': revision['digest'], 'source_digest': digest(actual)}
            self.sessions._event(connection, loaded, 'source_advanced', key, digest(effect), receipt_digest=digest(effect))


def _intent_by_key(store, connection, key):
    if key is None or not _exists(connection):
        return None
    row = connection.execute('SELECT id FROM research_session_experiments WHERE bootstrap_effect_key=? OR job_effect_key=?', (key, key)).fetchone()
    if row:
        return SessionExperiments(store)._load(connection, row['id'])
    step = connection.execute('SELECT job_id FROM research_job_steps WHERE json_extract(payload,\'$.effect_key\')=?', (key,)).fetchone()
    if step:
        return _intent_by_resource(store, connection, 'job', step['job_id'])
    if key.startswith(('experiment-bootstrap-', 'experiment-job-')):
        raise SessionError('Reserved experiment effect has no durable owner', code='EXPERIMENT_INTEGRITY')
    return None


def _intent_by_resource(store, connection, kind, identity):
    if not _exists(connection):
        return None
    row = connection.execute('SELECT * FROM research_session_experiment_resources WHERE kind=? AND resource_id=?', (kind, identity)).fetchone()
    if row is None:
        # The resource table is an index, never the sole ownership authority.
        # Missing index rows must not turn a budgeted child into manual work.
        anchored = connection.execute('''SELECT 1 FROM research_session_experiment_effects e,
            json_each(e.payload, '$.resources') r WHERE json_extract(r.value,'$[0]')=?
            AND json_extract(r.value,'$[1]')=? LIMIT 1''', (kind, identity)).fetchone()
        actual_key = None
        if kind in {'job', 'run'}:
            table = 'research_jobs' if kind == 'job' else 'runs'
            actual = connection.execute(f'SELECT idempotency_key FROM {table} WHERE id=?', (identity,)).fetchone()
            actual_key = actual['idempotency_key'] if actual else None
        elif kind == 'revision':
            actual = connection.execute("SELECT idempotency_key FROM workflow_mutation_receipts WHERE operation='revision.create' AND revision_id=?", (identity,)).fetchone()
            actual_key = actual['idempotency_key'] if actual else None
        owned_key = actual_key and _intent_by_key(store, connection, actual_key)
        if anchored or owned_key:
            raise SessionError('Experiment resource ownership index is missing', code='EXPERIMENT_INTEGRITY')
        return None
    value = _parse(row['payload'])
    if (digest(value) != row['digest'] or any(value[name] != row[name] for name in ('kind', 'resource_id', 'experiment_id', 'effect_key'))):
        raise SessionError('Experiment resource ownership changed', code='EXPERIMENT_INTEGRITY')
    service = SessionExperiments(store)
    intent = service._load(connection, row['experiment_id'])
    effects = connection.execute('SELECT kind FROM research_session_experiment_effects WHERE experiment_id=? AND effect_key=?', (intent['id'], row['effect_key'])).fetchone()
    effect = service._effect(connection, intent, effects['kind']) if effects else None
    if effect is None or digest(effect) != value['effect_digest'] or [kind, identity] not in effect['resources']:
        raise SessionError('Experiment resource has no exact effect receipt', code='EXPERIMENT_INTEGRITY')
    return intent


def _source_current(store, connection, intent, *, committed=False):
    """Recheck the original authority before bootstrap, then its exact lineage."""
    service = SessionExperiments(store)
    # Reconstruct original accepted bytes without requiring its old destination
    # to remain latest after our own bootstrap/commit advances it.
    plan = intent['plan']
    if intent['request']['schema_version'] == 2:
        from .provider_experiment_source import load_corrected_candidate
        actual_plan = load_corrected_candidate(store, connection, intent['request']['candidate_source'])
        if actual_plan != plan:
            raise SessionError('Experiment correction source differs from its exact host receipt', code='EXPERIMENT_INTEGRITY')
    else:
        from .provider_experiment_source import load_accepted_candidate
        packet = load_accepted_candidate(store, connection, intent['request']['proposal_id'], intent['request']['accepted_response_digest'])
        if (digest(packet['request']) != plan['request_digest']
                or packet['proposal']['context_digest'] != plan['context_digest']
                or packet['proposal']['accepted_attempt_id'] != plan['attempt_id']
                or digest(packet['output']['draft']) != plan['submitted_draft_digest']):
            raise SessionError('Experiment accepted candidate changed', code='EXPERIMENT_INTEGRITY')
    if plan['material']:
        from .paper_materials import PaperMaterials
        from .provider_material_bridge import accepted_packet_resolver
        materials = PaperMaterials(store, accepted_packet_resolver=accepted_packet_resolver(store))
        frozen = plan['material']
        draft = materials._draft(connection.execute('SELECT * FROM paper_materials WHERE id=?', (frozen['material_id'],)).fetchone())
        version = materials._version(connection.execute('SELECT * FROM paper_material_versions WHERE id=?', (frozen['version_id'],)).fetchone(), draft, connection=connection)
        materials._accepted_packet(connection, draft['source'], digest(draft['draft']))
        actual_material = {'material_id': version['material_id'], 'version_id': version['id'],
            'material_digest': version['digest'], 'draft_digest': version['draft_digest'],
            'selection_digest': version['selection']['selection_digest'], 'source': version['source']}
        if actual_material != frozen:
            raise SessionError('Experiment material version changed', code='EXPERIMENT_INTEGRITY')
    base = service._bootstrap(connection, intent)
    if base is None:
        from .provider_experiment_source import prepare_experiment_source
        if intent['request']['schema_version'] == 2:
            from .provider_experiment_source import prepare_corrected_experiment_source
            actual = prepare_corrected_experiment_source(store, intent['request']['candidate_source'], connection=connection)
        else:
            actual = prepare_experiment_source(store, intent['request']['proposal_id'], intent['request']['accepted_response_digest'], connection=connection)
        if actual != intent['plan']:
            raise SessionError('Experiment candidate source changed', code='SOURCE_STALE')
        return
    revision_effect = service._effect(connection, intent, 'revision')
    target = revision_effect['response']['id'] if revision_effect else base['revision_id']
    research = store._one(connection, 'researches', base['research_id'])
    revision = store._one(connection, 'revisions', target)
    if research['latest_revision_id'] != target or revision['research_id'] != research['id']:
        raise SessionError('Experiment lineage changed outside its own commit', code='SOURCE_STALE')
    from .provider_context import _provider_source
    actual = _provider_source(store, research['id'], target)
    plan = intent['plan']
    if (actual['research']['paper_id'] != plan['paper_id'] or actual['research']['dataset_id'] != plan['dataset_id']
            or ((plan['bootstrap_operation'] is not None or revision_effect is not None) and actual['revision']['task'] != plan['task'])):
        raise SessionError('Experiment frozen task or resources changed', code='SOURCE_STALE')
    original = plan['source_snapshot']
    if plan['source_kind'] == 'research_revision':
        fields = ('paper_sha256', 'paper_document_digest', 'dataset_sha256', 'dataset_metadata_digest', 'data_file_sha256', 'metadata_file_sha256')
        matches = all(actual[name] == original[name] for name in fields)
        if revision_effect is None and actual != original:
            matches = False
    else:
        dataset = original['dataset']
        matches = (actual['paper_sha256'] == original['paper_sha256']
            and actual['paper_document_digest'] == original['document_digest']
            and actual['dataset_sha256'] == dataset['dataset_sha256']
            and actual['dataset_metadata_digest'] == dataset['metadata_digest']
            and actual['data_file_sha256'] == dataset['data_sha256']
            and actual['metadata_file_sha256'] == dataset['metadata_file_sha256'])
    if not matches:
        raise SessionError('Experiment paper or data identity changed', code='SOURCE_STALE')


def authorize_store_effect(store, connection, operation, idempotency_key, request):
    if operation == 'revision.create':
        guard_revision_submission(store, connection, request['base_revision_id'], idempotency_key)
    elif operation == 'run.submit':
        guard_revision_submission(store, connection, request['revision_id'], idempotency_key)
    intent = _intent_by_key(store, connection, idempotency_key)
    if intent is None:
        return
    service = SessionExperiments(store)
    service._open(connection, intent)
    _source_current(store, connection, intent)
    plan = intent['plan']
    if idempotency_key == intent['bootstrap_effect_key']:
        expected_operation = plan['bootstrap_operation']
        if expected_operation == 'research.create':
            expected = {'title': plan['title'], 'paper_id': plan['paper_id'], 'dataset_id': plan['dataset_id'], 'task': plan['task']}
        else:
            expected = {'research_id': plan['destination']['research_id'], 'base_revision_id': plan['destination']['base_revision_id'],
                        'task': plan['task'], 'note': intent['request']['note']}
    else:
        from .research_jobs import ResearchJobs
        job_effect = service._effect(connection, intent, 'job')
        if job_effect is None:
            raise SessionError('Store effect has no session-owned job', code='EXPERIMENT_INTEGRITY')
        job = ResearchJobs(store)._load(connection, job_effect['response']['id'])
        _job_deadline(service, job)
        step = next((s for s in job['steps'] if s['effect_key'] == idempotency_key and s['status'] == 'running'), None)
        if step is None or step['action'] not in {'commit', 'submit'}:
            raise SessionError('Store effect has no charged job transition', code='EXPERIMENT_INTEGRITY')
        if step['action'] == 'commit':
            expected_operation = 'revision.create'
            expected = {'research_id': job['research_id'], 'base_revision_id': job['base_revision_id'],
                        'task': ResearchJobs(store)._validated_task(job), 'note': intent['request']['note']}
        else:
            expected_operation, expected = 'run.submit', {'revision_id': job['revision_id'], 'mode': 'normalized_fixed'}
    if operation != expected_operation or request != expected:
        raise SessionError('Store mutation differs from the exact experiment intent', code='EXPERIMENT_REQUEST_MISMATCH')
    # Source verification may read sizeable artifacts. Its start time cannot
    # grant a write permit after the absolute deadline has passed.
    service._open(connection, intent)
    if idempotency_key != intent['bootstrap_effect_key']:
        _job_deadline(service, job)


def record_store_effect(store, connection, operation, idempotency_key, request, response):
    intent = _intent_by_key(store, connection, idempotency_key)
    if intent is None:
        return
    if idempotency_key == intent['bootstrap_effect_key']:
        kind = 'bootstrap'
        if operation == 'research.create':
            resources = [['research', response['id']], ['revision', response['revisions'][0]['id']]]
        else:
            resources = [['revision', response['id']]]
    else:
        kind = 'revision' if operation == 'revision.create' else 'run'
        resources = [[kind, response['id']]]
    SessionExperiments(store)._record(connection, intent, kind, idempotency_key, request, response, resources)


def guard_revision_submission(store, connection, revision_id, idempotency_key):
    owner = _intent_by_resource(store, connection, 'revision', revision_id)
    if owner is None:
        return
    actor = _intent_by_key(store, connection, idempotency_key)
    actor_base = SessionExperiments(store)._bootstrap(connection, actor) if actor else None
    if actor and actor_base is None and idempotency_key == actor['bootstrap_effect_key'] and actor['plan']['bootstrap_operation'] == 'revision.create':
        actor_base = {'revision_id': actor['plan']['destination']['base_revision_id']}
    if actor is None or (actor['id'] != owner['id'] and (not actor_base or actor_base['revision_id'] != revision_id)):
        raise SessionError('Session-owned revision requires its original budgeted experiment', code='SESSION_OWNED_REVISION')


def assert_run_retry_allowed(store, connection, run_id):
    if _intent_by_resource(store, connection, 'run', run_id) is not None:
        raise SessionError('Session-owned experiments require a new explicit reservation, not an in-place retry', code='SESSION_RETRY_DENIED')


def authorize_job_create(store, connection, request):
    intent = _intent_by_key(store, connection, request['idempotency_key'])
    if intent is None:
        guard_revision_submission(store, connection, request['base_revision_id'], request['idempotency_key'])
        return
    service = SessionExperiments(store)
    service._open(connection, intent)
    if service._job_request(connection, intent) != request:
        raise SessionError('Job request differs from its frozen experiment', code='EXPERIMENT_REQUEST_MISMATCH')
    _source_current(store, connection, intent)
    service._open(connection, intent)


def record_job_created(store, connection, job):
    row = connection.execute('SELECT * FROM research_jobs WHERE id=?', (job['id'],)).fetchone()
    intent = _intent_by_key(store, connection, row['idempotency_key'])
    if intent is None:
        return
    body = _parse(row['payload'], MAX_BYTES)
    service = SessionExperiments(store)
    if body['request'] != service._job_request(connection, intent):
        raise SessionError('Created job does not match the experiment request', code='EXPERIMENT_REQUEST_MISMATCH')
    service._record(connection, intent, 'job', row['idempotency_key'], body['request'],
                    {'id': job['id'], 'request_digest': row['request_digest'], 'source': body['source']}, [['job', job['id']]])
    service.sessions.record_effect_in_transaction(connection, intent['request']['session_id'],
                                                  intent['reservation_token'], {'research_job_id': job['id']})


def verify_experiment_child(store, connection, loaded, reservation, children):
    """Return False for legacy direct-job reservations; preserve their contract."""
    if not _exists(connection):
        return False
    row = connection.execute('SELECT id FROM research_session_experiments WHERE reservation_token=?', (reservation['token'],)).fetchone()
    if row is None:
        return False
    service = SessionExperiments(store)
    intent = service._load(connection, row['id'])
    effect = service._effect(connection, intent, 'job')
    if (reservation['action'] != 'experiment' or effect is None
            or children != {'proposal_id': None, 'research_job_id': effect['response']['id'], 'run_id': None}):
        raise SessionError('Experiment child differs from its recorded effect', code='CHILD_IDENTITY_MISMATCH')
    from .research_jobs import ResearchJobs
    job = ResearchJobs(store)._load(connection, children['research_job_id'])
    actual = connection.execute('SELECT * FROM research_jobs WHERE id=?', (job['id'],)).fetchone()
    body = _parse(actual['payload'], MAX_BYTES)
    if (body['request'] != service._job_request(connection, intent) or body['request'] != effect['request']
            or digest(body['request']) != actual['request_digest'] or actual['request_digest'] != effect['response']['request_digest']
            or body['source'] != effect['response']['source']):
        raise SessionError('Experiment job request or source differs from its anchored receipt', code='CHILD_REQUEST_MISMATCH')
    return True


def _job_deadline(service, job):
    if (_timestamp(job['created_at']) + timedelta(seconds=job['budget']['max_seconds']) - service.sessions._now()).total_seconds() <= 0:
        raise SessionError('Experiment job deadline elapsed', code='SESSION_EXPIRED')


def authorize_job_transition(store, connection, job, action):
    intent = _intent_by_resource(store, connection, 'job', job['id'])
    if intent is None or action == 'cancel':
        return
    service = SessionExperiments(store)
    service._open(connection, intent)
    _job_deadline(service, job)
    _source_current(store, connection, intent)
    service._open(connection, intent)
    _job_deadline(service, job)



def check_completion_authority(store, connection, job):
    """Final read-result ledger write cannot outlive its original authority.

    This lightweight endpoint gate does not re-dispatch effects or reinterpret
    an already committed submit acknowledgement. Source checks remain in the
    original transition authorization before observe/complete dispatch.
    """
    intent = _intent_by_resource(store, connection, 'job', job['id'])
    if intent is None:
        return
    service = SessionExperiments(store)
    service._open(connection, intent)
    _job_deadline(service, job)


def run_guard(store, connection, run, *, phase='execute'):
    intent = _intent_by_resource(store, connection, 'run', run['id'])
    if intent is None:
        return None
    service = SessionExperiments(store)
    service._open(connection, intent)
    job_effect = service._effect(connection, intent, 'job')
    from .research_jobs import ResearchJobs
    job = ResearchJobs(store)._load(connection, job_effect['response']['id'])
    expected = 'job-effect-' + digest({'job_id': job['id'], 'action': 'submit'})
    if run['idempotency_key'] != expected or (phase == 'claim' and run['attempt_count'] != 0):
        raise SessionError('Run identity differs or tries an unbudgeted retry', code='EXPERIMENT_INTEGRITY')
    if phase in {'claim', 'prepare', 'spawn', 'publish'}:
        _source_current(store, connection, intent)
    service._open(connection, intent)
    remaining = min((_timestamp(intent['loaded_session']['row']['deadline_at']) - service.sessions._now()).total_seconds(),
                    (_timestamp(job['created_at']) + timedelta(seconds=job['budget']['max_seconds']) - service.sessions._now()).total_seconds())
    if remaining <= 0:
        raise SessionError('Experiment absolute deadline elapsed', code='SESSION_EXPIRED')
    return remaining


def check_run(store, run_id, *, phase='execute'):
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        return run_guard(store, connection, store._one(connection, 'runs', run_id), phase=phase)


def cancel_session_experiments(store, session_id):
    """Find real runs by stable keys even if job step acknowledgement was lost."""
    from .research_jobs import ResearchJobs
    with closing(connect(store.db_path)) as connection:
        if not _exists(connection):
            return
        rows = connection.execute('SELECT id FROM research_session_experiments WHERE session_id=?', (session_id,)).fetchall()
    for row in rows:
        service = SessionExperiments(store)
        current = service.get(row['id'])
        if current['job'] is None:
            continue
        job = current['job']
        key = 'job-effect-' + digest({'job_id': job['id'], 'action': 'submit'})
        runs = store._read('SELECT id,status FROM runs WHERE idempotency_key=?', (key,))
        for run in runs:
            if run['status'] in {'queued', 'running', 'cancelling', 'cancelled'}:
                store.cancel_run(run['id'])
        if job['state'] not in {'completed', 'cancelled'}:
            ResearchJobs(store).cancel(job['id'], 'experiment-cancel-' + current['request_digest'])


def acknowledge_job_step(store, connection, job_id):
    intent = _intent_by_resource(store, connection, 'job', job_id)
    if intent is not None:
        SessionExperiments(store)._advance_source(intent['id'], _connection=connection)
