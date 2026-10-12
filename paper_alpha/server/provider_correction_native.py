"""Versioned Provider correction over the original Loop/Session/Proposal chain.

Observation is pure. A policy pass is permitted only in output validation after
the original permanent repair charge. Saved proof/receipt readers never rerun
the policy, reserve a budget, or mint an execution permit.
"""
from contextlib import closing
from copy import deepcopy
import ast

from ..storage import digest, json_text
from .db import connect
from .provider_contract import digest as wire_digest
from .provider_research_schema import ResearchProposalCreate, ProviderCorrectionTarget, output_model
from .research_sessions import ResearchSessions, SessionError, _parse

ADAPTER_VERSION = 'research-provider-correction-v1'
CORRECTION_INSTRUCTION = 'Repair only the exact target implementation under the supplied constraints.'
PROOF_KEYS = frozenset({'schema_version', 'adapter_version', 'input_digest', 'parent_binding_digest',
    'source_digest', 'capability_digest', 'diagnostics_digest', 'policy_id', 'policy_digest',
    'plan', 'draft_digest', 'task', 'task_digest', 'proof_digest'})


def _need(condition, code='CORRECTION_PROVIDER_INTEGRITY'):
    if not condition:
        raise SessionError('Provider correction differs from its original bound records', code=code)


def is_provider_correction(request):
    value = request.model_dump() if hasattr(request, 'model_dump') else request
    return (type(value) is dict and value.get('operation') == 'repair_candidate'
            and type(value.get('target')) is dict and value['target'].get('correction_version') == 1)


def _input(observed, *, loop_id, sequence, effect_key, policy_id, policy_digest):
    return {'adapter_version': ADAPTER_VERSION, 'loop_id': loop_id, 'action_sequence': sequence,
        'effect_key': effect_key, 'policy_id': policy_id, 'policy_digest': policy_digest,
        'parent_binding_digest': observed['parent_binding_digest'], 'draft_digest': digest(observed['draft']),
        'source_digest': observed['parent_request']['expected_source_digest'],
        'capability_digest': observed['capability_digest'], 'diagnostics_digest': observed['diagnostics_digest']}


def correction_target(observed, *, loop_id, sequence, effect_key, policy_id, policy_digest):
    """Bind observed input only; no repaired output or model bytes are inferred."""
    identity = _input(observed, loop_id=loop_id, sequence=sequence, effect_key=effect_key,
                      policy_id=policy_id, policy_digest=policy_digest)
    return ProviderCorrectionTarget.model_validate({**observed['target'],
        **{k: v for k, v in identity.items() if k not in {'adapter_version', 'draft_digest'}},
        'correction_version': 1, 'input_digest': digest(identity)}).model_dump()


def _loop_intent(store, connection, request, observed):
    """Verify the persisted original Loop intent without recursive Loop readers."""
    from .research_loops_schema import parse_loop_create
    target = request.target
    row = connection.execute('SELECT * FROM research_loops WHERE id=?', (target.loop_id,)).fetchone()
    _need(row is not None, 'CORRECTION_INTENT_REQUIRED')
    loop = parse_loop_create(_parse(row['payload'], 1024 * 1024)).model_dump()
    _need(loop['schema_version'] == 2 and loop['correction_mode'] == 'provider_optional'
          and loop['max_repairs'] == 1 and row['session_id'] == request.session_id
          and row['request_digest'] == row['payload_digest'] == digest(loop)
          and row['id'] == 'research_loop_' + digest(loop)
          and loop['correction_policy_id'] == target.policy_id
          and loop['correction_policy_digest'] == target.policy_digest)
    rows = connection.execute('SELECT * FROM research_loop_actions WHERE loop_id=? ORDER BY sequence',
                              (target.loop_id,)).fetchall()
    actions, previous = [], None
    for item in rows:
        action = _parse(item['payload'], 1024 * 1024)
        _need(item['sequence'] == action['sequence'] == len(actions) + 1
              and action['loop_id'] == target.loop_id and action['previous_digest'] == previous
              and item['digest'] == digest(action) and item['idempotency_key'] == action['idempotency_key'])
        actions.append(action)
        previous = item['digest']
    _need(len(actions) == row['action_count'] and digest([digest(a) for a in actions]) == row['ledger_digest'])
    matches = [a for a in actions if a['sequence'] == target.action_sequence]
    _need(len(matches) == 1, 'CORRECTION_INTENT_REQUIRED')
    intent = matches[0]
    _need(intent['kind'] == 'proposal.create' and intent['request'] == request.model_dump()
          and intent['effect_key'] == request.idempotency_key == target.effect_key
          and target.effect_key == 'loop-effect-' + digest({'loop_id': target.loop_id, 'sequence': target.action_sequence}),
          'CORRECTION_INTENT_MISMATCH')
    repairs = [a for a in actions if a['kind'] == 'proposal.create'
               and a['request'].get('operation') == 'repair_candidate']
    _need(len(repairs) == 1 and not any(a['kind'] == 'correction.apply' for a in actions),
          'CORRECTION_SLOT_MISMATCH')
    # C00 has one initial generation, its reconciliation, then one repair.
    _need(target.action_sequence == 3 and actions[0]['kind'] == 'proposal.create'
          and actions[0]['request'] == loop['initial_proposal']
          and actions[0]['receipt'] == {'proposal_id': target.proposal_id}
          and target.proposal_id == 'proposal_' + digest(loop['initial_proposal'])
          and actions[1]['kind'] == 'proposal.reconcile'
          and actions[1]['request']['token'] == observed['parent_binding']['parent_reservation_token']
          and actions[1]['receipt'] is not None, 'CORRECTION_PARENT_NOT_OWNED')
    return intent


def resolve_correction_target(store, request, *, connection=None, require_current=True):
    if connection is None:
        with closing(connect(store.db_path)) as opened:
            opened.execute('BEGIN')
            return resolve_correction_target(store, request, connection=opened, require_current=require_current)
    from .research_loop_correction import observe_rejected_parent
    request = ResearchProposalCreate.model_validate(request)
    _need(isinstance(request.target, ProviderCorrectionTarget), 'CORRECTION_TARGET_VERSION_REQUIRED')
    target = request.target
    observed = observe_rejected_parent(store, connection, session_id=request.session_id,
        proposal_id=target.proposal_id, attempt_id=target.attempt_id, response_digest=target.response_digest,
        candidate_id=target.candidate_id, require_current=require_current, require_reconciled=True)
    actual = correction_target(observed, loop_id=target.loop_id, sequence=target.action_sequence,
        effect_key=target.effect_key, policy_id=target.policy_id, policy_digest=target.policy_digest)
    _need(actual == target.model_dump() and request.expected_source_digest == target.source_digest,
          'CORRECTION_INPUT_MISMATCH')
    _loop_intent(store, connection, request, observed)
    candidate = observed['draft']['candidates'][0]
    repair = {'parent_proposal_id': target.proposal_id, 'parent_attempt_id': target.attempt_id,
        'parent_response_digest': target.response_digest, 'candidate': deepcopy(candidate),
        'candidate_digest': digest(candidate), 'diagnostics': observed['diagnostics'],
        'diagnostics_digest': target.diagnostics_digest,
        'allowed_changes': 'syntax_or_parameters_without_changing_the_research_hypothesis'}
    binding = {**observed['parent_binding'], 'operation': 'repair_candidate', 'adapter_version': ADAPTER_VERSION,
        'policy_id': target.policy_id, 'policy_digest': target.policy_digest,
        'input_digest': target.input_digest, 'loop_id': target.loop_id,
        'action_sequence': target.action_sequence, 'effect_key': target.effect_key}
    return {'operation': 'repair_candidate', 'parent_draft': deepcopy(observed['draft']),
        'repair_target': repair, 'decision_results': [], 'binding': binding,
        'binding_digest': digest(binding), 'observation': observed}


def build_correction_context(store, request, *, connection=None):
    from .research_correction_policy import resolve_policy
    from .research_session_context import frozen_session_snapshot, build_repair_context
    from .provider_context import build_provider_context
    request = ResearchProposalCreate.model_validate(request)
    resolved = resolve_correction_target(store, request, connection=connection)
    session = frozen_session_snapshot(store, request, connection=connection)
    base = build_provider_context(store, request.source.research_id, request.source.base_revision_id)
    value = build_repair_context(session, base, resolved['repair_target'])
    policy = resolve_policy(request.target.policy_id, request.target.policy_digest)
    value.update(schema_version=2, context_version=ADAPTER_VERSION, source_kind='research_revision',
        target_binding_digest=resolved['binding_digest'], correction_input_digest=request.target.input_digest,
        correction_policy={'policy_id': policy['policy_id'], 'policy_digest': policy['policy_digest'],
                           'rule_ids': policy['rule_ids']})
    return value


def validate_correction_response(store, request, context, output):
    """One unique policy plan after the original permanent repair charge."""
    from .provider_repair_semantics import validate_versioned_repair_output
    from .research_draft_validation import validate_research_draft
    request = ResearchProposalCreate.model_validate(request)
    output = output_model().model_validate(output)
    diagnostics, checks, proof = [], [], None
    def problem(code, message):
        diagnostics.append({'code': code, 'message': message, 'semantic_fidelity': 'unverified'})
    try:
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            resolved = resolve_correction_target(store, request, connection=connection)
            loaded = ResearchSessions(store)._load(connection, request.session_id)
            charges = [r for r in loaded['reservations'] if r['effect_key'] == request.idempotency_key]
            _need(len(charges) == 1 and charges[0]['action'] == 'repair'
                  and charges[0]['action_digest'] == digest(request.model_dump())
                  and charges[0]['charges']['provider_dispatches'] == 1
                  and (charges[0]['children'] or {}).get('proposal_id') == output.proposal_id,
                  'CORRECTION_CHARGE_REQUIRED')
        _need(output.operation == request.operation and output.context_digest == wire_digest(context)
              and build_correction_context(store, request) == context, 'SOURCE_CHANGED')
        if output.outcome == 'abstain':
            from .provider_context import _validate_abstention
            exposed = context['research_context']
            if not set(output.abstention.evidence_ids) <= {e['id'] for e in exposed['evidence']}:
                problem('EVIDENCE_NOT_EXPOSED', 'Abstention cites evidence not sent in this request.')
            _validate_abstention(output.abstention.model_dump(), exposed, problem)
        else:
            draft = output.draft.model_dump()
            observed = resolved['observation']
            checked = validate_versioned_repair_output(observed['draft'], draft,
                candidate_id=request.target.candidate_id, available_fields=observed['actual_fields'],
                policy_id=request.target.policy_id, policy_digest=request.target.policy_digest)
            for item in checked['diagnostics']:
                problem(item['code'], item['message'])
            task = None
            if any(c['origin'] == 'user_modification' for c in draft['candidates']):
                problem('ATTRIBUTION_INVALID', 'Model output cannot claim user modification authority.')
            if not diagnostics:
                state, error, result = validate_research_draft(store,
                    source=observed['source_snapshot'], draft=draft)
                checks = result.get('candidate_checks', [])
                if state != 'validated':
                    problem(error['code'], error['message'])
                else:
                    task = result['task']
            proof = {'schema_version': 1, 'adapter_version': ADAPTER_VERSION,
                **{k: getattr(request.target, k) for k in ('input_digest', 'parent_binding_digest',
                    'source_digest', 'capability_digest', 'diagnostics_digest', 'policy_id', 'policy_digest')},
                'plan': checked['plan'], 'draft_digest': digest(draft), 'task': task,
                'task_digest': digest(task) if task is not None else None}
            proof['proof_digest'] = digest(proof)
        _need(build_correction_context(store, request) == context, 'SOURCE_CHANGED')
    except (SessionError, ValueError, TypeError, KeyError, OSError) as exc:
        problem(getattr(exc, 'code', 'CORRECTION_PROVIDER_INVALID'), store._safe_error(exc))
    return {'valid': not diagnostics, 'diagnostics': diagnostics, 'candidate_checks': checks,
            'correction_proof': proof}


def verify_saved_correction(store, connection, request, attempt, draft, *, require_accepted=True):
    """Verify actual saved proof and original pins, with zero policy passes."""
    from .research_correction_schema import CorrectionPlan
    from .provider_repair_semantics import _normalized
    from ..expressions import _parse as parse_expression
    request = ResearchProposalCreate.model_validate(request)
    resolved = resolve_correction_target(store, request, connection=connection, require_current=False)
    validation = attempt['validation']
    proof = (validation or {}).get('correction_proof')
    _need(type(proof) is dict and set(proof) == PROOF_KEYS
          and proof['schema_version'] == 1 and proof['adapter_version'] == ADAPTER_VERSION
          and proof['proof_digest'] == digest({k: v for k, v in proof.items() if k != 'proof_digest'})
          and proof['draft_digest'] == digest(draft))
    for key in ('input_digest', 'parent_binding_digest', 'source_digest', 'capability_digest',
                'diagnostics_digest', 'policy_id', 'policy_digest'):
        _need(proof[key] == getattr(request.target, key))
    if proof['plan'] is not None:
        plan = CorrectionPlan.model_validate(proof['plan']).model_dump()
        _need(plan['policy_id'] == request.target.policy_id and plan['policy_digest'] == request.target.policy_digest
              and plan['submitted_expression'] == resolved['parent_draft']['candidates'][0]['expression']
              and plan['available_fields_digest'] == digest(resolved['observation']['actual_fields']))
    else:
        plan = None
    if require_accepted:
        _need(validation['valid'] is True and plan is not None and type(proof['task']) is dict
              and proof['task_digest'] == digest(proof['task']))
        _need(len(draft['candidates']) == 1 and draft['candidates'][0]['id'] == request.target.candidate_id)
        candidate = draft['candidates'][0]
        tree, _ = _normalized(candidate['expression'], convert_windows=False)
        _need(ast.dump(tree, include_attributes=False) ==
              ast.dump(parse_expression(plan['accepted_expression']), include_attributes=False))
        frozen = deepcopy(draft)
        frozen['candidates'][0]['expression'] = resolved['parent_draft']['candidates'][0]['expression']
        _need(json_text(frozen) == json_text(resolved['parent_draft']))
        canonical = deepcopy(draft)
        canonical['candidates'][0]['expression'] = plan['accepted_expression']
        source_task = resolved['observation']['source_snapshot']['revision']['task']
        expected = store._task({**canonical, 'evaluation': source_task['evaluation'], 'budget': source_task['budget']},
            resolved['observation']['source_snapshot']['research']['title'],
            resolved['observation']['source_snapshot']['research']['dataset_id'])
        _need(expected == proof['task'])
    return {'proof': deepcopy(proof), 'resolved': resolved}


def correction_receipt(store, connection, proposal, *, created_at):
    """Build a terminal receipt from the actual response and saved validator proof."""
    from .research_correction_schema import CorrectionReceipt
    from .proposal_requests import ProposalRequests
    request = ResearchProposalCreate.model_validate(proposal['request'])
    if not is_provider_correction(request):
        return None
    attempt = next((a for a in proposal['attempts'] if a['id'] == proposal['current_attempt_id']), None)
    if (attempt is None or attempt['response_digest'] is None
            or (attempt.get('receipt') or {}).get('process_stopped') is not True
            or (attempt.get('validation') or {}).get('correction_proof') is None):
        return None
    requests = ProposalRequests(store, configs={})
    output = requests._json_path(requests._folder(proposal['id']) / 'attempts' / attempt['id'] / 'parsed.json')
    checked = verify_saved_correction(store, connection, request, attempt, output['draft'],
                                     require_accepted=proposal['status'] == 'ready')
    proof = checked['proof']; plan = proof['plan']
    accepted = proposal['status'] == 'ready'
    before = checked['resolved']['parent_draft']['candidates'][0]['expression']
    receipt = {'schema_version': 1, 'loop_id': request.target.loop_id, 'session_id': request.session_id,
        'candidate_id': request.target.candidate_id, 'intent_digest': digest(request.model_dump()),
        **{k: getattr(request.target, k) for k in ('parent_binding_digest', 'diagnostics_digest',
                                               'source_digest', 'policy_id', 'policy_digest')},
        'submitted_expression': before, 'accepted_expression': plan['accepted_expression'] if accepted else None,
        'expression_before_digest': digest(before),
        'expression_after_digest': plan['expression_after_digest'] if accepted else None,
        'changed_paths': plan['changed_paths'] if plan else [], 'rule_ids': plan['rule_ids'] if plan else [],
        'method': 'provider', 'validation_digest': digest(attempt['validation']),
        'accepted_task_digest': proof['task_digest'] if accepted else None,
        'sequence': request.target.action_sequence, 'created_at': created_at,
        'status': 'completed' if accepted else 'blocked',
        'provider_association': {'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
                                 'response_digest': attempt['response_digest']}}
    receipt['receipt_digest'] = digest(receipt)
    return CorrectionReceipt.model_validate(receipt).model_dump()
