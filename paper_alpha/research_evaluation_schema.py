"""Exact research samples and bounded deterministic evaluation projections."""
from typing import Literal

from pydantic import Field

from .author_panel_schema import AuthorModel
from .server.review_materials_schema import ReviewMaterialsDetail
from .server.research_claims_schema import ResolvedMetric


class EvaluationImplementation(AuthorModel):
    engine: Literal['research-contract-evaluation-v1']
    source_sha256: dict[str, str]
    python_version: str = Field(max_length=100)
    pydantic_version: str = Field(max_length=100)


class ResearchSample(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['research_evaluation_sample']
    scope: Literal['exact_development_case_and_claim']
    material: ReviewMaterialsDetail
    implementation: EvaluationImplementation
    model_execution: Literal['model_not_run']
    id: str = Field(pattern=r'^research_sample_[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class EvaluationCheck(AuthorModel):
    name: str = Field(max_length=120)
    status: Literal['passed', 'not_checked']
    detail: str = Field(max_length=2000)


class ReviewReference(AuthorModel):
    id: str = Field(pattern=r'^claim_review_[0-9a-f]{64}$')
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class ReferenceDimension(AuthorModel):
    dimension: str = Field(max_length=100)
    status: Literal['eligible', 'pending', 'unknown', 'conflicting']
    outcome: Literal['passed', 'failed'] | None
    declared_outcomes: list[str] = Field(max_length=4)
    references: list[ReviewReference] = Field(max_length=500)


class HumanReference(AuthorModel):
    scope: Literal['exact_target_local_human_declarations_not_authenticated']
    status: Literal['pending', 'unknown', 'conflicting', 'available']
    total_records: int = Field(ge=0, le=500)
    human_records: int = Field(ge=0, le=500)
    automation_records: int = Field(ge=0, le=500)
    active_human: list[ReviewReference] = Field(max_length=500)
    active_automation: list[ReviewReference] = Field(max_length=500)
    superseded: list[ReviewReference] = Field(max_length=500)
    dimensions: list[ReferenceDimension] = Field(min_length=5, max_length=5)
    eligible_dimensions: int = Field(ge=0, le=5)
    pending_dimensions: int = Field(ge=0, le=5)
    unknown_dimensions: int = Field(ge=0, le=5)
    conflicting_dimensions: int = Field(ge=0, le=5)


class DeclarationComparison(AuthorModel):
    metric: Literal['local_declaration_agreement_not_model_accuracy']
    status: Literal['not_evaluated_no_prediction', 'no_comparable_dimensions', 'available']
    comparable_dimensions: int = Field(ge=0, le=5)
    matched_dimensions: int = Field(ge=0, le=5)
    declaration_agreement_rate: float | None
    details: list['DeclarationDimension'] = Field(min_length=5, max_length=5)


class DeclarationDimension(AuthorModel):
    dimension: str = Field(max_length=100)
    reference_outcome: Literal['passed', 'failed'] | None
    automation_declared_outcomes: list[Literal['passed', 'failed', 'not_assessed', 'not_applicable']] = Field(max_length=4)
    comparable: bool
    matched: bool | None
    excluded_reason: str | None = Field(max_length=200)


class ResearchEvaluation(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['research_sample_evaluation']
    sample_id: str = Field(pattern=r'^research_sample_[0-9a-f]{64}$')
    sample_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    material_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    target_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    domain: Literal['daily_alpha101', 'industry_mom']
    technical_status: Literal['passed', 'incomplete']
    checks: list[EvaluationCheck] = Field(min_length=1, max_length=20)
    metrics: list[ResolvedMetric] = Field(max_length=10)
    human_reference: HumanReference
    declaration_comparison: DeclarationComparison
    model_execution: Literal['model_not_run']
    semantic_quality_score: None
    model_accuracy: None
    source_integrity_scope: Literal['source_bytes_and_exact_snapshot_consistency', 'source_bytes_not_checked']
    numerical_reference_scope: Literal['frozen_result_pointer_values_only']
    reproduction: Literal['not_rerun']
    reserved_evaluated: Literal[False]
    limitations: list[str] = Field(max_length=30)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
