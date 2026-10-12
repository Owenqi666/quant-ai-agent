"""Connect budget reservations to the existing proposal queue.

The queue calls the transaction hooks immediately around its own insertion and
before claiming an attempt. There is no second queue or copy of child status.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy

from ..storage import digest
from .db import connect, transaction
from .provider_contract import digest as wire_digest
from .provider_research_schema import ResearchProposalCreate
from .research_sessions import ResearchSessions, SessionError, _timestamp

OPERATIONS = {'extract_material': 'extract', 'generate_candidates': 'generate',
              'repair_candidate': 'repair', 'decide_next_action': 'decide'}
TARGET_OPERATIONS = frozenset({'repair_candidate', 'decide_next_action'})


class SessionExecution:
    def __init__(self, store, proposals=None, *, sessions=None):
        self.store = store
        self.proposals = proposals
        self.sessions = sessions or ResearchSessions(store)

    @staticmethod
    def _request(request):
        return ResearchProposalCreate.model_validate(request).model_dump()

    def _supported(self, request, config, *, connection=None, phase='create'):
        if request['operation'] not in OPERATIONS:
            raise SessionError('This research operation is not yet connected', 422, code='OPERATION_NOT_ENABLED')
        if request['operation'] in TARGET_OPERATIONS:
            from .provider_research_capabilities import verify_capabilities
            verify_capabilities()
        if hasattr(config, 'model_dump'):
            config = config.model_dump()
        authority = getattr(self.proposals, 'research_authority', None)
        bound = False
        if authority is not None and hasattr(authority, 'owns_session'):
            if connection is None:
                with closing(connect(self.store.db_path)) as opened:
                    bound = authority.owns_session(opened, request['session_id'])
            else:
                bound = authority.owns_session(connection, request['session_id'])
        if config.get('mode') != 'fixture' or bound:
            if authority is None:
                raise SessionError('Research provider v2 has no explicit host authority', 503,
                                   code='RESEARCH_V2_REAL_UNQUALIFIED')
            if connection is None:
                with closing(connect(self.store.db_path)) as opened:
                    opened.execute('BEGIN')
                    authority.verify_request(opened, request, config, phase=phase)
            else:
                authority.verify_request(connection, request, config, phase=phase)
        return config

    def _binding(self, connection, request, config, *, phase='create'):
        request = self._request(request)
        config = self._supported(request, config, connection=connection, phase=phase)
        loaded = self.sessions._load(connection, request['session_id'])
        reservation = next((r for r in loaded['reservations'] if r['effect_key'] == request['idempotency_key']), None)
        if reservation is None:
            raise SessionError('Proposal has no prior durable budget reservation', code='RESERVATION_REQUIRED')
        if (reservation['action'] != OPERATIONS[request['operation']]
                or reservation['action_digest'] != digest(request)):
            raise SessionError('Proposal differs from its original reserved request', code='RESERVATION_REQUEST_MISMATCH')
        expected_source = reservation['source']
        descriptor = (expected_source['source_request'] if expected_source['kind'] != 'research_revision' else
                      {'kind': 'research_revision', 'research_id': expected_source['research_id'],
                       'base_revision_id': expected_source['revision_id']})
        if (request['source'] != descriptor or request['expected_source_digest'] != reservation['source_digest']
                or loaded['state']['current_source'] != expected_source):
            raise SessionError('Proposal differs from the reserved source', code='SOURCE_STALE')
        frozen_config = loaded['request']['config']
        if (frozen_config['contract_id'] != request['contract_id']
                or frozen_config['provider_config_id'] != request['provider_config_id']
                or frozen_config['provider_config_id'] != config['config_id']
                or frozen_config['provider_config_digest'] != wire_digest(config)):
            raise SessionError('Proposal differs from the frozen session configuration', code='SESSION_CONFIG_MISMATCH')
        reserved_event = next((index for index, event in enumerate(loaded['events'])
                               if event['kind'] == 'reserved' and event['reservation']['token'] == reservation['token']), None)
        if (reserved_event is None or reserved_event == 0
                or digest(loaded['events'][reserved_event - 1]['state_after']) != request['expected_session_digest']):
            raise SessionError('Proposal does not name the state preceding its reservation', code='SESSION_CONFLICT')
        if (reservation['charges']['provider_dispatches'] != 1
                or request['budget']['max_provider_invocations'] != 1):
            raise SessionError('Provider request exceeds its dispatch permit', code='CHILD_BUDGET_MISMATCH')
        return request, loaded, reservation

    def session_deadline(self, connection, request):
        """Read the absolute cap; reading it alone does not authorize an effect."""
        request = self._request(request)
        return self.sessions._load(connection, request['session_id'])['row']['deadline_at']

    def _target_context(self, connection, request, expected=None):
        if request['operation'] not in TARGET_OPERATIONS:
            return None
        from .provider_contract_registry import request_handler
        context = request_handler(request).build_context(self.store, request)
        if expected is not None and context != expected:
            raise SessionError('Target or historical prompt snapshot changed', code='SOURCE_CHANGED')
        return context

    def authorize_proposal_create(self, connection, request, config, *, deadline_at, context=None):
        request, loaded, reservation = self._binding(connection, request, config)
        self.sessions._open(loaded)
        if connection.execute("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'").fetchone():
            raise SessionError('Proposal dispatch is paused; inspect its recovery state', 503, code='DISPATCH_PAUSED')
        if reservation['status'] != 'reserved' or reservation['children'] is not None:
            raise SessionError('The original reservation already owns an effect or outcome', code='EFFECT_ALREADY_RECORDED')
        if (_timestamp(deadline_at) > _timestamp(loaded['row']['deadline_at'])
                or _timestamp(deadline_at) <= self.sessions._now()):
            raise SessionError('Proposal deadline exceeds its live session permit', code='CHILD_BUDGET_MISMATCH')
        self.sessions._resolve(connection, reservation['source'])
        self._target_context(connection, request, context)
        self.sessions._open(loaded)
        if _timestamp(deadline_at) <= self.sessions._now():
            raise SessionError('Proposal deadline elapsed while verifying its target', code='SESSION_EXPIRED')
        return deepcopy(reservation)

    def record_proposal_created(self, connection, proposal):
        request, _, reservation = self._binding(connection, proposal['request'], proposal['config'])
        return self.sessions.record_effect_in_transaction(connection, request['session_id'], reservation['token'],
                                                          {'proposal_id': proposal['id']})

    def authorize_proposal_dispatch(self, connection, proposal, *, phase='claim'):
        request, loaded, reservation = self._binding(connection, proposal['request'], proposal['config'], phase=phase)
        self.sessions._open(loaded)
        if proposal['status'] not in {'queued', 'running'}:
            raise SessionError('Proposal is no longer dispatchable', code='CHILD_NOT_DISPATCHABLE')
        if (reservation['status'] != 'effect_recorded' or reservation['outcome'] is not None
                or reservation['children'] != {'proposal_id': proposal['id'], 'research_job_id': None, 'run_id': None}):
            raise SessionError('Proposal is not the unresolved child of its permit', code='CHILD_IDENTITY_MISMATCH')
        self.sessions._children(connection, loaded, reservation, reservation['children'])
        self.sessions._resolve(connection, reservation['source'])
        self._target_context(connection, request, proposal['context'])
        self.sessions._open(loaded)
        remaining = (_timestamp(loaded['row']['deadline_at']) - self.sessions._now()).total_seconds()
        remaining = min(remaining, (_timestamp(proposal['deadline_at']) - self.sessions._now()).total_seconds())
        if remaining <= 0:
            raise SessionError('The child deadline expired before dispatch', code='SESSION_EXPIRED')
        return remaining

    def propose(self, body):
        """Persist a reservation, then create the child with its unchanged key."""
        if self.proposals is None:
            raise RuntimeError('The existing ProposalRequests service must be supplied')
        request = self._request(body)
        prior = self.proposals.replay_create(ResearchProposalCreate.model_validate(request))
        if prior is not None:
            # A lost acknowledgement is recovered before applying today's
            # permission or source state. Verify the historical association,
            # without interpreting it as authorization for another dispatch.
            with closing(connect(self.store.db_path)) as connection:
                connection.execute('BEGIN')
                loaded = self.sessions._load(connection, request['session_id'])
                reservation = next((r for r in loaded['reservations']
                                    if r['effect_key'] == request['idempotency_key']), None)
                if reservation is None or reservation['children'] is None:
                    raise SessionError('Existing proposal lost its atomic session association', code='LEDGER_INTEGRITY')
                self.sessions._children(connection, loaded, reservation, reservation['children'])
                if reservation['children']['proposal_id'] != prior['id']:
                    raise SessionError('Existing proposal names another reserved effect', code='CHILD_IDENTITY_MISMATCH')
                return {'proposal': prior, 'session': self.sessions._view(loaded)}
        config = self.proposals.configs.get(request['provider_config_id'])
        if config is None or not config.enabled:
            raise SessionError('Provider is not configured and enabled', 503, code='PROVIDER_DISABLED')
        self._supported(request, config)
        # Reject malformed sources and configuration before spending budget. The
        # create hook rechecks them under the queue's write transaction.
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            loaded = self.sessions._load(connection, request['session_id'])
            frozen_config = loaded['request']['config']
            if (frozen_config['contract_id'] != request['contract_id']
                    or frozen_config['provider_config_id'] != request['provider_config_id']
                    or frozen_config['provider_config_digest'] != wire_digest(config.model_dump())):
                raise SessionError('Provider differs from the frozen session configuration', code='SESSION_CONFIG_MISMATCH')
            source = loaded['state']['current_source']
            descriptor = source.get('source_request') or {'kind': 'research_revision',
                'research_id': source['research_id'], 'base_revision_id': source['revision_id']}
            if descriptor != request['source'] or source['source_digest'] != request['expected_source_digest']:
                raise SessionError('Proposal differs from its frozen session source', code='SOURCE_STALE')
            if request['operation'] in TARGET_OPERATIONS:
                self.sessions._open(loaded)
                context = self._target_context(connection, request)
                from .provider_contract_registry import request_handler
                request_handler(request).build_prompt(request, context, 'proposal_' + digest(request), 'proposal_attempt_' + '0' * 64)
        with transaction(self.store.db_path) as connection:
            reserved = self.sessions.reserve(request['session_id'], OPERATIONS[request['operation']], request['idempotency_key'],
                              request['expected_session_digest'], action_digest=digest(request), _connection=connection)
            authority = getattr(self.proposals, 'research_authority', None)
            if authority is not None and hasattr(authority, 'reserve_session_action'):
                authority.reserve_session_action(connection, request, reserved['reservation'])
        # Exceptions deliberately leave the original reservation charged. A retry
        # uses the same request and key, and the insertion hook checks time again.
        proposal = self.proposals.create(ResearchProposalCreate.model_validate(request))
        return {'proposal': proposal, 'session': self.sessions.get(request['session_id'])}

    def reconcile(self, identity, token):
        """Derive completion from the actual child ledger, never client verdicts."""
        with closing(connect(self.store.db_path)) as connection:
            experiment = connection.execute('SELECT id FROM research_session_experiments WHERE session_id=? AND reservation_token=?', (identity, token)).fetchone()
        if experiment:
            from .research_session_experiments import SessionExperiments
            value = SessionExperiments(self.store).reconcile(experiment['id'])
            return {'reservation': value['reservation'], 'session': value['session'], 'replayed': False,
                    'observation': 'experiment_reconciled'}
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            loaded = self.sessions._load(connection, identity)
            reservation = self.sessions._reservation(loaded, token)
            if reservation['outcome'] is not None:
                return {'reservation': deepcopy(reservation), 'session': self.sessions._view(loaded), 'replayed': True}
            if reservation['children'] is None:
                return {'reservation': deepcopy(reservation), 'session': self.sessions._view(loaded),
                        'replayed': False, 'observation': 'no_child_retry_original_request'}
            if not reservation['children']['proposal_id']:
                raise SessionError('Experiment reconciliation is a later integration stage', 422, code='OPERATION_NOT_ENABLED')
            from .proposal_requests import ProposalRequests
            child = ProposalRequests(self.store, configs={})._load(connection, reservation['children']['proposal_id'])
            actual_digest = child['accepted_response_digest'] or (child['attempts'][-1]['response_digest'] if child['attempts'] else None)
            status = child['status']
            if status in {'queued', 'running', 'validating', 'cancelling', 'handing_off'}:
                return {'reservation': deepcopy(reservation), 'session': self.sessions._view(loaded),
                        'replayed': False, 'observation': 'child_pending'}
            if status == 'interrupted':
                outcome, code = 'interrupted', 'OUTCOME_UNKNOWN'
            elif status == 'abstained':
                outcome, code = 'awaiting_input', 'PROVIDER_ABSTAINED'
            elif status in {'ready', 'handed_off'}:
                outcome, code = 'awaiting_handoff', 'PROPOSAL_READY'
            else:
                outcome, code = 'failed', 'PROPOSAL_' + status.upper()
        return self.sessions.finish(identity, token, {'status': outcome, 'code': code, 'result_digest': actual_digest})

    def cancel(self, identity, expected_digest, key):
        """Stop the parent first, then idempotently propagate its safety stop."""
        session = self.sessions.cancel(identity, expected_digest, key)
        from .proposal_requests import ProposalRequests
        proposals = self.proposals or ProposalRequests(self.store, configs={})
        children = []
        for reservation in session['reservations']:
            linked = reservation['children']
            if linked and linked['proposal_id']:
                child = proposals.cancel(linked['proposal_id'], session_id=identity)
                children.append({'proposal_id': child['id'], 'status': child['status']})
        from .research_session_experiments import cancel_session_experiments
        cancel_session_experiments(self.store, identity)
        return {'session': self.sessions.get(identity), 'children': children}


def proposal_dispatch_guard(store, proposal_id, *, proposals=None):
    from .proposal_requests import ProposalRequests
    proposals = proposals or ProposalRequests(store, configs={})
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        proposal = proposals._load(connection, proposal_id)
        return SessionExecution(store, proposals).authorize_proposal_dispatch(connection, proposal)


def proposal_session_cancelled(store, proposal):
    if proposal['request'].get('schema_version', 1) != 2:
        return False
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        sessions = ResearchSessions(store)
        loaded = sessions._load(connection, proposal['request']['session_id'])
        view = sessions._view(loaded)
        return view['effective_state'] in {'completed', 'stopped', 'cancelled', 'exhausted', 'interrupted'}
