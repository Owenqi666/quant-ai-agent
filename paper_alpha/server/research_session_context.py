"""Development-only session projections; no raw job context or oracle answers."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy

from ..storage import digest, json_text
from .provider_context import ProviderContext
from .research_sessions_schema import DecisionResult, RepairTarget

MAX_CONTEXT_BYTES = 128 * 1024
METRICS = frozenset({'mean_rank_ic', 'rank_ic_days', 'mean_gross_return', 'sum_gross_return',
                    'evaluated_days', 'eligible_days', 'skipped_days', 'purged_days',
                    'finite_factor_observations', 'possible_factor_observations', 'factor_coverage'})


def frozen_session_snapshot(store, request, *, connection=None, require_current=False):
    """Reconstruct the exact pre-reservation projection from the event chain.

    The proposal itself stores its immutable context. Rebuilding that context
    uses a historical event, never today's usage or child state. This reader
    grants no execution permission; create/claim/finish retain separate gates.
    """
    from .db import connect
    from .provider_research_schema import ResearchProposalCreate
    from .research_sessions import ResearchSessions, SessionError
    request = ResearchProposalCreate.model_validate(request).model_dump()
    if connection is None:
        with closing(connect(store.db_path)) as opened:
            opened.execute('BEGIN')
            return frozen_session_snapshot(store, request, connection=opened, require_current=require_current)
    loaded = ResearchSessions(store)._load(connection, request['session_id'])
    matches = [(index, event) for index, event in enumerate(loaded['events'])
               if digest(event['state_after']) == request['expected_session_digest']]
    if len(matches) != 1:
        raise SessionError('Prompt snapshot has no unique anchored session state', code='SESSION_CONFLICT')
    index, event = matches[0]
    prior = next((item for item in loaded['reservations'] if item['effect_key'] == request['idempotency_key']), None)
    if prior is not None:
        reserved = [(n, item) for n, item in enumerate(loaded['events'])
                    if item['kind'] == 'reserved' and item['reservation']['token'] == prior['token']]
        if (prior['action_digest'] != digest(request) or len(reserved) != 1 or reserved[0][0] != index + 1
                or prior['source'] != event['state_after']['current_source']):
            raise SessionError('Prompt snapshot differs from its exact reservation', code='RESERVATION_REQUEST_MISMATCH')
    elif digest(loaded['state']) != request['expected_session_digest']:
        raise SessionError('An unreserved action must name the current session state', code='SESSION_CONFLICT')
    if require_current and digest(loaded['state']) != request['expected_session_digest']:
        raise SessionError('Session changed before request preparation', code='SESSION_CONFLICT')
    state = event['state_after']
    if (state['current_source']['source_digest'] != request['expected_source_digest']
            or loaded['request']['config']['contract_id'] != request['contract_id']):
        raise SessionError('Prompt snapshot source or contract differs', code='SOURCE_STALE')
    return {'id': request['session_id'], 'state_digest': request['expected_session_digest'],
            'current_source': deepcopy(state['current_source']), 'usage': deepcopy(state['usage']),
            'budget': deepcopy(loaded['request']['budget']), 'deadline_at': loaded['row']['deadline_at']}


def _base(session, context):
    value = ProviderContext.model_validate(context).model_dump()
    source = session['current_source']
    if (source['kind'] != 'research_revision' or source['research_id'] != value['research_id']
            or source['revision_id'] != value['base_revision_id']
            or source['source_digest'] != value['source_digest']):
        raise ValueError('Session and provider context have different frozen sources')
    budget, usage = session['budget'], session['usage']
    remaining = {name: budget['max_' + name] - usage[name]
                 for name in ('provider_dispatches', 'tool_actions', 'experiments', 'failures')}
    return {'schema_version': 1, 'session_id': session['id'],
            'session_digest': session['state_digest'], 'source_digest': source['source_digest'],
            'deadline_at': session['deadline_at'], 'remaining': remaining,
            'research_context': value,
            'omissions': ['No reserved final-test intervals or observations.',
                          'No independent oracle answers or human review labels.',
                          'No complete ResearchJobs context or market rows.'],
            'semantic_fidelity': 'unverified'}


def _bounded(value):
    if len(json_text(value).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError('Session context exceeds its byte budget; nothing was truncated')
    return value


def build_repair_context(session: dict, provider_context: dict, target: dict) -> dict:
    """Project one supplied target; the adapter must first verify parent artifacts."""
    target = RepairTarget.model_validate(target).model_dump()
    if digest(target['candidate']) != target['candidate_digest']:
        raise ValueError('Repair candidate identity differs')
    if digest(target['diagnostics']) != target['diagnostics_digest']:
        raise ValueError('Repair diagnostic identity differs')
    if any(item['candidate_id'] != target['candidate']['id'] for item in target['diagnostics']):
        raise ValueError('Repair diagnostics target another candidate')
    value = _base(session, provider_context)
    if target['candidate']['hypothesis_id'] not in {h['id'] for h in value['research_context']['hypotheses']}:
        raise ValueError('Repair may not introduce an unbound research hypothesis')
    value.update(operation='repair_candidate', target=deepcopy(target))
    return _bounded(value)


def build_decision_context(session: dict, provider_context: dict, results: list[dict]) -> dict:
    """Accept only validation metrics; host verifies result identities beforehand."""
    if not isinstance(results, list) or not 1 <= len(results) <= 20:
        raise ValueError('A decision needs one to twenty exact validation result projections')
    values = [DecisionResult.model_validate(item).model_dump() for item in results]
    if len({item['candidate_id'] for item in values}) != len(values):
        raise ValueError('Duplicate decision targets')
    if any(not set(item['metrics']) <= METRICS for item in values):
        raise ValueError('Unregistered metric names cannot enter the model context')
    if any(d['candidate_id'] != item['candidate_id'] for item in values for d in item['diagnostics']):
        raise ValueError('Decision diagnostic targets another candidate')
    value = _base(session, provider_context)
    value.update(operation='decide_next_action', results=values,
                 allowed_actions=['repair', 'request_information', 'propose_experiment', 'stop'],
                 execution_policy='Experiment suggestions require an explicit host handoff; no automatic execution.')
    return _bounded(value)
