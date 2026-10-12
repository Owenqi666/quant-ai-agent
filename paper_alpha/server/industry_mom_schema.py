"""Closed HTTP contracts for a fixed industry-portfolio development study."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel

SourceId = str
Status = Literal['queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted']


class IndustryMomAck(AuthorModel):
    experiment_id: str


class IndustryMomSourceSummary(AuthorModel):
    id: str
    digest: str
    title: str
    note: str
    created_at: str
    data_kind: Literal['market_derived_portfolio_returns']
    asset_kind: Literal['industry_portfolio']
    market_source_id: Literal['kenneth-french-49-industry-monthly-value-weighted']
    research_scope: Literal['project_modification']
    archive_sha256: str
    method_digest: str
    config_digest: str
    input_digest: str
    panel_digest: str
    result_digest: str
    manifest_digest: str
    code_digest: str
    verification_scope: Literal['local_bytes_and_independent_numeric_reference']


class IndustryMomSourceDetail(IndustryMomSourceSummary):
    config_json: str = Field(max_length=1048576)
    method_json: str = Field(max_length=1048576)
    source_json: str = Field(max_length=1048576)
    verification_json: str = Field(max_length=1048576)
    human_judgment: None
    reserved_evaluated: Literal[False]


class IndustryMomSourcePage(AuthorModel):
    items: list[IndustryMomSourceSummary]
    total: int
    limit: int
    offset: int


class IndustryMomCreate(AuthorModel):
    source_id: str = Field(pattern=r'^industry_mom_source_[0-9a-f]{64}$')
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: str = Field(min_length=1, max_length=128)


class IndustryMomCancel(AuthorModel):
    expected_attempt_id: str | None
    idempotency_key: str = Field(min_length=1, max_length=128)


class IndustryMomRetry(AuthorModel):
    expected_attempt_id: str
    idempotency_key: str = Field(min_length=1, max_length=128)


class IndustryMomExperimentSummary(AuthorModel):
    id: str
    source_id: str
    source_digest: str
    created_at: str
    updated_at: str
    status: Status
    attempt_count: int
    attempt_id: str | None
    worker_id: str | None
    error: str | None
    phase: Literal['preparing', 'executing', 'verifying', 'publishing'] | None
    input_digest: str
    config_digest: str
    max_seconds: Literal[60]
    max_attempts: Literal[3]
    research_scope: Literal['project_modification']


class IndustryMomStatus(AuthorModel):
    id: str
    status: Status
    attempt_id: str | None
    attempt_count: int
    phase: str | None
    change_token: str
    integrity_checked: Literal[False]


class IndustryMomAttempt(AuthorModel):
    id: str
    number: int
    worker_id: str
    status: Status
    started_at: str
    finished_at: str | None
    error: str | None
    result_digest: str | None
    verification_json: str | None


class IndustryMomVerification(AuthorModel):
    verified: Literal[True]
    calculation_verified: Literal[True]
    reference_passed: Literal[True]
    source_id: str
    source_digest: str
    source_input_digest: str
    source_manifest_digest: str
    attempt_id: str
    input_digest: str
    result_digest: str
    panel_digest: str
    manifest_digest: str
    code_digest: str
    environment_digest: str
    config_digest: str
    method_digest: str
    archive_sha256: str
    reserved_evaluated: Literal[False]
    human_review: Literal['pending']


class IndustryMomReviewTarget(AuthorModel):
    attempt_id: str
    result_digest: str


class IndustryMomExperimentDetail(AuthorModel):
    experiment: IndustryMomExperimentSummary
    source: IndustryMomSourceDetail
    attempts: list[IndustryMomAttempt] = Field(max_length=3)
    result_json: str | None = Field(max_length=2097152)
    reference_json: str | None = Field(max_length=1048576)
    report_markdown: str | None = Field(max_length=1048576)
    verification: IndustryMomVerification | None
    review_target: IndustryMomReviewTarget | None


class IndustryMomExperimentPage(AuthorModel):
    items: list[IndustryMomExperimentSummary]
    total: int
    limit: int
    offset: int
