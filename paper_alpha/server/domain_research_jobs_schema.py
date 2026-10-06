"""Closed domain orchestration contracts. Existing daily job contracts stay intact."""
from typing import Annotated, Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .monthly_schema import MonthlyConfig
from .research_jobs_schema import JobBudget, JobError

DomainKind = Literal['monthly_fixture', 'author_study_diagnostic']
DomainState = Literal['created', 'validated', 'submitted', 'observed', 'completed', 'blocked', 'failed', 'exhausted', 'cancelled']
DomainAction = Literal['validate', 'submit', 'observe', 'complete', 'cancel']


class MonthlyJobSource(AuthorModel):
    kind: Literal['monthly_fixture']
    protocol_id: str = Field(pattern=r'^protocol_[0-9a-f]{64}$')
    protocol_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    config: MonthlyConfig


class AuthorStudyJobSource(AuthorModel):
    kind: Literal['author_study_diagnostic']
    study_id: str = Field(pattern=r'^author_study_[0-9a-f]{64}$')
    study_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


DomainSource = Annotated[MonthlyJobSource | AuthorStudyJobSource, Field(discriminator='kind')]


class DomainJobCreate(AuthorModel):
    source: DomainSource
    budget: JobBudget
    idempotency_key: str = Field(min_length=1, max_length=128)
    note: str = Field(default='', max_length=4000)


class DomainJobAdvance(AuthorModel):
    action: DomainAction
    idempotency_key: str = Field(min_length=1, max_length=128)


class DomainJobStep(AuthorModel):
    id: str
    sequence: int
    action: DomainAction
    idempotency_key: str
    request_digest: str
    effect_key: str
    status: Literal['running', 'completed', 'failed', 'interrupted']
    started_at: str
    finished_at: str | None
    elapsed_seconds: float
    next_state: DomainState
    error: JobError | None
    output_json: str | None


class DomainJobUsage(AuthorModel):
    steps: int
    failures: int
    elapsed_seconds: float


class DomainJobSummary(AuthorModel):
    id: str
    digest: str
    created_at: str
    source_kind: DomainKind
    source_id: str
    source_digest: str
    budget: JobBudget
    usage: DomainJobUsage
    state: DomainState
    stop_reason: str | None
    experiment_id: str | None
    attempt_id: str | None
    provider_connected: Literal[False]
    semantic_fidelity: Literal['unverified']


class DomainResearchJob(DomainJobSummary):
    source: DomainSource
    note: str
    context_json: str
    steps: list[DomainJobStep]
    result_json: str | None


class DomainJobPage(AuthorModel):
    items: list[DomainJobSummary]
    total: int
    limit: int
    offset: int
