"""Stable, read-only observations of completed research-job experiments.

Completion freezes a public run snapshot. Recheck the underlying engine files
and exact attempt before projecting that snapshot; later human reviews are not
part of this observation. File integrity is not independent numerical validation.
"""
from contextlib import closing
from copy import deepcopy
import re
from typing import Literal

from pydantic import Field

from ..author_panel_schema import AuthorModel
from ..storage import digest, json_text
from .db import connect
from .research_jobs import ResearchJobs, MAX_BODY_BYTES, _parse
from .research_jobs_schema import Origin
from .service import ServiceError


class ExperimentResultError(ServiceError):
    def __init__(self, message, *, code='RESULT_INTEGRITY', status=409):
        super().__init__(message, status)
        self.code = code
        self.retryable = False


class ValidationMetrics(AuthorModel):
    mean_rank_ic: float | None
    rank_ic_days: int = Field(ge=0)
    mean_gross_return: float | None
    sum_gross_return: float | None
    evaluated_days: int = Field(ge=0)
    eligible_days: int = Field(ge=0)
    skipped_days: int = Field(ge=0)
    purged_days: int = Field(ge=0)
    finite_factor_observations: int = Field(ge=0)
    possible_factor_observations: int = Field(ge=0)
    factor_coverage: float = Field(ge=0, le=1)


class ResultEvidence(AuthorModel):
    id: str
    page: int = Field(ge=1)
    quote: str
    source_digest: str


class IndependentVerification(AuthorModel):
    status: Literal['not_run']
    reason: str


class ExperimentCandidateResult(AuthorModel):
    id: str
    hypothesis_id: str
    submitted_expression: str
    accepted_expression: str
    executed_expression: str | None
    origin: Origin
    changes: list[str]
    evidence_ids: list[str]
    candidate_digest: str
    tool_result_digest: str | None
    execution_status: Literal['evaluated', 'not_evaluable', 'rejected', 'blocked', 'budget_stopped']
    reason: str | None
    metrics: ValidationMetrics | None
    limitations: list[str]
    independent_verification: IndependentVerification


class ExperimentResult(AuthorModel):
    schema_version: Literal[1]
    job_id: str
    job_digest: str
    source_digest: str
    completion_step_id: str
    completion_step_digest: str
    completed_at: str
    frozen_result_digest: str
    research_id: str
    revision_id: str
    task_digest: str
    paper_id: str
    paper_sha256: str
    dataset_id: str
    dataset_digest: str
    run_id: str
    attempt_id: str
    state_digest: str
    evaluation_period: Literal['validation']
    integrity_verified: Literal[True]
    semantic_fidelity: Literal['unverified']
    evidence: list[ResultEvidence]
    candidates: list[ExperimentCandidateResult]
    limitations: list[str]
    public_projection_redacted: bool
    view_digest: str


_PATH = re.compile(r'''(?<![\w:/])(?:[A-Za-z]:[\\/][^\s"<>]+|/(?:Users|private|tmp|var|home|Applications|Volumes|opt|usr|etc|bin|sbin)(?:/[^\s"'<>]+)+)''')
_URL = re.compile(r'''https?://[^\s"'<>]+''', re.IGNORECASE)
_CREDENTIAL = re.compile(r'(?i)(?:sk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9_.-]+)')


def _public(store, value):
    if isinstance(value, list):
        return [_public(store, item) for item in value]
    if isinstance(value, dict):
        return {key: _public(store, item) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    text = _CREDENTIAL.sub('[credential]', value)
    parts, position = [], 0
    for match in _URL.finditer(text):
        parts.extend((_PATH.sub('[local-path]', store._public(text[position:match.start()])), match.group()))
        position = match.end()
    parts.append(_PATH.sub('[local-path]', store._public(text[position:])))
    return ''.join(parts)


def _frozen_state(store, state):
    value = deepcopy(state)
    value['task'] = {key: item for key, item in value['task'].items()
                     if key not in {'paper', 'paper_pdf', 'data', 'data_metadata'}}
    for candidate in value['candidates']:
        candidate.get('result', {}).pop('daily', None)
    return store._public(value)


def _require(condition):
    if not condition:
        raise ExperimentResultError('Completed experiment no longer matches its frozen sources or actual output')


def build_experiment_result(store, job_id):
    """Read one frozen job result and verify its existing output; never execute it."""
    try:
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            jobs = ResearchJobs(store)
            job = jobs._load(connection, job_id)
            if job['state'] != 'completed':
                raise ExperimentResultError('A completed research job is required', code='RESULT_NOT_READY')
            steps = [step for step in job['steps'] if step['action'] == 'complete'
                     and step['status'] == 'completed' and step['next_state'] == 'completed' and step['error'] is None]
            _require(len(steps) == 1 and steps[0] == job['steps'][-1])
            step = steps[0]
            receipt = _parse(step['output_json'], MAX_BODY_BYTES)
            frozen = _parse(receipt['result_json'], MAX_BODY_BYTES // 2)
            _require(receipt['result_json'] == job['result_json'] and digest(frozen) == receipt['result_digest']
                     and receipt['run_id'] == job['run_id'] and frozen['verification']['verified'] is True)
            initial = store._one(connection, 'runs', job['run_id'])
            rows = store._review_rows(connection, initial['id'], initial['attempt_id'])
            files = store._review_files(initial['id'], initial['attempt_id'])
            run, state = store._verified_state(connection, job['run_id'])
            _require(run['status'] == 'completed' and state['status'] == 'completed'
                     and run['research_id'] == job['research_id'] and run['revision_id'] == job['revision_id']
                     and run['mode'] == 'normalized_fixed')
            for key, value in store._public(store._run_public(run)).items():
                _require(frozen[key] == value)
            _require(frozen['state'] == _frozen_state(store, state))
            state_digest = digest(state)
            _require(frozen['regression_target'] == {'attempt_id': run['attempt_id'], 'result_digest': state_digest})
            targets = [{'candidate_id': item['id'], 'attempt_id': run['attempt_id'], 'result_digest': digest(item)}
                       for item in state['candidates']]
            _require(frozen['review_targets'] == targets)
            attempt = rows['attempt']
            _require(attempt['run_id'] == run['id'] and attempt['status'] == 'completed'
                     and attempt['state_digest'] == state_digest)
            saved_attempt = next(item for item in frozen['attempts'] if item['id'] == run['attempt_id'])
            _require(saved_attempt == store._public({key: attempt[key] for key in
                     ('id', 'number', 'worker_id', 'status', 'started_at', 'finished_at', 'error', 'state_digest')}))
            task = _parse(rows['revision']['task'], MAX_BODY_BYTES)
            _require(digest(task) == rows['revision']['digest'] and task == jobs._validated_task(job))
            source_candidates = {item['id']: item for item in task['candidates']}
            submitted = {item['id']: item for item in job['draft']['candidates']}
            hypotheses = {item['id']: item for item in task['hypotheses']}
            _require(len(source_candidates) == len(state['candidates'])
                     and set(source_candidates) == {item['id'] for item in state['candidates']})
            candidates = []
            for item in state['candidates']:
                source = source_candidates[item['id']]
                _require(all(item[key] == source[key] for key in ('id', 'hypothesis_id', 'expression', 'origin', 'changes')))
                result = item.get('result')
                if result is not None:
                    _require(result['split'] == 'validation' and result['status'] == item['status']
                             and result['expression'] == item['attempts'][-1]['expression'])
                    metrics = ValidationMetrics.model_validate(result['metrics']).model_dump()
                else:
                    _require(item['status'] not in {'evaluated', 'not_evaluable'})
                    metrics = None
                candidates.append({
                    'id': item['id'], 'hypothesis_id': source['hypothesis_id'],
                    'submitted_expression': submitted[item['id']]['expression'],
                    'accepted_expression': source['expression'], 'executed_expression': result['expression'] if result else None,
                    'origin': source['origin'], 'changes': source['changes'],
                    'evidence_ids': hypotheses[source['hypothesis_id']]['evidence_ids'],
                    'candidate_digest': digest(item), 'tool_result_digest': digest(result) if result else None,
                    'execution_status': item['status'], 'reason': result['reason'] if result else item.get('reason'),
                    'metrics': metrics, 'limitations': result['limitations'] if result else [],
                    'independent_verification': {'status': 'not_run',
                        'reason': 'The original engine result has no independent numerical verification receipt. Artifact integrity does not establish numerical or scientific correctness.'}})
            _require(any(item['execution_status'] == 'evaluated' for item in candidates))
            original = {
                'schema_version': 1, 'job_id': job['id'], 'job_digest': job['digest'], 'source_digest': job['source_digest'],
                'completion_step_id': step['id'], 'completion_step_digest': digest(step), 'completed_at': step['finished_at'],
                'frozen_result_digest': receipt['result_digest'], 'research_id': run['research_id'],
                'revision_id': run['revision_id'], 'task_digest': rows['revision']['digest'],
                'paper_id': rows['paper']['id'], 'paper_sha256': rows['paper']['sha256'],
                'dataset_id': rows['dataset']['id'], 'dataset_digest': rows['dataset']['sha256'],
                'run_id': run['id'], 'attempt_id': run['attempt_id'], 'state_digest': state_digest,
                'evaluation_period': 'validation', 'integrity_verified': True, 'semantic_fidelity': 'unverified',
                'evidence': [{**item, 'source_digest': digest(item)} for item in task['evidence']], 'candidates': candidates,
                'limitations': [
                    'This is a read-only observation of the frozen completion; later human reviews do not change it.',
                    'Only validation metrics are exposed. Reserved test periods, raw rows and model-answer oracles are omitted.',
                    'Session limits count top-level actions and experiments; child jobs separately bound steps and engine execution.',
                    'Hash and artifact verification do not establish paper fidelity, causality or investment performance.',
                    'Source digests identify original local records; view_digest identifies this potentially redacted public projection.']}
            public = _public(store, original)
            public['public_projection_redacted'] = public != original
            public['view_digest'] = digest(public)
            value = ExperimentResult.model_validate(public).model_dump()
            # End the original read snapshot, then compare immutable source rows
            # and file inventory in a fresh snapshot. Reviews are intentionally
            # absent from this fence; changing a run/attempt cannot pass it.
            connection.rollback()
            connection.execute('BEGIN')
            store._assert_review_snapshot(connection, rows, files)
            current = jobs._load(connection, job_id)
            _require(current['digest'] == job['digest'] and current['steps'] == job['steps'])
            return value
    except ExperimentResultError:
        raise
    except ServiceError as exc:
        raise ExperimentResultError('Completed experiment source failed verification',
                                    code='RESULT_INTEGRITY', status=exc.status) from exc
    except (ValueError, TypeError, KeyError, StopIteration, OSError) as exc:
        raise ExperimentResultError('Completed experiment has invalid or inconsistent saved result data') from exc


def markdown_result(result):
    """Serialize the closed, already verified observation without new statistics."""
    value = ExperimentResult.model_validate(result).model_dump()
    _require(value['view_digest'] == digest({key: item for key, item in value.items() if key != 'view_digest'}))

    def block(text, language=''):
        fence = '`' * max(3, 1 + max((len(match.group()) for match in re.finditer(r'`+', text)), default=0))
        return fence + language + '\n' + text.rstrip() + '\n' + fence

    def inline(text):
        return (str(text).replace('\\', '\\\\').replace('`', '\\`').replace('|', '\\|')
                .replace('<', '&lt;').replace('>', '&gt;').replace('\n', ' '))

    lines = ['# Completed research experiment', '',
        'These numbers come from the original validation-only computation. This read does not run another experiment.', '',
        'Saved artifacts and identities were verified when this observation was built. Paper fidelity and independent numerical verification remain unverified.', '',
        '## Provenance', '', '| Identity | Value |', '| --- | --- |']
    for field in ('completed_at', 'job_id', 'research_id', 'revision_id', 'paper_id', 'dataset_id',
                  'run_id', 'attempt_id', 'completion_step_id'):
        lines.append(f'| {field} | {inline(value[field])} |')
    lines.extend(['', '## Paper evidence', ''])
    for evidence in value['evidence']:
        lines.extend([f"Evidence {inline(evidence['id'])}; PDF page {evidence['page']}.", '',
                      block(evidence['quote']), '', f"Original evidence digest: {evidence['source_digest']}", ''])
    lines.extend(['## Candidate results', ''])
    for candidate in value['candidates']:
        lines.extend([f"### {inline(candidate['id'])}", '',
            f"Status: {candidate['execution_status']}; origin: {candidate['origin']}; hypothesis: {inline(candidate['hypothesis_id'])}.", '',
            'Evidence IDs: ' + ', '.join(inline(identity) for identity in candidate['evidence_ids']) + '.', ''])
        for label, field in [('Submitted expression', 'submitted_expression'), ('Accepted expression', 'accepted_expression'),
                             ('Executed expression', 'executed_expression')]:
            lines.extend([label + ':', '', block(candidate[field]) if candidate[field] is not None else 'No executed expression was recorded.', ''])
        if candidate['changes']:
            lines.extend(['Declared changes:', '', block('\n'.join(candidate['changes'])), ''])
        if candidate['reason'] is not None:
            lines.extend(['Tool reason:', '', block(candidate['reason']), ''])
        if candidate['metrics'] is None:
            lines.extend(['No tool metrics were produced for this candidate. No default numbers are supplied.', ''])
        else:
            lines.extend(['| Validation tool metric | Original value |', '| --- | --- |'])
            for name, number in candidate['metrics'].items():
                lines.append(f'| {name} | {json_text(number).strip()} |')
            lines.extend(['', 'Null means the tool did not produce a defined value; it is not zero.', ''])
        lines.extend([f"Independent numerical verification: {candidate['independent_verification']['status']}.", '',
                      candidate['independent_verification']['reason'], '',
                      f"Original candidate digest: {candidate['candidate_digest']}", '',
                      f"Original tool result digest: {candidate['tool_result_digest'] or 'not available'}", ''])
        for limitation in candidate['limitations']:
            lines.extend([block(limitation), ''])
    lines.extend(['## Verification references and limits', '', '| Digest | Value |', '| --- | --- |'])
    for field in ('job_digest', 'source_digest', 'task_digest', 'paper_sha256', 'dataset_digest',
                  'completion_step_digest', 'frozen_result_digest', 'state_digest', 'view_digest'):
        lines.append(f'| {field} | {value[field]} |')
    lines.extend(['', 'Public text redacted: ' + str(value['public_projection_redacted']).lower() + '.', ''])
    for limitation in value['limitations']:
        lines.extend([block(limitation), ''])
    lines.extend(['## Full structured observation', '', block(json_text(value), 'json'), ''])
    return '\n'.join(lines)
