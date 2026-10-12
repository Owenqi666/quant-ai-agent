"""Pure allowlist projection of already verified original host correction records.

This module does not read files/DB, run a correction rule or grant authority.
HTTP callers must obtain records from the original guarded services first.
"""
from copy import deepcopy

from ..storage import digest
from .service import ServiceError
from .research_correction_schema import CorrectionReceipt, CorrectionPlan
from .research_jobs_schema import ResearchJobSummary
from .provider_correction_public_schema import PublicResearchLoopV2, PublicExperimentV2

MAX_ACTIONS = 24


class CorrectionProjectionError(ServiceError):
    def __init__(self, *, code='CORRECTION_PUBLIC_INTEGRITY'):
        super().__init__('Correction display differs from its verified host record', 409)
        self.code, self.retryable = code, False


def _need(condition):
    if not condition:
        raise CorrectionProjectionError()


def _parent(value, intent):
    parents = [p for p in value['proposals'] if p['id'] == intent['parent_proposal_id']]
    _need(len(parents) == 1)
    parent = parents[0]
    _need(parent['status'] == 'rejected' and parent['accepted_response_digest'] is None
          and parent['current_attempt_id'] == intent['parent_attempt_id'])
    attempts = [a for a in parent['attempts'] if a['id'] == intent['parent_attempt_id']]
    _need(len(attempts) == 1)
    attempt = attempts[0]
    _need(attempt['status'] == 'rejected' and attempt['response_digest'] == intent['parent_response_digest'])
    validation = attempt['validation']
    _need(validation['valid'] is False)
    checks = [c for c in validation['candidate_checks'] if c['candidate_id'] == intent['candidate_id']]
    _need(len(checks) == 1 and checks[0]['status'] == 'blocked' and checks[0]['code'] == 'invalid_window')
    return checks[0]


def _correction(value, action):
    intent, correction = action['request'], action['correction']
    check = _parent(value, intent)
    _need(type(action['sequence']) is int and 1 <= action['sequence'] <= MAX_ACTIONS
          and intent['sequence'] == action['sequence'] and intent['loop_id'] == value['id']
          and intent['session_id'] == value['session_id'] and correction['intent_digest'] == digest(intent))
    request = value['request']
    _need(intent['method'] == 'deterministic'
          and intent['policy_id'] == request['correction_policy_id']
          and intent['policy_digest'] == request['correction_policy_digest'])
    receipt, rules, bundle_digest = None, [], None
    if correction['bundle'] is not None:
        bundle = correction['bundle']
        _need(bundle['bundle_digest'] == digest({k:v for k,v in bundle.items() if k != 'bundle_digest'})
              and bundle['loop_id'] == value['id'] and bundle['sequence'] == action['sequence']
              and bundle['intent_digest'] == correction['intent_digest'])
        original = bundle['receipt']
        checked = CorrectionReceipt.model_validate(original).model_dump()
        _need(checked == original and original['loop_id'] == value['id']
              and original['session_id'] == value['session_id']
              and original['sequence'] == action['sequence']
              and original['intent_digest'] == correction['intent_digest']
              and original['candidate_id'] == intent['candidate_id']
              and original['source_digest'] == intent['source_digest']
              and original['parent_binding_digest'] == intent['parent_binding_digest']
              and original['diagnostics_digest'] == intent['diagnostics_digest']
              and original['submitted_expression'] == check['submitted_expression']
              and original['method'] == 'deterministic' and original['provider_association'] is None
              and original['validation_digest'] == digest(bundle['validation']))
        if original['status'] == 'completed':
            task = bundle['task']
            _need(task is not None and original['accepted_task_digest'] == digest(task)
                  and bundle['validation']['state'] == 'validated'
                  and bundle['validation']['result']['task'] == task)
            candidates = [c for c in task['candidates'] if c['id'] == intent['candidate_id']]
            _need(len(candidates) == 1 and candidates[0]['expression'] == original['accepted_expression'])
        else:
            _need(bundle['task'] is None)
        if bundle['plan'] is not None:
            plan = CorrectionPlan.model_validate(bundle['plan']).model_dump()
            _need(plan == bundle['plan'] and plan['submitted_expression'] == original['submitted_expression'])
            rules = plan['normalization_rule_ids']
            if original['status'] == 'completed':
                _need(plan['accepted_expression'] == original['accepted_expression'])
        receipt_keys = ('schema_version','receipt_digest','policy_id','policy_digest','candidate_id','method','status',
            'source_digest','parent_binding_digest','diagnostics_digest','validation_digest','accepted_task_digest',
            'submitted_expression','accepted_expression','expression_before_digest','expression_after_digest',
            'changed_paths','rule_ids','created_at','provider_association')
        receipt = {k:deepcopy(original[k]) for k in receipt_keys}
        bundle_digest = bundle['bundle_digest']
    return {'sequence':action['sequence'], 'effect_status':correction['status'],
        'started_at':correction['started_at'], 'intent_digest':correction['intent_digest'],
        'parent':{'proposal_id':intent['parent_proposal_id'], 'attempt_id':intent['parent_attempt_id'],
                  'response_digest':intent['parent_response_digest'],'candidate_id':intent['candidate_id'],'status':'rejected'},
        'source_digest':intent['source_digest'], 'capability_digest':intent['capability_digest'],
        'diagnostics_digest':intent['diagnostics_digest'],
        'diagnostics':[{'code':check['code'],'message':check['message'],'phase':'expression','candidate_id':intent['candidate_id']}],
        'receipt':receipt,'normalization_rule_ids':rules,'correction_bundle_digest':bundle_digest,
        'equivalence_status':'same_value_representation_verified' if correction['status'] == 'completed' else 'not_verified'}


RECEIPT_KEYS = ('schema_version','receipt_digest','policy_id','policy_digest','candidate_id','method','status',
    'source_digest','parent_binding_digest','diagnostics_digest','validation_digest','accepted_task_digest',
    'submitted_expression','accepted_expression','expression_before_digest','expression_after_digest',
    'changed_paths','rule_ids','created_at','provider_association')


def _provider_intent(value, action):
    from .provider_research_schema import ProviderCorrectionTarget
    request = action['request']
    target = ProviderCorrectionTarget.model_validate(request['target']).model_dump()
    _need(value['request']['correction_mode'] == 'provider_optional'
          and request['operation'] == 'repair_candidate' and request['session_id'] == value['session_id']
          and target['loop_id'] == value['id'] and target['action_sequence'] == action['sequence']
          and target['effect_key'] == request['idempotency_key']
          and target['policy_id'] == value['request']['correction_policy_id']
          and target['policy_digest'] == value['request']['correction_policy_digest']
          and request['expected_source_digest'] == target['source_digest'])
    _parent(value, {'parent_proposal_id':target['proposal_id'], 'parent_attempt_id':target['attempt_id'],
                   'parent_response_digest':target['response_digest'], 'candidate_id':target['candidate_id']})
    identity = (action['receipt'] or {}).get('proposal_id')
    child = None
    if identity is not None:
        children = [p for p in value['proposals'] if p['id'] == identity]
        _need(len(children) == 1 and children[0]['request'] == request)
        child = children[0]
    return {'schema_version':1, 'intent_digest':digest(request), 'input_digest':target['input_digest'],
        'effect_key':target['effect_key'],
        **{k:target[k] for k in ('policy_id','policy_digest','source_digest','capability_digest',
                                'diagnostics_digest','parent_binding_digest')},
        'parent':{'proposal_id':target['proposal_id'], 'attempt_id':target['attempt_id'],
                  'response_digest':target['response_digest'], 'candidate_id':target['candidate_id'],
                  'status':'rejected'}, 'proposal_id':identity,
        'proposal_status':child['status'] if child else None}


def _provider_receipt(value, action):
    original = (action['receipt'] or {}).get('correction_receipt')
    if original is None:
        return None
    _need(value['request']['correction_mode'] == 'provider_optional' and action['kind'] == 'proposal.reconcile')
    reservations = [r for r in value['session']['reservations'] if r['token'] == action['request']['token']]
    _need(len(reservations) == 1 and reservations[0]['outcome'] is not None
          and action['receipt']['outcome_digest'] == digest(reservations[0]['outcome']))
    child_id = (reservations[0]['children'] or {}).get('proposal_id')
    children = [p for p in value['proposals'] if p['id'] == child_id]
    _need(len(children) == 1)
    child = children[0]
    creators = [a for a in value['actions'] if a['kind'] == 'proposal.create'
                and (a['receipt'] or {}).get('proposal_id') == child['id']]
    _need(len(creators) == 1)
    creator = creators[0]
    intent = _provider_intent(value, creator)
    target = creator['request']['target']
    checked = CorrectionReceipt.model_validate(original).model_dump()
    _need(checked == original and original['method'] == 'provider'
          and original['loop_id'] == value['id'] and original['session_id'] == value['session_id']
          and original['sequence'] == creator['sequence'] and original['intent_digest'] == intent['intent_digest']
          and original['created_at'] == action['planned_at'])
    _need(all(original[k] == target[k] for k in ('policy_id','policy_digest','candidate_id',
                                                'source_digest','parent_binding_digest','diagnostics_digest')))
    attempts = [a for a in child['attempts'] if a['id'] == child['current_attempt_id']]
    _need(len(attempts) == 1)
    attempt = attempts[0]
    _need(original['provider_association'] == {'proposal_id':child['id'], 'attempt_id':attempt['id'],
                                              'response_digest':attempt['response_digest']}
          and original['validation_digest'] == digest(attempt['validation']))
    proof = attempt['validation']['correction_proof']
    check = _parent(value, {'parent_proposal_id':target['proposal_id'], 'parent_attempt_id':target['attempt_id'],
                           'parent_response_digest':target['response_digest'], 'candidate_id':target['candidate_id']})
    _need(original['submitted_expression'] == check['submitted_expression'])
    if original['status'] == 'completed':
        _need(child['status'] == 'ready' and original['accepted_task_digest'] == proof['task_digest']
              and original['accepted_expression'] == proof['plan']['accepted_expression'])
    else:
        _need(child['status'] != 'ready' and original['accepted_task_digest'] is None)
    return {k:deepcopy(original[k]) for k in RECEIPT_KEYS}


def _candidate_source(experiment):
    request = experiment['request']
    version = request.get('schema_version', 1)
    _need(type(version) is int)
    if version == 1:
        return {'kind':'provider_accepted_candidate','proposal_id':request['proposal_id'],
                'accepted_response_digest':request['accepted_response_digest']}
    _need(version == 2 and request['candidate_source']['kind'] == 'host_corrected_rejected_candidate'
          and request['candidate_source'] == experiment['plan']['candidate_source']
          and request['candidate_source']['accepted_task_digest'] == experiment['plan']['task_digest'])
    return deepcopy(request['candidate_source'])


def _finish(original, model, redact, *, nested_redacted=False):
    # Sanitize a copy so a callback cannot mutate the host record or our comparison.
    public = redact(deepcopy(original))
    _need(type(public) is dict)
    public['public_projection_redacted'] = public != original or nested_redacted
    return model.model_validate(public).model_dump()


def project_public_loop_v2(verified_value, *, public_proposals, public_session, redact):
    """Project service.get output. No file/DB access, rule pass or execution."""
    try:
        value, request = verified_value, verified_value['request']
        _need(value['schema_version'] == request['schema_version'] == 2
              and request['correction_mode'] in {'deterministic', 'provider_optional'}
              and value['session_id'] == public_session['id'] == value['session']['id']
              and public_session['state_digest'] == value['session']['state_digest']
              and [p['id'] for p in public_proposals] == [p['id'] for p in value['proposals']])
        actions = []
        for action in value['actions']:
            ack, kind = action['receipt'] or {}, action['kind']
            actions.append({'sequence':action['sequence'],'idempotency_key':action['idempotency_key'],
                'kind':kind,'experiment_action':action['request']['action'] if kind == 'experiment.advance' else None,
                'planned_at':action['planned_at'],'status':'pending' if action['receipt'] is None else 'acknowledged',
                'request_digest':digest(action['request']),
                'receipt_digest':digest(action['receipt']) if action['receipt'] is not None else None,
                **{k:ack.get(k) for k in ('proposal_id','experiment_id','step_id')},
                'correction':_correction(value, action) if kind == 'correction.apply' else None,
                'provider_correction':_provider_intent(value, action) if kind == 'proposal.create'
                    and (action['request'].get('target') or {}).get('correction_version') == 1 else None,
                'provider_correction_receipt':_provider_receipt(value, action)})
        experiment = value['experiment']
        experiment_view = None
        if experiment is not None:
            job = experiment['job']
            experiment_view = {'id':experiment['id'],'candidate_source':_candidate_source(experiment),
                'task_digest':experiment['plan']['task_digest'],'job_id':job['id'] if job else None,
                **{k:job.get(k) if job else None for k in ('state','revision_id','run_id')}}
        original = {k:deepcopy(value[k]) for k in ('schema_version','id','session_id','created_at','request_digest','state','reason','next_action')}
        original.update(policy={**{k:deepcopy(request[k]) for k in ('max_repairs','allow_experiment','experiment_budget',
            'correction_policy_id','correction_policy_digest','correction_mode')},'max_experiments':1,
            'max_actions':MAX_ACTIONS,'max_deterministic_passes':1},actions=actions,
            proposals=deepcopy(public_proposals),experiment=experiment_view,
            decision=None,decision_applied=False,session=deepcopy(public_session),
            semantic_fidelity='unverified',real_dispatch_qualified=False)
        return _finish(original, PublicResearchLoopV2, redact,
            nested_redacted=public_session['public_projection_redacted'])
    except (KeyError, TypeError, ValueError) as exc:
        raise CorrectionProjectionError() from exc


def project_public_experiment_v2(verified_value, *, public_session, public_reservation, redact):
    """Project the real corrected experiment; no fake accepted Provider fields."""
    try:
        value, request, plan = verified_value, verified_value['request'], verified_value['plan']
        _need(request['schema_version'] == 2 and request['session_id'] == public_session['id'] == value['session']['id']
              and public_session['state_digest'] == value['session']['state_digest']
              and public_reservation['token'] == value['reservation']['token']
              and public_reservation['action'] == 'experiment'
              and plan['source_request']['kind'] == 'research_revision')
        source = _candidate_source(value)
        job = value['job']
        original = {'schema_version':2,'id':value['id'],'session_id':request['session_id'],
            'candidate_source':source,'request_digest':value['request_digest'],'task_digest':plan['task_digest'],
            'source_kind':plan['source_request']['kind'],'created_at':value['created_at'],'budget':request['budget'],
            'bootstrap':value['bootstrap'],'job':{k:job[k] for k in ResearchJobSummary.model_fields} if job else None,
            'session':deepcopy(public_session),'reservation':deepcopy(public_reservation),
            'candidate_checks':deepcopy(plan['candidate_checks']),'semantic_fidelity':'unverified',
            'budget_scope':'session_top_level_actions_and_experiments_with_separate_job_steps'}
        nested = (public_session['public_projection_redacted'] or public_reservation != value['reservation'])
        return _finish(original, PublicExperimentV2, redact, nested_redacted=nested)
    except (KeyError, TypeError, ValueError) as exc:
        raise CorrectionProjectionError() from exc
