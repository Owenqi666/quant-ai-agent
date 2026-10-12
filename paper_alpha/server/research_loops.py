"""Bounded host orchestration over the original proposal and experiment queues.

Only explicit loop policy authorizes transitions. Each advance persists its
exact effect request before dispatch, and recovery reuses that request and key.
Child state is always read from the authoritative existing services.
"""
from contextlib import closing
from copy import deepcopy

from ..storage import digest, json_text
from .db import connect, transaction
from .provider_research_schema import ResearchProposalCreate
from .proposal_requests import ProposalRequests
from .research_loops_schema import LoopCreate, parse_loop_create, SCHEMA
from .research_session_execution import SessionExecution
from .research_session_experiments import SessionExperiments
from .research_session_experiments_schema import ExperimentCreate, ExperimentCreateV2, parse_experiment_create
from .research_sessions import ResearchSessions, SessionError, _parse

MAX_BYTES = 1024 * 1024
MAX_ACTIONS = 24
PREFIX = 'research_loop_'
PROPOSAL_TERMINAL = {'ready', 'rejected', 'abstained', 'failed', 'cancelled', 'expired',
                     'stale', 'exhausted', 'interrupted'}
SESSION_TERMINAL = {'completed', 'stopped', 'cancelled', 'exhausted', 'interrupted'}


def _text(value):
    encoded = json_text(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise SessionError('Loop provenance exceeds its byte limit', 413, code='PAYLOAD_TOO_LARGE')
    return encoded


class ResearchLoops:
    def __init__(self, store, proposals=None):
        self.store = store
        self.proposals = proposals or ProposalRequests(store, configs={})
        self.sessions = ResearchSessions(store)
        self.execution = SessionExecution(store, self.proposals, sessions=self.sessions)
        self.experiments = SessionExperiments(store)

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM research_loops WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise SessionError('Research loop not found', 404, code='LOOP_NOT_FOUND')
        try:
            request = parse_loop_create(_parse(row['payload'], MAX_BYTES)).model_dump()
            if (digest(request) != row['request_digest'] or row['payload_digest'] != digest(request)
                    or identity != PREFIX + digest(request) or request['idempotency_key'] != row['idempotency_key']
                    or request['initial_proposal']['session_id'] != row['session_id']):
                raise ValueError('Loop request identity differs')
            actions, previous = [], None
            for record in connection.execute('SELECT * FROM research_loop_actions WHERE loop_id=? ORDER BY sequence', (identity,)):
                action = _parse(record['payload'], MAX_BYTES)
                if (record['sequence'] != len(actions) + 1 or action['sequence'] != record['sequence']
                        or action['idempotency_key'] != record['idempotency_key'] or digest(action) != record['digest']
                        or action['previous_digest'] != previous or action['loop_id'] != identity
                        or action['effect_key'] != self._effect_key(identity, record['sequence'])
                        or actions and actions[-1]['receipt'] is None):
                    raise ValueError('Loop action chain differs')
                actions.append(action)
                previous = digest(action)
            if (len(actions) != row['action_count'] or len(actions) > MAX_ACTIONS
                    or digest([digest(item) for item in actions]) != row['ledger_digest']):
                raise ValueError('Loop action count or ledger anchor differs')
            session = self.sessions._load(connection, row['session_id'])
            corrections = [a for a in actions if a['kind'] == 'correction.apply']
            if request['schema_version'] == 1 and corrections:
                raise ValueError('V1 loops cannot own host corrections')
            if request['schema_version'] == 2:
                native_repairs = [a for a in actions if a['kind'] == 'proposal.create'
                                  and a['request'].get('operation') == 'repair_candidate']
                if len(corrections) + len(native_repairs) > request['max_repairs']:
                    raise ValueError('Unqualified correction mode or exceeded correction slots')
                if request['correction_mode'] == 'provider_optional':
                    if corrections or request['max_repairs'] != 1:
                        raise ValueError('Provider correction cannot fall back to deterministic correction')
                    for repair in native_repairs:
                        from .provider_correction_native import is_provider_correction
                        target = repair['request'].get('target') or {}
                        if (not is_provider_correction(repair['request']) or target['loop_id'] != identity
                                or target['action_sequence'] != repair['sequence']
                                or target['effect_key'] != repair['effect_key']
                                or target['policy_id'] != request['correction_policy_id']
                                or target['policy_digest'] != request['correction_policy_digest']):
                            raise ValueError('Provider correction intent has another version or policy')
                elif native_repairs:
                    raise ValueError('Deterministic correction cannot spend a Provider repair slot')
                for action in corrections:
                    correction, intent = action['correction'], action['request']
                    if (set(correction) != {'intent_digest', 'reservation_token', 'status', 'started_at', 'bundle'}
                            or correction['status'] not in {'planned', 'started', 'completed', 'blocked', 'unknown'}
                            or correction['intent_digest'] != digest(intent)
                            or intent['loop_id'] != identity or intent['session_id'] != row['session_id']
                            or intent['sequence'] != action['sequence'] or intent['effect_key'] != action['effect_key']
                            or intent['method'] != 'deterministic'
                            or intent['policy_id'] != request['correction_policy_id']
                            or intent['policy_digest'] != request['correction_policy_digest']
                            or intent['deadline_at'] != session['row']['deadline_at']
                            or (correction['status'] == 'planned') != (correction['started_at'] is None)
                            or (correction['status'] in {'completed', 'blocked'}) != (correction['bundle'] is not None)):
                        raise ValueError('Correction intent/state differs')
                    reservation = self.sessions._reservation(session, correction['reservation_token'])
                    if (reservation['action'] != 'read' or reservation['effect_key'] != self._correction_read_key(identity, action['sequence'])
                            or reservation['action_digest'] != correction['intent_digest']
                            or reservation['source_digest'] != intent['source_digest']
                            or reservation['children'] is not None
                            or reservation['charges'] != {'tool_actions': 1, 'provider_dispatches': 0,
                                                         'experiments': 0, 'failures': 0}):
                        raise ValueError('Correction lost its actual tool reservation')
            return {'id': identity, 'row': dict(row), 'request': request, 'actions': actions, 'loaded_session': session}
        except (ValueError, KeyError, TypeError) as exc:
            raise SessionError('Research loop ledger failed verification', code='LOOP_INTEGRITY') from exc

    def create(self, body):
        if isinstance(body, LoopCreate):
            body = body.model_dump()
        request = parse_loop_create(body).model_dump()
        fingerprint, identity = digest(request), PREFIX + digest(request)
        initial = request['initial_proposal']
        with transaction(self.store.db_path) as connection:
            prior = connection.execute('SELECT id,request_digest FROM research_loops WHERE idempotency_key=?',
                                       (request['idempotency_key'],)).fetchone()
            if prior:
                if prior['request_digest'] != fingerprint:
                    raise SessionError('Loop key names another policy', code='IDEMPOTENCY_CONFLICT')
                self._load(connection, prior['id'])
            else:
                if request['schema_version'] == 2 and request['correction_mode'] == 'provider_optional':
                    from .provider_loop_native_authority import LoopNativeResearchAuthority
                    from .provider_authority_router import ResearchAuthorityRouter
                    authority = getattr(self.proposals, 'research_authority', None)
                    if type(authority) is ResearchAuthorityRouter:
                        authority = authority.leaf_for_session(initial['session_id'], connection=connection)
                    if type(authority) is not LoopNativeResearchAuthority:
                        raise SessionError('Provider correction needs its actual versioned Native authority',
                                           code='CORRECTION_MODE_UNQUALIFIED')
                    authority.qualify_correction_mode(connection, initial['session_id'],
                        request['correction_policy_id'], request['correction_policy_digest'])
                    from .research_loop_correction import _historical_source
                    _historical_source(self.store, connection, initial['source'])
                loaded = self.sessions._load(connection, initial['session_id'])
                self.sessions._expected(loaded, initial['expected_session_digest'])
                self.sessions._open(loaded)
                source = loaded['state']['current_source']
                descriptor = {'kind': source['kind'], 'research_id': source.get('research_id'),
                              'base_revision_id': source.get('revision_id')}
                if (descriptor != initial['source'] or source['source_digest'] != initial['expected_source_digest']
                        or loaded['request']['config']['contract_id'] != initial['contract_id']
                        or loaded['request']['config']['provider_config_id'] != initial['provider_config_id']):
                    raise SessionError('Loop begins from another session source or provider', code='SOURCE_STALE')
                if loaded['reservations']:
                    raise SessionError('A loop needs an unused session; existing actions are not adopted implicitly',
                                       code='LOOP_SESSION_USED')
                if connection.execute('SELECT 1 FROM research_loops WHERE session_id=?', (initial['session_id'],)).fetchone():
                    raise SessionError('This session already owns a loop', code='LOOP_ALREADY_EXISTS')
                self.sessions._resolve(connection, source)
                self.sessions._open(loaded)
                connection.execute('INSERT INTO research_loops VALUES (?,?,?,?,?,?,?,?,?)',
                    (identity, initial['session_id'], request['idempotency_key'], fingerprint, _text(request),
                     fingerprint, self.sessions._now().isoformat(), 0, digest([])))
        return self.get(identity)

    def _children(self, connection, loaded):
        proposals, experiment = [], None
        for action in loaded['actions']:
            if action['kind'] == 'proposal.create':
                request = ResearchProposalCreate.model_validate(action['request']).model_dump()
                identity = 'proposal_' + digest(request)
                exists = connection.execute('SELECT 1 FROM proposal_requests WHERE id=?', (identity,)).fetchone()
                if not exists:
                    if action['receipt'] is not None:
                        raise SessionError('A recorded proposal effect is missing', code='LOOP_INTEGRITY')
                    continue
                child = self.proposals._load(connection, identity)
                if child['request'] != request or action['receipt'] not in (None, {'proposal_id': identity}):
                    raise SessionError('Loop proposal differs from its exact request', code='LOOP_INTEGRITY')
                reservation = next((r for r in loaded['loaded_session']['reservations']
                                    if (r['children'] or {}).get('proposal_id') == identity), None)
                if reservation is None:
                    raise SessionError('Loop proposal lost its real parent relation', code='LOOP_INTEGRITY')
                self.sessions._children(connection, loaded['loaded_session'], reservation, reservation['children'])
                proposals.append(child)
            elif action['kind'] == 'experiment.create':
                request = parse_experiment_create(action['request']).model_dump()
                identity = 'session_experiment_' + digest(request)
                exists = connection.execute('SELECT 1 FROM research_session_experiments WHERE id=?', (identity,)).fetchone()
                if not exists:
                    if action['receipt'] is not None:
                        raise SessionError('A recorded experiment effect is missing', code='LOOP_INTEGRITY')
                    continue
                intent = self.experiments._load(connection, identity)
                if intent['request'] != request or action['receipt'] not in (None, {'experiment_id': identity}):
                    raise SessionError('Loop experiment differs from its exact request', code='LOOP_INTEGRITY')
                experiment = self.experiments.get(identity)
            elif action['kind'] == 'correction.apply' and action['correction']['status'] in {'completed', 'blocked'}:
                from .research_loop_correction import verify_correction_bundle
                verify_correction_bundle(self.store, connection, loop_request=loaded['request'], action=action)
        self._finished_receipts(connection, loaded, proposals, experiment)
        return proposals, experiment

    def _automatic_finalization_ack(self, connection, loaded, action):
        """Confirm an existing canonical bookkeeping effect, never execute one.

        This private receipt aliases only observe/complete already performed by
        the original automatic finalizer. All pins come from the existing Loop,
        SessionExperiment, Job, submit effect, Run and terminal Attempt ledgers.
        Absence of a qualifying step returns None and preserves normal dispatch.
        Ledger or ownership corruption fails closed. Historical acknowledgements
        are rebuilt from the same records and compared exactly, without a slot,
        current permission check, provider dispatch, or numerical recomputation.
        """
        from .research_jobs import ResearchJobs
        from .service import ServiceError
        from .research_session_experiments import _intent_by_resource, verify_experiment_child

        def require(condition):
            if not condition:
                raise SessionError('Automatic finalization acknowledgement lost its exact owner or effect',
                                   code='LOOP_INTEGRITY')

        try:
            if (action['kind'] != 'experiment.advance'
                    or action['request']['action'] not in {'observe', 'complete'}):
                return None
            original = [item for item in loaded['actions'] if item['sequence'] == action['sequence']]
            require(type(action['sequence']) is int and len(original) == 1
                    and original[0]['kind'] == action['kind'] and original[0]['request'] == action['request']
                    and original[0]['effect_key'] == action['effect_key']
                    and action['loop_id'] == loaded['id'])
            owned_creates = [item for item in loaded['actions'] if item['kind'] == 'experiment.create'
                            and item['sequence'] < action['sequence']]
            require(len(owned_creates) == 1)
            identity = 'session_experiment_' + digest(owned_creates[0]['request'])
            require(identity == action['request']['experiment_id']
                    and owned_creates[0]['receipt'] == {'experiment_id': identity})
            intent = self.experiments._load(connection, identity)
            require(intent['request'] == owned_creates[0]['request']
                    and intent['row']['session_id'] == loaded['row']['session_id'])
            job_effect = self.experiments._effect(connection, intent, 'job')
            if job_effect is None:
                return None
            job_id = job_effect['response']['id']
            job = ResearchJobs(self.store)._load(connection, job_id)
            name = action['request']['action']
            canonical_key = ResearchJobs._finalization_key(job_id, name)
            canonical = [step for step in job['steps'] if step['idempotency_key'] == canonical_key]
            if not canonical:
                return None
            require(len(canonical) == 1)
            step = canonical[0]
            if (step['action'] != name or step['status'] != 'completed' or step['error'] is not None
                    or step['next_state'] != {'observe': 'observed', 'complete': 'completed'}[name]):
                return None
            # Prefer a genuine receipt under the originally requested key. An alias
            # never hides an existing, running, failed, or unknown original step.
            if any(item['idempotency_key'] == action['effect_key'] for item in job['steps']):
                return None
            require(intent['reservation']['children'] == {
                'proposal_id': None, 'research_job_id': job_id, 'run_id': None})
            require(verify_experiment_child(self.store, connection, intent['loaded_session'],
                                           intent['reservation'], intent['reservation']['children']))
            owner = _intent_by_resource(self.store, connection, 'job', job_id)
            require(owner is not None and owner['id'] == identity)
            if intent['reservation']['outcome'] is not None:
                self.sessions._verify_outcome(connection, intent['reservation'], intent['reservation']['outcome'])
            actual_job = connection.execute('SELECT * FROM research_jobs WHERE id=?', (job_id,)).fetchone()
            run = connection.execute('SELECT * FROM runs WHERE idempotency_key=?',
                                     (actual_job['submit_effect_key'],)).fetchone()
            run_effect = self.experiments._effect(connection, intent, 'run')
            require(run is not None and run_effect is not None and job['run_id'] == run['id'])
            owner = _intent_by_resource(self.store, connection, 'run', run['id'])
            require(owner is not None and owner['id'] == identity)
            submits = [item for item in job['steps'] if item['action'] == 'submit'
                       and item['effect_key'] == actual_job['submit_effect_key']]
            expected_submit = {'revision_id': job['revision_id'], 'mode': 'normalized_fixed'}
            require(len(submits) == 1 and submits[0]['status'] == 'completed' and submits[0]['error'] is None
                    and run_effect['effect_key'] == actual_job['submit_effect_key']
                    and run_effect['request'] == expected_submit and run['request_digest'] == digest(expected_submit)
                    and run['mode'] == 'normalized_fixed' and run['revision_id'] == job['revision_id']
                    and run['research_id'] == job['research_id']
                    and all(run_effect['response'][field] == run[field]
                            for field in ('id', 'revision_id', 'research_id', 'mode')))
            submitted = _parse(submits[0]['output_json'], MAX_BYTES)
            require(submitted.get('run_id') == run['id'] and submitted.get('revision_id') == run['revision_id'])
            if run['status'] != 'completed' or run['attempt_id'] is None:
                return None
            attempts = connection.execute('SELECT * FROM attempts WHERE run_id=? ORDER BY number', (run['id'],)).fetchall()
            current = [item for item in attempts if item['id'] == run['attempt_id']]
            require(len(current) == 1 and len(attempts) == run['attempt_count']
                    and current[0]['number'] == len(attempts) and current[0]['status'] == 'completed'
                    and current[0]['finished_at'] is not None and current[0]['error'] is None)
            attempt = current[0]
            state, verification = _parse(run['state'], MAX_BYTES), _parse(run['verification'], MAX_BYTES)
            require(verification.get('verified') is True and digest(state) == attempt['state_digest']
                    and any(item['status'] == 'evaluated' for item in state['candidates']))
            # Reuse the original saved-artifact integrity check. It explicitly
            # separates the frozen producer from current code and never recomputes.
            self.store._check_integrity(connection, dict(run))
            output = _parse(step['output_json'], MAX_BYTES)
            if name == 'observe':
                require(output == {'run_id': run['id'], 'run_status': 'completed', 'attempt_id': attempt['id']})
            else:
                require(set(output) == {'run_id', 'result_json', 'result_digest', 'semantic_fidelity'}
                        and output['run_id'] == run['id'] and output['semantic_fidelity'] == 'unverified')
                result = _parse(output['result_json'], MAX_BYTES)
                projected = deepcopy(state)
                projected['task'] = {key: value for key, value in projected.get('task', {}).items()
                                     if key not in {'paper', 'paper_pdf', 'data', 'data_metadata'}}
                for candidate in projected.get('candidates', []):
                    candidate.get('result', {}).pop('daily', None)
                require(result['state'] == self.store._public(projected))
                require(digest(result) == output['result_digest'] and result['id'] == run['id']
                        and result['status'] == 'completed'
                        and result['revision_id'] == run['revision_id'] and result['mode'] == run['mode']
                        and (result['verification'] or {}).get('verified') is True
                        and any(item['status'] == 'evaluated' for item in result['state']['candidates']))
                saved_attempts = [item for item in result['attempts'] if item['id'] == attempt['id']]
                require(len(saved_attempts) == 1 and saved_attempts[0]['state_digest'] == attempt['state_digest']
                        and saved_attempts[0]['status'] == 'completed'
                        and result['attempt_count'] == run['attempt_count']
                        and result['review_targets'] == [
                            {'candidate_id': item['id'], 'attempt_id': attempt['id'], 'result_digest': digest(item)}
                            for item in state['candidates']]
                        and result['regression_target'] == {
                            'attempt_id': attempt['id'], 'result_digest': attempt['state_digest']})
            owners = {}
            for kind, resource_id in (('job', job_id), ('run', run['id'])):
                record = connection.execute('SELECT * FROM research_session_experiment_resources WHERE kind=? AND resource_id=?',
                                            (kind, resource_id)).fetchone()
                require(record is not None and record['experiment_id'] == identity)
                owners[kind] = record['digest']
            # Locator fields are excluded: backup restoration may rebase paths but
            # cannot change the frozen effect/attempt identity acknowledged here.
            attempt_pin = {field: attempt[field] for field in ('id', 'run_id', 'number', 'worker_id',
                           'status', 'started_at', 'finished_at', 'error', 'state_digest')}
            return {'origin': 'existing_automatic_finalization', 'loop_id': loaded['id'],
                'sequence': action['sequence'], 'requested_effect_key': action['effect_key'], 'action': name,
                'session_id': loaded['row']['session_id'], 'experiment_id': identity,
                'experiment_request_digest': intent['row']['request_digest'],
                'experiment_plan_digest': intent['plan']['plan_digest'],
                'reservation_token': intent['reservation']['token'], 'job_id': job_id,
                'job_digest': job['digest'], 'job_source_digest': job['source_digest'],
                'job_owner_digest': owners['job'], 'run_id': run['id'],
                'run_request_digest': run['request_digest'], 'run_revision_id': run['revision_id'],
                'run_mode': run['mode'], 'run_owner_digest': owners['run'],
                'attempt_id': attempt['id'], 'attempt_pin_digest': digest(attempt_pin),
                'step_id': step['id'], 'step_digest': digest(step),
                'step_idempotency_key': step['idempotency_key'], 'step_effect_key': step['effect_key']}
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise SessionError('Automatic finalization receipt contains malformed identity metadata',
                               code='LOOP_INTEGRITY') from exc

    def _finished_receipts(self, connection, loaded, proposals, experiment):
        """Recheck orchestration acknowledgements against the actual ledgers."""
        session = loaded['loaded_session']

        def require(condition):
            if not condition:
                raise SessionError('Loop acknowledgement differs from its actual effect', code='LOOP_INTEGRITY')

        def closure(action, reason):
            expected = digest({'status': 'stopped', 'reason': reason})
            events = [event for event in session['events'] if event['kind'] == 'session_finished'
                      and event['operation_key'] == action['effect_key']]
            require(len(events) == 1 and events[0]['operation_digest'] == expected
                    and events[0]['state_after']['state'] == 'stopped'
                    and events[0]['state_after']['stop_reason'] == reason)

        for action in loaded['actions']:
            receipt, request, kind = action['receipt'], action['request'], action['kind']
            if receipt is None or kind in {'proposal.create', 'experiment.create'}:
                continue
            if kind == 'proposal.reconcile':
                reservation = self.sessions._reservation(session, request['token'])
                expected = {'token': request['token'], 'outcome_digest': digest(reservation['outcome'])}
                child_id = (reservation['children'] or {}).get('proposal_id')
                child = next((p for p in proposals if p['id'] == child_id), None)
                from .provider_correction_native import is_provider_correction, correction_receipt
                if child is not None and is_provider_correction(child['request']):
                    expected['correction_receipt'] = correction_receipt(self.store, connection, child,
                        created_at=action['planned_at'])
                require(reservation['outcome'] is not None and receipt == expected)
                self.sessions._verify_outcome(connection, reservation, reservation['outcome'])
            elif kind == 'correction.apply':
                from .research_loop_correction import guard_correction_outcome, verify_correction_bundle
                correction = action['correction']
                reservation = self.sessions._reservation(session, correction['reservation_token'])
                require(loaded['request']['schema_version'] == 2 and reservation['outcome'] is not None)
                guard_correction_outcome(self.store, connection, session_id=session['row']['id'],
                                         reservation=reservation, outcome=reservation['outcome'])
                if correction['status'] in {'completed', 'blocked'}:
                    bundle = verify_correction_bundle(self.store, connection, loop_request=loaded['request'], action=action)
                    expected = bundle['receipt']['receipt_digest']
                else:
                    require(correction['status'] == 'unknown' and correction['bundle'] is None)
                    expected = None
                require(receipt == {'status': correction['status'], 'correction_receipt_digest': expected})
            elif kind == 'experiment.advance':
                require(experiment is not None and request['experiment_id'] == experiment['id'] and experiment['job'] is not None)
                require(type(receipt) is dict)
                if receipt.get('origin') == 'existing_automatic_finalization':
                    expected = self._automatic_finalization_ack(connection, loaded, action)
                    require(expected is not None and receipt == expected)
                else:
                    steps = [step for step in experiment['job']['steps'] if step['idempotency_key'] == action['effect_key']]
                    require(len(steps) == 1 and steps[0]['action'] == request['action']
                            and receipt == {'experiment_id': experiment['id'], 'step_id': steps[0]['id'], 'step_digest': digest(steps[0])})
            elif kind == 'experiment.reconcile':
                require(experiment is not None and request['experiment_id'] == experiment['id']
                        and experiment['reservation']['outcome'] is not None
                        and receipt == {'experiment_id': experiment['id'],
                                        'outcome_digest': digest(experiment['reservation']['outcome'])})
            elif kind == 'session.stop':
                closure(action, request['reason'])
                require(receipt == {'session_id': session['row']['id'], 'state': 'stopped', 'reason': request['reason']})
            elif kind == 'decision.apply':
                from .provider_research_targets import resolve_target, validate_decision_output
                children = [child for child in proposals if child['id'] == request['proposal_id']]
                require(len(children) == 1 and children[0]['status'] == 'ready'
                        and children[0]['accepted_response_digest'] == request['accepted_response_digest'])
                child = children[0]
                output = self.proposals._json_path(self.proposals._folder(child['id']) / 'accepted/response.json')
                require(output['decision'] == request['decision'])
                target = resolve_target(self.store, ResearchProposalCreate.model_validate(child['request']), connection=connection)
                require(not validate_decision_output(target, output['decision']))
                reservation = self.sessions._reservation(session, request['token'])
                require((reservation['children'] or {}).get('proposal_id') == child['id']
                        and (reservation['outcome'] or {}).get('result_digest') == child['accepted_response_digest'])
                self.sessions._verify_outcome(connection, reservation, reservation['outcome'])
                choice = request['decision']['action']
                if choice == 'stop':
                    closure(action, 'ACCEPTED_DECISION_STOP')
                require(receipt == {'proposal_id': child['id'], 'accepted_response_digest': child['accepted_response_digest'],
                    'action': choice, 'target_digest': request['decision']['target_digest'], 'session_id': session['row']['id']})
            else:
                raise SessionError('Unknown completed loop action', code='LOOP_INTEGRITY')

    @staticmethod
    def _descriptor(session):
        source = session['current_source']
        if source['kind'] != 'research_revision':
            raise SessionError('This loop requires a maintained revision', code='SOURCE_STALE')
        return {'kind': 'research_revision', 'research_id': source['research_id'], 'base_revision_id': source['revision_id']}

    def _proposal_request(self, loaded, session, operation, target, effect_key):
        initial = loaded['request']['initial_proposal']
        return ResearchProposalCreate.model_validate({**initial, 'operation': operation,
            'source': self._descriptor(session), 'expected_source_digest': session['current_source']['source_digest'],
            'expected_session_digest': session['state_digest'], 'target': target, 'idempotency_key': effect_key,
            'instruction': ('Repair only the exact target implementation under the supplied constraints.'
                            if operation == 'repair_candidate' else
                            'Suggest one next action from the exact frozen validation result. This is not execution authority.')}).model_dump()

    def _next(self, loaded, session, proposals, experiment, effect_key, *, connection=None):
        """Read current children to derive exactly one permitted next effect."""
        if session['effective_state'] in SESSION_TERMINAL:
            return session['effective_state'], session['stop_reason'], None
        if loaded['actions'] and loaded['actions'][-1]['receipt'] is None:
            return 'recovering', 'RETRY_ORIGINAL_ADVANCE_KEY', None
        if loaded['actions'] and loaded['actions'][-1]['kind'] == 'decision.apply':
            action = loaded['actions'][-1]['request']['decision']['action']
            state = 'awaiting_input' if action == 'request_information' else 'awaiting_handoff'
            return state, 'DECISION_REQUIRES_EXPLICIT_FOLLOWUP', None
        if not proposals:
            return 'ready', None, {'kind': 'proposal.create', 'request': loaded['request']['initial_proposal']}
        child = proposals[-1]
        operation = child['request']['operation']
        reservation = next(r for r in session['reservations'] if (r['children'] or {}).get('proposal_id') == child['id'])
        if child['status'] not in PROPOSAL_TERMINAL:
            return 'waiting_provider', 'PROVIDER_PENDING', None
        if operation == 'decide_next_action' and child['status'] == 'ready':
            from .provider_research_targets import resolve_target
            request = ResearchProposalCreate.model_validate(child['request'])
            resolve_target(self.store, request)
            output = self.proposals._json_path(self.proposals._folder(child['id']) / 'accepted/response.json')
            return 'ready', None, {'kind': 'decision.apply', 'request': {'proposal_id': child['id'],
                'accepted_response_digest': child['accepted_response_digest'], 'decision': output['decision'],
                'token': reservation['token']}}
        if reservation['outcome'] is None:
            return 'ready', None, {'kind': 'proposal.reconcile', 'request': {'token': reservation['token']}}
        if loaded['request']['schema_version'] == 2:
            return self._next_v2(connection, loaded, session, child, experiment, effect_key)
        if child['status'] != 'ready':
            repairs = sum(p['request']['operation'] == 'repair_candidate' for p in proposals)
            if child['status'] == 'rejected' and repairs < loaded['request']['max_repairs']:
                from .provider_repair_semantics import repair_candidate_expression
                from .provider_research_targets import resolve_target, ResearchTargetError
                attempt = child['attempts'][-1]
                parsed = (self.proposals._json_path(self.proposals._folder(child['id']) / 'attempts' / attempt['id'] / 'parsed.json')
                          if attempt['response_digest'] is not None else {})
                candidates = (parsed.get('draft') or {}).get('candidates', [])
                if len(candidates) == 1 and repair_candidate_expression(candidates[0]['expression']) is not None:
                    target = {'proposal_id': child['id'], 'attempt_id': attempt['id'],
                        'response_digest': attempt['response_digest'], 'candidate_id': candidates[0]['id'],
                        'research_job_id': None, 'result_digest': None}
                    request = self._proposal_request(loaded, session, 'repair_candidate', target, effect_key)
                    try:
                        resolve_target(self.store, request)
                    except ResearchTargetError as exc:
                        if exc.code not in {'TARGET_NOT_REPAIRABLE', 'TARGET_ASSERTIONS_INVALID',
                                             'TARGET_REPAIR_SEMANTICS', 'TARGET_ANCESTRY_LIMIT', 'TARGET_REPAIR_SCOPE'}:
                            raise
                    else:
                        return 'ready', None, {'kind': 'proposal.create', 'request': request}
            return 'ready', None, {'kind': 'session.stop', 'request': {'reason':
                'REPAIR_LIMIT_REACHED' if child['status'] == 'rejected' and repairs >= loaded['request']['max_repairs']
                else 'NO_AUTHORIZED_REPAIR'}}
        if experiment is None:
            if not loaded['request']['allow_experiment']:
                return 'awaiting_handoff', 'EXPERIMENT_NOT_AUTHORIZED', None
            request = ExperimentCreate.model_validate({'session_id': session['id'],
                'expected_session_digest': session['state_digest'], 'proposal_id': child['id'],
                'accepted_response_digest': child['accepted_response_digest'],
                'budget': loaded['request']['experiment_budget'], 'idempotency_key': effect_key,
                'note': 'Explicit bounded host-loop experiment; semantic fidelity remains unverified.'}).model_dump()
            return 'ready', None, {'kind': 'experiment.create', 'request': request}
        job = experiment['job']
        if job is None:
            return 'recovering', 'RETRY_ORIGINAL_EXPERIMENT_ACTION', None
        actions = {'created': 'validate', 'validated': 'commit', 'committed': 'submit', 'observed': 'complete'}
        if job['state'] in actions:
            return 'ready', None, {'kind': 'experiment.advance', 'request': {'experiment_id': experiment['id'],
                'action': actions[job['state']]}}
        if job['state'] == 'submitted':
            run = self.store._fetch('runs', job['run_id'])
            if run['status'] in {'queued', 'running', 'cancelling'}:
                return 'waiting_execution', 'RUN_PENDING', None
            return 'ready', None, {'kind': 'experiment.advance', 'request': {'experiment_id': experiment['id'], 'action': 'observe'}}
        if experiment['reservation']['outcome'] is None:
            return 'ready', None, {'kind': 'experiment.reconcile', 'request': {'experiment_id': experiment['id']}}
        if job['state'] != 'completed':
            return 'ready', None, {'kind': 'session.stop', 'request': {'reason': 'EXPERIMENT_' + job['state'].upper()}}
        from .provider_experiment_results import build_experiment_result
        from .provider_research_targets import resolve_target
        result = build_experiment_result(self.store, job['id'])
        target = {'proposal_id': child['id'], 'attempt_id': child['accepted_attempt_id'],
            'response_digest': child['accepted_response_digest'], 'candidate_id': result['candidates'][0]['id'],
            'research_job_id': job['id'], 'result_digest': result['frozen_result_digest']}
        request = self._proposal_request(loaded, session, 'decide_next_action', target, effect_key)
        resolve_target(self.store, request)
        return 'ready', None, {'kind': 'proposal.create', 'request': request}

    def _next_v2(self, connection, loaded, session, child, experiment, effect_key):
        """Deterministic correction follows the original rejected ledger, once.

        Observation produces no repaired expression. Only a later, durably
        charged correction action is allowed to run the policy.
        """
        if loaded['request']['correction_mode'] == 'provider_optional' and child['status'] != 'ready':
            return self._next_provider_correction(connection, loaded, session, child, effect_key)
        corrections = [a for a in loaded['actions'] if a['kind'] == 'correction.apply']
        correction = corrections[-1] if corrections else None
        if child['status'] != 'ready' and correction is None:
            if child['status'] != 'rejected' or len(corrections) >= loaded['request']['max_repairs']:
                reason = 'REPAIR_LIMIT_REACHED' if child['status'] == 'rejected' else 'NO_AUTHORIZED_CORRECTION'
                return 'ready', None, {'kind': 'session.stop', 'request': {'reason': reason}}
            attempt = child['attempts'][-1]
            checks = (attempt.get('validation') or {}).get('candidate_checks', [])
            if len(checks) != 1 or checks[0].get('code') != 'invalid_window':
                return 'ready', None, {'kind': 'session.stop', 'request': {'reason': 'NO_AUTHORIZED_CORRECTION'}}
            from .research_loop_correction import observe_rejected_parent, CorrectionExecutionError
            try:
                observation = observe_rejected_parent(self.store, connection, session_id=session['id'],
                    proposal_id=child['id'], attempt_id=attempt['id'], response_digest=attempt['response_digest'],
                    candidate_id=checks[0]['candidate_id'])
            except CorrectionExecutionError as exc:
                if exc.code not in {'CORRECTION_NOT_REPAIRABLE', 'CORRECTION_SOURCE_UNQUALIFIED',
                                    'CORRECTION_PARENT_UNQUALIFIED', 'CORRECTION_ASSERTIONS_INVALID',
                                    'CORRECTION_PARENT_UNSUPPORTED', 'CORRECTION_NOT_REJECTED',
                                    'CORRECTION_DIAGNOSTIC_UNSUPPORTED', 'CORRECTION_FIELDS_UNAVAILABLE'}:
                    raise
                return 'ready', None, {'kind': 'session.stop', 'request': {'reason': exc.code}}
            sequence = len(loaded['actions']) + 1
            intent = {'schema_version': 1, 'loop_id': loaded['id'], 'session_id': session['id'],
                'sequence': sequence, 'effect_key': effect_key, 'parent_proposal_id': child['id'],
                'parent_attempt_id': attempt['id'], 'parent_response_digest': attempt['response_digest'],
                'candidate_id': checks[0]['candidate_id'], 'source_digest': session['current_source']['source_digest'],
                'capability_digest': observation['capability_digest'],
                'diagnostics_digest': observation['diagnostics_digest'],
                'parent_binding_digest': observation['parent_binding_digest'],
                'policy_id': loaded['request']['correction_policy_id'],
                'policy_digest': loaded['request']['correction_policy_digest'],
                'method': 'deterministic', 'deadline_at': session['deadline_at']}
            return 'ready', None, {'kind': 'correction.apply', 'request': intent}
        if child['status'] != 'ready':
            if correction['correction']['status'] != 'completed':
                return 'ready', None, {'kind': 'session.stop', 'request': {'reason': 'CORRECTION_BLOCKED'}}
            if experiment is None:
                if not loaded['request']['allow_experiment']:
                    return 'awaiting_handoff', 'EXPERIMENT_NOT_AUTHORIZED', None
                receipt = correction['correction']['bundle']['receipt']
                source = {'kind': 'host_corrected_rejected_candidate', 'loop_id': loaded['id'],
                    'correction_action_sequence': correction['sequence'],
                    'intent_digest': receipt['intent_digest'], 'correction_receipt_digest': receipt['receipt_digest'],
                    'parent_proposal_id': child['id'], 'parent_attempt_id': correction['request']['parent_attempt_id'],
                    'parent_response_digest': correction['request']['parent_response_digest'],
                    'accepted_task_digest': receipt['accepted_task_digest']}
                request = ExperimentCreateV2.model_validate({'schema_version': 2, 'session_id': session['id'],
                    'expected_session_digest': session['state_digest'], 'candidate_source': source,
                    'budget': loaded['request']['experiment_budget'], 'idempotency_key': effect_key,
                    'note': 'Host-corrected representation; original rejected response is retained.'}).model_dump()
                return 'ready', None, {'kind': 'experiment.create', 'request': request}
        elif experiment is None:
            if not loaded['request']['allow_experiment']:
                return 'awaiting_handoff', 'EXPERIMENT_NOT_AUTHORIZED', None
            request = ExperimentCreate.model_validate({'session_id': session['id'],
                'expected_session_digest': session['state_digest'], 'proposal_id': child['id'],
                'accepted_response_digest': child['accepted_response_digest'],
                'budget': loaded['request']['experiment_budget'], 'idempotency_key': effect_key,
                'note': 'Explicit deterministic-loop development experiment.'}).model_dump()
            return 'ready', None, {'kind': 'experiment.create', 'request': request}
        job = experiment['job']
        if job is None:
            return 'recovering', 'RETRY_ORIGINAL_EXPERIMENT_ACTION', None
        transitions = {'created': 'validate', 'validated': 'commit', 'committed': 'submit', 'observed': 'complete'}
        if job['state'] in transitions:
            return 'ready', None, {'kind': 'experiment.advance', 'request': {
                'experiment_id': experiment['id'], 'action': transitions[job['state']]}}
        if job['state'] == 'submitted':
            run = self.store._fetch('runs', job['run_id'])
            if run['status'] in {'queued', 'running', 'cancelling'}:
                return 'waiting_execution', 'RUN_PENDING', None
            return 'ready', None, {'kind': 'experiment.advance', 'request': {
                'experiment_id': experiment['id'], 'action': 'observe'}}
        if experiment['reservation']['outcome'] is None:
            return 'ready', None, {'kind': 'experiment.reconcile', 'request': {'experiment_id': experiment['id']}}
        if job['state'] != 'completed':
            return 'ready', None, {'kind': 'session.stop', 'request': {'reason': 'EXPERIMENT_' + job['state'].upper()}}
        return 'awaiting_handoff', 'DEVELOPMENT_RESULT_REQUIRES_REVIEW', None

    def _next_provider_correction(self, connection, loaded, session, child, effect_key):
        """Plan an observed, original-queue repair, without running a policy pass."""
        repairs = [a for a in loaded['actions'] if a['kind'] == 'proposal.create'
                   and a['request'].get('operation') == 'repair_candidate']
        if repairs or child['status'] != 'rejected' or child['request']['operation'] != 'generate_candidates':
            return 'ready', None, {'kind': 'session.stop', 'request': {'reason':
                'REPAIR_LIMIT_REACHED' if repairs else 'NO_AUTHORIZED_CORRECTION'}}
        attempt = child['attempts'][-1]
        checks = (attempt.get('validation') or {}).get('candidate_checks', [])
        if len(checks) != 1 or checks[0].get('code') != 'invalid_window':
            return 'ready', None, {'kind': 'session.stop', 'request': {'reason': 'NO_AUTHORIZED_CORRECTION'}}
        from .research_loop_correction import observe_rejected_parent, CorrectionExecutionError
        from .provider_correction_native import correction_target
        try:
            observed = observe_rejected_parent(self.store, connection, session_id=session['id'],
                proposal_id=child['id'], attempt_id=attempt['id'], response_digest=attempt['response_digest'],
                candidate_id=checks[0]['candidate_id'])
        except CorrectionExecutionError as exc:
            if exc.code in {'CORRECTION_SOURCE_UNQUALIFIED', 'CORRECTION_PARENT_UNSUPPORTED',
                           'CORRECTION_DIAGNOSTIC_UNSUPPORTED', 'CORRECTION_ASSERTIONS_INVALID',
                           'CORRECTION_FIELDS_UNAVAILABLE'}:
                return 'ready', None, {'kind': 'session.stop', 'request': {'reason': exc.code}}
            raise
        target = correction_target(observed, loop_id=loaded['id'], sequence=len(loaded['actions']) + 1,
            effect_key=effect_key, policy_id=loaded['request']['correction_policy_id'],
            policy_digest=loaded['request']['correction_policy_digest'])
        return 'ready', None, {'kind': 'proposal.create', 'request':
            self._proposal_request(loaded, session, 'repair_candidate', target, effect_key)}

    def _dispatch_correction(self, identity, original):
        """Persist start before one pass; unknown starts are never rerun/refunded."""
        from .research_loop_correction import execute_correction, verify_correction_bundle
        # The original local queue lock prevents a concurrent recovery from
        # treating an executor still in its rule pass as a lost outcome.
        with self.proposals._lock('loop-correction-' + digest(identity)):
            with transaction(self.store.db_path) as connection:
                loaded = self._load(connection, identity)
                action = next(a for a in loaded['actions'] if a['sequence'] == original['sequence'])
                correction = action['correction']
                if correction['status'] == 'planned':
                    self.sessions._open(loaded['loaded_session'])
                    self.sessions._resolve(connection, loaded['loaded_session']['state']['current_source'])
                    action = {**action, 'correction': {**correction, 'status': 'started',
                        'started_at': self.sessions._now().isoformat()}}
                    self._save_action(connection, loaded, action)
                    should_execute = True
                else:
                    should_execute = False
            if should_execute:
                with closing(connect(self.store.db_path)) as connection:
                    connection.execute('BEGIN')
                    bundle = execute_correction(self.store, connection, loop_request=loaded['request'],
                        action=action, created_at=self.sessions._now().isoformat())
                with transaction(self.store.db_path) as connection:
                    loaded = self._load(connection, identity)
                    latest = next(a for a in loaded['actions'] if a['sequence'] == action['sequence'])
                    if latest != action:
                        raise SessionError('Correction start changed during execution', code='LOOP_INTEGRITY')
                    action = {**latest, 'correction': {**latest['correction'],
                        'status': bundle['receipt']['status'], 'bundle': bundle}}
                    self._save_action(connection, loaded, action)
                    outcome = self._correction_result(action)
                    self.sessions.finish(loaded['row']['session_id'], action['correction']['reservation_token'],
                                         outcome, _connection=connection)
            else:
                with transaction(self.store.db_path) as connection:
                    loaded = self._load(connection, identity)
                    action = next(a for a in loaded['actions'] if a['sequence'] == original['sequence'])
                    if action['correction']['status'] in {'completed', 'blocked'}:
                        verify_correction_bundle(self.store, connection, loop_request=loaded['request'], action=action)
                    elif action['correction']['status'] == 'started':
                        action = {**action, 'correction': {**action['correction'], 'status': 'unknown'}}
                        self._save_action(connection, loaded, action)
                    elif action['correction']['status'] != 'unknown':
                        raise SessionError('Correction state is inconsistent', code='LOOP_INTEGRITY')
                    self.sessions.finish(loaded['row']['session_id'], action['correction']['reservation_token'],
                                         self._correction_result(action), _connection=connection)
            correction = action['correction']
            return {'status': correction['status'], 'correction_receipt_digest':
                    correction['bundle']['receipt']['receipt_digest'] if correction['bundle'] else None}

    @staticmethod
    def _correction_result(action):
        correction = action['correction']
        if correction['status'] == 'unknown':
            return {'status': 'interrupted', 'code': 'OUTCOME_UNKNOWN', 'result_digest': None}
        receipt = correction['bundle']['receipt']
        return {'status': 'awaiting_handoff' if correction['status'] == 'completed' else 'failed',
                'code': 'CORRECTION_READY' if correction['status'] == 'completed' else 'CORRECTION_BLOCKED',
                'result_digest': receipt['receipt_digest']}

    def get(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            loaded = self._load(connection, identity)
            session = self.sessions._view(loaded['loaded_session'])
            proposals, experiment = self._children(connection, loaded)
            state, reason, following = self._planned_next(loaded, session, proposals, experiment,
                self._effect_key(identity, len(loaded['actions']) + 1), connection=connection)
            return {'schema_version': loaded['request']['schema_version'], 'id': identity, 'session_id': session['id'],
                'created_at': loaded['row']['created_at'], 'request_digest': loaded['row']['request_digest'],
                'request': deepcopy(loaded['request']), 'state': state, 'reason': reason,
                'next_action': following['kind'] if following else None, 'actions': deepcopy(loaded['actions']),
                'proposals': proposals, 'experiment': experiment, 'session': session,
                'semantic_fidelity': 'unverified'}

    def _planned_next(self, loaded, session, proposals, experiment, effect_key, *, connection=None):
        state, reason, following = self._next(loaded, session, proposals, experiment, effect_key, connection=connection)
        if following and following['kind'] in {'proposal.create', 'experiment.create'}:
            charges = ['tool_actions', 'provider_dispatches' if following['kind'] == 'proposal.create' else 'experiments']
            if any(session['usage'][name] >= session['budget']['max_' + name] for name in charges):
                # A safety stop has no provider/experiment effect to charge. The
                # queue still rechecks live budgets at each actual transaction.
                following = {'kind': 'session.stop', 'request': {'reason': 'SESSION_ACTION_BUDGET_EXHAUSTED'}}
        if following and following['kind'] == 'correction.apply' and session['usage']['tool_actions'] >= session['budget']['max_tool_actions']:
            following = {'kind': 'session.stop', 'request': {'reason': 'SESSION_ACTION_BUDGET_EXHAUSTED'}}
        return state, reason, following

    @staticmethod
    def _effect_key(identity, sequence):
        return 'loop-effect-' + digest({'loop_id': identity, 'sequence': sequence})

    @staticmethod
    def _correction_read_key(identity, sequence):
        # A reserved namespace makes missing owner rows fail closed rather than
        # downgrading a charged host correction to an unrestricted manual read.
        return 'loop-correction-read-' + digest({'loop_id': identity, 'sequence': sequence})

    def _save_action(self, connection, loaded, action):
        connection.execute('UPDATE research_loop_actions SET payload=?,digest=? WHERE loop_id=? AND sequence=?',
                           (_text(action), digest(action), loaded['id'], action['sequence']))
        actions = loaded['actions'][:-1] + [action]
        connection.execute('UPDATE research_loops SET action_count=?,ledger_digest=? WHERE id=?',
                           (len(actions), digest([digest(item) for item in actions]), loaded['id']))

    def advance(self, identity, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or key.strip() != key:
            raise SessionError('Advance requires a bounded nonempty key', 422, code='INVALID_KEY')
        with transaction(self.store.db_path) as connection:
            loaded = self._load(connection, identity)
            old = next((a for a in loaded['actions'] if a['idempotency_key'] == key), None)
            if old is not None:
                action = old
            else:
                if loaded['actions'] and loaded['actions'][-1]['receipt'] is None:
                    raise SessionError('Recover the unfinished action with its original key', code='LOOP_ACTION_UNRESOLVED')
                if len(loaded['actions']) >= MAX_ACTIONS:
                    raise SessionError('Loop action bound reached', code='LOOP_ACTION_LIMIT')
                session = self.sessions._view(loaded['loaded_session'])
                proposals, experiment = self._children(connection, loaded)
                sequence = len(loaded['actions']) + 1
                effect_key = self._effect_key(identity, sequence)
                state, reason, following = self._planned_next(loaded, session, proposals, experiment, effect_key, connection=connection)
                if following is None:
                    raise SessionError('Loop has no permitted next effect: ' + (reason or state), code='LOOP_WAITING')
                action = {'loop_id': identity, 'sequence': sequence, 'idempotency_key': key,
                    'effect_key': effect_key, **following, 'planned_at': self.sessions._now().isoformat(),
                    'previous_digest': digest(loaded['actions'][-1]) if loaded['actions'] else None,
                    'session_digest': session['state_digest'], 'receipt': None}
                if action['kind'] == 'correction.apply':
                    reserved = self.sessions.reserve(session['id'], 'read',
                        self._correction_read_key(identity, sequence), session['state_digest'],
                        action_digest=digest(action['request']), _connection=connection)
                    action['correction'] = {'intent_digest': digest(action['request']),
                        'reservation_token': reserved['reservation']['token'], 'status': 'planned',
                        'started_at': None, 'bundle': None}
                connection.execute('INSERT INTO research_loop_actions VALUES (?,?,?,?,?)',
                    (identity, sequence, key, _text(action), digest(action)))
                loaded['actions'].append(action)
                self._save_action(connection, loaded, action)
        if action['receipt'] is not None:
            return self.get(identity)
        receipt = self._dispatch(identity, action)
        with transaction(self.store.db_path) as connection:
            loaded = self._load(connection, identity)
            latest = next(a for a in loaded['actions'] if a['idempotency_key'] == key)
            if latest['receipt'] is not None and latest['receipt'] != receipt:
                raise SessionError('Loop effect acknowledgement differs', code='LOOP_INTEGRITY')
            if latest['receipt'] is None:
                self._save_action(connection, loaded, {**latest, 'receipt': receipt})
        return self.get(identity)

    def _dispatch(self, identity, action):
        request, key = action['request'], action['effect_key']
        with closing(connect(self.store.db_path)) as connection:
            loaded = self._load(connection, identity)
        session_id = loaded['row']['session_id']
        if action['kind'] == 'correction.apply':
            return self._dispatch_correction(identity, action)
        if action['kind'] == 'proposal.create':
            return {'proposal_id': self.execution.propose(request)['proposal']['id']}
        if action['kind'] == 'proposal.reconcile':
            result = self.execution.reconcile(session_id, request['token'])
            if result['reservation']['outcome'] is None:
                raise SessionError('Proposal outcome is still unknown', code='LOOP_ACTION_UNRESOLVED')
            receipt = {'token': request['token'], 'outcome_digest': digest(result['reservation']['outcome'])}
            child_id = (result['reservation']['children'] or {}).get('proposal_id')
            if child_id:
                child = self.proposals.get(child_id)
                from .provider_correction_native import is_provider_correction, correction_receipt
                if is_provider_correction(child['request']):
                    with closing(connect(self.store.db_path)) as connection:
                        connection.execute('BEGIN')
                        receipt['correction_receipt'] = correction_receipt(self.store, connection, child,
                            created_at=action['planned_at'])
            return receipt
        if action['kind'] == 'experiment.create':
            return {'experiment_id': self.experiments.create(request)['id']}
        if action['kind'] == 'experiment.advance':
            if request['action'] in {'observe', 'complete'}:
                with closing(connect(self.store.db_path)) as connection:
                    connection.execute('BEGIN')
                    current = self._load(connection, identity)
                    receipt = self._automatic_finalization_ack(connection, current, action)
                if receipt is not None:
                    return receipt
            result = self.experiments.advance(request['experiment_id'], request['action'], key)
            step = next(s for s in result['job']['steps'] if s['idempotency_key'] == key)
            return {'experiment_id': result['id'], 'step_id': step['id'], 'step_digest': digest(step)}
        if action['kind'] == 'experiment.reconcile':
            result = self.experiments.reconcile(request['experiment_id'])
            return {'experiment_id': result['id'], 'outcome_digest': digest(result['reservation']['outcome'])}
        if action['kind'] == 'session.stop':
            value = self.sessions.outcome(session_id, action['session_digest'], key,
                                          status='stopped', reason=request['reason'])
            return {'session_id': session_id, 'state': value['state'], 'reason': request['reason']}
        if action['kind'] == 'decision.apply':
            return self._apply_decision(loaded, action)
        raise SessionError('Unrecognized loop effect', code='LOOP_INTEGRITY')

    def _apply_decision(self, loaded, action):
        from .provider_research_targets import resolve_target, validate_decision_output
        request = action['request']
        child = self.proposals.get(request['proposal_id'])
        target = resolve_target(self.store, ResearchProposalCreate.model_validate(child['request']))
        output = self.proposals._json_path(self.proposals._folder(child['id']) / 'accepted/response.json')
        if (child['status'] != 'ready' or child['accepted_response_digest'] != request['accepted_response_digest']
                or output['decision'] != request['decision'] or validate_decision_output(target, output['decision'])):
            raise SessionError('Decision no longer matches its accepted effect', code='LOOP_INTEGRITY')
        choice = request['decision']['action']
        # Use the same canonical completion as the generic reconciliation API.
        # The loop's own host policy records how to act on the suggestion.
        current = self.execution.reconcile(loaded['row']['session_id'], request['token'])['session']
        if choice == 'stop':
            if current['reservations'][-1]['token'] != request['token']:
                raise SessionError('Another action intervened before decision stop', code='SESSION_CONFLICT')
            current = self.sessions.outcome(current['id'], current['state_digest'], action['effect_key'],
                                           status='stopped', reason='ACCEPTED_DECISION_STOP')
        return {'proposal_id': child['id'], 'accepted_response_digest': child['accepted_response_digest'],
                'action': choice, 'target_digest': request['decision']['target_digest'], 'session_id': current['id']}

    def cancel(self, identity, key):
        with closing(connect(self.store.db_path)) as connection:
            loaded = self._load(connection, identity)
            proposals, _ = self._children(connection, loaded)
            unknown = {child['id'] for child in proposals if child['status'] == 'interrupted'}
            tokens = [r['token'] for r in loaded['loaded_session']['reservations']
                      if r['outcome'] is None and (r['children'] or {}).get('proposal_id') in unknown]
            session_id = loaded['row']['session_id']
        # A safety cancellation is not evidence that an unknown provider outcome
        # became known. Preserve that observation before propagating the stop.
        for token in tokens:
            self.execution.reconcile(session_id, token)
        session = self.sessions.get(session_id)
        self.execution.cancel(session['id'], session['state_digest'], key)
        return self.get(identity)
