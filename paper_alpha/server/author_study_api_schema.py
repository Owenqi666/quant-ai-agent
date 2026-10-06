"""Strict author-study HTTP projections; no raw-MAT authentication claim."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from ..eligibility_schema import EligibilityPlan, EligibilityScan, EligibilityResult

StudyDecision = Literal['data_insufficient', 'rules_unresolved', 'implementation_error', 'accepted_with_limits']
PanelIdentity = Annotated[str, Field(pattern=r'^author_panel_[0-9a-f]{64}$')]


class StudyCreate(AuthorModel):
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=4000)
    scan: EligibilityScan
    parent_review_id: str | None = Field(pattern=r'^author_review_[0-9a-f]{64}$')
    author_panel_ids: list[PanelIdentity] = Field(max_length=8)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode='before')
    @classmethod
    def validate_raw_scan(cls, value):
        if isinstance(value, dict) and 'scan' in value:
            from ..eligibility import validate_scan
            validate_scan(value['scan'])
        return value


class StudyChange(AuthorModel):
    field: str
    before: str
    after: str


class StudyDetail(AuthorModel):
    id: str
    digest: str
    created_at: str
    title: str
    note: str
    scan: EligibilityScan
    result: EligibilityResult
    parent_review_id: str | None
    parent_study_id: str | None
    author_panel_ids: list[str]
    changes: list[StudyChange]
    verification_scope: Literal['aggregate_consistency_only']
    raw_source_reverified: Literal[False]


class StudyGateSummary(AuthorModel):
    months: int
    months_meeting_threshold: int
    min_selected: int
    max_selected: int
    status: Literal['screen_passed', 'screen_blocked']


class StudySummary(AuthorModel):
    id: str
    digest: str
    created_at: str
    title: str
    note: str
    plan: EligibilityPlan
    summary: StudyGateSummary
    parent_review_id: str | None
    parent_study_id: str | None
    verification_scope: Literal['aggregate_consistency_only']
    raw_source_reverified: Literal[False]


class StudyPage(AuthorModel):
    items: list[StudySummary]
    total: int
    limit: int
    offset: int


class StudyReviewCreate(AuthorModel):
    study_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    decision: StudyDecision
    note: str = Field(min_length=1, max_length=4000)
    actor: Literal['human', 'automation']
    idempotency_key: str = Field(min_length=1, max_length=128)


class StudyReview(AuthorModel):
    id: str
    digest: str
    created_at: str
    study_id: str
    study_digest: str
    decision: StudyDecision
    note: str
    actor: Literal['human', 'automation']


class StudyReviews(AuthorModel):
    items: list[StudyReview]
