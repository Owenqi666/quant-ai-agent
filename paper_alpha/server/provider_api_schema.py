"""Closed HTTP projections for proposals; private invocation files stay local."""
from typing import Literal

from .response_schemas import ResponseModel, CandidateResponse, CandidateAttempt, CandidateStatus, JsonObject
from .provider_schema import ProposalBudget, ProposalModelOutput
from .provider_context import ProviderContext, ProviderDevelopment
from .research_jobs_schema import JobBudget, JobCandidateCheck, DraftHypothesis

ProposalStatus = Literal['queued', 'running', 'validating', 'retry_wait', 'ready', 'handing_off',
                         'handed_off', 'abstained', 'rejected', 'failed', 'cancelled', 'cancelling',
                         'expired', 'stale', 'interrupted', 'exhausted']
ArtifactKind = Literal['raw_response', 'parsed_response', 'validation', 'accepted_response']


class PublicProviderError(ResponseModel):
    code: str
    message: str
    retryable: bool


class PublicReadiness(ResponseModel):
    provider: Literal['codex_cli', 'disabled', 'fixture']
    configured: bool
    authenticated: bool | None
    inference_verified: bool
    tool_boundary_verified: bool
    cli_version: str | None
    checked_at: str
    errors: list[PublicProviderError]


class PublicProviderConfig(ResponseModel):
    config_id: str
    mode: Literal['codex_cli', 'fixture']
    enabled: bool
    dispatchable: bool
    model: str | None
    reasoning_effort: str
    expected_cli_version: str
    compatibility_verified: bool
    tool_boundary_verified: bool
    inference_verified: bool
    readiness: PublicReadiness | None
    error: PublicProviderError | None


class ProvidersResponse(ResponseModel):
    schema_version: Literal[1]
    allow_fixture: bool
    allow_real: bool
    real_dispatch_enabled: bool
    dispatch_paused: bool
    providers: list[PublicProviderConfig]


class ProposalContextResponse(ResponseModel):
    schema_version: Literal[1]
    context_digest: str
    context: ProviderContext
    public_projection_redacted: bool


class PublicProviderUsage(ResponseModel):
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    complete: bool
    observed_turns: int
    tool_events: int
    model_calls: int | None
    cost: float | None


class PublicProviderReceipt(ResponseModel):
    started_at: str
    finished_at: str
    elapsed_seconds: float
    exit_code: int | None
    requested_model: str | None
    reported_model: str | None
    invocation_started: bool
    fixture: bool
    cli_version: str | None
    usage: PublicProviderUsage
    response_sha256: str | None
    process_stopped: bool


class PublicProposalDiagnostic(ResponseModel):
    code: str
    message: str
    semantic_fidelity: Literal['unverified']


class PublicProposalValidation(ResponseModel):
    valid: bool
    diagnostics: list[PublicProposalDiagnostic]
    candidate_checks: list[JobCandidateCheck]


class ProposalArtifactReference(ResponseModel):
    id: str
    kind: ArtifactKind
    attempt_id: str | None
    source_bytes: int
    source_sha256: str


class PublicProposalAttempt(ResponseModel):
    id: str
    sequence: int
    status: ProposalStatus
    started_at: str
    finished_at: str | None
    error: PublicProviderError | None
    receipt: PublicProviderReceipt | None
    output: ProposalModelOutput | None
    validation: PublicProposalValidation | None
    artifacts: list[ProposalArtifactReference]


class ProposalSummary(ResponseModel):
    id: str
    research_id: str
    base_revision_id: str
    provider_config_id: str
    provider_mode: Literal['codex_cli', 'fixture']
    requested_model: str | None
    purpose: Literal['generate_from_evidence', 'repair_candidate']
    candidate_limit: int
    status: ProposalStatus
    created_at: str
    generation_deadline_at: str
    provider_invocations: int
    current_attempt_id: str | None
    accepted_attempt_id: str | None
    accepted_response_digest: str | None
    research_job_id: str | None
    error: PublicProviderError | None


class ProposalPage(ResponseModel):
    items: list[ProposalSummary]
    total: int
    limit: int
    offset: int


class ProposalDetail(ProposalSummary):
    schema_version: Literal[1]
    source_digest: str
    context_digest: str
    instruction: str
    budget: ProposalBudget
    accepted_response: ProposalModelOutput | None
    attempts: list[PublicProposalAttempt]
    artifacts: list[ProposalArtifactReference]
    handoff_budget: JobBudget | None
    public_projection_redacted: bool
    semantic_fidelity: Literal['unverified']


class ProposalArtifactView(ResponseModel):
    """Source hashes validate local files; view_sha256 validates public material."""
    schema_version: Literal[1]
    proposal_id: str
    artifact: ProposalArtifactReference
    public_projection_redacted: bool
    view_sha256: str
    raw_text: str | None
    output: ProposalModelOutput | None
    validation: PublicProposalValidation | None
    hash_scope: Literal['source_sha256 hashes original file bytes; view_sha256 hashes canonical JSON of raw_text/output/validation.']


class ResearchReviewIdentities(ResponseModel):
    research_id: str
    base_revision_id: str
    paper_id: str
    dataset_id: str
    source_digest: str
    context_digest: str
    paper_sha256: str
    paper_document_digest: str
    dataset_sha256: str
    data_file_sha256: str
    metadata_file_sha256: str
    base_task_digest: str
    review_exporter_source_sha256: str


class ResearchReviewPaper(ResponseModel):
    id: str
    title: str
    url: str
    version: str
    document_sha256: str
    pdf_url: str


class ResearchReviewEvidence(ResponseModel):
    id: str
    page: int
    quote: str
    paper_id: str
    document_sha256: str
    normalized_start: int | None
    normalized_end: int | None
    citation_verified: bool
    semantic_fidelity: Literal['requires_human_review']
    error: str | None


class ResearchReviewResponse(ResponseModel):
    attempt_id: str
    response_digest: str
    raw_sha256: str | None
    model_output: ProposalModelOutput


class ResearchReviewCandidate(CandidateResponse):
    hypothesis: DraftHypothesis | None
    machine_checks: list[JobCandidateCheck]


class ResearchReviewOracleCheck(ResponseModel):
    name: str
    outcome: Literal['passed', 'failed', 'not_comparable']
    reason: str
    reason_code: str | None
    details: JsonObject


class ResearchReviewOracle(ResponseModel):
    outcome: Literal['passed', 'failed', 'not_comparable']
    scope: str | None
    oracle_version: str | None
    reason: str | None
    checks: list[ResearchReviewOracleCheck]


class ResearchReviewMetrics(ResponseModel):
    schema_version: Literal[1]
    status: str
    reason: str | None
    split: str
    expression: str | None
    metrics: dict[str, int | float | None] | None
    config: JsonObject
    execution: JsonObject
    factor_diagnostics: JsonObject | None
    expression_diagnostics: JsonObject | None
    limitations: list[str] | None


class ResearchReviewComputedCandidate(CandidateResponse):
    status: CandidateStatus
    attempts: list[CandidateAttempt]
    artifacts: dict[str, str]
    result: ResearchReviewMetrics | None
    independent_verification: ResearchReviewOracle


class ResearchReviewComputation(ResponseModel):
    run_id: str
    attempt_id: str | None
    status: str
    revision_id: str
    integrity: Literal['verified', 'not_verified']
    state_digest: str | None
    manifest_sha256: str | None
    candidates: list[ResearchReviewComputedCandidate]
    error: str | None
    missing_reason: str | None


class ResearchReviewJob(ResponseModel):
    id: str
    digest: str
    state: str
    stop_reason: str | None
    base_revision_id: str
    revision_id: str | None
    run_id: str | None
    candidate_checks: list[JobCandidateCheck]
    saved_result_digest: str | None
    handoff_note: str | None
    execution_consent_is_human_quality_review: Literal[False]


class BlankResearchJudgment(ResponseModel):
    outcome: None
    reason: None


class PendingResearchDimensions(ResponseModel):
    evidence_accuracy: BlankResearchJudgment
    hypothesis_fidelity: BlankResearchJudgment
    mechanism_attribution: BlankResearchJudgment
    field_semantics: BlankResearchJudgment
    implementation_alignment: BlankResearchJudgment


class PendingResearchReview(ResponseModel):
    status: Literal['pending']
    reviewer: None
    confirmed_at: None
    dimensions: PendingResearchDimensions


class ResearchReviewScope(ResponseModel):
    dataset_kind: Literal['synthetic']
    model_context_development: ProviderDevelopment
    context_omission_claims_apply_to_frozen_provider_context_projection_only: Literal[True]
    model_context_contains_market_rows: Literal[False]
    model_context_contains_final_test_range: Literal[False]
    local_complete_snapshot_and_factor_computation_may_read_test_bytes: Literal[True]
    computed_metrics_scope: str
    human_judgments_written: Literal[0]
    economic_semantics_verified: Literal[False]
    market_performance_claim: Literal[False]


class ProposalResearchReview(ResponseModel):
    schema_version: Literal[1]
    proposal: ProposalSummary
    identities: ResearchReviewIdentities
    paper: ResearchReviewPaper
    input_evidence: list[ResearchReviewEvidence]
    input_hypotheses: list[DraftHypothesis]
    response: ResearchReviewResponse | None
    output_evidence: list[ResearchReviewEvidence]
    candidates: list[ResearchReviewCandidate]
    attempts: list[PublicProposalAttempt]
    research_job: ResearchReviewJob | None
    computation: ResearchReviewComputation | None
    scope: ResearchReviewScope
    human_review: PendingResearchReview
    semantic_fidelity: Literal['unverified']
    source_packet_digest: str
    public_view_digest: str
    public_projection_redacted: bool
    hash_scope: Literal['source_packet_digest hashes the private verified packet; public_view_digest hashes this public JSON without public_view_digest.']
