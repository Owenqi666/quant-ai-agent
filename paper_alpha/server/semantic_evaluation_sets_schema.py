"""Closed reference snapshots and declaration agreement; no model score."""
from typing import Literal
from pydantic import Field, field_validator

from ..author_panel_schema import AuthorModel
from ..semantic_materials import AssessmentDimensions, Annotation
from .semantic_annotations_schema import SemanticAnnotationDetail, SemanticMaterialDetail

SHA = r'^[0-9a-f]{64}$'
SET_ID = r'^semantic_evaluation_set_[0-9a-f]{64}$'


class SemanticAnnotationReference(AuthorModel):
    id: str = Field(pattern=r'^semantic_annotation_[0-9a-f]{64}$')
    digest: str = Field(pattern=SHA)


class SemanticEvaluationSetPreviewRequest(AuthorModel):
    material_sha256: str = Field(pattern=SHA)
    case_ids: list[str] = Field(min_length=1, max_length=7)

    @field_validator('case_ids')
    @classmethod
    def distinct_cases(cls, values):
        if len(set(values)) != len(values) or any(not value.strip() or len(value) > 120 for value in values):
            raise ValueError('Select distinct explicit material cases')
        return sorted(values)


class SemanticEvaluationSetCreate(SemanticEvaluationSetPreviewRequest):
    expected_active_annotations: list[SemanticAnnotationReference] = Field(max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator('expected_active_annotations')
    @classmethod
    def distinct_references(cls, values):
        if len({value.id for value in values}) != len(values):
            raise ValueError('Active annotation references must be distinct')
        return sorted(values, key=lambda value: value.id)

    @field_validator('idempotency_key')
    @classmethod
    def nonblank_key(cls, value):
        if not value.strip():
            raise ValueError('Idempotency key cannot be blank')
        return value


class SemanticFrozenSource(AuthorModel):
    source_id: Literal['semantic_material', 'semantic_manifest', 'alpha101_paper', 'momentum_source_registry']
    path: str
    sha256: str = Field(pattern=SHA)
    media_type: Literal['application/json', 'application/pdf']
    content: str | None
    content_scope: Literal['bounded_utf8_text', 'digest_only_binary_not_exported']


class SemanticDimensionReference(AuthorModel):
    case_id: str
    dimension: Literal['evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution', 'field_semantics', 'implementation_alignment']
    status: Literal['eligible', 'pending', 'unknown', 'conflicting']
    outcome: Literal['passed', 'failed'] | None
    annotation_refs: list[SemanticAnnotationReference]
    declared_outcomes: list[Literal['passed', 'failed', 'not_assessed', 'not_applicable']]
    unknown_outcomes: list[Literal['not_assessed', 'not_applicable']]


class SemanticReferenceSummary(AuthorModel):
    selected_cases: int
    total_dimensions: int
    eligible_dimensions: int
    pending_dimensions: int
    unknown_dimensions: int
    conflicting_dimensions: int
    active_human_records: int
    human_history_records: int
    automation_audit_records: int


class SemanticEvaluationSetPreview(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['fixed_development_semantic_reference']
    material_sha256: str = Field(pattern=SHA)
    case_ids: list[str]
    material_snapshot: dict
    case_snapshots: list[SemanticMaterialDetail]
    source_snapshots: list[SemanticFrozenSource]
    annotation_history: list[SemanticAnnotationDetail]
    active_human_annotations: list[SemanticAnnotationReference]
    dimension_references: list[SemanticDimensionReference]
    summary: SemanticReferenceSummary
    scope: Literal['seven_fixed_development_materials_not_independent_final_test']
    identity_verified: Literal[False]
    software_verified_semantic_truth: Literal[False]
    semantic_quality_score: None
    limitations: list[str]


class SemanticEvaluationSetDetail(SemanticEvaluationSetPreview):
    id: str = Field(pattern=SET_ID)
    digest: str = Field(pattern=SHA)
    created_at: str


class SemanticEvaluationSetPage(AuthorModel):
    items: list[SemanticEvaluationSetDetail]
    total: int
    limit: int
    offset: int


class SemanticEvaluationCaseDeclaration(AuthorModel):
    case_id: str = Field(min_length=1, max_length=120)
    dimensions: AssessmentDimensions


class SemanticEvaluationComparisonCreate(AuthorModel):
    set_id: str = Field(pattern=SET_ID)
    set_digest: str = Field(pattern=SHA)
    source: Literal['human', 'automation']
    reviewer: str = Field(min_length=1, max_length=120)
    declared_at: str = Field(min_length=20, max_length=40)
    execution_reference: str = Field(min_length=1, max_length=1000)
    case_declarations: list[SemanticEvaluationCaseDeclaration] = Field(min_length=1, max_length=7)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator('reviewer', 'execution_reference', 'idempotency_key')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Explicit declaration fields cannot be blank')
        return value

    @field_validator('declared_at')
    @classmethod
    def timestamp(cls, value):
        # The existing validator supplies aware/nonfuture semantics; no labels
        # are generated or saved by this validation.
        Annotation.model_validate({'case_id': 'timestamp_validation', 'reviewer': 'validation',
                                  'confirmed_at': value,
                                  'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Timestamp validation.'}
                                                 for name in AssessmentDimensions.model_fields}})
        return value

    @field_validator('case_declarations')
    @classmethod
    def distinct_cases(cls, values):
        if len({value.case_id for value in values}) != len(values):
            raise ValueError('Every selected case requires one complete declaration')
        return sorted(values, key=lambda value: value.case_id)


class SemanticAgreementDimension(AuthorModel):
    case_id: str
    dimension: str
    reference_status: Literal['eligible', 'pending', 'unknown', 'conflicting']
    reference_outcome: Literal['passed', 'failed'] | None
    declared_outcome: Literal['passed', 'failed', 'not_assessed', 'not_applicable']
    comparable: bool
    agrees: bool | None
    excluded_reason: Literal['reference_pending', 'reference_unknown', 'reference_conflicting', 'declaration_unknown'] | None


class SemanticAgreementSummary(AuthorModel):
    metric: Literal['declaration_agreement_not_model_quality']
    matched_dimensions: int
    comparable_dimensions: int
    total_dimensions: int
    excluded_dimensions: int
    excluded_reasons: dict[str, int]
    declaration_agreement_rate: float | None
    semantic_quality_score: None


class SemanticEvaluationComparisonDetail(AuthorModel):
    schema_version: Literal[1]
    set_id: str
    set_digest: str
    source: Literal['human', 'automation']
    reviewer: str
    declared_at: str
    execution_reference: str
    case_declarations: list[SemanticEvaluationCaseDeclaration]
    dimension_results: list[SemanticAgreementDimension]
    summary: SemanticAgreementSummary
    identity_verified: Literal[False]
    execution_reference_verified: Literal[False]
    software_verified_semantic_truth: Literal[False]
    claim_approval: Literal[False]
    limitations: list[str]
    id: str = Field(pattern=r'^semantic_evaluation_comparison_[0-9a-f]{64}$')
    digest: str = Field(pattern=SHA)
    created_at: str


class SemanticEvaluationComparisonPage(AuthorModel):
    items: list[SemanticEvaluationComparisonDetail]
    total: int
    limit: int
    offset: int


class SemanticEvaluationBundle(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['semantic_evaluation_set_portable_integrity_bundle']
    reference: SemanticEvaluationSetDetail
    reference_metadata_digest: str = Field(pattern=SHA)
    comparisons: list[SemanticEvaluationComparisonDetail]
    comparison_metadata_digests: dict[str, str]
    manifest_digest: str = Field(pattern=SHA)
    binary_sources_included: Literal[False]
    scope: Literal['frozen_declared_reference_and_agreement_integrity_not_authenticated_authorship']
    limitations: list[str]
