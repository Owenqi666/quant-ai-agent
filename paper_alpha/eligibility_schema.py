"""Strict source-specific research eligibility contracts, independent of daily ID."""
from typing import Literal

from pydantic import Field, model_validator

from .author_panel_schema import AuthorModel, AuthorSource


class EligibilityPlan(AuthorModel):
    schema_version: Literal[1]
    source_file: Literal['IntnlData.mat', 'USData.mat']
    development_start: str
    development_end: str
    reserved_from: str
    task: Literal['momentum', 'momentum_dgw']
    requires_market_cap: bool
    minimum_assets: int = Field(ge=1, le=50000)
    threshold_origin: Literal['project_screen', 'table8_initial_upper_bound']
    rationale: str = Field(min_length=1, max_length=2000)


class EligibilityMonth(AuthorModel):
    month: str
    patterns: list[int] = Field(min_length=8, max_length=8)
    momentum_missing: int = Field(ge=0)
    momentum_invalid: int = Field(ge=0)
    momentum_unrepresentable: int = Field(ge=0)


class EligibilityScan(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['author_eligibility_scan']
    semantics_version: Literal['gjs-author-eligibility-v1']
    source: AuthorSource
    plan: EligibilityPlan
    plan_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    months: list[EligibilityMonth] = Field(min_length=1, max_length=180)


class StudyCitation(AuthorModel):
    id: str
    origin: Literal['paper', 'author_code', 'project']
    locator: str
    claim: str


class StudyEvidence(AuthorModel):
    paper_id: str
    doi: str
    pdf_sha256: str
    version: str
    citations: list[StudyCitation]


class EligibilityResultMonth(AuthorModel):
    month: str
    assets: int
    momentum_ready: int
    momentum_dgw_ready: int
    momentum_mv_ready: int
    momentum_dgw_mv_ready: int
    selected_ready: int
    threshold_met: bool
    momentum_missing: int
    momentum_invalid: int
    momentum_unrepresentable: int


class EligibilitySummary(AuthorModel):
    months: int
    months_meeting_threshold: int
    min_selected: int
    max_selected: int
    status: Literal['screen_passed', 'screen_blocked']


class EligibilityResult(AuthorModel):
    schema_version: Literal[1]
    semantics_version: Literal['gjs-author-eligibility-v1']
    input_digest: str
    plan_digest: str
    source: AuthorSource
    plan: EligibilityPlan
    evidence: StudyEvidence
    months: list[EligibilityResultMonth]
    summary: EligibilitySummary
    limitations: list[str]
    execution_ready: Literal[False]
    verification_scope: Literal['aggregate_consistency_only']

    @model_validator(mode='before')
    @classmethod
    def no_execution_claim(cls, value):
        if isinstance(value, dict) and value.get('execution_ready') is not False:
            raise ValueError('An eligibility screen cannot grant portfolio execution')
        return value
