"""Host correction helpers over original loop/session/provider records.

Only execute_correction runs a correction pass. Its caller owns the original
loop-correction lock and a durably committed started CAS; this module never
reserves, writes, dispatches, or refunds a slot. Observers and historical bundle
verification never replay a rule. Native *parent* receipts can be consumed when
original stopped/failed ownership is proven; no Native invocation occurs here.
N2 task qualification is limited to the existing synthetic ordinary Task.
"""
from __future__ import annotations
from copy import deepcopy
import ast
import json
from types import SimpleNamespace

from ..storage import digest, json_text
from ..expressions import validate_expression, ExpressionError
from .research_sessions import ResearchSessions, SessionError, _parse, _timestamp
from .research_loops_schema import parse_loop_create
from .research_correction_schema import CorrectionPlan, CorrectionReceipt
from .research_correction_policy import resolve_policy, correct_draft
from .research_draft_validation import validate_research_draft

MAX_BYTES = 1024 * 1024
INTENT_KEYS = frozenset({'schema_version','loop_id','session_id','sequence','effect_key','parent_proposal_id','parent_attempt_id','parent_response_digest','candidate_id','source_digest','capability_digest','diagnostics_digest','parent_binding_digest','policy_id','policy_digest','method','deadline_at'})
CORRECTION_KEYS = frozenset({'intent_digest','reservation_token','status','started_at','bundle'})
BUNDLE_KEYS = frozenset({'schema_version','loop_id','sequence','intent_digest','receipt','plan','validation','task','source_snapshot','parent_binding','bundle_digest'})
SOURCE_KEYS = frozenset({'kind','loop_id','correction_action_sequence','intent_digest','correction_receipt_digest','parent_proposal_id','parent_attempt_id','parent_response_digest','accepted_task_digest'})


class CorrectionExecutionError(SessionError):
    def __init__(self, *, code='CORRECTION_INTEGRITY'):
        super().__init__('Host correction records are unavailable or differ from their original binding', code=code)


def _need(value, code='CORRECTION_INTEGRITY'):
    if not value:
        raise CorrectionExecutionError(code=code)


def _same(left, right):
    return json_text(left) == json_text(right)


def _historical_source(store, connection, source_request):
    from .research_jobs import ResearchJobs
    from .provider_catalog import synthetic_daily_projection
    from ..evaluation import FIELDS, AVAILABILITY
    _need(source_request.get('kind') == 'research_revision', 'CORRECTION_SOURCE_UNQUALIFIED')
    research = connection.execute('SELECT * FROM researches WHERE id=?', (source_request['research_id'],)).fetchone()
    _need(research is not None, 'CORRECTION_SOURCE_INTEGRITY')
    metadata_row = connection.execute('SELECT metadata FROM datasets WHERE id=?', (research['dataset_id'],)).fetchone()
    _need(metadata_row is not None, 'CORRECTION_SOURCE_INTEGRITY')
    metadata = _parse(metadata_row['metadata'], MAX_BYTES)
    # Check qualification before any source-data resource is opened.
    _need(metadata.get('data_kind') == 'synthetic' and metadata.get('columns') == ['date','asset',*FIELDS]
          and metadata.get('field_availability') == AVAILABILITY, 'CORRECTION_SOURCE_UNQUALIFIED')
    source = ResearchJobs(store)._source(source_request['research_id'], source_request['base_revision_id'])
    _need(source['revision']['task'].get('schema_version', 1) == 1, 'CORRECTION_SOURCE_UNQUALIFIED')
    projection = synthetic_daily_projection(metadata, source['revision']['task']['evaluation'])
    return source, projection['capabilities']


def _diagnostic_scope(expression, fields, required_fields, capabilities):
    """Inspect the original failed tool diagnostic; never derive a repair."""
    _need(set(required_fields) <= set(fields), 'CORRECTION_FIELDS_UNAVAILABLE')
    try:
        tree = ast.parse(expression, mode='eval')
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        operators = {item['name']: item['arity'] for item in capabilities['operators']}
        aliases = capabilities['documented_aliases']
        function_nodes = set()
        for node in calls:
            _need(isinstance(node.func, ast.Name) and not node.keywords, 'CORRECTION_DIAGNOSTIC_UNSUPPORTED')
            function_nodes.add(id(node.func))
            name = aliases.get(node.func.id, node.func.id)
            _need(name in operators and len(node.args) == operators[name], 'CORRECTION_DIAGNOSTIC_UNSUPPORTED')
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in function_nodes}
        _need(used <= set(fields) and used <= set(required_fields), 'CORRECTION_FIELDS_UNAVAILABLE')
        try:
            validate_expression(expression, set(fields))
        except ExpressionError as exc:
            _need(exc.code == 'invalid_window', 'CORRECTION_DIAGNOSTIC_UNSUPPORTED')
        else:
            raise CorrectionExecutionError(code='CORRECTION_NOT_REJECTED')
    except (SyntaxError, TypeError, RecursionError):
        raise CorrectionExecutionError(code='CORRECTION_DIAGNOSTIC_UNSUPPORTED') from None


def observe_rejected_parent(store, connection, *, session_id, proposal_id, attempt_id,
                            response_digest, candidate_id, require_current=True,
                            require_reconciled=True):
    """Read the actual generation rejection; no correction planning or writes."""
    from .proposal_requests import ProposalRequests
    from .provider_research_schema import ResearchProposalCreate
    from .provider_research_targets import _parent_packet
    from .provider_research_context import build_research_context
    _need(type(require_current) is bool and type(require_reconciled) is bool)
    try:
        proposals = ProposalRequests(store, configs={})
        preliminary = proposals._load(connection, proposal_id)
        prior = ResearchProposalCreate.model_validate(preliminary['request'])
        # Do this before _parent_packet: its old repair-ancestor branch plans.
        _need(prior.operation == 'generate_candidates' and prior.session_id == session_id
              and prior.source.kind == 'research_revision', 'CORRECTION_PARENT_UNSUPPORTED')
        target = {'proposal_id': proposal_id, 'attempt_id': attempt_id, 'response_digest': response_digest,
                  'candidate_id': candidate_id, 'research_job_id': None, 'result_digest': None}
        carrier = SimpleNamespace(session_id=session_id, target=SimpleNamespace(**target))
        parent, request, attempt, output, candidate, reservation = _parent_packet(store, connection, carrier, ())
        _need(parent['status'] == attempt['status'] == 'rejected'
              and parent['accepted_response_digest'] is None and parent['accepted_attempt_id'] is None,
              'CORRECTION_NOT_REJECTED')
        validation = attempt['validation'] or {}
        _need(validation.get('valid') is False and len(validation.get('diagnostics', [])) == 1
              and validation['diagnostics'][0]['code'] == 'CANDIDATES_BLOCKED', 'CORRECTION_DIAGNOSTIC_UNSUPPORTED')
        draft = output['draft']
        exposed = parent['context'].get('research_context', parent['context'])
        _need(len(draft['candidates']) == 1 and draft['evidence'] == exposed['evidence']
              and draft['hypotheses'] == exposed['hypotheses'], 'CORRECTION_ASSERTIONS_INVALID')
        checks = validation.get('candidate_checks', [])
        _need(len(checks) == 1 and checks[0]['candidate_id'] == candidate_id and checks[0]['status'] == 'blocked'
              and checks[0]['code'] == 'invalid_window' and checks[0]['submitted_expression'] == candidate['expression']
              and checks[0]['accepted_expression'] is None, 'CORRECTION_DIAGNOSTIC_UNSUPPORTED')
        if require_reconciled or reservation['outcome'] is not None:
            _need(reservation['status'] == 'finished' and reservation['outcome'] is not None
                  and reservation['outcome']['status'] == 'failed' and reservation['outcome']['code'] == 'PROPOSAL_REJECTED'
                  and reservation['outcome']['result_digest'] == response_digest, 'CORRECTION_PARENT_NOT_RECONCILED')
            ResearchSessions(store)._verify_outcome(connection, reservation, reservation['outcome'])
        source, capabilities = _historical_source(store, connection, request.source.model_dump())
        _need(digest(source) == request.expected_source_digest and exposed['capabilities'] == capabilities,
              'CORRECTION_SOURCE_INTEGRITY')
        fields = sorted(item['name'] for item in capabilities['fields'])
        hypotheses = [h for h in draft['hypotheses'] if h['id'] == candidate['hypothesis_id']]
        _need(len(hypotheses) == 1, 'CORRECTION_ASSERTIONS_INVALID')
        _diagnostic_scope(candidate['expression'], fields, hypotheses[0]['required_fields'], capabilities)
        if require_current:
            sessions = ResearchSessions(store)
            loaded = sessions._load(connection, session_id)
            sessions._open(loaded)
            _need(loaded['state']['usage']['failures'] < loaded['request']['budget']['max_failures'], 'SESSION_BUDGET_EXCEEDED')
            sessions._resolve(connection, loaded['state']['current_source'])
            _need(loaded['state']['current_source']['source_digest'] == request.expected_source_digest
                  and build_research_context(store, request, connection=connection) == parent['context'], 'SOURCE_CHANGED')
        diagnostics = [{'code': 'invalid_window', 'message': checks[0]['message'], 'phase': 'expression', 'candidate_id': candidate_id}]
        binding = {'schema_version': 1, 'operation': 'host_deterministic_correction', 'session_id': session_id,
            'parent_proposal_id': proposal_id, 'parent_attempt_id': attempt_id, 'parent_response_digest': response_digest,
            'parent_context_digest': parent['context_digest'], 'parent_request_digest': digest(request.model_dump()),
            'parent_reservation_token': reservation['token'], 'parent_raw_sha256': attempt['raw_sha256'],
            'parent_receipt_digest': digest(attempt['receipt']), 'candidate_id': candidate_id,
            'candidate_digest': digest(candidate), 'validation_digest': digest(validation),
            'diagnostics_digest': digest(diagnostics), 'source_digest': request.expected_source_digest,
            'capability_digest': digest(capabilities)}
        return {'source_snapshot': source, 'actual_fields': fields, 'capability_digest': digest(capabilities),
                'diagnostics': diagnostics, 'diagnostics_digest': digest(diagnostics),
                'parent_binding': binding, 'parent_binding_digest': digest(binding), 'draft': deepcopy(draft),
                'parent_request': request.model_dump(), 'context_digest': parent['context_digest'],
                'raw_sha256': attempt['raw_sha256'], 'target': target}
    except SessionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise CorrectionExecutionError(code='CORRECTION_PARENT_INTEGRITY') from None


def _loop_record(connection, loop_id):
    """Independent original DB chain reader; deliberately never Loop._load."""
    row = connection.execute('SELECT * FROM research_loops WHERE id=?', (loop_id,)).fetchone()
    _need(row is not None, 'CORRECTION_LOOP_REQUIRED')
    request = parse_loop_create(_parse(row['payload'], MAX_BYTES)).model_dump()
    _need(request['schema_version'] == 2 and request['correction_mode'] == 'deterministic', 'CORRECTION_MODE_UNQUALIFIED')
    _need(row['request_digest'] == row['payload_digest'] == digest(request)
          and row['id'] == 'research_loop_' + digest(request) and request['idempotency_key'] == row['idempotency_key']
          and request['initial_proposal']['session_id'] == row['session_id'])
    actions, previous, keys = [], None, set()
    for record in connection.execute('SELECT * FROM research_loop_actions WHERE loop_id=? ORDER BY sequence', (loop_id,)):
        action = _parse(record['payload'], MAX_BYTES)
        _need(type(action['sequence']) is int and record['sequence'] == action['sequence'] == len(actions) + 1
              and action['loop_id'] == loop_id and action['previous_digest'] == previous
              and type(action['idempotency_key']) is str and 0 < len(action['idempotency_key']) <= 128
              and action['idempotency_key'] not in keys and action['idempotency_key'] == record['idempotency_key']
              and digest(action) == record['digest'] and (not actions or actions[-1]['receipt'] is not None)
              and action['effect_key'] == 'loop-effect-' + digest({'loop_id': loop_id, 'sequence': action['sequence']}))
        if action['kind'] == 'correction.apply':
            correction = action.get('correction')
            _need(type(correction) is dict and set(correction) == CORRECTION_KEYS)
            _need(correction['status'] in {'planned','started','unknown','completed','blocked'})
            if correction['status'] == 'planned':
                _need(correction['started_at'] is None and correction['bundle'] is None and action['receipt'] is None)
            else:
                _need(type(correction['started_at']) is str
                      and _timestamp(action['planned_at']) <= _timestamp(correction['started_at'])
                      < _timestamp(action['request']['deadline_at']))
                if correction['status'] in {'started','unknown'}:
                    _need(correction['bundle'] is None and (correction['status'] != 'started' or action['receipt'] is None))
                else:
                    _need(type(correction['bundle']) is dict)
        keys.add(action['idempotency_key'])
        actions.append(action)
        previous = digest(action)
    _need(len(actions) == row['action_count'] and len(actions) <= 24
          and row['ledger_digest'] == digest([digest(a) for a in actions]))
    _need(sum(a['kind'] == 'correction.apply' for a in actions) <= request['max_repairs'], 'CORRECTION_SLOT_MISMATCH')
    return dict(row), request, actions



def _prior_acknowledgements(store, connection, request, actions, current, session):
    """Original generation/reconcile acknowledgements, without Loop recursion.

    N2 correction begins directly after the initial owned generation failure.
    Other pre-correction action kinds have no qualification in this path.
    Later experiment actions do not change the correction's earlier provenance.
    """
    from .proposal_requests import ProposalRequests
    prior = [a for a in actions if a['sequence'] < current['sequence']]
    _need(len(prior) == 2 and [a['kind'] for a in prior] == ['proposal.create','proposal.reconcile'],
          'CORRECTION_LOOP_HISTORY_UNQUALIFIED')
    generated, reconciled = prior
    _need(_same(generated['request'],request['initial_proposal']))
    identity = 'proposal_' + digest(generated['request'])
    proposal = ProposalRequests(store,configs={})._load(connection,identity)
    _need(_same(proposal['request'],generated['request'])
          and generated['receipt'] == {'proposal_id':identity})
    matches = [r for r in session['reservations'] if (r['children'] or {}).get('proposal_id') == identity]
    _need(len(matches) == 1)
    reserved = matches[0]
    _need(reserved['action'] == 'generate' and reserved['effect_key'] == generated['request']['idempotency_key']
          and reserved['action_digest'] == digest(generated['request'])
          and reconciled['request'] == {'token':reserved['token']} and reserved['outcome'] is not None
          and reconciled['receipt'] == {'token':reserved['token'],'outcome_digest':digest(reserved['outcome'])})
    ResearchSessions(store)._children(connection,session,reserved,reserved['children'])
    ResearchSessions(store)._verify_outcome(connection,reserved,reserved['outcome'])


def _action_binding(store, connection, *, loop_request, action):
    row, request, actions = _loop_record(connection, action['loop_id'])
    _need(_same(request, loop_request))
    matches = [a for a in actions if a['sequence'] == action['sequence']]
    _need(len(matches) == 1 and _same(matches[0], action))
    current = matches[0]
    _need(current['kind'] == 'correction.apply' and type(current.get('correction')) is dict
          and set(current['correction']) == CORRECTION_KEYS)
    intent, correction = current['request'], current['correction']
    _need(type(intent) is dict and set(intent) == INTENT_KEYS)
    _need(type(intent['schema_version']) is int and intent['schema_version'] == 1
          and type(intent['sequence']) is int and intent['sequence'] == current['sequence']
          and intent['loop_id'] == current['loop_id'] and intent['effect_key'] == current['effect_key']
          and intent['session_id'] == row['session_id'] and intent['method'] == 'deterministic'
          and intent['policy_id'] == request['correction_policy_id'] and intent['policy_digest'] == request['correction_policy_digest']
          and correction['intent_digest'] == digest(intent))
    resolve_policy(intent['policy_id'], intent['policy_digest'])
    _need(correction['status'] in {'planned','started','completed','blocked','unknown'})
    sessions = ResearchSessions(store)
    session = sessions._load(connection, row['session_id'])
    reservation = sessions._reservation(session, correction['reservation_token'])
    _need(reservation['action'] == 'read' and reservation['action_digest'] == digest(intent)
          and reservation['effect_key'] == 'loop-correction-read-' + digest({'loop_id': intent['loop_id'], 'sequence': intent['sequence']})
          and reservation['source_digest'] == intent['source_digest']
          and reservation['children'] is None and reservation['deadline_at'] == intent['deadline_at'] == session['row']['deadline_at']
          and reservation['charges'] == {'provider_dispatches': 0, 'tool_actions': 1, 'experiments': 0, 'failures': 0})
    _prior_acknowledgements(store, connection, request, actions, current, session)
    return row, request, current, session, reservation


def _observation(store, connection, intent, *, require_current):
    observed = observe_rejected_parent(store, connection, session_id=intent['session_id'],
        proposal_id=intent['parent_proposal_id'], attempt_id=intent['parent_attempt_id'],
        response_digest=intent['parent_response_digest'], candidate_id=intent['candidate_id'],
        require_current=require_current, require_reconciled=True)
    for key in ('source_digest','capability_digest','diagnostics_digest','parent_binding_digest'):
        actual = observed['parent_request']['expected_source_digest'] if key == 'source_digest' else observed[key]
        _need(intent[key] == actual)
    return observed


def execute_correction(store, connection, *, loop_request, action, created_at):
    """One pass after original caller lock and committed started CAS; no writes."""
    try:
        _, request, actual, session, reservation = _action_binding(store, connection, loop_request=loop_request, action=action)
        correction, intent = actual['correction'], actual['request']
        _need(correction['status'] == 'started' and correction['bundle'] is None
              and actual['receipt'] is None and correction['started_at'] is not None
              and reservation['status'] == 'reserved' and reservation['outcome'] is None, 'CORRECTION_NOT_STARTED')
        sessions = ResearchSessions(store)
        sessions._open(session)
        _need(session['state']['usage']['failures'] < session['request']['budget']['max_failures'], 'SESSION_BUDGET_EXCEEDED')
        _need(_timestamp(correction['started_at']) <= _timestamp(created_at) < _timestamp(intent['deadline_at']), 'SESSION_EXPIRED')
        observed = _observation(store, connection, intent, require_current=True)
        # Exactly one planning pass. No classifier or speculative normalization.
        corrected = correct_draft(observed['draft'], candidate_id=intent['candidate_id'],
            available_fields=observed['actual_fields'], policy_id=intent['policy_id'], digest=intent['policy_digest'])
        if corrected is None:
            state, error, result = 'blocked', {'code':'CORRECTION_UNSUPPORTED','message':'No equivalent complete legal implementation under the frozen policy.'}, {}
            plan, task = None, None
        else:
            plan = CorrectionPlan.model_validate(corrected['plan']).model_dump()
            state, error, result = validate_research_draft(store, source=observed['source_snapshot'], draft=corrected['draft'])
            task = result.get('task') if state == 'validated' else None
        validation = {'state':state,'error':error,'result':result}
        accepted = plan['accepted_expression'] if task is not None else None
        selected = observed['draft']['candidates'][0]
        receipt = {'schema_version':1,'loop_id':actual['loop_id'],'session_id':intent['session_id'],
            'candidate_id':intent['candidate_id'],'intent_digest':correction['intent_digest'],
            'parent_binding_digest':intent['parent_binding_digest'],'diagnostics_digest':intent['diagnostics_digest'],
            'source_digest':intent['source_digest'],'policy_id':intent['policy_id'],'policy_digest':intent['policy_digest'],
            'submitted_expression':selected['expression'],'accepted_expression':accepted,
            'expression_before_digest':digest(selected['expression']),'expression_after_digest':digest(accepted) if accepted is not None else None,
            'changed_paths':plan['changed_paths'] if plan else [],'rule_ids':plan['rule_ids'] if plan else [],
            'method':'deterministic','validation_digest':digest(validation),'accepted_task_digest':digest(task) if task is not None else None,
            'sequence':actual['sequence'],'created_at':created_at,'status':'completed' if task is not None else 'blocked',
            'provider_association':None}
        receipt['receipt_digest'] = digest(receipt)
        receipt = CorrectionReceipt.model_validate(receipt).model_dump()
        # Recheck actual source/parent/session safety after the domain tool read.
        _need(_same(observed, _observation(store, connection, intent, require_current=True)), 'SOURCE_CHANGED')
        sessions._open(sessions._load(connection, intent['session_id']))
        bundle = {'schema_version':1,'loop_id':actual['loop_id'],'sequence':actual['sequence'],
            'intent_digest':correction['intent_digest'],'receipt':receipt,'plan':plan,'validation':validation,'task':task,
            'source_snapshot':observed['source_snapshot'],'parent_binding':observed['parent_binding']}
        bundle['bundle_digest'] = digest(bundle)
        return bundle
    except SessionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise CorrectionExecutionError() from None


def verify_correction_bundle(store, connection, *, loop_request, action, require_current=False):
    """Read original DB-bound terminal provenance; never plan, classify or rerule."""
    try:
        _, request, actual, session, reservation = _action_binding(store, connection, loop_request=loop_request, action=action)
        correction, intent = actual['correction'], actual['request']
        _need(correction['status'] in {'completed','blocked'} and type(correction['bundle']) is dict, 'CORRECTION_NOT_TERMINAL')
        bundle = correction['bundle']
        _need(set(bundle) == BUNDLE_KEYS and type(bundle['schema_version']) is int and bundle['schema_version'] == 1
              and bundle['bundle_digest'] == digest({k:v for k,v in bundle.items() if k != 'bundle_digest'})
              and bundle['loop_id'] == actual['loop_id'] and bundle['sequence'] == actual['sequence']
              and bundle['intent_digest'] == correction['intent_digest'])
        receipt = CorrectionReceipt.model_validate(bundle['receipt']).model_dump()
        _need(_same(receipt, bundle['receipt']) and receipt['status'] == correction['status']
              and receipt['loop_id'] == actual['loop_id'] and receipt['sequence'] == actual['sequence']
              and receipt['session_id'] == intent['session_id'] and receipt['candidate_id'] == intent['candidate_id']
              and receipt['intent_digest'] == correction['intent_digest'] and receipt['method'] == 'deterministic'
              and receipt['policy_id'] == intent['policy_id'] and receipt['policy_digest'] == intent['policy_digest']
              and receipt['source_digest'] == intent['source_digest'] and receipt['parent_binding_digest'] == intent['parent_binding_digest']
              and receipt['diagnostics_digest'] == intent['diagnostics_digest'])
        _need(_timestamp(correction['started_at']) <= _timestamp(receipt['created_at']) < _timestamp(intent['deadline_at']))
        observed = _observation(store, connection, intent, require_current=require_current)
        _need(_same(bundle['source_snapshot'], observed['source_snapshot']) and _same(bundle['parent_binding'], observed['parent_binding'])
              and receipt['submitted_expression'] == observed['draft']['candidates'][0]['expression'])
        validation = bundle['validation']
        _need(type(validation) is dict and set(validation) == {'state','error','result'}
              and receipt['validation_digest'] == digest(validation) and type(validation['result']) is dict)
        plan = CorrectionPlan.model_validate(bundle['plan']).model_dump() if bundle['plan'] is not None else None
        if plan is not None:
            _need(_same(plan, bundle['plan']) and plan['policy_id'] == intent['policy_id'] and plan['policy_digest'] == intent['policy_digest']
                  and plan['submitted_expression'] == receipt['submitted_expression']
                  and plan['available_fields_digest'] == digest(observed['actual_fields'])
                  and plan['changed_paths'] == receipt['changed_paths'] and plan['rule_ids'] == receipt['rule_ids'])
            # Static acceptance tool verification, not a rule/normalization pass.
            _need(validate_expression(plan['accepted_expression'], set(observed['actual_fields'])) == plan['validation'])
        if receipt['status'] == 'completed':
            _need(plan is not None and validation['state'] == 'validated' and validation['error'] is None
                  and bundle['task'] is not None and validation['result'].get('task') == bundle['task']
                  and receipt['accepted_expression'] == plan['accepted_expression']
                  and receipt['accepted_task_digest'] == digest(bundle['task']))
            changed = deepcopy(observed['draft'])
            changed['candidates'][0]['expression'] = receipt['accepted_expression']
            source = observed['source_snapshot']
            expected_task = store._task({**changed, 'evaluation':source['revision']['task']['evaluation'],
                'budget':source['revision']['task']['budget']}, source['research']['title'], source['research']['dataset_id'])
            _need(_same(expected_task, bundle['task']), 'CORRECTION_TASK_DRIFT')
        else:
            _need(bundle['task'] is None and validation['state'] == 'blocked' and type(validation['error']) is dict
                  and validation['error'].get('code') and validation['result'].get('task') is None)
        if reservation['outcome'] is not None:
            expected = _terminal_outcome(receipt)
            _need(reservation['status'] == 'finished' and all(reservation['outcome'][k] == v for k,v in expected.items()), 'CORRECTION_OUTCOME_DRIFT')
        _need(_same(observed, _observation(store, connection, intent, require_current=require_current)), 'SOURCE_CHANGED')
        return deepcopy(bundle)
    except SessionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise CorrectionExecutionError() from None


def _terminal_outcome(receipt):
    return {'status':'awaiting_handoff' if receipt['status'] == 'completed' else 'failed',
            'code':'CORRECTION_READY' if receipt['status'] == 'completed' else 'CORRECTION_BLOCKED',
            'result_digest':receipt['receipt_digest']}


def prepare_corrected_experiment_source(store, connection, *, candidate_source, require_current=True):
    """Return recorded accepted Task, never a client Task or fake ready proposal."""
    try:
        _need(type(candidate_source) is dict and set(candidate_source) == SOURCE_KEYS
              and candidate_source['kind'] == 'host_corrected_rejected_candidate', 'CORRECTION_SOURCE_INVALID')
        _, loop_request, actions = _loop_record(connection, candidate_source['loop_id'])
        actions = [a for a in actions if a['sequence'] == candidate_source['correction_action_sequence']]
        _need(len(actions) == 1)
        action = actions[0]
        bundle = verify_correction_bundle(store, connection, loop_request=loop_request, action=action, require_current=require_current)
        receipt, intent = bundle['receipt'], action['request']
        _need(receipt['status'] == 'completed' and candidate_source == {
            'kind':'host_corrected_rejected_candidate','loop_id':action['loop_id'],'correction_action_sequence':action['sequence'],
            'intent_digest':action['correction']['intent_digest'],'correction_receipt_digest':receipt['receipt_digest'],
            'parent_proposal_id':intent['parent_proposal_id'],'parent_attempt_id':intent['parent_attempt_id'],
            'parent_response_digest':intent['parent_response_digest'],'accepted_task_digest':receipt['accepted_task_digest']})
        session = ResearchSessions(store)._load(connection, intent['session_id'])
        reservation = ResearchSessions(store)._reservation(session, action['correction']['reservation_token'])
        _need(reservation['status'] == 'finished' and reservation['outcome'] is not None
              and all(reservation['outcome'][k] == v for k,v in _terminal_outcome(receipt).items()), 'CORRECTION_NOT_RECONCILED')
        observed = _observation(store, connection, intent, require_current=require_current)
        source, prior = observed['source_snapshot'], observed['parent_request']
        changed = deepcopy(observed['draft'])
        changed['candidates'][0]['expression'] = receipt['accepted_expression']
        plan = {'schema_version':2,'candidate_source':deepcopy(candidate_source),'session_id':intent['session_id'],
            'proposal_id':intent['parent_proposal_id'],'attempt_id':intent['parent_attempt_id'],'parent_response_digest':intent['parent_response_digest'],
            'request_digest':digest(prior),'context_digest':observed['context_digest'],'source_digest':intent['source_digest'],
            'source_kind':'research_revision','source_request':prior['source'],'source_snapshot':source,
            'destination':{'kind':'existing_research','research_id':prior['source']['research_id'],'base_revision_id':prior['source']['base_revision_id']},
            'bootstrap_operation':None,'title':source['research']['title'],'paper_id':source['research']['paper_id'],'dataset_id':source['research']['dataset_id'],
            'task':bundle['task'],'task_digest':digest(bundle['task']),'submitted_draft_digest':digest(observed['draft']),
            'corrected_draft_digest':digest(changed),'candidate_checks':bundle['validation']['result']['candidate_checks'],'material':None,
            'correction_receipt_digest':receipt['receipt_digest'],'correction_bundle_digest':bundle['bundle_digest']}
        return {**plan,'plan_digest':digest(plan)}
    except SessionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise CorrectionExecutionError(code='CORRECTION_SOURCE_INVALID') from None


def guard_correction_outcome(store, connection, *, session_id, reservation, outcome):
    """True only for an original DB-owned correction read token; no recursion."""
    # The read effect has a reserved DB-owned namespace, separate from the
    # generic loop action effect. Losing its owner cannot turn it into a manual
    # read, including during replay of an already-finished original token.
    owned_namespace = (type(reservation.get('effect_key')) is str
                       and reservation['effect_key'].startswith('loop-correction-read-'))
    try:
        row = connection.execute('SELECT id FROM research_loops WHERE session_id=?', (session_id,)).fetchone()
        if row is None:
            _need(not owned_namespace, 'CORRECTION_OWNER_MISSING')
            return False
        raw = connection.execute('SELECT payload FROM research_loops WHERE id=?', (row['id'],)).fetchone()
        if _parse(raw['payload'], MAX_BYTES).get('schema_version', 1) != 2:
            _need(not owned_namespace, 'CORRECTION_OWNER_MISSING')
            return False
        _, request, actions = _loop_record(connection, row['id'])
        matches = [a for a in actions if a['kind'] == 'correction.apply' and
            (a.get('correction', {}).get('reservation_token') == reservation['token']
             or 'loop-correction-read-' + digest({'loop_id': a['loop_id'], 'sequence': a['sequence']}) == reservation['effect_key'])]
        if not matches:
            _need(not owned_namespace, 'CORRECTION_OWNER_MISSING')
            return False
        _need(len(matches) == 1)
        action = matches[0]
        _, _, actual, session, original = _action_binding(store, connection, loop_request=request, action=action)
        _need(_same(original, reservation))
        correction = actual['correction']
        if correction['status'] in {'completed','blocked'}:
            bundle = verify_correction_bundle(store, connection, loop_request=request, action=actual, require_current=False)
            expected = _terminal_outcome(bundle['receipt'])
        else:
            _need(correction['status'] in {'started','unknown'} and correction['bundle'] is None, 'CORRECTION_NOT_TERMINAL')
            expected = {'status':'interrupted','code':'OUTCOME_UNKNOWN','result_digest':None}
        _need(all(outcome.get(k) == v for k,v in expected.items()), 'CORRECTION_OUTCOME_DRIFT')
        return True
    except SessionError:
        raise
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise CorrectionExecutionError() from None
