"""Versioned HTTP projections, independent of persisted scientific artifacts.

Required nullable properties distinguish an absent field from an explicit empty
state. Only engine/evaluation payloads have open extension fields. Historical
regression checks have a separate closed shape; new checks cannot use it.
"""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .research_assessments import ResearchAssessment
from .daily_snapshot_schema import DailySnapshotSchema

RunStatus = Literal['queued', 'running', 'cancelling', 'completed', 'failed', 'interrupted', 'cancelled']
ExecutionMode = Literal['agent', 'fixed', 'normalized_fixed']
CandidateStatus = Literal['pending', 'running', 'evaluated', 'blocked', 'failed', 'rejected', 'not_evaluable', 'budget_stopped']
TerminalCandidateStatus = Literal['evaluated', 'blocked', 'failed', 'rejected', 'not_evaluable', 'budget_stopped']
Attribution = Literal['paper_original', 'user_modification', 'model_conjecture']
ReviewSource = Literal['human', 'automation', 'imported', 'legacy_unknown']
RegressionOutcome = Literal['passed', 'failed', 'not_comparable']
ImportStatus = Literal['uploaded', 'validating', 'valid', 'invalid', 'interrupted', 'registering', 'registered']
JsonObject = dict[str, Any]


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ErrorResponse(ResponseModel):
    detail: str
    code: str | None = None
    retryable: bool | None = None


class WorkerHealth(ResponseModel):
    online: bool
    last_seen: str | None


class HealthResponse(ResponseModel):
    status: Literal['ok']
    version: str
    database_schema: int
    ai_enabled: bool
    ai_status_scope: Literal['host_dispatch_permission_only']
    ai_inference_verified: Literal[False]
    workspace_id: str
    worker: WorkerHealth


class CapabilityDomain(ResponseModel):
    domain: Literal['daily_alpha_engine', 'monthly_fixture', 'author_diagnostics', 'industry_mom']
    data_kind: Literal['synthetic_only', 'controlled_fixture', 'author_aggregate_diagnostics', 'market_derived_portfolio_returns']
    scope: str


class CapabilitiesResponse(ResponseModel):
    ai_enabled: bool
    ai_status_scope: Literal['host_dispatch_permission_only']
    ai_inference_verified: Literal[False]
    ai_provider: Literal['codex_cli'] | None
    proposal_mode: Literal['manual_or_reviewed_template', 'manual_or_reviewed_template_or_provider']
    data_kind: Literal['synthetic_only']
    data_kind_scope: Literal['daily_alpha_engine'] = 'daily_alpha_engine'
    domains: list[CapabilityDomain] = Field(default_factory=list)
    custom_synthetic_import: bool
    deployment: Literal['local_single_user']
    execution_modes: list[ExecutionMode]
    mode_labels: dict[str, str]
    max_run_attempts: int
    test_split: Literal['reserved']
    rag_enabled: Literal[False]
    brain_enabled: Literal[False]
    review_scope: str


class PaperSummary(ResponseModel):
    id: str
    title: str
    sha256: str
    created_at: str


class PaperPage(ResponseModel):
    page: int
    text: str


class PaperDetail(PaperSummary):
    pages: list[PaperPage]
    extraction: str


class DatasetResponse(ResponseModel):
    id: str
    title: str
    sha256: str
    version: str
    data_kind: Literal['synthetic', 'daily_development_snapshot', 'real_daily_ohlc_development']
    fields: list[str]
    metadata: JsonObject


class DatasetImportReference(ResponseModel):
    id: str
    input_digest: str
    latest_validation_attempt_id: str | None


class DatasetRegistration(ResponseModel):
    kind: Literal['validated_import', 'legacy_registration', 'declared_daily_snapshot', 'declared_real_daily_prefix']
    imports: list[DatasetImportReference]
    snapshot: DailySnapshotSchema | None = None
    prefix: JsonObject | None = None


class DailySnapshotCapabilities(ResponseModel):
    schema_version: Literal[1] = 1
    data_kind: Literal['daily_development_snapshot'] = 'daily_development_snapshot'
    input_origin: Literal['engineering_fixture'] = 'engineering_fixture'
    validation_scope: Literal['development_prefix_only'] = 'development_prefix_only'
    guard_policy: Literal['literal_asset_date_potential_overlap'] = 'literal_asset_date_potential_overlap'
    legacy_worker_allowed: Literal[False] = False
    provider_allowed: Literal[False] = False
    final_execution_allowed: Literal[False] = False
    full_dataset_validated: Literal[False] = False
    test_freshness: Literal['unknown'] = 'unknown'
    real_source_ready: Literal[False] = False
    scientific_score: None = None
    whole_byte_hash_permission_required: Literal[True] = True
    maximum_raw_bytes: int
    maximum_metadata_bytes: int
    database_schema: int


class DatasetDetail(DatasetResponse):
    registration: DatasetRegistration


class EvidenceResponse(ResponseModel):
    id: str
    page: int
    quote: str


class HypothesisResponse(ResponseModel):
    id: str
    claim: str
    attribution: Attribution
    evidence_ids: list[str]
    economic_mechanism: str
    mechanism_attribution: Attribution
    signal_direction: str
    required_fields: list[str]
    assumptions: list[str]


class CandidateResponse(ResponseModel):
    id: str
    hypothesis_id: str
    expression: str
    origin: Attribution
    changes: list[str]


class DailyExecutionBinding(ResponseModel):
    kind: Literal['registered_daily_development_v2']
    registration_id: str
    dataset_id: str
    registration_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    declaration_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    development_content_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    development_index_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    config_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    catalog_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


from ..real_daily_web_contract import ExecutionBinding as MainDailyExecutionBinding


class ResearchTask(ResponseModel):
    evidence: list[EvidenceResponse]
    hypotheses: list[HypothesisResponse]
    candidates: list[CandidateResponse]
    evaluation: JsonObject
    budget: JsonObject
    schema_version: Literal[2, 3] | None = None
    title: str | None = None
    execution: DailyExecutionBinding | MainDailyExecutionBinding | None = None

    @model_validator(mode='after')
    def _versioned_binding(self):
        if self.schema_version in (2, 3):
            expected_kind = 'registered_daily_development_v2' if self.schema_version == 2 else 'real_daily_ohlc_development_v1'
            if self.execution is None or self.execution.kind != expected_kind or not self.title:
                raise ValueError('Daily task requires its title and execution binding')
        elif self.execution is not None or self.title is not None:
            raise ValueError('Legacy task must not declare a daily execution binding')
        return self


class RevisionResponse(ResponseModel):
    id: str
    research_id: str
    number: int
    task: ResearchTask
    note: str
    digest: str
    created_at: str


class ResearchSummary(ResponseModel):
    id: str
    title: str
    paper_id: str
    dataset_id: str
    created_at: str
    latest_revision_id: str


class CandidatePreflight(ResponseModel):
    candidate_id: str
    status: Literal['ready', 'warning', 'blocked']
    code: str
    expression_changed: bool
    warmup_rows: int | None
    missing_fields: list[str]
    suggested_expression: str | None = None
    message: str | None = None


class ResearchPreflight(ResponseModel):
    dataset_version: str
    fields: list[str]
    candidates: list[CandidatePreflight]
    scope: str


class ResearchDetail(ResearchSummary):
    revisions: list[RevisionResponse]
    preflight: ResearchPreflight


class ExampleResponse(ResponseModel):
    research_id: str
    revision_id: str


class AssessmentSummary(ResponseModel):
    semantic_status: Literal['passed', 'failed', 'incomplete', 'not_assessed', 'not_human']
    total_active_seconds: float
    timing_recorded: bool
    source_is_declared: Literal[True]


class ReviewTarget(ResponseModel):
    candidate_id: str
    attempt_id: str
    result_digest: str


class ReviewResponse(ResponseModel):
    id: str
    run_id: str
    revision_id: str
    attempt_id: str
    candidate_id: str
    verdict: Literal['accepted', 'needs_changes', 'rejected']
    category: Literal['evidence', 'hypothesis', 'implementation', 'data', 'evaluation', 'other']
    note: str
    result_digest: str
    created_at: str
    source: ReviewSource
    assessment: ResearchAssessment | None
    assessment_summary: AssessmentSummary


class RunSummary(ResponseModel):
    id: str
    revision_id: str
    research_id: str
    status: RunStatus
    mode: ExecutionMode
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    attempt_count: int


class RunStatusResponse(RunSummary):
    attempt_id: str | None
    phase: Literal['preparing', 'executing', 'verifying', 'publishing'] | None
    change_token: str
    integrity_checked: Literal[False]


class RegressionTarget(ResponseModel):
    attempt_id: str
    result_digest: str


class RunAttempt(ResponseModel):
    id: str
    number: int
    worker_id: str
    status: Literal['running', 'completed', 'failed', 'interrupted', 'cancelled']
    started_at: str
    finished_at: str | None
    error: str | None
    state_digest: str | None


class CandidateAttempt(ResponseModel):
    model_config = ConfigDict(extra='allow', strict=True, allow_inf_nan=False)
    number: int
    expression: str
    status: Literal['running', 'evaluated', 'not_evaluable', 'failed', 'interrupted', 'budget_stopped']


class CandidateEvaluation(ResponseModel):
    model_config = ConfigDict(extra='allow', strict=True, allow_inf_nan=False)
    schema_version: Literal[1]
    status: Literal['evaluated', 'not_evaluable']
    reason: str | None
    split: str
    config: JsonObject
    execution: JsonObject
    metrics: dict[str, int | float | None]
    limitations: list[str]


class CandidateState(CandidateResponse):
    model_config = ConfigDict(extra='allow', strict=True, allow_inf_nan=False)
    status: CandidateStatus
    attempts: list[CandidateAttempt]
    result: CandidateEvaluation | None = None


class EngineState(ResponseModel):
    model_config = ConfigDict(extra='allow', strict=True, allow_inf_nan=False)
    schema_version: Literal[1]
    task: JsonObject
    mode: ExecutionMode
    status: Literal['running', 'completed', 'failed', 'budget_exhausted']
    tool_calls: int
    elapsed_seconds: float
    events: list[JsonObject]
    candidates: list[CandidateState]


class RunVerification(ResponseModel):
    model_config = ConfigDict(extra='allow', strict=True, allow_inf_nan=False)
    verified: bool


class FrozenSourceEvidence(ResponseModel):
    id: str
    page: int
    quote: str
    citation_verified: Literal[True]


class FrozenReviewSource(ResponseModel):
    schema_version: Literal[1]
    run_id: str
    revision_id: str
    attempt_id: str
    state_digest: str
    paper_id: str
    document_sha256: str
    evidence: list[FrozenSourceEvidence]


class RunDetail(RunSummary):
    state: EngineState | None
    frozen_source: FrozenReviewSource | None = None
    verification: RunVerification | None
    reviews: list[ReviewResponse]
    review_targets: list[ReviewTarget]
    attempts: list[RunAttempt]
    regression_target: RegressionTarget | None = None
    status_token: str | None = None


class EventResponse(ResponseModel):
    id: int
    run_id: str
    attempt_id: str | None
    kind: str
    created_at: str
    payload: JsonObject
    sequence: int


class EventPage(ResponseModel):
    run_id: str
    items: list[EventResponse]
    next_cursor: int
    has_more: bool
    high_watermark: int
    total_records: int


class ArtifactResponse(ResponseModel):
    id: str
    name: str
    size: int
    sha256: str
    attempt_id: str


class ValidationRule(ResponseModel):
    name: str
    outcome: Literal['passed', 'failed', 'not_run']
    code: str
    message: str


class LegacyValidationReport(ResponseModel):
    schema_version: Literal[1]
    validator: dict[str, str]
    input_digest: str
    status: Literal['valid', 'invalid', 'interrupted']
    checks: list[ValidationRule]
    summary: JsonObject | None
    duration_seconds: float
    diagnostics: JsonObject
    limitations: list[str]


class DiagnosticSample(ResponseModel):
    row: int | None
    column: str | None
    date: str | None
    asset: str | None
    related_rows: list[int]
    value: str | None
    reason: str


class DatasetDiagnostics(ResponseModel):
    mode: Literal['fail_fast']
    detected_error_count: Literal[0, 1]
    all_errors_enumerated: Literal[False]
    samples: list[DiagnosticSample]
    sample_limit: Literal[5]
    sample_note: str
    row_numbering: Literal['csv_logical_record_1_based_header_included']
    first_error_location: DiagnosticSample | None


class ValidationReport(ResponseModel):
    schema_version: Literal[2]
    validator: dict[str, str]
    input_digest: str
    status: Literal['valid', 'invalid', 'interrupted']
    checks: list[ValidationRule]
    summary: JsonObject | None
    duration_seconds: float
    diagnostics: DatasetDiagnostics
    limitations: list[str]


class DatasetValidationAttempt(ResponseModel):
    id: str
    number: int
    status: Literal['validating', 'valid', 'invalid', 'interrupted']
    started_at: str
    finished_at: str | None
    validator_digest: str
    report: ValidationReport | LegacyValidationReport | None
    report_digest: str | None
    error: str | None


class DatasetRegistrationAttempt(ResponseModel):
    id: str
    validation_attempt_id: str
    status: Literal['registering', 'registered', 'interrupted']
    created_at: str
    finished_at: str | None
    dataset_id: str | None
    error: str | None


class DatasetImportEvent(ResponseModel):
    sequence: int
    kind: str
    created_at: str
    payload: JsonObject


class DatasetImportResponse(ResponseModel):
    id: str
    title: str
    status: ImportStatus
    created_at: str
    updated_at: str
    csv_sha256: str
    metadata_sha256: str
    input_digest: str
    latest_validation_attempt_id: str | None
    registered_dataset_id: str | None
    error: str | None
    validation_attempts: list[DatasetValidationAttempt]
    latest_validation: DatasetValidationAttempt | None
    registration_attempts: list[DatasetRegistrationAttempt]
    events: list[DatasetImportEvent]


class RegressionCaseResponse(ResponseModel):
    id: str
    review_id: str
    research_id: str
    paper_id: str
    dataset_id: str
    candidate_id: str
    expected_status: TerminalCandidateStatus
    note: str
    created_at: str
    version: int
    approved: bool
    contract: JsonObject | None
    contract_digest: str | None


class RegressionAssertion(ResponseModel):
    model_config = ConfigDict(extra='allow', strict=True, allow_inf_nan=False)
    name: str
    outcome: RegressionOutcome
    reason: str


class LegacyRegressionResult(ResponseModel):
    case_id: str
    candidate_id: str
    expected_status: TerminalCandidateStatus
    actual_status: CandidateStatus | None
    compatible: bool
    passed: bool
    reason: str | None


class RegressionResult(LegacyRegressionResult):
    outcome: RegressionOutcome
    differences: list[str]
    checks: list[RegressionAssertion]
    # v0.3 checks lacked these presentation fields. New POSTs require them below.
    reason_code: str | None = None
    scope: str | None = None


class CurrentRegressionResult(RegressionResult):
    reason_code: str
    scope: str


class LegacyRegressionCheck(ResponseModel):
    id: str
    run_id: str
    attempt_id: str
    revision_id: str
    result_digest: str
    created_at: str
    scope: str
    passed: bool
    results: list[LegacyRegressionResult]


class RegressionCheckResponse(LegacyRegressionCheck):
    outcome: RegressionOutcome
    results: list[CurrentRegressionResult]
    timing: JsonObject


class PreviousRegressionCheck(LegacyRegressionCheck):
    """v0.3 saved scientific checks: outcome present, timing not yet recorded."""
    outcome: RegressionOutcome
    results: list[RegressionResult]


HistoricalRegressionCheck = RegressionCheckResponse | PreviousRegressionCheck | LegacyRegressionCheck


IssueState = Literal['open', 'proposed', 'awaiting_review', 'resolved', 'deferred']


class IssueEvent(ResponseModel):
    id: str
    issue_id: str
    created_at: str
    state: IssueState
    disposition: Literal['implementation_fix', 'evidence_update', 'hypothesis_change', 'data_change', 'accept_limitation']
    note: str
    source: ReviewSource
    revision_id: str | None
    target_run_id: str | None
    check_id: str | None
    target_attempt_id: str | None
    target_result_digest: str | None
    resolution_review_id: str | None = None


class IssueResponse(ResponseModel):
    id: str
    review_id: str
    created_at: str
    research_id: str
    candidate_id: str
    source: ReviewSource
    state: IssueState
    latest_event_id: str
    events: list[IssueEvent]
    original_review: ReviewResponse | None = None


class FeedbackSummary(ResponseModel):
    schema_version: Literal[1]
    created_at: str
    filters: dict[str, str | None]
    metrics: JsonObject
    inputs: JsonObject
    input_digest: str
    skipped_attempts: list[JsonObject]
    attempt_timings: list[JsonObject]
    elapsed_seconds: float
    limitations: list[str]


class CatalogPage(ResponseModel):
    items: list[ResearchSummary | RunSummary | ReviewResponse | RegressionCaseResponse | HistoricalRegressionCheck | IssueResponse]
    next_cursor: int
    has_more: bool
    high_watermark: int
    total_records: int
    filters: dict[str, str | None]
    scope: str
