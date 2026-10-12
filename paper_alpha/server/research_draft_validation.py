"""Shared draft checks; reads existing source data without creating business effects."""
import json

from ..contracts import Task
from ..evidence import match_evidence, verify_paper
from ..evaluation import FIELDS
from ..expressions import validate_expression, repair_expression, ExpressionError
from ..workflow import verify_candidate_origin


def validate_research_draft(store, *, source, draft, allow_partial_execution=False,
                            method_status='resolved', unresolved_rules=None):
    """Return the original ResearchJobs validation tuple without writing a job.

    The source is the existing verified ResearchJobs source snapshot. Callers
    remain responsible for checking that snapshot before and after this read.
    Keep the legacy error type and messages to preserve saved job contracts.
    """
    from .research_jobs import JobServiceError, _error
    request = {'draft': draft, 'allow_partial_execution': allow_partial_execution,
               'method_status': method_status, 'unresolved_rules': unresolved_rules or []}
    if request['method_status'] != 'resolved' or request['unresolved_rules']:
        return 'blocked', _error('RULES_UNRESOLVED', 'Resolve declared method rules before executing this draft.'), {}
    base = source['revision']['task']
    task = store._task({**request['draft'], 'evaluation': base['evaluation'], 'budget': base['budget']},
                            source['research']['title'], source['research']['dataset_id'])
    from ..daily_task_v2 import CORE_KEYS, is_daily_task
    core = {k: task[k] for k in CORE_KEYS} if is_daily_task(task) else task
    parsed = Task.parse({**core, 'schema_version': 1, 'title': source['research']['title'],
                         'paper': 'paper.json', 'paper_pdf': 'paper.pdf', 'data': 'market.csv', 'data_metadata': 'metadata.json'})
    paper_row = store._fetch('papers', source['research']['paper_id'])
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
    if is_daily_task(task):
        from .daily_research_v2 import assert_daily_task
        daily_source = assert_daily_task(store, task, source['research']['dataset_id'])
        fields = {f['name'] for f in daily_source['catalog']['public_projection']['declared_capabilities']['fields']}
    else:
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
    metadata = json.loads(store._fetch('datasets', source['research']['dataset_id'])['metadata'])
    if is_daily_task(task):
        from .daily_research_v2 import inspect_daily_task
        preflight = inspect_daily_task(task, metadata)
    else:
        preflight = inspect_task(task, metadata)
    if any(item['status'] == 'blocked' for item in preflight['candidates']):
        return 'blocked', _error('PREFLIGHT_BLOCKED', 'Candidate has no usable validation rows; no window substitution performed.'), {'candidate_checks': checks, 'preflight': preflight}
    from .research_guard import ResearchGuard
    guard = ResearchGuard(store).check(source['research']['dataset_id'], task)
    if not guard['allowed']:
        return 'blocked', _error('TEMPORAL_GUARD', guard['reason']), {'candidate_checks': checks, 'guard': guard}
    return 'validated', None, {'task': task, 'candidate_checks': checks, 'preflight': preflight,
                               'attribution_scope': 'Exact formula and declarations checked; economic/prose semantic fidelity unverified.'}
