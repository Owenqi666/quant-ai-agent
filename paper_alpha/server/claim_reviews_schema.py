"""Exact-version semantic declarations, separate from immutable automation drafts."""
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..author_panel_schema import AuthorModel
from .research_assessments import AssessmentDimensions, parse_timestamp
from .research_cases_schema import CaseDefinition, CaseEvidence, CaseProvenance, CaseResultKind, CaseState, SourceKind
from .research_claims_schema import ResolvedClaim


class ClaimReviewResultContext(AuthorModel):
    id: str = Field(min_length=1, max_length=200)
    kind: CaseResultKind
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    summary: str = Field(max_length=4000)


class ClaimReviewTarget(AuthorModel):
    schema_version: Literal[1]
    claims_id: str = Field(pattern=r'^research_claims_[0-9a-f]{64}$')
    claims_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    claim_id: str = Field(min_length=1, max_length=120)
    claim_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    case_id: str = Field(pattern=r'^research_case_[0-9a-f]{64}$')
    case_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_kind: SourceKind
    source_id: str = Field(min_length=1, max_length=200)
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    case_context_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    claim: ResolvedClaim
    evidence: list[CaseEvidence] = Field(max_length=100)
    definitions: list[CaseDefinition] = Field(max_length=100)
    results: list[ClaimReviewResultContext] = Field(min_length=1, max_length=20)
    provenance: list[CaseProvenance] = Field(max_length=100)
    data_scope: str = Field(max_length=4000)
    method_scope: str = Field(max_length=4000)
    case_state: CaseState
    stop_reason: str | None = Field(max_length=4000)
    case_limitations: list[str] = Field(max_length=100)
    original_claim_limitations: list[str] = Field(max_length=100)
    limitations: list[str]
    target_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class ClaimReviewPreviewRequest(AuthorModel):
    claims_id: str = Field(pattern=r'^research_claims_[0-9a-f]{64}$')
    claim_id: str = Field(min_length=1, max_length=120)
    expected_target_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    source: Literal['human', 'automation']
    reviewer: str = Field(min_length=1, max_length=120)
    confirmed_at: str = Field(min_length=20, max_length=40)
    dimensions: AssessmentDimensions
    supersedes_id: str | None = Field(default=None, pattern=r'^claim_review_[0-9a-f]{64}$')
    expected_supersedes_digest: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @field_validator('claim_id', 'reviewer')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Claim and reviewer identities must not be blank')
        return value

    @field_validator('confirmed_at')
    @classmethod
    def aware(cls, value):
        parse_timestamp(value)
        return value

    @model_validator(mode='after')
    def parent_binding(self):
        if (self.supersedes_id is None) != (self.expected_supersedes_digest is None):
            raise ValueError('Replacement requires both exact parent ID and digest')
        return self


class ClaimReviewCreate(ClaimReviewPreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator('idempotency_key')
    @classmethod
    def nonblank_key(cls, value):
        if not value.strip():
            raise ValueError('Review key must not be blank')
        return value


class ClaimReviewPreview(AuthorModel):
    schema_version: Literal[1]
    target: ClaimReviewTarget
    source: Literal['human', 'automation']
    reviewer: str = Field(min_length=1, max_length=120)
    confirmed_at: str = Field(min_length=20, max_length=40)
    dimensions: AssessmentDimensions
    supersedes_id: str | None
    supersedes_digest: str | None
    declared_semantic_status: Literal['passed', 'failed', 'incomplete']
    identity_scope: Literal['local_declaration_not_authenticated']
    original_result_approval: Literal[False]
    software_verified_semantic_truth: Literal[False]
    semantic_quality_score: None
    limitations: list[str]


class ClaimReviewDetail(ClaimReviewPreview):
    id: str = Field(pattern=r'^claim_review_[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at: str


class ClaimReviewPage(AuthorModel):
    items: list[ClaimReviewDetail]
    total: int
    limit: int
    offset: int


class ClaimReviewStatus(AuthorModel):
    schema_version: Literal[1]
    target: ClaimReviewTarget
    records: int
    human_records: int
    automation_records: int
    superseded_records: int
    active_human_review_ids: list[str]
    active_automation_review_ids: list[str]
    human_declared_status: Literal['pending', 'passed', 'failed', 'incomplete', 'conflicting']
    unknown_dimensions: list[str]
    semantic_quality_score: None
    original_result_approval: Literal[False]
    software_verified_semantic_truth: Literal[False]
    limitations: list[str]
