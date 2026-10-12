"""One bounded program over the original session, proposal and Native ledgers.

Planning/preflight/readback never construct a Store or dispatch a model. Run and
resume require the same frozen plan, exact original effect keys, and an explicit
host real-dispatch switch plus READY_MODEL receipt. There is no model retry,
material adoption, new final permission, direct CLI spawn, or second budget.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from ..storage import digest, json_text, atomic_json
from ..research_quality_v2_binding import read_bounded_json, _original_connection
from .provider_contract import digest as wire_digest
from .provider_loop_native_authority_schema import NativeCallSlot
from .provider_research_source_schema import ResearchSource
from .research_sessions_schema import SessionBudget
from .research_jobs_schema import JobBudget
from .service import Store, ServiceError

RUNNER_ID = 'research-completion-models-v1'
RUNNER_ID_V2 = 'research-completion-models-v2'
CALL_ORDER_V2 = ('N04', 'N05', 'Q01')
CALL_ORDER = ('N01', 'N02', 'N03', 'N04', 'N05', 'Q01', 'Q02', 'Q03', 'Q04', 'Q05', 'Q06')
OPERATIONS = ('generate_candidates', 'repair_candidate', 'decide_next_action',
              'generate_candidates', 'decide_next_action', 'extract_material',
              'generate_candidates', 'extract_material', 'generate_candidates',
              'extract_material', 'generate_candidates')
PARENTS = {'N02': 'N01', 'N03': 'N02', 'N05': 'N04',
           'Q02': 'Q01', 'Q04': 'Q03', 'Q06': 'Q05'}
REPO = Path(__file__).resolve().parents[2]


class ModelProgramError(ServiceError):
    def __init__(self, message, code='MODEL_PROGRAM_INVALID'):
        super().__init__(message, 409)
        self.code = code


def _need(value, message, code='MODEL_PROGRAM_INVALID'):
    if not value:
        raise ModelProgramError(message, code)


def _safe_path(path):
    path = Path(path).absolute()
    _need('..' not in path.parts and not any(x.is_symlink() for x in (path, *path.parents)),
          'Program paths may not contain symlinks or traversal')
    return path


class EvidencePin(AuthorModel):
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    required_true: list[str] = Field(default_factory=list, max_length=32)


class ProgramSession(AuthorModel):
    source: ResearchSource | None = None
    purpose: str = Field(min_length=1, max_length=4000)
    budget: SessionBudget


class ProgramCall(AuthorModel):
    spec: NativeCallSlot
    session_key: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    instruction: str = Field(max_length=4000)
    candidate_id: str = Field(default='candidate', min_length=1, max_length=64)
    gates: dict[str, bool]
    evidence_pins: list[EvidencePin] = Field(default_factory=list, max_length=32)
    missing_inputs: list[str] = Field(default_factory=list, max_length=32)


class ModelProgramPlan(AuthorModel):
    schema_version: int = 1
    runner_id: str = RUNNER_ID
    program_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    scope_plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    provider_config_id: str = Field(min_length=1, max_length=128)
    authorization: EvidencePin | None = None
    ready_model: EvidencePin | None = None
    code_pins: dict[str, str] = Field(default_factory=dict)
    max_seconds: int = Field(default=7200, ge=1, le=7200)
    proposal_seconds: int = Field(default=240, ge=1, le=240)
    experiment_budget: JobBudget = Field(default_factory=lambda: JobBudget(
        max_steps=10, max_failures=2, max_seconds=180))
    sessions: dict[str, ProgramSession]
    calls: list[ProgramCall] = Field(min_length=11, max_length=11)

    @model_validator(mode='after')
    def frozen_inventory(self):
        if self.schema_version != 1 or self.runner_id != RUNNER_ID:
            raise ValueError('Unsupported program version')
        if tuple(c.spec.call_id for c in self.calls) != CALL_ORDER:
            raise ValueError('All eleven planned slots must remain in their declared order')
        if tuple(c.spec.operation for c in self.calls) != OPERATIONS:
            raise ValueError('Program operation inventory differs')
        if len({c.spec.effect_key for c in self.calls}) != 11:
            raise ValueError('Permanent slot effect keys must be unique')
        for c in self.calls:
            if c.session_key not in self.sessions or not c.gates:
                raise ValueError('Every slot requires an explicit session and gate inventory')
            if c.spec.instruction_digest != digest(c.instruction):
                raise ValueError('Every instruction must match its frozen digest')
            if c.spec.operation in {'repair_candidate', 'decide_next_action'} and c.spec.target_from_call_id != PARENTS[c.spec.call_id]:
                raise ValueError('Repair/decision must name its predeclared exact parent')
            if c.spec.call_id in ('Q02', 'Q04', 'Q06') and c.spec.adopted_from_call_id != PARENTS[c.spec.call_id]:
                raise ValueError('Paper generation must name its original extraction slot')
            if c.spec.call_id.startswith('N') and c.spec.source_kind != 'research_revision':
                raise ValueError('Native qualification scenarios require maintained revisions')
            if c.spec.candidate_limit > (1 if c.spec.call_id.startswith('N') else 2):
                raise ValueError('Declared candidate bound exceeds the qualification scope')
        for key, session in self.sessions.items():
            count = sum(c.session_key == key for c in self.calls)
            if not count or session.budget.max_provider_dispatches > count or session.budget.max_seconds > min(1800, self.max_seconds):
                raise ValueError('Session exceeds its fixed call inventory or deadline')
            if session.budget.max_experiments > 1 or session.budget.max_failures > 2:
                raise ValueError('Qualification session exceeds experiment/failure bounds')
        for child in ('N02', 'N03', 'N05'):
            by_id = {c.spec.call_id: c for c in self.calls}
            if by_id[child].session_key != by_id[PARENTS[child]].session_key:
                raise ValueError('A target chain must keep its original session')
        if self.calls[0].session_key == self.calls[3].session_key:
            raise ValueError('Normal scenario needs its separately declared original session')
        return self


class ModelProgramPlanV2(AuthorModel):
    """Additive three-call qualification; never a shortened v1 permission."""
    schema_version: Literal[2] = 2
    runner_id: Literal['research-completion-models-v2'] = RUNNER_ID_V2
    program_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    scope_plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    provider_config_id: str = Field(min_length=1, max_length=128)
    authorization: EvidencePin | None = None
    ready_model: EvidencePin | None = None
    code_pins: dict[str, str] = Field(default_factory=dict)
    max_seconds: int = Field(default=7200, ge=1, le=7200)
    proposal_seconds: int = Field(default=240, ge=1, le=240)
    experiment_budget: JobBudget = Field(default_factory=lambda: JobBudget(
        max_steps=10, max_failures=2, max_seconds=180))
    sessions: dict[str, ProgramSession]
    calls: list[ProgramCall] = Field(min_length=3, max_length=3)

    @model_validator(mode='after')
    def frozen_inventory(self):
        if tuple(c.spec.call_id for c in self.calls) != CALL_ORDER_V2:
            raise ValueError('V2 requires exactly N04, N05, Q01 in that order')
        if tuple(c.spec.operation for c in self.calls) != (
                'generate_candidates', 'decide_next_action', 'extract_material'):
            raise ValueError('V2 operation inventory differs')
        if len({c.spec.effect_key for c in self.calls}) != 3:
            raise ValueError('Permanent slot effect keys must be unique')
        first, decision, extract = self.calls
        if first.session_key != decision.session_key or first.session_key == extract.session_key:
            raise ValueError('V2 keeps one normal chain and a separate extraction session')
        if set(self.sessions) != {first.session_key, extract.session_key}:
            raise ValueError('V2 cannot carry undeclared sessions')
        for call in self.calls:
            if (call.session_key not in self.sessions or not call.gates
                    or call.spec.instruction_digest != digest(call.instruction)
                    or call.spec.adopted_from_call_id is not None):
                raise ValueError('V2 requires frozen instruction, gates and session inventory')
            expected_kind = 'paper_selection' if call is extract else 'research_revision'
            if call.spec.source_kind != expected_kind or call.spec.candidate_limit > (2 if call is extract else 1):
                raise ValueError('V2 source or candidate scope differs')
            if call.spec.target_from_call_id != ('N04' if call is decision else None):
                raise ValueError('V2 decision must target its exact N04 parent')
        for key, session in self.sessions.items():
            count = sum(c.session_key == key for c in self.calls)
            if (session.budget.max_provider_dispatches > count
                    or session.budget.max_seconds > min(1800, self.max_seconds)
                    or session.budget.max_experiments > (1 if count == 2 else 0)
                    or session.budget.max_failures > 2):
                raise ValueError('V2 session exceeds its fixed qualification scope')
            expected_kind = 'research_revision' if key == first.session_key else 'paper_selection'
            if session.source is not None and session.source.kind != expected_kind:
                raise ValueError('V2 session source differs from its call kind')
        return self


def parse_program_plan(value):
    """Closed version dispatch; v1 validation and serialization stay unchanged."""
    if isinstance(value, (ModelProgramPlan, ModelProgramPlanV2)):
        version = value.schema_version
    elif isinstance(value, dict):
        version = value.get('schema_version', 1)
    else:
        raise ValueError('A model program must be a versioned object')
    if version == 1:
        return ModelProgramPlan.model_validate(value)
    if version == 2:
        return ModelProgramPlanV2.model_validate(value)
    raise ValueError('Unsupported model program version')


class ReadOnlyStore(Store):
    """Use existing read methods without initialization, migration or seeding."""
    def __init__(self, root):
        self.root = _safe_path(root)
        self.db_path = self.root / 'workbench.sqlite3'
        _need(self.db_path.is_file(), 'An existing initialized workspace is required')
        self._redacted_roots = [str(self.root)]

    def _read(self, sql, values=()):
        _, connection = _original_connection(self.root)
        try:
            return [dict(r) for r in connection.execute(sql, values).fetchall()]
        finally:
            connection.close()

    def _fetch(self, table, identity):
        _, connection = _original_connection(self.root)
        try:
            return self._one(connection, table, identity)
        finally:
            connection.close()


def open_existing_store(root):
    """Mutation-capable view; callers already own an initialized isolated Store."""
    view = ReadOnlyStore(root)
    store = object.__new__(Store)
    store.root, store.db_path = view.root, view.db_path
    store._redacted_roots = view._redacted_roots
    rows = view._read("SELECT value FROM settings WHERE key='schema_version'")
    _need(len(rows) == 1 and rows[0]['value'] == '27',
          'Runner requires the already initialized schema27; no migrations are applied')
    return store


def _pin(pin):
    value, actual = read_bounded_json(_safe_path(pin.path))
    _need(actual == pin.sha256, 'Frozen gate evidence bytes differ', 'MODEL_GATE_CHANGED')
    _need(type(value) is dict and all(value.get(key) is True for key in pin.required_true),
          'Required per-slot gate is not accepted', 'MODEL_GATE_BLOCKED')
    return value


def load_program_plan(path):
    value, _ = read_bounded_json(_safe_path(path))
    return parse_program_plan(value)


def program_plan(plan):
    plan = parse_program_plan(plan)
    return {'runner_id': plan.runner_id, 'program_id': plan.program_id,
        'plan_digest': digest(plan.model_dump()), 'planned_slots': len(plan.calls),
        'call_order': [c.spec.call_id for c in plan.calls], 'automatic_retries': 0, 'reset_cards': 0,
        'slot_reassignment_allowed': False, 'real_final_permission': False,
        'calls': [{'call_id': c.spec.call_id, 'operation': c.spec.operation,
                   'effect_key': c.spec.effect_key, 'session_key': c.session_key,
                   'gates': c.gates, 'missing_inputs': c.missing_inputs}
                  for c in plan.calls]}


def frozen_program_digest(plan):
    """Freeze the exact dispatch manifest without its later READY_MODEL pin."""
    value = parse_program_plan(plan).model_dump()
    value.pop('ready_model')
    return digest(value)


def _validate_target_metrics(raw, candidate):
    metrics = raw.get('metrics') or {}
    _need(raw.get('status') == candidate.get('execution_status') == 'evaluated' and
          raw.get('split') == 'validation' and candidate.get('tool_result_digest') == digest(raw) and
          metrics == candidate.get('metrics'),
          'Decision requires the exact evaluated original tool result', 'RESULT_NOT_EVALUATED')
    counts = ('evaluated_days', 'eligible_days', 'skipped_days', 'purged_days',
              'rank_ic_days', 'finite_factor_observations', 'possible_factor_observations')
    _need(all(type(metrics.get(k)) is int and metrics[k] >= 0 for k in counts) and
          metrics['evaluated_days'] > 0 and
          metrics['eligible_days'] == metrics['evaluated_days'] + metrics['skipped_days'] and
          metrics['rank_ic_days'] <= metrics['evaluated_days'] and
          metrics['finite_factor_observations'] <= metrics['possible_factor_observations'] and
          metrics['possible_factor_observations'] > 0,
          'Actual validation counts are empty or inconsistent', 'RESULT_COUNTS_INVALID')
    for key in ('mean_gross_return', 'sum_gross_return', 'factor_coverage'):
        value = metrics.get(key)
        _need(type(value) in (float, int) and math.isfinite(value),
              'Actual validation metrics are not finite', 'RESULT_METRICS_INVALID')
    rank_ic = metrics.get('mean_rank_ic')
    _need((metrics['rank_ic_days'] == 0 and rank_ic is None) or
          (metrics['rank_ic_days'] > 0 and type(rank_ic) in (int, float) and math.isfinite(rank_ic)),
          'Actual rank IC count/value differs', 'RESULT_METRICS_INVALID')
    _need(0 <= metrics['factor_coverage'] <= 1 and math.isclose(metrics['factor_coverage'],
          metrics['finite_factor_observations'] / metrics['possible_factor_observations'], abs_tol=1e-12),
          'Actual factor coverage differs from original counts', 'RESULT_COUNTS_INVALID')
    daily = raw.get('daily')
    _need(type(daily) is list and
          sum(d.get('status') == 'evaluated' for d in daily) == metrics['evaluated_days'] and
          sum(d.get('status') == 'purged' for d in daily) == metrics['purged_days'] and
          len(daily) == metrics['eligible_days'] + metrics['purged_days'],
          'Daily original tool rows differ from aggregate counts', 'RESULT_COUNTS_INVALID')


def verify_decision_experiment(store, result, candidate_id):
    """Read the original registered-prefix output; never recompute any values."""
    from ..daily_workflow_v2 import verify_daily_run
    candidates = [c for c in result['candidates'] if c['id'] == candidate_id]
    _need(len(candidates) == 1, 'Actual result lacks the frozen candidate')
    revision = store._fetch('revisions', result['revision_id'])
    task = json.loads(revision['task'])
    _need((task.get('execution') or {}).get('kind') == 'registered_daily_development_v2',
          'A decision requires the registered daily prefix execution path', 'RESULT_SCOPE_INVALID')
    attempt = store._fetch('attempts', result['attempt_id'])
    output = _safe_path(attempt['output_dir'])
    verified = verify_daily_run(output, expected_task=task, require_current_code=True)
    raw, raw_sha = read_bounded_json(output / 'candidates' / candidate_id / 'attempt-1' / 'result.json')
    _validate_target_metrics(raw, candidates[0])
    _need(verified['valid'] is True and verified['status'] == 'completed' and
          verified['state_digest'] == result['state_digest'] == attempt['state_digest'] and
          verified['candidate_results'].get(candidate_id) == candidates[0]['tool_result_digest'] and
          verified['evaluation_scope'] == raw.get('evaluation_scope') == 'development_prefix_validation_only' and
          type(verified['final_numeric_rows_parsed']) is int and verified['final_numeric_rows_parsed'] == 0 and
          type(raw.get('final_numeric_rows_used')) is int and raw['final_numeric_rows_used'] == 0 and
          digest(raw['config']) == digest(task['evaluation']),
          'Decision result has no authenticated development-only scope', 'RESULT_SCOPE_INVALID')
    return {'run_id': result['run_id'], 'attempt_id': result['attempt_id'],
        'candidate_id': candidate_id, 'tool_result_digest': candidates[0]['tool_result_digest'],
        'tool_result_sha256': raw_sha, 'manifest_sha256': verified['manifest_sha256'],
        'evaluation_scope': verified['evaluation_scope'], 'final_numeric_rows_parsed': 0,
        'final_numeric_rows_used': 0, 'evaluated_days': raw['metrics']['evaluated_days']}


def _owned_resume_source(store, plan, registry, output, call):
    """Derive a private continuation proof from original read-only ledgers.

Only the original N03/N05 decision may use this proof. No output JSON, revision
ID or caller dictionary alone substitutes for the grant/session/effect chain.
    """
    from .provider_loop_native_authority import LoopNativeResearchAuthority
    from .provider_experiment_source import load_accepted_candidate
    from .provider_research_context import resolve_research_source
    from .research_sessions import ResearchSessions
    from .research_session_experiments import SessionExperiments, _source_current
    from .research_session_experiments_schema import ExperimentCreate
    from .research_jobs import ResearchJobs
    output = _safe_path(output)
    _need(call.spec.call_id in ('N03', 'N05') and call.spec.operation == 'decide_next_action',
          'Only a declared decision can continue an owned source', 'RESUME_SCOPE_INVALID')
    _need(read_bounded_json(output / 'run-plan.json')[0] == plan.model_dump(),
          'Resume must retain its original plan', 'RESUME_PLAN_CHANGED')
    saved_grant, _ = read_bounded_json(output / 'grant.json')
    parent_call = next(c for c in plan.calls if c.spec.call_id == call.spec.target_from_call_id)
    authority = LoopNativeResearchAuthority(store, registry)
    _, connection = _original_connection(store.root)
    try:
        grant = authority._grant(connection, saved_grant['id'])
        _need(grant == saved_grant and grant['request']['program_id'] == plan.program_id
              and grant['request']['plan_digest'] == digest(plan.model_dump())
              and grant['request']['slots'] == [c.spec.model_dump() for c in plan.calls],
              'Resume grant differs from the frozen program', 'RESUME_GRANT_CHANGED')
        authority._active(connection, grant)
        parent_slot = authority._slot(connection, grant['id'], parent_call.spec.effect_key)
        decision_slot = authority._slot(connection, grant['id'], call.spec.effect_key)
        _need(parent_slot['proposal_id'] is not None and parent_slot['attempt_id'] is not None,
              'Resume lacks its original accepted parent', 'RESUME_PARENT_MISSING')
        from .proposal_requests import ProposalRequests
        parent = ProposalRequests(store, configs={})._load(connection, parent_slot['proposal_id'])
        _need(parent['request']['session_id'] == parent_slot['session_id']
              and parent['accepted_attempt_id'] == parent_slot['attempt_id']
              and parent['request']['idempotency_key'] == parent_call.spec.effect_key,
              'Resume parent belongs to another original slot', 'RESUME_PARENT_CHANGED')
        packet = load_accepted_candidate(store, connection, parent['id'], parent['accepted_response_digest'])
        sessions = ResearchSessions(store)
        loaded = sessions._load(connection, parent_slot['session_id'])
        binding, bound_grant = authority._binding(connection, parent_slot['session_id'])
        root = loaded['request']['source']
        descriptor = plan.sessions[call.session_key].source
        _need(bound_grant == grant and call.session_key == parent_call.session_key
              and loaded['request']['idempotency_key'] == plan.program_id + '-session-' + call.session_key
              and decision_slot['session_id'] in (None, parent_slot['session_id'])
              and descriptor is not None and descriptor.kind == 'research_revision'
              and root['kind'] == 'research_revision'
              and root['research_id'] == descriptor.research_id
              and root['revision_id'] == descriptor.base_revision_id
              and root['source_digest'] == binding['root_source_digest'] == call.spec.root_source_digest
              == parent_call.spec.root_source_digest == parent['request']['expected_source_digest'],
              'Resume session/root differs from its frozen parent', 'RESUME_SESSION_CHANGED')
        sessions._open(loaded)
        request_path = output / 'experiments' / (parent['id'] + '.json')
        request = ExperimentCreate.model_validate(read_bounded_json(request_path)[0]).model_dump()
        experiment_key = plan.program_id + '-experiment-' + parent['id'][-16:]
        _need(request['idempotency_key'] == experiment_key
              and request['session_id'] == parent_slot['session_id']
              and request['proposal_id'] == parent['id']
              and request['accepted_response_digest'] == parent['accepted_response_digest']
              and request['budget'] == plan.experiment_budget.model_dump(),
              'Resume experiment request differs', 'RESUME_EXPERIMENT_CHANGED')
        row = connection.execute('SELECT id FROM research_session_experiments WHERE idempotency_key=?',
                                 (experiment_key,)).fetchone()
        _need(row is not None, 'Original experiment is missing', 'RESUME_EXPERIMENT_MISSING')
        experiments = SessionExperiments(store)
        intent = experiments._load(connection, row['id'])
        _need(intent['request'] == request and intent['plan']['source_request'] == descriptor.model_dump()
              and digest(intent['plan']['source_snapshot']) == root['source_digest']
              and intent['plan']['source_snapshot']['revision']['digest'] == root['revision_digest'],
              'Resume experiment has another frozen source', 'RESUME_EXPERIMENT_CHANGED')
        job_effect = experiments._effect(connection, intent, 'job')
        revision_effect = experiments._effect(connection, intent, 'revision')
        _need(job_effect is not None and revision_effect is not None,
              'Resume requires the anchored original commit', 'RESUME_COMMIT_MISSING')
        job = ResearchJobs(store)._load(connection, job_effect['response']['id'])
        commits = [s for s in job['steps'] if s['action'] == 'commit' and s['status'] == 'completed']
        _need(len(commits) == 1 and commits[0]['idempotency_key'] == experiment_key + '-commit'
              and json.loads(commits[0]['output_json'])['revision_id'] == revision_effect['response']['id']
              and revision_effect['response']['research_id'] == root['research_id'],
              'Resume commit has no completed original acknowledgement', 'RESUME_COMMIT_MISSING')
        _source_current(store, connection, intent)
        current = loaded['state']['current_source']
        _need(current['kind'] == 'research_revision' and current['research_id'] == root['research_id']
              and current['revision_id'] == revision_effect['response']['id']
              and current['revision_digest'] == revision_effect['response']['digest'],
              'Resume current revision differs from the owned commit', 'RESUME_SOURCE_CHANGED')
        advanced = [e for e in loaded['events'] if e['kind'] == 'source_advanced'
                    and e['operation_key'] == 'experiment-source-' + intent['row']['request_digest']]
        _need(len(advanced) == 1 and advanced[0]['operation_digest'] == digest(revision_effect)
              and advanced[0]['commit_receipt_digest'] == digest(revision_effect)
              and advanced[0]['state_after']['current_source'] == current,
              'Resume lacks the original source advancement event', 'RESUME_SOURCE_EVENT_MISSING')
        actual = resolve_research_source(store, {'kind': 'research_revision',
            'research_id': current['research_id'], 'base_revision_id': current['revision_id']}, connection=connection)
        original = intent['plan']['source_snapshot']
        excluded = {'research', 'revision', 'business_revision_binding'}
        _need({k: v for k, v in actual.items() if k not in excluded} ==
              {k: v for k, v in original.items() if k not in excluded}
              and {k: v for k, v in actual['research'].items() if k != 'latest_revision_id'} ==
              {k: v for k, v in original['research'].items() if k != 'latest_revision_id'}
              and digest(actual) == current['source_digest'],
              'Resume paper/data/registration or source digest changed', 'RESUME_SOURCE_CHANGED')
        base = store._revision(store._one(connection, 'revisions', root['revision_id']))
        _need(base == original['revision'], 'Original root revision changed', 'RESUME_SOURCE_CHANGED')
        attempt = next(a for a in parent['attempts'] if a['id'] == parent_slot['attempt_id'])
        if grant['config']['mode'] == 'codex_cli':
            authority._saved_transport(parent, attempt, connection=connection)
            ModelProgramRunner._native_observation(store, grant, parent)
        return actual, {'grant_id': grant['id'], 'grant_digest': digest(grant),
            'session_id': parent_slot['session_id'], 'session_ledger_digest': loaded['row']['ledger_digest'],
            'parent_proposal_id': parent['id'], 'parent_attempt_id': attempt['id'],
            'accepted_response_digest': packet['proposal']['accepted_response_digest'],
            'experiment_id': intent['id'], 'experiment_request_digest': intent['row']['request_digest'],
            'commit_effect_digest': digest(revision_effect), 'source_advanced_digest': digest(advanced[0]),
            'root_source_digest': root['source_digest'], 'current_source': deepcopy(current)}
    finally:
        connection.close()


def preflight_program(store_root, plan, registry, *, allow_real=False, resume_output=None):
    """Read-only static/source gates; backend compatibility remains unverified."""
    from .provider_loop_native_authority import _current_profile
    from .provider_research_context import resolve_research_source
    plan = parse_program_plan(plan)
    store = ReadOnlyStore(store_root)
    config = registry.ensure_dispatchable(plan.provider_config_id)
    profile = _current_profile(config)
    real = config.mode == 'codex_cli'
    if real:
        _need(allow_real, 'Real dispatch requires the explicit local host switch', 'REAL_DISPATCH_DISABLED')
        _need(plan.authorization is not None and plan.ready_model is not None and plan.code_pins,
              'Current authorization, source freeze and READY_MODEL are required', 'MODEL_NOT_FROZEN')
        auth, ready = _pin(plan.authorization), _pin(plan.ready_model)
        _need(auth.get('program_id') == plan.program_id and auth.get('project_model_call_cap') == len(plan.calls) and
              auth.get('model') == 'gpt-5.5' and auth.get('reasoning_effort') == 'low' and
              auth.get('cli_expected_version') == '0.145.0' and auth.get('automatic_retries') == 0 and
              auth.get('new_reset_cards_authorized') == 0 and auth.get('real_final_permission') is False,
              'User authorization does not cover this exact bounded program')
        _need(ready.get('status') == 'READY_MODEL' and ready.get('program_id') == plan.program_id and
              ready.get('scope_plan_digest') == plan.scope_plan_digest and
              ready.get('code_inventory_digest') == digest(plan.code_pins) and
              ready.get('program_spec_digest') == frozen_program_digest(plan),
              'The current source has not been frozen READY_MODEL', 'MODEL_NOT_FROZEN')
    for relative, expected in plan.code_pins.items():
        path = Path(relative)
        _need(not path.is_absolute() and '..' not in path.parts and re.fullmatch('[a-f0-9]{64}', expected),
              'Code inventory must use safe exact source paths and hashes')
        actual = hashlib.sha256(_safe_path(REPO / path).read_bytes()).hexdigest()
        _need(actual == expected, 'Frozen program source changed: ' + relative, 'MODEL_SOURCE_CHANGED')
    rows = []
    for call in plan.calls:
        missing = [name for name, yes in call.gates.items() if not yes] + call.missing_inputs
        continuation = None
        try:
            for evidence in call.evidence_pins:
                _pin(evidence)
            descriptor = plan.sessions[call.session_key].source
            if descriptor is None:
                missing.append('explicit_adopted_or_root_source_missing')
            elif not missing:
                try:
                    source = resolve_research_source(store, descriptor.model_dump())
                except (ValueError, OSError, KeyError, TypeError):
                    if resume_output is None or call.spec.call_id not in ('N03', 'N05'):
                        raise
                    source, continuation = _owned_resume_source(store, plan, registry, resume_output, call)
                if real and call.spec.call_id.startswith('N'):
                    _need((source['revision']['task'].get('execution') or {}).get('kind') ==
                          'registered_daily_development_v2',
                          'Real Native scenarios require a registered development-prefix source',
                          'MODEL_PREFIX_SOURCE_REQUIRED')
                if call.spec.root_source_digest is not None and continuation is None:
                    _need(digest(source) == call.spec.root_source_digest,
                          'Root source differs from its frozen slot', 'SOURCE_CHANGED')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            missing.append(getattr(exc, 'code', type(exc).__name__))
        row = {'call_id': call.spec.call_id, 'ready': not missing,
            'missing_inputs': sorted(set(missing)), 'conditional_parent': PARENTS.get(call.spec.call_id)}
        if continuation is not None:
            row['owned_resume_proof'] = continuation
        rows.append(row)
    return {'runner_id': plan.runner_id, 'program_id': plan.program_id,
        'plan_digest': digest(plan.model_dump()), 'provider_config': config.model_dump(),
        'profile': profile, 'calls': rows, 'real_dispatch_allowed': real and allow_real,
        'backend_compatibility': 'unverified_until_the_first_planned_business_call',
        'new_grant_created': False, 'model_calls': 0, 'final_numeric_reads': 0}


class ModelProgramRunner:
    def __init__(self, store_root, plan, registry, output, *, allow_real=False,
                 fixture_provider_factory=None):
        self.plan = parse_program_plan(plan)
        self.registry = registry
        self.root, self.output = _safe_path(store_root), _safe_path(output)
        self.allow_real = allow_real
        self.fixture_provider_factory = fixture_provider_factory
        self.by_id = {c.spec.call_id: c for c in self.plan.calls}

    def _published(self, relative, value):
        path = _safe_path(self.output / relative)
        if path.exists():
            _need(read_bounded_json(path)[0] == value,
                  'An existing program effect cannot change', 'PROGRAM_EFFECT_CHANGED')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf8') as handle:
                handle.write(json_text(value))
        return value

    def _grant(self, store):
        from .provider_loop_native_authority import create_loop_native_grant
        real = self.registry.ensure_dispatchable(self.plan.provider_config_id).mode == 'codex_cli'
        if real:
            _need(self.fixture_provider_factory is None, 'Real programs cannot inject fixture outputs')
        auth_digest = self.plan.authorization.sha256 if real else None
        return create_loop_native_grant(store, self.registry,
            program_id=self.plan.program_id, plan_digest=digest(self.plan.model_dump()),
            provider_config_id=self.plan.provider_config_id,
            slots=[c.spec.model_dump() for c in self.plan.calls],
            max_seconds=self.plan.max_seconds, max_proposal_seconds=self.plan.proposal_seconds,
            authorization_kind='bounded_qualification' if real else 'fixture',
            human_authorization_digest=auth_digest, idempotency_key=self.plan.program_id + '-grant')

    def _session(self, store, authority, grant, call):
        from .provider_research_context import resolve_research_source
        from .research_sessions import ResearchSessions
        key = self.plan.program_id + '-session-' + call.session_key
        found = store._read('SELECT id FROM research_sessions WHERE idempotency_key=?', (key,))
        if found:
            session = ResearchSessions(store).get(found[0]['id'])
            _, connection = _original_connection(store.root)
            try:
                binding, old = authority._binding(connection, session['id'])
                _need(old['id'] == grant['id'], 'Session cannot switch to another grant')
            finally:
                connection.close()
            return session
        spec = self.plan.sessions[call.session_key]
        _need(spec.source is not None, 'An explicit adopted/root source is missing', 'MODEL_GATE_BLOCKED')
        descriptor = spec.source.model_dump()
        source = resolve_research_source(store, descriptor)
        if descriptor['kind'] == 'research_revision':
            revision = source['revision']
            root = {'kind': 'research_revision', 'research_id': descriptor['research_id'],
                    'revision_id': descriptor['base_revision_id'], 'revision_digest': revision['digest'],
                    'source_digest': digest(source)}
        elif descriptor['kind'] == 'paper_selection':
            root = {k: source[k] for k in ('kind', 'paper_id', 'selection_digest')}
            root.update(source_digest=digest(source), source_request=descriptor)
        else:
            root = {k: source[k] for k in ('kind', 'material_id', 'version_id', 'material_digest')}
            root.update(source_digest=digest(source), source_request=descriptor)
        config = self.registry.ensure_dispatchable(self.plan.provider_config_id)
        return authority.create_session(grant['id'], root, spec.purpose,
            {'provider_config_id': config.config_id, 'provider_config_digest': wire_digest(config.model_dump()),
             'contract_id': 'research_provider_v2'}, spec.budget.model_dump(), key)

    def _parent(self, store, grant_id, call_id):
        from .provider_loop_native_authority import _decode
        from .proposal_requests import ProposalRequests
        _, connection = _original_connection(store.root)
        try:
            row = connection.execute('SELECT * FROM provider_loop_native_slots WHERE grant_id=? AND call_id=?',
                                     (grant_id, call_id)).fetchone()
            value = _decode(row)
            if value['proposal_id'] is None:
                return None
            return ProposalRequests(store, configs={})._load(connection, value['proposal_id'])
        finally:
            connection.close()

    def _experiment(self, store, session_id, parent, candidate_id):
        from .research_sessions import ResearchSessions
        from .research_session_experiments import SessionExperiments
        from .research_session_experiments_schema import ExperimentCreate
        from .provider_experiment_results import build_experiment_result
        from .runner import Worker, worker_lock
        from .provider_research_context import resolve_research_source
        key = self.plan.program_id + '-experiment-' + parent['id'][-16:]
        path = self.output / 'experiments' / (parent['id'] + '.json')
        experiments = SessionExperiments(store)
        if path.exists():
            request = ExperimentCreate.model_validate(read_bounded_json(path)[0]).model_dump()
            _need(request['session_id'] == session_id and request['proposal_id'] == parent['id']
                  and request['accepted_response_digest'] == parent['accepted_response_digest']
                  and request['idempotency_key'] == key
                  and request['budget'] == self.plan.experiment_budget.model_dump(),
                  'Existing experiment request differs', 'RESUME_EXPERIMENT_CHANGED')
            rows = store._read('SELECT id FROM research_session_experiments WHERE idempotency_key=?', (key,))
            value = experiments.get(rows[0]['id']) if rows else experiments.create(request)
            _need(value['request'] == request, 'Existing experiment receipt differs', 'RESUME_EXPERIMENT_CHANGED')
        else:
            current_session = ResearchSessions(store).get(session_id)
            session = current_session
            request = {'session_id': session_id, 'expected_session_digest': session['state_digest'],
                'proposal_id': parent['id'], 'accepted_response_digest': parent['accepted_response_digest'],
                'budget': self.plan.experiment_budget.model_dump(), 'idempotency_key': key}
            self._published('experiments/' + parent['id'] + '.json', request)
            value = experiments.create(request)
        current = value['session']['current_source']
        source = resolve_research_source(store, {'kind': 'research_revision',
            'research_id': current['research_id'], 'base_revision_id': current['revision_id']})
        _need((source['revision']['task'].get('execution') or {}).get('kind') ==
              'registered_daily_development_v2',
              'Experiment must use the registered development-only executor', 'RESULT_SCOPE_INVALID')
        if value['job']['state'] == 'completed':
            value = experiments.reconcile(value['id'])
            result = build_experiment_result(store, value['job']['id'])
            proof = verify_decision_experiment(store, result, candidate_id)
            self._published('experiment-results/' + parent['id'] + '.json', proof)
            return result
        for action in ('validate', 'commit', 'submit'):
            value = experiments.get(value['id'])
            saved = [s for s in value['job']['steps'] if s['action'] == action]
            if saved:
                _need(len(saved) == 1 and saved[0]['idempotency_key'] == key + '-' + action
                      and saved[0]['status'] == 'completed',
                      'Original experiment step is not a completed owned effect', 'OUTCOME_UNKNOWN')
                continue
            value = experiments.advance(value['id'], action, key + '-' + action)
            _need(not any(s['error'] for s in value['job']['steps']), 'Actual experiment failed', 'EXPERIMENT_FAILED')
        job = value['job']
        if job['state'] in {'submitted', 'observed'}:
            run = experiments.submitted_run(value['id'], job['id'])
            _need(run is not None, 'Original submitted run is missing', 'EXPERIMENT_INTEGRITY')
            if run['status'] == 'queued':
                _need(not store._read('SELECT id FROM attempts WHERE run_id=?', (run['id'],)),
                      'An existing computation attempt cannot be replayed', 'OUTCOME_UNKNOWN')
                queued = store._read("SELECT id FROM runs WHERE status IN ('queued','running')")
                _need(len(queued) == 1 and queued[0]['id'] == job['run_id'],
                      'Runner may advance only its one original computation child', 'PROGRAM_QUEUE_CONFLICT')
                with worker_lock(store.root) as lock:
                    _need(Worker(store, lock).run_once(), 'Original computation queue did not advance')
            elif run['status'] == 'completed':
                # A crashed completion callback must acknowledge its one saved
                # result through the original finalizer, never recompute it.
                from .research_jobs import ResearchJobs
                ResearchJobs(store).finalize_existing_run(job['id'], value['id'])
            else:
                raise ModelProgramError('Original computation cannot be repeated',
                    'OUTCOME_UNKNOWN' if run['status'] in ('running', 'interrupted') else 'EXPERIMENT_FAILED')
        for action in ('observe', 'complete'):
            # Worker.run_once may already have completed these original
            # transitions through ExperimentAutofinalizer. Read the owned job
            # before each action; a second key cannot replay a closed effect.
            value = experiments.get(value['id'])
            if value['job']['state'] == 'completed':
                break
            value = experiments.advance(value['id'], action, key + '-' + action)
            _need(not any(s['error'] for s in value['job']['steps']), 'Actual experiment failed', 'EXPERIMENT_FAILED')
        _need(value['job']['state'] == 'completed', 'Only a completed validation tool result allows decide')
        value = experiments.reconcile(value['id'])
        result = build_experiment_result(store, value['job']['id'])
        proof = verify_decision_experiment(store, result, candidate_id)
        self._published('experiment-results/' + parent['id'] + '.json', proof)
        return result

    def _request(self, store, authority, grant, call, session):
        from .provider_research_schema import ResearchProposalCreate
        from .provider_research_targets import resolve_target
        target = None
        if call.spec.target_from_call_id:
            parent = self._parent(store, grant['id'], call.spec.target_from_call_id)
            _need(parent is not None, 'Required original parent did not run', 'PARENT_NOT_RUN')
            attempt = parent['attempts'][-1]
            _need(attempt['receipt'] and attempt['receipt']['process_stopped'] is True,
                  'Original parent stop is unknown', 'OUTCOME_UNKNOWN')
            if grant['config']['mode'] == 'codex_cli':
                authority._saved_transport(parent, attempt)
            target = {'proposal_id': parent['id'], 'attempt_id': attempt['id'],
                'response_digest': attempt['response_digest'], 'candidate_id': call.candidate_id,
                'research_job_id': None, 'result_digest': None}
            if call.spec.operation == 'decide_next_action':
                _need(parent['status'] == 'ready' and (attempt['validation'] or {}).get('valid') is True,
                      'Required parent is not a valid adopted draft', 'PARENT_NOT_ACCEPTED')
                accepted, _ = read_bounded_json(store.root / 'proposals' / parent['id'] / 'accepted' / 'response.json')
                _need(wire_digest(accepted) == parent['accepted_response_digest'] and
                      len([c for c in (accepted.get('draft') or {}).get('candidates', [])
                           if c['id'] == call.candidate_id]) == 1,
                      'Original accepted output lacks the exact frozen candidate', 'PARENT_NOT_ACCEPTED')
                result = self._experiment(store, session['id'], parent, call.candidate_id)
                target.update(research_job_id=result['job_id'], result_digest=result['frozen_result_digest'])
                from .research_sessions import ResearchSessions
                session = ResearchSessions(store).get(session['id'])
        source = session['current_source']
        descriptor = source.get('source_request') or {'kind': 'research_revision',
            'research_id': source['research_id'], 'base_revision_id': source['revision_id']}
        request = ResearchProposalCreate(operation=call.spec.operation, source=descriptor,
            session_id=session['id'], expected_session_digest=session['state_digest'],
            expected_source_digest=source['source_digest'], provider_config_id=self.plan.provider_config_id,
            instruction=call.instruction, candidate_limit=call.spec.candidate_limit, target=target,
            budget={'max_provider_invocations': 1, 'max_seconds': self.plan.proposal_seconds},
            idempotency_key=call.spec.effect_key)
        if target is not None:
            # This is the exact original domain, receipt, assertion and tool-result
            # gate. The runner never synthesizes an invalid parent to force repair.
            resolve_target(store, request)
            if grant['config']['mode'] == 'codex_cli':
                authority._saved_transport(parent, attempt)
        return request.model_dump()

    def _run_call(self, store, proposals, authority, grant, call, gate):
        from .provider_research_schema import ResearchProposalCreate
        from .research_session_execution import SessionExecution
        from .proposal_worker import ProposalWorker
        prior = self._parent(store, grant['id'], call.spec.call_id)
        if prior is not None and prior['status'] not in {'queued'}:
            if prior['status'] in {'running', 'validating', 'interrupted', 'cancelling'}:
                raise ModelProgramError('Original attempt requires recovery; no redispatch', 'OUTCOME_UNKNOWN')
            self._native_observation(store, grant, prior)
            return self._call_view(store, grant['id'], call, prior)
        _need(gate['ready'], ', '.join(gate['missing_inputs']), 'MODEL_GATE_BLOCKED')
        if 'owned_resume_proof' in gate:
            _, proof = _owned_resume_source(ReadOnlyStore(store.root), self.plan, self.registry, self.output, call)
            _need(proof == gate['owned_resume_proof'], 'Owned resume evidence changed after preflight', 'RESUME_SOURCE_CHANGED')
        path = self.output / 'requests' / (call.spec.call_id + '.json')
        if path.exists():
            request = read_bounded_json(path)[0]
        else:
            session = self._session(store, authority, grant, call)
            request = self._request(store, authority, grant, call, session)
            self._published('requests/' + call.spec.call_id + '.json', request)
        execution = SessionExecution(store, proposals)
        proposal = execution.propose(ResearchProposalCreate.model_validate(request))['proposal']
        if proposal['status'] == 'queued':
            pending = store._read("SELECT id FROM proposal_requests WHERE status IN ('queued','running','validating','cancelling')")
            _need(len(pending) == 1 and pending[0]['id'] == proposal['id'],
                  'Runner may dispatch only its one original queued child', 'PROGRAM_QUEUE_CONFLICT')
            provider = self.fixture_provider_factory(call.spec.call_id, request) if self.fixture_provider_factory else None
            advanced = asyncio.run(ProposalWorker(proposals).run_once(provider,
                registry=None if provider is not None else self.registry))
            _need(advanced, 'Original proposal queue did not advance')
        proposal = proposals.get(proposal['id'])
        _need(proposal['status'] not in {'running','validating','interrupted','cancelling'},
              'Original process or transport outcome is unknown', 'OUTCOME_UNKNOWN')
        session = execution.sessions.get(request['session_id'])
        reservation = next(r for r in session['reservations']
            if (r['children'] or {}).get('proposal_id') == proposal['id'])
        execution.reconcile(session['id'], reservation['token'])
        self._native_observation(store, grant, proposal)
        return self._call_view(store, grant['id'], call, proposal)

    @staticmethod
    def _native_observation(store, grant, proposal):
        if grant['config']['mode'] == 'codex_cli':
            _need(proposal['attempts'], 'No original Native attempt was observed', 'OUTCOME_UNKNOWN')
            from .provider_loop_native_authority import _decode
            _, connection = _original_connection(store.root)
            try:
                # Read the complete digest-sealed observation, not a caller
                # projection. This check also runs after a lost terminal ack.
                observed = connection.execute('SELECT * FROM provider_loop_native_observations WHERE attempt_id=? ORDER BY sequence DESC LIMIT 1',
                    (proposal['attempts'][-1]['id'],)).fetchone()
                value = _decode(observed) if observed is not None else None
            finally:
                connection.close()
            _need(value is not None and value['observed_invocations'] == 1 and
                  value['transport_status'] == 'confirmed_success' and value['process_stopped'] is True,
                  'Native transport authentication is unknown', 'OUTCOME_UNKNOWN')

    def _execution_scope(self, store, connection, grant_id):
        from ..daily_workflow_v2 import verify_daily_run
        rows = connection.execute('''SELECT DISTINCT a.* FROM attempts a JOIN runs r ON r.id=a.run_id
            JOIN research_session_experiment_resources x ON x.kind='run' AND x.resource_id=r.id
            JOIN research_session_experiments e ON e.id=x.experiment_id
            JOIN provider_loop_native_sessions s ON s.session_id=e.session_id
            WHERE s.grant_id=?''', (grant_id,)).fetchall()
        proofs = []
        for row in rows:
            attempt = dict(row)
            if attempt['output_dir'] is None:
                return None, proofs
            run = store._fetch('runs', attempt['run_id'])
            revision = store._fetch('revisions', run['revision_id'])
            task = json.loads(revision['task'])
            if (task.get('execution') or {}).get('kind') != 'registered_daily_development_v2':
                return None, proofs
            try:
                proof = verify_daily_run(_safe_path(attempt['output_dir']), expected_task=task,
                                         require_current_code=True)
                _need(proof['verified'] is True and proof['final_numeric_rows_parsed'] == 0 and
                      proof['evaluation_scope'] == 'development_prefix_validation_only',
                      'Original development-only computation scope is not verified')
                proofs.append({'run_id': run['id'], 'attempt_id': attempt['id'], **proof})
            except (ValueError, OSError, KeyError, TypeError):
                return None, proofs
        return 0, proofs

    def _call_view(self, store, grant_id, call, proposal):
        attempt = proposal['attempts'][-1] if proposal['attempts'] else None
        folder = store.root / 'proposals' / proposal['id']
        receipt_path = folder / 'attempts' / attempt['id'] / 'receipt.json' if attempt else None
        receipt_sha = read_bounded_json(receipt_path)[1] if receipt_path and receipt_path.is_file() else None
        return {'call_id': call.spec.call_id, 'state': proposal['status'],
            'grant_id': grant_id, 'session_id': proposal['request']['session_id'],
            'proposal_id': proposal['id'], 'attempt_id': attempt['id'] if attempt else None,
            'charged': True, 'invocation_started': (attempt['receipt'] or {}).get('invocation_started') if attempt else False,
            'raw_sha256': attempt['raw_sha256'] if attempt else None,
            'receipt_sha256': receipt_sha, 'context_sha256': read_bounded_json(folder / 'context.json')[1],
            'process_stopped': (attempt['receipt'] or {}).get('process_stopped') if attempt else None,
            'error': proposal['error'], 'absence_reason': None}

    def run(self, *, resume=False):
        from .provider_loop_native_authority import LoopNativeResearchAuthority
        marker = self.output / 'run-plan.json'
        if marker.exists():
            _need(resume, 'Use resume for this existing program output', 'PROGRAM_ALREADY_STARTED')
            _need(read_bounded_json(marker)[0] == self.plan.model_dump(), 'Resume cannot change the frozen plan')
        else:
            _need(not resume, 'No original run exists to resume')
        preflight = preflight_program(self.root, self.plan, self.registry, allow_real=self.allow_real,
                                     resume_output=self.output if resume else None)
        self._published('run-plan.json', self.plan.model_dump())
        store = open_existing_store(self.root)
        grant = self._grant(store)
        self._published('grant.json', grant)
        authority = LoopNativeResearchAuthority(store, self.registry)
        proposals = self.registry.proposal_requests(store)
        gates = {g['call_id']: g for g in preflight['calls']}
        paused = False
        for call in self.plan.calls:
            record_path = self.output / 'slots' / (call.spec.call_id + '.json')
            if record_path.exists():
                prior = read_bounded_json(record_path)[0]
                if prior['state'] == 'UNKNOWN_PAUSED':
                    paused = True
                continue
            if paused:
                value = {'call_id': call.spec.call_id, 'state': 'not_run', 'charged': False,
                    'absence_reason': 'program_paused_original_outcome_unknown'}
            else:
                try:
                    value = self._run_call(store, proposals, authority, grant, call, gates[call.spec.call_id])
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    code = getattr(exc, 'code', type(exc).__name__)
                    unknown = code in {'OUTCOME_UNKNOWN','TARGET_OUTCOME_UNKNOWN'}
                    paused = paused or unknown
                    actual = self._parent(store, grant['id'], call.spec.call_id)
                    if actual is not None:
                        value = self._call_view(store, grant['id'], call, actual)
                        value.update(state='UNKNOWN_PAUSED' if unknown else 'failed',
                                     absence_reason=code)
                    else:
                        _, connection = _original_connection(store.root)
                        try:
                            row = connection.execute('SELECT reservation_token FROM provider_loop_native_slots WHERE grant_id=? AND call_id=?',
                                (grant['id'], call.spec.call_id)).fetchone()
                            charged = row['reservation_token'] is not None
                        finally:
                            connection.close()
                        value = {'call_id': call.spec.call_id, 'state': 'UNKNOWN_PAUSED' if unknown else 'not_run',
                                 'charged': charged, 'absence_reason': code, 'message': str(exc)[:1000]}
            self._published('slots/' + call.spec.call_id + '.json', value)
        result = self.readback()
        atomic_json(self.output / 'readback-latest.json', result)
        return result

    def readback(self):
        """Read the exact original ledger and publications; never rerun anything."""
        from .provider_loop_native_authority import LoopNativeResearchAuthority, _decode
        store = ReadOnlyStore(self.root)
        _need(read_bounded_json(self.output / 'run-plan.json')[0] == self.plan.model_dump(),
              'Original frozen run plan differs')
        grant_saved, grant_sha = read_bounded_json(self.output / 'grant.json')
        _, connection = _original_connection(store.root)
        try:
            authority = LoopNativeResearchAuthority(store, self.registry)
            grant = authority._grant(connection, grant_saved['id'])
            _need(grant == grant_saved and grant['request']['plan_digest'] == digest(self.plan.model_dump()),
                  'Original program grant differs from its frozen plan')
            slots = [_decode(r) for r in connection.execute('SELECT * FROM provider_loop_native_slots WHERE grant_id=? ORDER BY call_id', (grant['id'],))]
            views, observations = [], []
            for call in self.plan.calls:
                path = self.output / 'slots' / (call.spec.call_id + '.json')
                recorded = read_bounded_json(path)[0] if path.is_file() else {
                    'call_id': call.spec.call_id, 'state': 'not_run', 'charged': False, 'absence_reason': 'not_started'}
                slot = next(s for s in slots if s['call_id'] == call.spec.call_id)
                if slot['proposal_id'] is not None:
                    proposal = self._parent(store, grant['id'], call.spec.call_id)
                    actual = self._call_view(store, grant['id'], call, proposal)
                    # A stored unknown disposition stays unknown even if the
                    # application has since changed its status; no retry follows.
                    if recorded['state'] == 'UNKNOWN_PAUSED':
                        actual['state'] = 'UNKNOWN_PAUSED'
                    views.append(actual)
                else:
                    _need(recorded.get('charged') == (slot['reservation_token'] is not None),
                          'Report charge differs from original program reservation')
                    views.append(recorded)
                if slot['attempt_id'] is not None:
                    row = connection.execute('SELECT * FROM provider_loop_native_observations WHERE attempt_id=? ORDER BY sequence DESC LIMIT 1', (slot['attempt_id'],)).fetchone()
                    observations.append(_decode(row) if row is not None else None)
            fixture = grant['config']['mode'] == 'fixture'
            unknown = sum(o is None or o['observed_invocations'] is None for o in observations)
            native_calls = 0 if fixture else None if unknown else sum(o['observed_invocations'] for o in observations)
            final_reads, scope_proofs = self._execution_scope(store, connection, grant['id'])
            return {'schema_version': self.plan.schema_version, 'runner_id': self.plan.runner_id, 'program_id': self.plan.program_id,
                'plan_digest': digest(self.plan.model_dump()), 'grant_id': grant['id'],
                'grant_sha256': digest(grant), 'grant_file_sha256': grant_sha,
                'profile_digest': grant['profile']['native_identity'].get('profile_digest'),
                'deadline_at': grant['deadline_at'], 'planned_slots': len(self.plan.calls),
                'charged_slots': sum(s['reservation_token'] is not None for s in slots),
                'claimed_attempts': sum(s['attempt_id'] is not None for s in slots),
                'actual_native_invocations': native_calls,
                'unknown_native_attempts': 0 if fixture else unknown,
                'fixture': fixture, 'slots': views,
                'status': 'UNKNOWN_PAUSED' if any(v['state'] == 'UNKNOWN_PAUSED' for v in views) else 'CLOSED_BOUNDED',
                'automatic_retries': 0, 'reset_cards': 0,
                'scientific_score': None, 'final_numeric_reads': final_reads,
                'original_computation_scope_proofs': scope_proofs,
                'model_calls_during_readback': 0}
        finally:
            connection.close()
