"""Read-only exact-target material projections; no judgment or write DTO."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .claim_reviews_schema import ClaimReviewDetail, ClaimReviewStatus, ClaimReviewTarget
from .research_cases_schema import CaseDetail
from .research_claims_schema import ClaimDetail


class ReviewMaterialSource(AuthorModel):
    id: str = Field(pattern=r'^(alpha101-paper|industry-paper|industry-method|industry-config|industry-author-(main|SetupDataA|SetupDataB|Table1|Table8A|Table8B))$')
    label: str = Field(min_length=1, max_length=200)
    filename: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,120}$')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size: int = Field(ge=1, le=16777216)
    media_type: Literal['application/pdf', 'application/json', 'text/plain']
    download_url: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(max_length=100)


class ReviewMaterialsDetail(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['exact_claim_review_materials']
    case: CaseDetail
    claims: ClaimDetail
    target: ClaimReviewTarget
    review_status: ClaimReviewStatus
    reviews: list[ClaimReviewDetail] = Field(max_length=500)
    sources: list[ReviewMaterialSource] = Field(min_length=1, max_length=16)
    human_judgments: None
    semantic_quality_score: None
    llm_api_called: Literal[False]
    limitations: list[str] = Field(max_length=30)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
