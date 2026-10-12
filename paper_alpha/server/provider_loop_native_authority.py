"""Versioned local Native permission tied to the original session ledger.

The permanent slot is an association with an existing ResearchSessions charge.
It never refunds a failed preflight, issues a replacement attempt, or treats a
domain rejection as transport failure. No I/O runs at import time.
"""
from contextlib import closing, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from pathlib import Path

from ..storage import digest, json_text
from .db import connect, transaction
from .provider_contract import digest as wire_digest, strict_json
from .provider_host_authority import HostAuthorityError, _decode, _need, _now, _workspace, _profile
from .provider_loop_native_authority_schema import AUTHORITY_ID, LoopNativeGrantCreate
from .provider_research_schema import CONTRACT_ID, ResearchProposalCreate
from .service import ServiceError
from .provider_native_transport_identity import (native_transport, authority_profile_digest,
    app_server_operation_supported)

_FILES = ('provider_loop_native_authority.py', 'provider_loop_native_authority_schema.py',
          'research_sessions.py', 'research_session_execution.py', 'provider_research_targets.py',
          'provider_research_schema.py', 'provider_repair_semantics.py', 'provider_contract_registry.py',
          'research_correction_policy.py', 'research_correction_schema.py', 'research_loop_correction.py',
          'research_loops.py', 'research_loops_schema.py', 'provider_correction_native.py',
          'provider_experiment_source.py')
_ROOT = Path(__file__).resolve().parent


def _current_profile(config):
    return {**_profile(config), 'loop_source_digests': {
        name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest() for name in _FILES}}


def _save_slot(connection, value):
    connection.execute('''UPDATE provider_loop_native_slots SET session_id=?,reservation_token=?,
        proposal_id=?,attempt_id=?,payload=?,digest=? WHERE grant_id=? AND call_id=?''',
        (value['session_id'], value['reservation_token'], value['proposal_id'], value['attempt_id'],
         json_text(value), digest(value), value['grant_id'], value['call_id']))


class LoopNativeResearchAuthority:
    """A concrete trusted leaf; an arbitrary router is never a Native permit."""
    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    def _grant(self, connection, identity):
        row = connection.execute('SELECT * FROM provider_loop_native_grants WHERE id=?', (identity,)).fetchone()
        grant = _decode(row)
        _need(grant['id'] == row['id'] == 'loop_native_grant_' + digest(grant['request'])
              and grant['request']['idempotency_key'] == row['idempotency_key'], 'Native grant identity differs')
        _need(grant['workspace_id'] == _workspace(connection), 'Native grant belongs to another workspace', 'LOOP_NATIVE_WORKSPACE_MISMATCH')
        slots = connection.execute('SELECT * FROM provider_loop_native_slots WHERE grant_id=?', (identity,)).fetchall()
        actual = {_decode(row)['spec']['call_id']: _decode(row)['spec'] for row in slots}
        _need(actual == {s['call_id']: s for s in grant['request']['slots']}, 'Permanent program slot inventory changed', 'LOOP_NATIVE_LEDGER_INTEGRITY')
        for row in slots:
            value = _decode(row)
            _need(all(value[k] == row[k] for k in ('grant_id', 'call_id', 'effect_key', 'session_id',
                        'reservation_token', 'proposal_id', 'attempt_id')), 'Program slot index differs', 'LOOP_NATIVE_LEDGER_INTEGRITY')
            if value['reservation_token'] is not None:
                from .research_sessions import ResearchSessions
                loaded = ResearchSessions(self.store)._load(connection, value['session_id'])
                reserved = ResearchSessions._reservation(loaded, value['reservation_token'])
                _need(reserved['effect_key'] == value['effect_key'] and reserved['action_digest'] == value['request_digest']
                      and reserved['charges']['provider_dispatches'] == 1, 'Program slot lost its original charge', 'LOOP_NATIVE_LEDGER_INTEGRITY')
                if value['proposal_id'] is not None:
                    _need((reserved['children'] or {}).get('proposal_id') == value['proposal_id'], 'Program slot owns another original child', 'LOOP_NATIVE_LEDGER_INTEGRITY')
                if value['attempt_id'] is not None:
                    attempt = connection.execute('SELECT proposal_id FROM proposal_attempts WHERE id=?', (value['attempt_id'],)).fetchone()
                    _need(attempt is not None and attempt['proposal_id'] == value['proposal_id'], 'Charged Native attempt is missing', 'LOOP_NATIVE_LEDGER_INTEGRITY')
        original_attempts = {row[0] for row in connection.execute(
            "SELECT a.id FROM proposal_attempts a JOIN proposal_requests p ON p.id=a.proposal_id "
            "JOIN provider_loop_native_sessions s ON s.session_id=json_extract(p.payload,'$.request.session_id') WHERE s.grant_id=?",
            (identity,))}
        _need(original_attempts == {row['attempt_id'] for row in slots if row['attempt_id'] is not None},
              'Program permit inventory differs from original charged attempts', 'LOOP_NATIVE_LEDGER_INTEGRITY')
        return grant

    def _active(self, connection, grant):
        config = self.registry.ensure_dispatchable(grant['request']['provider_config_id'])
        _need(config.model_dump() == grant['config'] and _current_profile(config) == grant['profile'],
              'Native grant profile or source changed', 'LOOP_NATIVE_PROFILE_CHANGED')
        revoked = connection.execute('SELECT * FROM provider_loop_native_revocations WHERE grant_id=?', (grant['id'],)).fetchone()
        if revoked is not None:
            _decode(revoked)
            raise HostAuthorityError('Native grant was revoked', 'LOOP_NATIVE_REVOKED')
        _need(_now() < datetime.fromisoformat(grant['deadline_at']), 'Native grant expired', 'LOOP_NATIVE_EXPIRED')
        _need(not connection.execute("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'").fetchone(),
              'Provider dispatch is paused', 'DISPATCH_PAUSED')
        return config

    def owns_session(self, connection, identity):
        return connection.execute('SELECT 1 FROM provider_loop_native_sessions WHERE session_id=?', (identity,)).fetchone() is not None

    def qualify_correction_mode(self, connection, session_id, policy_id, policy_digest):
        """A real bound versioned slot is required even for fixture qualification."""
        from .research_sessions import ResearchSessions
        _, grant = self._binding(connection, session_id)
        self._active(connection, grant)
        specs = grant['request']['slots']
        slots = [s for s in specs if s.get('correction_version') == 1]
        generations = [s for s in specs if s['operation'] == 'generate_candidates']
        _need(len(specs) == 2 and len(slots) == len(generations) == 1
              and slots[0]['policy_id'] == policy_id and slots[0]['policy_digest'] == policy_digest
              and slots[0]['target_from_call_id'] == generations[0]['call_id']
              and all(s['source_kind'] == 'research_revision' and s['candidate_limit'] == 1
                      and s['adopted_from_call_id'] is None for s in specs)
              and slots[0]['root_source_digest'] == generations[0]['root_source_digest'],
              'The original program has no exact versioned correction slot', 'CORRECTION_MODE_UNQUALIFIED')
        loaded = ResearchSessions(self.store)._load(connection, session_id)
        _need(loaded['request']['budget']['max_provider_dispatches'] <= 2,
              'Current correction qualification is bounded to two dispatches', 'LOOP_NATIVE_BUDGET_MISMATCH')
        return grant

    def _binding(self, connection, identity):
        row = connection.execute('SELECT * FROM provider_loop_native_sessions WHERE session_id=?', (identity,)).fetchone()
        value = _decode(row)
        _need(value['session_id'] == row['session_id'] == identity and value['grant_id'] == row['grant_id'], 'Native session binding differs')
        return value, self._grant(connection, value['grant_id'])

    def create_session(self, grant_id, source, purpose, config, budget, idempotency_key):
        """Local new route; leaves old HostResearchAuthority's one-attempt API intact."""
        from .research_sessions import ResearchSessions
        from .research_sessions_schema import SessionBudget
        budget = SessionBudget.model_validate(budget).model_dump()
        with transaction(self.store.db_path) as connection:
            grant = self._grant(connection, grant_id)
            old = connection.execute('SELECT id FROM research_sessions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if old:
                binding, old_grant = self._binding(connection, old['id'])
                _need(old_grant['id'] == grant_id, 'Session replay names another grant', 'IDEMPOTENCY_CONFLICT')
            else:
                self._active(connection, grant)
                eligible = [s for s in grant['request']['slots'] if self._source_matches(connection, grant, s, source)]
                _need(eligible, 'Root source is outside frozen slots', 'LOOP_NATIVE_SOURCE_MISMATCH')
                _need(config['provider_config_id'] == grant['config']['config_id']
                      and config['provider_config_digest'] == wire_digest(grant['config']), 'Session provider differs', 'LOOP_NATIVE_CONFIG_MISMATCH')
                _need(budget['max_provider_dispatches'] <= len(grant['request']['slots'])
                      and _now() + timedelta(seconds=budget['max_seconds']) <= datetime.fromisoformat(grant['deadline_at']),
                      'Session exceeds program limits', 'LOOP_NATIVE_BUDGET_MISMATCH')
            session = ResearchSessions(self.store).create(source, purpose, config, budget, idempotency_key, _connection=connection)
            if not old:
                value = {'session_id': session['id'], 'grant_id': grant_id, 'root_source_digest': source['source_digest']}
                connection.execute('INSERT INTO provider_loop_native_sessions VALUES(?,?,?,?)',
                                   (session['id'], grant_id, json_text(value), digest(value)))
            return session

    def bind_session(self, connection, grant_id, session_id):
        """Local explicit binding of an UNUSED original session.

This permits freezing deterministic loop effect keys after the original
session's timestamped identity exists; no reservation or old Host authority is
adopted, and no HTTP handler can mint a grant by calling this helper.
        """
        from .research_sessions import ResearchSessions
        _need(connection.in_transaction, 'Session binding requires the original host transaction')
        grant = self._grant(connection, grant_id)
        existing = connection.execute('SELECT * FROM provider_loop_native_sessions WHERE session_id=?', (session_id,)).fetchone()
        if existing:
            value = _decode(existing)
            _need(value['grant_id'] == grant_id, 'Session cannot switch program permission', 'IDEMPOTENCY_CONFLICT')
            return value
        self._active(connection, grant)
        _need(not connection.execute('SELECT 1 FROM provider_host_sessions WHERE session_id=?', (session_id,)).fetchone(),
              'An original Host session cannot be widened', 'RESEARCH_AUTHORITY_CONFLICT')
        loaded = ResearchSessions(self.store)._load(connection, session_id)
        ResearchSessions(self.store)._open(loaded)
        _need(not loaded['reservations'], 'A program cannot adopt previously charged actions', 'LOOP_NATIVE_SESSION_USED')
        source = loaded['request']['source']
        eligible = [s for s in grant['request']['slots'] if self._source_matches(connection, grant, s, source)]
        config = loaded['request']['config']
        _need(eligible and config['provider_config_id'] == grant['config']['config_id']
              and config['provider_config_digest'] == wire_digest(grant['config'])
              and config['contract_id'] == CONTRACT_ID, 'Original source or configuration differs from frozen program')
        _need(loaded['request']['budget']['max_provider_dispatches'] <= len(eligible)
              and datetime.fromisoformat(loaded['row']['deadline_at']) <= datetime.fromisoformat(grant['deadline_at']),
              'Original session exceeds the frozen program scope', 'LOOP_NATIVE_BUDGET_MISMATCH')
        value = {'session_id': session_id, 'grant_id': grant_id, 'root_source_digest': source['source_digest']}
        connection.execute('INSERT INTO provider_loop_native_sessions VALUES(?,?,?,?)', (session_id, grant_id, json_text(value), digest(value)))
        return value

    def _source_matches(self, connection, grant, spec, source):
        if source['kind'] != spec['source_kind']:
            return False
        if spec['root_source_digest'] is not None:
            return source['source_digest'] == spec['root_source_digest']
        if source['kind'] != 'accepted_material':
            return False
        from .paper_materials import PaperMaterials
        version = PaperMaterials(self.store).get_version(source['material_id'], source['version_id'])
        row = connection.execute('SELECT * FROM provider_loop_native_slots WHERE grant_id=? AND call_id=?',
            (grant['id'], spec['adopted_from_call_id'])).fetchone()
        parent = _decode(row)
        return (parent['spec']['operation'] == 'extract_material' and parent['proposal_id'] is not None
                and parent['proposal_id'] == version['source']['proposal_id']
                and parent['attempt_id'] == version['source']['attempt_id'])

    def _slot(self, connection, grant_id, effect_key):
        row = connection.execute('SELECT * FROM provider_loop_native_slots WHERE grant_id=? AND effect_key=?', (grant_id, effect_key)).fetchone()
        _need(row is not None, 'Request has no frozen program call slot', 'LOOP_NATIVE_SLOT_REQUIRED')
        return _decode(row)

    def verify_request(self, connection, request, config, *, phase):
        from .research_sessions import ResearchSessions
        from .provider_research_context import resolve_research_source
        request = ResearchProposalCreate.model_validate(request).model_dump()
        config = config.model_dump() if hasattr(config, 'model_dump') else config
        binding, grant = self._binding(connection, request['session_id'])
        self._active(connection, grant)
        value = self._slot(connection, grant['id'], request['idempotency_key'])
        spec = value['spec']
        from .provider_correction_native import is_provider_correction
        versioned = is_provider_correction(request)
        _need(versioned == (spec.get('correction_version') == 1),
              'A legacy slot cannot authorize the versioned correction branch', 'LOOP_NATIVE_CORRECTION_VERSION_MISMATCH')
        if versioned:
            _need(spec['policy_id'] == request['target']['policy_id']
                  and spec['policy_digest'] == request['target']['policy_digest'],
                  'Correction policy differs from its frozen program slot', 'LOOP_NATIVE_CORRECTION_POLICY_MISMATCH')
        _need(spec['operation'] == request['operation'] and spec['source_kind'] == request['source']['kind'],
              'Request differs from its exact program slot', 'LOOP_NATIVE_SCOPE_MISMATCH')
        _need(spec['instruction_digest'] is None or spec['instruction_digest'] == digest(request['instruction']),
              'Instruction differs from its frozen call', 'LOOP_NATIVE_SCOPE_MISMATCH')
        _need(config == grant['config'], 'Native program configuration differs', 'LOOP_NATIVE_CONFIG_MISMATCH')
        _need(app_server_operation_supported(config, request['operation'])
              and (native_transport(config) != 'app_server' or request['operation'] == 'generate_candidates'),
              'App Server Loop supports only generation; compact extraction requires a Host grant', 'APP_SERVER_OPERATION_UNSUPPORTED')
        budget = request['budget']
        _need(budget['max_provider_invocations'] == 1 and budget['max_seconds'] <= grant['request']['max_proposal_seconds']
              and request['candidate_limit'] == spec['candidate_limit']
              and budget['max_context_bytes'] <= 128 * 1024 and budget['max_output_bytes'] <= 64 * 1024
              and all(budget[k] is None for k in ('hard_token_limit', 'hard_model_call_limit', 'hard_cost_limit')),
              'Unsupported or excessive Native budget', 'LOOP_NATIVE_BUDGET_MISMATCH')
        loaded = ResearchSessions(self.store)._load(connection, request['session_id'])
        _need(loaded['request']['source']['source_digest'] == binding['root_source_digest'], 'Original session root differs', 'LOOP_NATIVE_SOURCE_MISMATCH')
        _need(self._source_matches(connection, grant, spec, loaded['request']['source']), 'Slot source or adopted lineage differs', 'LOOP_NATIVE_SOURCE_MISMATCH')
        private = resolve_research_source(self.store, request['source'], connection=connection)
        _need(digest(private) == request['expected_source_digest'], 'Current source changed', 'SOURCE_CHANGED')
        if value['reservation_token'] is not None:
            _need(value['session_id'] == request['session_id'] and value['request_digest'] == digest(request),
                  'A charged call slot cannot be replaced', 'LOOP_NATIVE_SLOT_SPENT')
        if request['operation'] in {'repair_candidate', 'decide_next_action'}:
            self._target(connection, request, grant)
            if spec['target_from_call_id'] is not None:
                prior = connection.execute('SELECT proposal_id,attempt_id FROM provider_loop_native_slots WHERE grant_id=? AND call_id=?',
                    (grant['id'], spec['target_from_call_id'])).fetchone()
                _need(prior is not None and prior['proposal_id'] == request['target']['proposal_id'] and prior['attempt_id'] == request['target']['attempt_id'],
                      'Target differs from its frozen earlier call', 'LOOP_NATIVE_TARGET_MISMATCH')
        return grant, binding, value

    def _target(self, connection, request, grant):
        from .provider_research_targets import resolve_target
        from .proposal_requests import ProposalRequests
        target = resolve_target(self.store, request, connection=connection)
        parent_id = request['target']['proposal_id']
        row = connection.execute('SELECT * FROM provider_loop_native_slots WHERE proposal_id=?', (parent_id,)).fetchone()
        parent_slot = _decode(row)
        _need(parent_slot['grant_id'] == grant['id'] and parent_slot['session_id'] == request['session_id'],
              'Target belongs to another program or session', 'LOOP_NATIVE_TARGET_MISMATCH')
        if grant['config']['mode'] == 'codex_cli':
            parent = ProposalRequests(self.store, configs={})._load(connection, parent_id)
            attempt = next(a for a in parent['attempts'] if a['id'] == request['target']['attempt_id'])
            self._saved_transport(parent, attempt, connection=connection)
        return target

    def reserve_session_action(self, connection, request, reservation):
        """Called in the SAME transaction as the original permanent reservation."""
        request = ResearchProposalCreate.model_validate(request).model_dump()
        binding, grant = self._binding(connection, request['session_id'])
        self.verify_request(connection, request, grant['config'], phase='reserve')
        value = self._slot(connection, grant['id'], request['idempotency_key'])
        _need(reservation['action_digest'] == digest(request) and reservation['effect_key'] == request['idempotency_key']
              and reservation['charges']['provider_dispatches'] == 1, 'Program slot requires its original charged action', 'LOOP_NATIVE_LEDGER_INTEGRITY')
        if value['reservation_token'] is not None:
            _need(value['reservation_token'] == reservation['token'], 'Call slot cannot acquire another reservation', 'LOOP_NATIVE_SLOT_SPENT')
            return value
        value.update(session_id=request['session_id'], reservation_token=reservation['token'],
                     request_digest=digest(request), target_digest=digest(request['target']), charged_at=_now().isoformat())
        _save_slot(connection, value)
        return value

    def reserve_attempt(self, connection, proposal, attempt_id):
        grant, _, value = self.verify_request(connection, proposal['request'], proposal['config'], phase='claim')
        _need(value['reservation_token'] is not None, 'Native claim has no atomically linked session charge', 'LOOP_NATIVE_RESERVATION_REQUIRED')
        if value['attempt_id'] is not None:
            _need(value['attempt_id'] == attempt_id and value['proposal_id'] == proposal['id'], 'No replacement Native attempt is permitted', 'LOOP_NATIVE_SLOT_SPENT')
            return value
        value.update(proposal_id=proposal['id'], attempt_id=attempt_id, context_digest=proposal['context_digest'])
        _save_slot(connection, value)
        return value

    def authorize_prepared(self, proposal, attempt, prepared_digest):
        from .proposal_requests import ProposalRequests
        from .provider_contract_registry import request_handler
        with transaction(self.store.db_path) as connection:
            grant, _, value = self.verify_request(connection, proposal['request'], proposal['config'], phase='prepare')
            _need(value['attempt_id'] == attempt['id'] and value['proposal_id'] == proposal['id'], 'Prepared attempt has no exact slot')
            handler = request_handler(proposal['request'])
            expected = {'proposal_id': proposal['id'], 'attempt_id': attempt['id'], 'context_digest': proposal['context_digest'],
                'input_text': handler.build_prompt(handler.parse_request(proposal['request']), proposal['context'], proposal['id'], attempt['id']),
                'output_schema': handler.output_schema(proposal['context']), 'provider_config': proposal['config'], 'budget': proposal['request']['budget']}
            requests = ProposalRequests(self.store, configs={})
            saved = requests._json_path(requests._folder(proposal['id']) / 'attempts' / attempt['id'] / 'prepared.json')
            _need(saved == expected and digest(saved) == prepared_digest == attempt['prepared_digest'], 'Prepared input differs from frozen request', 'LOOP_NATIVE_PREPARED_MISMATCH')
            _need(value['prepared_digest'] in {None, prepared_digest} and value['fencing_token'] in {None, attempt['fencing_token']}, 'Prepared binding cannot change')
            value.update(prepared_digest=prepared_digest, fencing_token=attempt['fencing_token'])
            _save_slot(connection, value)

    @staticmethod
    def _summary(grant, value):
        return {'authority_id': AUTHORITY_ID, 'grant_id': grant['id'], 'grant_digest': digest(grant),
            'program_id': grant['request']['program_id'], 'plan_digest': grant['request']['plan_digest'], 'call_id': value['call_id'],
            'permit_id': 'loop_native_permit_' + digest({'grant': grant['id'], 'call': value['call_id']}),
            'session_id': value['session_id'], 'reservation_token': value['reservation_token'], 'proposal_id': value['proposal_id'],
            'attempt_id': value['attempt_id'], 'request_digest': value['request_digest'], 'context_digest': value['context_digest'],
            'config_digest': wire_digest(grant['config']), 'profile_digest': authority_profile_digest(grant['profile'], grant['config']),
            'prepared_digest': value['prepared_digest'], 'target_digest': value['target_digest'], 'contract_id': CONTRACT_ID,
            'operation': value['spec']['operation'], 'source_kind': value['spec']['source_kind'],
            'dispatch_intent_digest': digest(value['dispatch_intent']) if value['dispatch_intent'] else None}

    def verify_native(self, request, *, phase):
        from .proposal_requests import ProposalRequests
        from .research_session_execution import SessionExecution
        _need(phase in {'entry', 'dispatch'}, 'Invalid Native dispatch phase')
        requests = ProposalRequests(self.store, configs={}, research_authority=self)
        with transaction(self.store.db_path) as connection:
            proposal = requests._load(connection, request.proposal_id)
            grant, _, value = self.verify_request(connection, proposal['request'], proposal['config'], phase=phase)
            _need(grant['request']['authorization_kind'] == 'bounded_qualification' and grant['config']['mode'] == 'codex_cli',
                  'Fixture policy is never a real Native permit', 'LOOP_NATIVE_REAL_AUTHORIZATION_REQUIRED')
            attempt = next((a for a in proposal['attempts'] if a['id'] == request.attempt_id), None)
            _need(attempt and proposal['status'] == attempt['status'] == 'running' and value['attempt_id'] == request.attempt_id, 'Native attempt is not currently owned')
            SessionExecution(self.store, requests).authorize_proposal_dispatch(connection, proposal)
            expected = {'proposal_id': request.proposal_id, 'attempt_id': request.attempt_id, 'context_digest': request.context_digest,
                'input_text': request.input_text, 'output_schema': request.output_schema, 'provider_config': request.config.model_dump(), 'budget': request.budget.model_dump()}
            _need(value['prepared_digest'] == digest(expected) == attempt['prepared_digest'] and value['fencing_token'] == attempt['fencing_token'],
                  'Native request differs from its prepared permit', 'LOOP_NATIVE_PREPARED_MISMATCH')
            if phase == 'dispatch':
                _need(value['dispatch_intent'] is None, 'Native dispatch intent was already consumed', 'LOOP_NATIVE_DISPATCH_SPENT')
                value['dispatch_intent'] = {'attempt_id': request.attempt_id, 'prepared_digest': value['prepared_digest'], 'created_at': _now().isoformat()}
                _save_slot(connection, value)
            return self._summary(grant, value)

    def _saved_transport(self, proposal, attempt, result=None, *, connection=None):
        if connection is None:
            with closing(connect(self.store.db_path)) as opened:
                return self._saved_transport(proposal, attempt, result, connection=opened)
        value = _decode(connection.execute('SELECT * FROM provider_loop_native_slots WHERE attempt_id=?', (attempt['id'],)).fetchone())
        grant = self._grant(connection, value['grant_id'])
        from .provider_schema import ProviderConfig
        _need(_current_profile(ProviderConfig.model_validate(proposal['config'])) == grant['profile'],
              'Transport evidence belongs to changed source', 'LOOP_NATIVE_PROFILE_CHANGED')
        expected = self._summary(grant, value)
        _need(value['dispatch_intent'] is not None, 'Native transport lacks its permanent dispatch intent')
        return verify_loop_native_transport(self.store, proposal, attempt, expected, result=result, connection=connection)

    def verify_result(self, proposal, attempt, *, response, receipt, error):
        return self._saved_transport(proposal, attempt, {'response': response, 'receipt': receipt, 'error': error})

    def _saved_pre_dispatch_failure(self, proposal, attempt, *, connection):
        """Read one original unconsumed slot; never create/refund/re-dispatch it."""
        from .provider_schema import ProviderConfig
        from .research_sessions import ResearchSessions
        from .provider_research_context import resolve_research_source
        from .provider_research_qualification import verify_app_server_pre_dispatch_failure
        request = ResearchProposalCreate.model_validate(proposal['request']).model_dump()
        config = ProviderConfig.model_validate(proposal['config'])
        _need(native_transport(config) == 'app_server', 'Legacy transport has no pre-dispatch failure proof')
        value = _decode(connection.execute('SELECT * FROM provider_loop_native_slots WHERE attempt_id=?',
                                          (attempt['id'],)).fetchone())
        binding, grant = self._binding(connection, request['session_id'])
        spec = value['spec']
        _need(grant['request']['authorization_kind'] == 'bounded_qualification'
              and grant['config'] == config.model_dump()
              and grant['profile'] == _current_profile(config),
              'Failure has no exact current real grant', 'LOOP_NATIVE_PROFILE_CHANGED')
        _need(value['grant_id'] == grant['id'] and value['session_id'] == request['session_id']
              and value['proposal_id'] == proposal['id'] and value['attempt_id'] == attempt['id']
              and value['effect_key'] == request['idempotency_key']
              and value['request_digest'] == digest(request)
              and value['context_digest'] == proposal['context_digest']
              and value['prepared_digest'] == attempt['prepared_digest']
              and value['fencing_token'] == attempt['fencing_token']
              and value['reservation_token'] is not None and value['dispatch_intent'] is None,
              'Failure differs from the original permanently charged slot', 'LOOP_NATIVE_LEDGER_INTEGRITY')
        _need(spec['operation'] == request['operation'] == 'generate_candidates'
              and spec['source_kind'] == request['source']['kind']
              and spec['candidate_limit'] == request['candidate_limit']
              and spec['instruction_digest'] == digest(request['instruction'])
              and value['target_digest'] == digest(request['target'])
              and request['target'] is None and not spec.get('correction_version'),
              'Failure differs from the exact permitted generation scope', 'LOOP_NATIVE_SCOPE_MISMATCH')
        budget = request['budget']
        _need(budget['max_provider_invocations'] == 1
              and budget['max_seconds'] <= grant['request']['max_proposal_seconds']
              and budget['max_context_bytes'] <= 128 * 1024 and budget['max_output_bytes'] <= 64 * 1024
              and all(budget[k] is None for k in ('hard_token_limit', 'hard_model_call_limit', 'hard_cost_limit')),
              'Failure budget differs from the frozen grant', 'LOOP_NATIVE_BUDGET_MISMATCH')
        session = ResearchSessions(self.store)._load(connection, request['session_id'])
        _need(session['request']['source']['source_digest'] == binding['root_source_digest']
              and self._source_matches(connection, grant, spec, session['request']['source'])
              and digest(resolve_research_source(self.store, request['source'], connection=connection))
                  == request['expected_source_digest'],
              'Failure source differs from the frozen original source', 'LOOP_NATIVE_SOURCE_MISMATCH')
        return verify_app_server_pre_dispatch_failure(self.store, proposal, attempt,
            expected_entry_permit=self._summary(grant, value), connection=connection)

    def observe_transport(self, proposal, attempt, *, receipt, error, connection=None):
        """Record observed success separately from domain validation; unknown is null."""
        if connection is None:
            with transaction(self.store.db_path) as opened:
                return self.observe_transport(proposal, attempt, receipt=receipt, error=error, connection=opened)
        previous = connection.execute('SELECT * FROM provider_loop_native_observations WHERE attempt_id=? ORDER BY sequence DESC LIMIT 1', (attempt['id'],)).fetchone()
        try:
            proof = self._saved_transport(proposal, attempt, connection=connection)
            observation = {'transport_status': 'confirmed_success', 'invocation_started': True,
                           'observed_invocations': 1, 'process_stopped': True, 'proof': proof}
        except (ServiceError, ValueError, OSError, KeyError, TypeError, IndexError) as exc:
            observation = {'transport_status': 'outcome_unknown', 'invocation_started': None,
                           'observed_invocations': None, 'process_stopped': None, 'proof': None,
                           'code': getattr(exc, 'code', 'LOOP_NATIVE_TRANSPORT_UNVERIFIED')}
            # An existing unknown record is historical, not a refundable ticket
            # that a later envelope can upgrade. This branch observes only a
            # first, new failure and never participates in verify_result/repair.
            prior_failure = previous is not None and _decode(previous).get('transport_status') == 'confirmed_pre_dispatch_failure'
            if (previous is None or prior_failure) and receipt == attempt.get('receipt') and error == attempt.get('error'):
                try:
                    proof = self._saved_pre_dispatch_failure(proposal, attempt, connection=connection)
                    observation = {'transport_status': 'confirmed_pre_dispatch_failure',
                                   'invocation_started': False, 'observed_invocations': 0,
                                   'process_stopped': True, 'proof': proof}
                except (ServiceError, ValueError, OSError, KeyError, TypeError, IndexError):
                    pass
        observation.update(attempt_id=attempt['id'], proposal_id=proposal['id'], domain_status=proposal['status'])
        if previous and _decode(previous) == observation:
            return observation
        sequence = previous['sequence'] + 1 if previous else 1
        connection.execute('INSERT INTO provider_loop_native_observations VALUES(?,?,?,?)', (attempt['id'], sequence, json_text(observation), digest(observation)))
        return observation

    def status(self, grant_id):
        """Report charges and independent transport observations, without calls."""
        with closing(connect(self.store.db_path)) as connection:
            grant = self._grant(connection, grant_id)
            rows = [_decode(r) for r in connection.execute('SELECT * FROM provider_loop_native_slots WHERE grant_id=? ORDER BY call_id', (grant_id,))]
            observations = []
            for slot in rows:
                if slot['attempt_id'] is not None:
                    row = connection.execute('SELECT * FROM provider_loop_native_observations WHERE attempt_id=? ORDER BY sequence DESC LIMIT 1', (slot['attempt_id'],)).fetchone()
                    observation = _decode(row) if row is not None else None
                    observations.append(observation)
            unknown = sum(o is None or o['observed_invocations'] is None for o in observations)
            return {'authority_id': AUTHORITY_ID, 'program_id': grant['request']['program_id'], 'grant_id': grant_id,
                    'planned_slots': len(rows), 'charged_slots': sum(r['reservation_token'] is not None for r in rows),
                    'claimed_attempts': sum(r['attempt_id'] is not None for r in rows),
                    'confirmed_native_invocations': sum(o['observed_invocations'] for o in observations if o is not None and o['observed_invocations'] is not None),
                    'unknown_native_attempts': unknown, 'native_invocations': None if unknown else sum(o['observed_invocations'] for o in observations),
                    'real_authorized_slots': len(rows) if grant['request']['authorization_kind'] == 'bounded_qualification' else 0,
                    'semantic_fidelity': 'unverified', 'retry_allowed': False}

    def cancelled(self, proposal):
        with closing(connect(self.store.db_path)) as connection:
            if not self.owns_session(connection, proposal['request']['session_id']):
                return False
            _, grant = self._binding(connection, proposal['request']['session_id'])
            revoked = connection.execute('SELECT * FROM provider_loop_native_revocations WHERE grant_id=?', (grant['id'],)).fetchone()
            if revoked:
                _decode(revoked)
            return revoked is not None or _now() >= datetime.fromisoformat(grant['deadline_at'])


def create_loop_native_grant(store, registry, **kwargs):
    """Explicit local host action; never called by an HTTP permission creator."""
    request = LoopNativeGrantCreate.model_validate(kwargs).model_dump()
    config = registry.ensure_dispatchable(request['provider_config_id'])
    _need((config.mode == 'fixture') == (request['authorization_kind'] == 'fixture'), 'Authorization kind differs from provider mode', 'LOOP_NATIVE_REAL_AUTHORIZATION_REQUIRED')
    authority = LoopNativeResearchAuthority(store, registry)
    with transaction(store.db_path) as connection:
        previous = connection.execute('SELECT id FROM provider_loop_native_grants WHERE idempotency_key=?', (request['idempotency_key'],)).fetchone()
        if previous:
            grant = authority._grant(connection, previous['id'])
            _need(grant['request'] == request, 'Grant key cannot change scope', 'IDEMPOTENCY_CONFLICT')
            return grant
        _need(not connection.execute('SELECT 1 FROM provider_loop_native_grants WHERE json_extract(payload,\'$.request.program_id\')=?', (request['program_id'],)).fetchone(),
              'A program cannot reset its slot allowance with another grant', 'LOOP_NATIVE_PROGRAM_ALREADY_BOUND')
        grant = {'id': 'loop_native_grant_' + digest(request), 'authority_id': AUTHORITY_ID, 'request': request,
                 'workspace_id': _workspace(connection), 'config': config.model_dump(), 'profile': _current_profile(config),
                 'created_at': _now().isoformat(), 'deadline_at': (_now() + timedelta(seconds=request['max_seconds'])).isoformat()}
        connection.execute('INSERT INTO provider_loop_native_grants VALUES(?,?,?,?)', (grant['id'], request['idempotency_key'], json_text(grant), digest(grant)))
        for spec in request['slots']:
            value = {'grant_id': grant['id'], 'call_id': spec['call_id'], 'effect_key': spec['effect_key'], 'spec': deepcopy(spec),
                     'session_id': None, 'reservation_token': None, 'proposal_id': None, 'attempt_id': None,
                     'request_digest': None, 'target_digest': None, 'context_digest': None, 'prepared_digest': None,
                     'fencing_token': None, 'dispatch_intent': None, 'charged_at': None}
            connection.execute('INSERT INTO provider_loop_native_slots VALUES(?,?,?,?,?,?,?,?,?)',
                (grant['id'], spec['call_id'], spec['effect_key'], None, None, None, None, json_text(value), digest(value)))
        return grant


def revoke_loop_native_grant(store, registry, grant_id, reason):
    _need(isinstance(reason, str) and 0 < len(reason.strip()) <= 4000, 'Revocation requires a bounded reason')
    with transaction(store.db_path) as connection:
        LoopNativeResearchAuthority(store, registry)._grant(connection, grant_id)
        previous = connection.execute('SELECT * FROM provider_loop_native_revocations WHERE grant_id=?', (grant_id,)).fetchone()
        if previous:
            return _decode(previous)
        value = {'grant_id': grant_id, 'reason': reason, 'created_at': _now().isoformat()}
        connection.execute('INSERT INTO provider_loop_native_revocations VALUES(?,?,?)', (grant_id, json_text(value), digest(value)))
        return value


def verify_loop_native_transport(store, proposal, attempt, permit, *, result=None, connection=None):
    """Verify original real transport, including rejected domain output.

Raw JSON or a copied receipt alone cannot pass: this re-reads the exact Native
profile, invocation, boundary challenges, charged origin and bound supervisor.
    """
    from .proposal_requests import ProposalRequests
    from .provider_native import _identity, _boundaries_match, _invocation_boundaries_match
    from .provider_contract_registry import request_handler
    from .provider_schema import ProviderConfig, ProviderReceipt
    from .provider_supervision_journal import read_metadata, termination_proof
    from .codex_cli_provider import _bounded_private_file
    requests = ProposalRequests(store, configs={})
    config = ProviderConfig.model_validate(proposal['config'])
    if native_transport(config) == 'app_server':
        from .provider_research_qualification import verify_app_server_attempt_transport
        return verify_app_server_attempt_transport(store, proposal, attempt, result,
            expected_permit=permit, connection=connection, scope='loop')
    _need(config.mode == 'codex_cli' and config.model == 'gpt-5.5' and config.reasoning_effort == 'low'
          and config.expected_cli_version == '0.145.0', 'Real transport has no pinned Native profile', 'LOOP_NATIVE_TRANSPORT_INVALID')
    _need(attempt['id'] == proposal['current_attempt_id'] and attempt['prepared_digest'] is not None, 'Transport requires the original prepared attempt')
    identity, profile = _identity(config, contract_id=CONTRACT_ID)
    _need(profile['static_checks']['passed'], 'Current Native profile failed')
    folder = requests._folder(proposal['id']) / 'attempts' / attempt['id']
    transport = folder / 'transport'
    native = read_metadata(transport / 'native-evidence.json', 512 * 1024)
    intent = read_metadata(transport / 'dispatch-intent.json')
    prepared = read_metadata(folder / 'prepared.json', 512 * 1024)
    _need(digest(prepared) == attempt['prepared_digest'] and read_metadata(transport / 'profile.json', 512 * 1024) == profile
          and native['identity'] == intent['identity'] == identity, 'Original transport identities changed')
    _need(native.get('contract_id') == CONTRACT_ID and native['host_authorized'] is True
          and native['automatic_provider_retry'] is False and native['native_authentication_confirmed'] is True
          and native['dispatch_started'] is True, 'Native authentication or dispatch was not observed')
    _need(native.get('research_authorization_dispatch') == permit and intent.get('research_authorization') == permit
          and native.get('research_authorization_entry') == {**permit, 'dispatch_intent_digest': None}, 'Original transport is not bound to this program permit')
    _need(intent['proposal_id'] == proposal['id'] and intent['attempt_id'] == attempt['id']
          and type(intent['provider_invocations']) is int and intent['provider_invocations'] == 1, 'Transport intent names another call')
    handler = request_handler(proposal['request'])
    request = handler.parse_request(proposal['request'])
    expected = {'proposal_id': proposal['id'], 'attempt_id': attempt['id'], 'context_digest': proposal['context_digest'],
        'input_text': handler.build_prompt(request, proposal['context'], proposal['id'], attempt['id']),
        'output_schema': handler.output_schema(proposal['context']), 'provider_config': proposal['config'], 'budget': proposal['request']['budget']}
    _need(prepared == expected, 'Transport prompt differs from original context')
    inputs = _bounded_private_file(transport / 'input.txt', request.budget.max_context_bytes)
    _need(inputs == prepared['input_text'].encode() and read_metadata(transport / 'output.schema.json', 512 * 1024) == prepared['output_schema'], 'Actual input or output schema changed')
    invocation = read_metadata(transport / 'invocation.json', 512 * 1024)
    _need(_invocation_boundaries_match(invocation, identity), 'Actual outer Native boundary differs')
    _need(wire_digest(invocation) == native['invocation_digest'] == intent['invocation_digest']
          and invocation['identity'] == identity and invocation['input_sha256'] == hashlib.sha256(inputs).hexdigest()
          and invocation['schema_digest'] == wire_digest(prepared['output_schema']) and invocation['automatic_provider_retry'] is False,
          'Actual Native invocation differs')
    for kind in ('read', 'write'):
        report = read_metadata(transport / (kind + '-boundary') / 'report.json', 512 * 1024)
        _need(_boundaries_match(report, kind, identity, profile)
              and native['boundary_checks'][kind] == {'process_stopped': True, 'verified': True}, 'Native boundary challenge is unverified')
    saved = read_metadata(transport / 'receipt.json')
    receipt = ProviderReceipt.model_validate(saved['receipt']).model_dump()
    _need(saved['error'] is None and receipt['fixture'] is False and receipt['invocation_started'] is True
          and receipt['process_stopped'] is True and receipt['exit_code'] == 0
          and receipt['proposal_id'] == proposal['id'] and receipt['attempt_id'] == attempt['id']
          and receipt['requested_model'] == config.model and receipt['cli_version'] == config.expected_cli_version, 'Native transport did not complete successfully')
    _need(receipt == attempt['receipt'] or (result is not None and receipt == result['receipt']), 'Queue receipt differs from original Native receipt')
    if result is not None:
        _need(result['error'] is None, 'Provider returned a transport error')
    raw = _bounded_private_file(transport / 'response.raw.json', request.budget.max_output_bytes)
    _need(raw == requests._read_path(folder / 'response.raw.json', request.budget.max_output_bytes)
          and hashlib.sha256(raw).hexdigest() == receipt['response_sha256'] == attempt['raw_sha256'], 'Original Native response differs')
    parsed = handler.parse_output(raw, request=request, context=proposal['context'], proposal_id=proposal['id'], attempt_id=attempt['id'],
        context_digest=proposal['context_digest'], candidate_limit=request.candidate_limit, max_bytes=request.budget.max_output_bytes).model_dump()
    expected_output = result['response'] if result is not None else read_metadata(folder / 'parsed.json')
    _need(parsed == expected_output, 'Parsed output differs from exact Native bytes')
    with (nullcontext(connection) if connection is not None else closing(connect(store.db_path))) as connection:
        origin_row = connection.execute('SELECT payload,digest FROM provider_attempt_origins WHERE attempt_id=?', (attempt['id'],)).fetchone()
        _need(origin_row is not None, 'Native attempt lacks its original charge provenance')
        origin = strict_json(origin_row['payload'].encode(), max_bytes=65536)
        _need(digest(origin) == origin_row['digest'], 'Charged origin digest changed')
        proof = termination_proof(store.root, proposal, _workspace(connection), origin)
    _need(proof['confirmed'] is True and proof['reason'] == 'BOUND_STOP_EVIDENCE', 'Original supervisor has no bound stop proof')
    stop = read_metadata(transport / 'real-supervision.json')
    matches = []
    for path in (folder / 'supervision').iterdir():
        if path.is_dir():
            actual = read_metadata(path / 'finished.json').get('stop') or {}
            if actual.get('nonce') == stop.get('nonce'):
                matches.append(actual)
    _need(len(matches) == 1 and all(matches[0].get(k) == v for k, v in stop.items())
          and stop['invocation_started'] is True and stop['process_stopped'] is True and stop['exit_code'] == 0
          and stop['response_saved'] is True and stop['response_sha256'] == receipt['response_sha256'], 'Native stop differs from its original supervisor')
    _need(_identity(config, contract_id=CONTRACT_ID)[0] == identity, 'Source changed during transport verification')
    return {'authority_id': AUTHORITY_ID, 'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
        'response_sha256': receipt['response_sha256'], 'response_digest': wire_digest(parsed), 'receipt_digest': digest(receipt),
        'native_evidence_digest': digest(native), 'supervision_digest': digest(proof), 'profile_digest': profile['profile_digest'],
        'semantic_fidelity': 'unverified', 'backend_model_calls': receipt['usage']['model_calls'], 'supplier_cost': receipt['usage']['cost']}
