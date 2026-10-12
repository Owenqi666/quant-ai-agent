"""Closed research-context projections shared by the UI and restricted tools."""
from typing import Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from .research_assessments import ResearchAssessment

SourceKind = Literal['daily_run', 'monthly_experiment', 'author_study', 'industry_mom_experiment']
CaseResultKind = Literal['daily_candidates', 'monthly_portfolio', 'author_eligibility', 'industry_mom_portfolio']
CaseAction = Literal['request_human_review', 'revise_plan', 'resolve_method', 'stop_data_insufficient']
CaseState = Literal['ready_for_review', 'data_insufficient', 'rules_unresolved', 'implementation_failed']


class CaseEvidence(AuthorModel):
    id: str = Field(min_length=1, max_length=200)
    origin: Literal['paper', 'author_code', 'project', 'unknown']
    locator: str = Field(max_length=4000)
    text: str = Field(max_length=16000)
    verification: Literal['literal_quote_verified', 'source_registry_reference', 'project_declaration']


class CaseDefinition(AuthorModel):
    id: str = Field(min_length=1, max_length=200)
    attribution: Literal['paper_original', 'user_modification', 'model_conjecture', 'project_convention', 'unresolved']
    text: str = Field(max_length=32000)
    evidence_ids: list[str] = Field(max_length=100)


class CaseProvenance(AuthorModel):
    label: str = Field(max_length=200)
    value: str = Field(max_length=32000)


class CaseResult(AuthorModel):
    id: str = Field(min_length=1, max_length=200)
    kind: CaseResultKind
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    summary: str = Field(max_length=4000)
    payload_json: str = Field(max_length=2097152)


class CaseAssessmentSummary(AuthorModel):
    semantic_status: Literal['not_assessed', 'not_human', 'failed', 'passed', 'incomplete']
    total_active_seconds: float
    timing_recorded: bool
    source_is_declared: Literal[True]


class CaseReview(AuthorModel):
    id: str = Field(max_length=200)
    actor: str = Field(max_length=100)
    decision: str = Field(max_length=100)
    scope: str = Field(max_length=1000)
    note: str = Field(max_length=4000)
    # v17 records did not project these fields. Absence remains historical
    # unknown; get() never backfills an assessment into their frozen bytes.
    assessment: ResearchAssessment | None = None
    assessment_summary: CaseAssessmentSummary | None = None


class CaseContext(AuthorModel):
    state: CaseState
    stop_reason: str | None = Field(max_length=4000)
    allowed_actions: list[CaseAction] = Field(min_length=1, max_length=4)
    evidence: list[CaseEvidence] = Field(max_length=100)
    definitions: list[CaseDefinition] = Field(max_length=100)
    data_scope: str = Field(max_length=4000)
    method_scope: str = Field(max_length=4000)
    provenance: list[CaseProvenance] = Field(max_length=100)
    results: list[CaseResult] = Field(min_length=1, max_length=20)
    reviews: list[CaseReview] = Field(max_length=1000)
    limitations: list[str] = Field(max_length=100)


class CasePreview(AuthorModel):
    source_kind: SourceKind
    source_id: str = Field(min_length=1, max_length=200)
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    context: CaseContext


class CaseCreate(AuthorModel):
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=4000)
    source_kind: SourceKind
    source_id: str = Field(min_length=1, max_length=200)
    source_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.title.strip() or not self.idempotency_key.strip():
            raise ValueError('Title and idempotency key must contain nonblank text')
        return self


class CaseDetail(CasePreview):
    id: str = Field(pattern=r'^research_case_[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at: str
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=4000)


class CaseSummary(AuthorModel):
    id: str
    digest: str
    created_at: str
    title: str
    note: str
    source_kind: SourceKind
    source_id: str
    source_digest: str
    state: CaseState
    stop_reason: str | None
    allowed_actions: list[CaseAction]


class CasePage(AuthorModel):
    items: list[CaseSummary]
    total: int
    limit: int
    offset: int
