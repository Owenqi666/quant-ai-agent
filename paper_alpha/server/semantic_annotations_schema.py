"""Closed declarations: material labels have no experiment/claim authority."""
from typing import Literal
from pydantic import Field, field_validator

from ..author_panel_schema import AuthorModel
from ..semantic_materials import Annotation, AssessmentDimensions


class MaterialSource(AuthorModel):
    source_id: str
    path: str
    sha256: str
    media_type: str
    verification: Literal['bundled_source_digest_verified']
    raw_pdf_reverified: bool


class SemanticMaterialDetail(AuthorModel):
    schema_version: Literal[1]
    material_sha256: str
    case_id: str
    material_case_digest: str
    proposal: dict
    evidence: list[dict]
    review_focus: list[str]
    reference_draft: str
    reference_status: Literal['unverified_reference_draft']
    sources: list[MaterialSource]
    original_confirmation: dict
    scope: str


class SemanticMaterialCollection(AuthorModel):
    schema_version: Literal[1]
    material_sha256: str
    items: list[SemanticMaterialDetail]
    total: int
    limitations: list[str]


class SemanticAnnotationPreviewRequest(Annotation):
    material_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source: Literal['human', 'automation']
    supersedes_id: str | None = Field(default=None, pattern=r'^semantic_annotation_[0-9a-f]{64}$')


class SemanticAnnotationCreate(SemanticAnnotationPreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator('idempotency_key')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Annotation key must not be blank')
        return value


class SemanticAnnotationPreview(AuthorModel):
    schema_version: Literal[1]
    material_sha256: str
    case_id: str
    material_case_digest: str
    source: Literal['human', 'automation']
    reviewer: str
    confirmed_at: str
    dimensions: AssessmentDimensions
    supersedes_id: str | None
    supersedes_digest: str | None
    declared_semantic_status: Literal['passed', 'failed', 'incomplete']
    identity_scope: Literal['local_declaration_not_authenticated']
    original_result_approval: Literal[False]
    claim_approval: Literal[False]
    software_verified_semantic_truth: Literal[False]
    limitations: list[str]


class SemanticAnnotationDetail(SemanticAnnotationPreview):
    id: str = Field(pattern=r'^semantic_annotation_[0-9a-f]{64}$')
    digest: str
    created_at: str


class SemanticAnnotationPage(AuthorModel):
    items: list[SemanticAnnotationDetail]
    total: int
    limit: int
    offset: int


class SemanticCaseCoverage(AuthorModel):
    case_id: str
    material_case_digest: str
    active_human_annotation_ids: list[str]
    human_declared_status: Literal['pending', 'passed', 'failed', 'incomplete', 'conflicting']
    unknown_dimensions: list[str]


class SemanticAnnotationSummary(AuthorModel):
    schema_version: Literal[1]
    material_sha256: str
    reviewer: str | None
    material_cases: int
    records: int
    human_records: int
    automation_records: int
    active_human_records: int
    superseded_records: int
    human_annotated_cases: int
    human_fully_assessed_cases: int
    pending_cases: int
    conflicting_cases: int
    semantic_quality_score: None
    status: Literal['pending_human_confirmation', 'partial_human_declarations', 'human_declarations_available']
    cases: list[SemanticCaseCoverage]
    limitations: list[str]
