"""Resolve repair/decision targets from exact local artifacts and owned effects.

This is historical identity verification, not a current execution permit. Queue
gates separately verify the live session, source, budgets and deadlines. No
result fields supplied by a caller or model are treated as tool observations.
"""
from contextlib import closing
from copy import deepcopy
import hashlib

from ..expressions import validate_expression, ExpressionError
from ..storage import digest
from .db import connect
from .provider_contract import digest as wire_digest
from .provider_research_contract import parse_model_output
from .provider_research_schema import ResearchProposalCreate
from .provider_schema import ProviderReceipt
from .provider_repair_semantics import repair_candidate_expression, validate_repair_output
from .research_sessions_schema import RepairTarget, DecisionResult, ResearchDecision
from .service import ServiceError


class ResearchTargetError(ServiceError):
    def __init__(self, message, *, code='TARGET_INTEGRITY', status=409):
        super().__init__(message, status)
        self.code, self.retryable = code, False


def _require(condition, message='Research target differs from its exact recorded authority', code='TARGET_INTEGRITY'):
    if not condition:
        raise ResearchTargetError(message, code=code)


def _parent_packet(store, connection, request, ancestry):
    from .proposal_requests import ProposalRequests
    from .research_sessions import ResearchSessions
    target = request.target
    _require(len(ancestry) < 3 and target.proposal_id not in ancestry,
             'Research target ancestry exceeds the bounded repair chain', 'TARGET_ANCESTRY_LIMIT')
    proposals = ProposalRequests(store, configs={})
    parent = proposals._load(connection, target.proposal_id)
    parent_request = ResearchProposalCreate.model_validate(parent['request'])
    _require(parent_request.operation in {'generate_candidates', 'repair_candidate'}
             and parent_request.session_id == request.session_id, code='TARGET_SESSION_MISMATCH')
    attempt = next((item for item in parent['attempts'] if item['id'] == target.attempt_id), None)
    _require(attempt is not None and parent['current_attempt_id'] == target.attempt_id
             and attempt['response_digest'] == target.response_digest, code='TARGET_RESPONSE_MISMATCH')
    receipt = ProviderReceipt.model_validate(attempt['receipt'])
    _require(receipt.process_stopped is True and receipt.proposal_id == target.proposal_id
             and receipt.attempt_id == target.attempt_id, code='TARGET_OUTCOME_UNKNOWN')
    _require(attempt['status'] not in {'running', 'interrupted', 'failed', 'cancelled', 'expired', 'stale', 'exhausted'},
             code='TARGET_OUTCOME_UNKNOWN')
    raw = proposals._read_path(proposals._folder(parent['id']) / 'attempts' / attempt['id'] / 'response.raw.json',
                               parent_request.budget.max_output_bytes)
    _require(hashlib.sha256(raw).hexdigest() == receipt.response_sha256 == attempt['raw_sha256'])
    output = parse_model_output(raw, proposal_id=parent['id'], attempt_id=attempt['id'],
        context_digest=parent['context_digest'], operation=parent_request.operation,
        candidate_limit=parent_request.candidate_limit, max_bytes=parent_request.budget.max_output_bytes).model_dump()
    parsed = proposals._json_path(proposals._folder(parent['id']) / 'attempts' / attempt['id'] / 'parsed.json')
    _require(output == parsed and wire_digest(output) == target.response_digest and output['outcome'] == 'draft')
    _require(wire_digest(parent['context']) == parent['context_digest']
             and parent['context']['source_digest'] == parent_request.expected_source_digest)
    candidates = [item for item in output['draft']['candidates'] if item['id'] == target.candidate_id]
    _require(len(candidates) == 1, code='TARGET_CANDIDATE_MISMATCH')
    sessions = ResearchSessions(store)
    loaded = sessions._load(connection, request.session_id)
    expected_action = 'generate' if parent_request.operation == 'generate_candidates' else 'repair'
    reservations = [item for item in loaded['reservations'] if item['action'] == expected_action
                    and (item['children'] or {}).get('proposal_id') == parent['id']]
    _require(len(reservations) == 1, code='TARGET_NOT_OWNED')
    reservation = reservations[0]
    _require(reservation['effect_key'] == parent_request.idempotency_key
             and reservation['action_digest'] == digest(parent_request.model_dump())
             and reservation['source_digest'] == parent_request.expected_source_digest,
             code='TARGET_NOT_OWNED')
    sessions._children(connection, loaded, reservation, reservation['children'])
    if parent_request.operation == 'repair_candidate':
        ancestor = _resolve_target(store, parent_request, connection, (*ancestry, parent['id']))
        _require(not validate_repair_output(ancestor['parent_draft'], output['draft'],
                     candidate_id=parent_request.target.candidate_id), code='TARGET_REPAIR_SEMANTICS')
    return parent, parent_request, attempt, output, candidates[0], reservation


def resolve_target(store, request, *, connection=None):
    """Return verified projections and a stable binding; never reserve/dispatch."""
    if connection is None:
        with closing(connect(store.db_path)) as opened:
            opened.execute('BEGIN')
            return resolve_target(store, request, connection=opened)
    return _resolve_target(store, request, connection, ())


def _resolve_target(store, request, connection, ancestry):
    try:
        request = ResearchProposalCreate.model_validate(request)
    except (ValueError, TypeError) as exc:
        raise ResearchTargetError('Target artifacts are unavailable, malformed or inconsistent') from exc
    from .provider_correction_native import is_provider_correction, resolve_correction_target
    if is_provider_correction(request):
        _require(not ancestry, code='TARGET_ANCESTRY_LIMIT')
        return resolve_correction_target(store, request, connection=connection)
    try:
        _require(request.operation in {'repair_candidate', 'decide_next_action'} and request.target is not None,
                 code='TARGET_OPERATION_INVALID')
        parent, prior, attempt, output, candidate, reservation = _parent_packet(store, connection, request, ancestry)
        binding = {'schema_version': 1, 'operation': request.operation, 'session_id': request.session_id,
            'parent_proposal_id': parent['id'], 'parent_attempt_id': attempt['id'],
            'parent_response_digest': request.target.response_digest, 'parent_context_digest': parent['context_digest'],
            'parent_request_digest': digest(prior.model_dump()), 'parent_reservation_token': reservation['token'],
            'parent_raw_sha256': attempt['raw_sha256'], 'parent_receipt_digest': digest(attempt['receipt']),
            'candidate_id': candidate['id'], 'candidate_digest': digest(candidate)}
        repair_target, decision_results = None, []
        if request.operation == 'repair_candidate':
            _require(request.target.research_job_id is None and request.target.result_digest is None,
                     code='TARGET_REPAIR_SCOPE')
            _require(parent['status'] == attempt['status'] == 'rejected'
                     and parent['accepted_response_digest'] is None
                     and (attempt['validation'] or {}).get('valid') is False,
                     code='TARGET_NOT_REJECTED')
            _require({item['code'] for item in attempt['validation'].get('diagnostics', [])} == {'CANDIDATES_BLOCKED'},
                     'Target has additional unresolved domain failures', 'TARGET_NOT_REPAIRABLE')
            _require(prior.source.model_dump() == request.source.model_dump()
                     and prior.expected_source_digest == request.expected_source_digest,
                     code='TARGET_SOURCE_MISMATCH')
            draft = output['draft']
            exposed = parent['context'].get('research_context', parent['context'])
            _require(len(draft['candidates']) == 1, 'This repair gate requires one exact candidate', 'TARGET_REPAIR_SCOPE')
            _require(draft['evidence'] == exposed['evidence']
                     and draft['hypotheses'] == exposed['hypotheses'], code='TARGET_ASSERTIONS_INVALID')
            checks = [item for item in attempt['validation'].get('candidate_checks', [])
                      if item['candidate_id'] == candidate['id']]
            _require(len(checks) == 1 and checks[0]['status'] == 'blocked' and checks[0]['code'] == 'invalid_window'
                     and checks[0]['submitted_expression'] == candidate['expression'],
                     'Target has no original implementation-only window diagnostic', 'TARGET_NOT_REPAIRABLE')
            available = {item['name'] for item in exposed['capabilities']['fields']}
            try:
                validate_expression(candidate['expression'], available)
            except ExpressionError as exc:
                _require(exc.code == 'invalid_window', code='TARGET_NOT_REPAIRABLE')
            else:
                raise ResearchTargetError('Target already passes the expression tool', code='TARGET_NOT_REPAIRABLE')
            repaired = repair_candidate_expression(candidate['expression'])
            _require(repaired is not None, 'Target requires a semantic change, not a representation repair', 'TARGET_NOT_REPAIRABLE')
            validate_expression(repaired, available)
            diagnostics = [{'code': checks[0]['code'], 'message': checks[0]['message'], 'phase': 'expression',
                            'candidate_id': candidate['id']}]
            repair_target = RepairTarget.model_validate({
                'parent_proposal_id': parent['id'], 'parent_attempt_id': attempt['id'],
                'parent_response_digest': request.target.response_digest, 'candidate': candidate,
                'candidate_digest': digest(candidate), 'diagnostics': diagnostics,
                'diagnostics_digest': digest(diagnostics),
                'allowed_changes': 'syntax_or_parameters_without_changing_the_research_hypothesis'}).model_dump()
            binding.update(validation_digest=digest(attempt['validation']), diagnostics_digest=digest(diagnostics),
                           repair_policy='integral-window-representation-v1')
        else:
            _require(parent['status'] == attempt['status'] == 'ready'
                     and parent['accepted_attempt_id'] == attempt['id']
                     and parent['accepted_response_digest'] == request.target.response_digest
                     and (attempt['validation'] or {}).get('valid') is True,
                     code='TARGET_NOT_ACCEPTED')
            _require(request.target.research_job_id is not None and request.target.result_digest is not None,
                     code='TARGET_RESULT_REQUIRED')
            from .research_session_experiments import (SessionExperiments, _intent_by_resource,
                                                       verify_experiment_child)
            experiments = SessionExperiments(store)
            intent = _intent_by_resource(store, connection, 'job', request.target.research_job_id)
            _require(intent is not None and intent['request']['session_id'] == request.session_id
                     and intent['request']['proposal_id'] == parent['id']
                     and intent['request']['accepted_response_digest'] == request.target.response_digest,
                     code='TARGET_EXPERIMENT_MISMATCH')
            effect = experiments._effect(connection, intent, 'job')
            _require(effect is not None and effect['response']['id'] == request.target.research_job_id,
                     code='TARGET_EXPERIMENT_MISMATCH')
            verify_experiment_child(store, connection, intent['loaded_session'], intent['reservation'],
                {'proposal_id': None, 'research_job_id': request.target.research_job_id, 'run_id': None})
            from .provider_experiment_results import build_experiment_result
            result = build_experiment_result(store, request.target.research_job_id)
            _require(result['frozen_result_digest'] == request.target.result_digest,
                     code='TARGET_RESULT_MISMATCH')
            _require(request.source.research_id == result['research_id']
                     and request.source.base_revision_id == result['revision_id'],
                     code='TARGET_SOURCE_MISMATCH')
            from .provider_research_context import resolve_research_source
            _require(digest(resolve_research_source(store, request.source.model_dump(), connection=connection))
                     == request.expected_source_digest, code='TARGET_SOURCE_MISMATCH')
            revision = experiments._effect(connection, intent, 'revision')
            run = experiments._effect(connection, intent, 'run')
            _require(revision is not None and revision['response']['id'] == result['revision_id']
                     and run is not None and run['response']['id'] == result['run_id'],
                     code='TARGET_EXPERIMENT_MISMATCH')
            values = [item for item in result['candidates'] if item['id'] == request.target.candidate_id]
            _require(len(values) == 1, code='TARGET_CANDIDATE_MISMATCH')
            actual = values[0]
            normalized = [item for item in intent['plan']['task']['candidates'] if item['id'] == candidate['id']]
            _require(len(normalized) == 1 and actual['accepted_expression'] == normalized[0]['expression']
                     and all(normalized[0][key] == candidate[key] for key in ('id', 'hypothesis_id', 'origin', 'changes')),
                     code='TARGET_CANDIDATE_MISMATCH')
            status = 'completed' if actual['execution_status'] == 'evaluated' else 'blocked'
            diagnostics = [] if status == 'completed' else [{'code': 'CANDIDATE_NOT_EVALUATED',
                'message': actual['reason'] or 'The actual candidate produced no evaluated validation result.',
                'phase': 'execution', 'candidate_id': candidate['id']}]
            decision_results = [DecisionResult.model_validate({'candidate_id': candidate['id'], 'run_id': result['run_id'],
                'result_digest': result['frozen_result_digest'], 'evaluation_period': 'validation',
                'execution_status': status, 'metrics': actual['metrics'] or {}, 'diagnostics': diagnostics}).model_dump()]
            binding.update(experiment_id=intent['id'], research_job_id=result['job_id'], job_digest=result['job_digest'],
                run_id=result['run_id'], attempt_id=result['attempt_id'], revision_id=result['revision_id'],
                completion_step_id=result['completion_step_id'], completion_step_digest=result['completion_step_digest'],
                frozen_result_digest=result['frozen_result_digest'], state_digest=result['state_digest'],
                result_candidate_digest=actual['candidate_digest'], tool_result_digest=actual['tool_result_digest'])
        return {'operation': request.operation, 'parent_draft': deepcopy(output['draft']),
                'repair_target': repair_target, 'decision_results': decision_results,
                'binding': binding, 'binding_digest': digest(binding)}
    except ResearchTargetError:
        raise
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise ResearchTargetError('Target artifacts are unavailable, malformed or inconsistent') from exc


def validate_decision_output(resolved, decision):
    """Validate a suggestion; it grants no execution or budget authority."""
    try:
        value = ResearchDecision.model_validate(decision).model_dump()
        _require(resolved['operation'] == 'decide_next_action'
                 and resolved['binding_digest'] == digest(resolved['binding']))
        target = resolved['decision_results'][0]
        if (value['candidate_id'] != target['candidate_id'] or value['target_digest'] != target['result_digest']):
            return [{'code': 'DECISION_TARGET_MISMATCH', 'message': 'Decision must identify the exact frozen candidate result'}]
        if value['action'] == 'repair':
            return [{'code': 'DECISION_REPAIR_UNSUPPORTED',
                     'message': 'A completed result does not authorize repairing an unverified implementation error'}]
        return []
    except (ValueError, TypeError, KeyError, IndexError):
        return [{'code': 'DECISION_INVALID', 'message': 'Decision has no verified frozen result target'}]
