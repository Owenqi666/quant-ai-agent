"""Closed claim drafts and server-resolved result references; no approval authority."""
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel

Attribution = Literal['paper_original', 'author_code', 'user_modification', 'model_conjecture', 'project_convention', 'unresolved']
ClaimKind = Literal['evidence_statement', 'interpretation', 'project_rule', 'metric']


class MetricReference(AuthorModel):
    case_id: str = Field(pattern=r'^research_case_[0-9a-f]{64}$')
    case_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    result_id: str = Field(min_length=1, max_length=200)
    result_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    pointer: str = Field(min_length=2, max_length=500)


class ClaimDraft(AuthorModel):
    id: str = Field(min_length=1, max_length=120)
    kind: ClaimKind
    attribution: Attribution
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(max_length=20)
    metric_references: list[MetricReference] = Field(max_length=10)

    @model_validator(mode='after')
    def shape(self):
        if not self.id.strip() or not self.text.strip():
            raise ValueError('Claim identity and text must not be blank')
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('Evidence identities must be unique')
        if (self.kind == 'metric') != bool(self.metric_references):
            raise ValueError('Only metric claims must carry metric references')
        return self


class ClaimPreviewRequest(AuthorModel):
    case_id: str = Field(pattern=r'^research_case_[0-9a-f]{64}$')
    case_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    claims: list[ClaimDraft] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def unique_claims(self):
        if len({claim.id for claim in self.claims}) != len(self.claims):
            raise ValueError('Claim identities must be unique')
        return self


class ClaimCreate(ClaimPreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def nonblank_key(self):
        if not self.idempotency_key.strip():
            raise ValueError('Idempotency key must not be blank')
        return self


class ResolvedMetric(MetricReference):
    value: int | float
    display: str = Field(max_length=1000)
    verification: Literal['verified_result_value']


class ResolvedClaim(AuthorModel):
    id: str
    kind: ClaimKind
    attribution: Attribution
    narrative_text: str = Field(max_length=4000)
    evidence_ids: list[str]
    evidence_verifications: list[str]
    metrics: list[ResolvedMetric]
    authoritative_display: str | None
    citation_integrity: Literal['verified', 'not_applicable']
    semantic_fidelity: Literal['unverified']
    status: Literal['draft']
    actor: Literal['automation']


class ClaimPreview(AuthorModel):
    case_id: str
    case_digest: str
    claims: list[ResolvedClaim]
    limitations: list[str]


class ClaimDetail(ClaimPreview):
    id: str = Field(pattern=r'^research_claims_[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at: str
    submitted_claims: list[ClaimDraft]


class ClaimSummary(AuthorModel):
    id: str
    digest: str
    case_id: str
    case_digest: str
    created_at: str
    count: int
    semantic_fidelity: Literal['unverified']


class ClaimPage(AuthorModel):
    items: list[ClaimSummary]
    total: int
    limit: int
    offset: int
