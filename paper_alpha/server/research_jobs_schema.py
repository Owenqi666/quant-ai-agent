"""Closed provider-independent research execution contracts; no file paths."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel

Origin = Literal['paper_original', 'user_modification', 'model_conjecture']
JobState = Literal['created', 'validated', 'committed', 'submitted', 'observed', 'completed', 'blocked', 'failed', 'cancelled', 'exhausted']
JobAction = Literal['validate', 'commit', 'submit', 'observe', 'complete', 'cancel']


class DraftEvidence(AuthorModel):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    page: int = Field(ge=1, le=500)
    quote: str = Field(min_length=1, max_length=4096)


class DraftHypothesis(AuthorModel):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    claim: str = Field(min_length=1, max_length=4000)
    attribution: Origin
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    economic_mechanism: str = Field(min_length=1, max_length=4000)
    mechanism_attribution: Origin
    signal_direction: str = Field(min_length=1, max_length=2000)
    required_fields: list[str] = Field(max_length=30)
    assumptions: list[str] = Field(max_length=100)


class DraftCandidate(AuthorModel):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    hypothesis_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    expression: str = Field(min_length=1, max_length=2048)
    origin: Origin
    changes: list[str] = Field(max_length=100)


class ResearchDraft(AuthorModel):
    evidence: list[DraftEvidence] = Field(min_length=1, max_length=100)
    hypotheses: list[DraftHypothesis] = Field(min_length=1, max_length=100)
    candidates: list[DraftCandidate] = Field(min_length=1, max_length=20)


class JobBudget(AuthorModel):
    max_steps: int = Field(ge=1, le=100)
    max_failures: int = Field(ge=1, le=20)
    max_seconds: int = Field(ge=1, le=600)


class ResearchJobCreate(AuthorModel):
    research_id: str = Field(min_length=1, max_length=64)
    base_revision_id: str = Field(min_length=1, max_length=64)
    draft: ResearchDraft
    budget: JobBudget
    idempotency_key: str = Field(min_length=1, max_length=128)
    note: str = Field(default='', max_length=4000)
    allow_partial_execution: bool = False
    method_status: Literal['resolved', 'unresolved'] = 'resolved'
    unresolved_rules: list[str] = Field(default_factory=list, max_length=30)


class ResearchJobAdvance(AuthorModel):
    action: JobAction
    idempotency_key: str = Field(min_length=1, max_length=128)


class JobError(AuthorModel):
    code: str
    message: str
    retryable: bool


class JobCandidateCheck(AuthorModel):
    candidate_id: str
    status: Literal['ready', 'blocked']
    code: str
    message: str
    submitted_expression: str
    accepted_expression: str | None
    normalization: Literal['unchanged', 'documented_operator_alias', 'excluded']


class JobStep(AuthorModel):
    id: str
    sequence: int
    action: JobAction
    idempotency_key: str
    request_digest: str
    effect_key: str
    status: Literal['running', 'completed', 'failed', 'interrupted']
    started_at: str
    finished_at: str | None
    elapsed_seconds: float
    next_state: JobState
    error: JobError | None
    output_json: str | None


class JobUsage(AuthorModel):
    steps: int
    failures: int
    elapsed_seconds: float


class ResearchJobSummary(AuthorModel):
    id: str
    digest: str
    created_at: str
    research_id: str
    base_revision_id: str
    paper_id: str
    dataset_id: str
    budget: JobBudget
    usage: JobUsage
    state: JobState
    stop_reason: str | None
    revision_id: str | None
    run_id: str | None
    provider_connected: Literal[False]
    semantic_fidelity: Literal['unverified']


class ResearchJob(ResearchJobSummary):
    source_digest: str
    draft: ResearchDraft
    allow_partial_execution: bool
    context_json: str
    candidate_checks: list[JobCandidateCheck]
    steps: list[JobStep]
    result_json: str | None


class ResearchJobPage(AuthorModel):
    items: list[ResearchJobSummary]
    total: int
    limit: int
    offset: int
