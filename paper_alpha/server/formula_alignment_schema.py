"""Exact-target structural tool facts; never human or economic judgments."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .evidence_lineage_schema import LineageTarget

SHA = r'^[0-9a-f]{64}$'

class FormulaCitation(AuthorModel):
    evidence_id: str = Field(min_length=1, max_length=64)
    page: int = Field(ge=1, le=500)

class FormulaReference(AuthorModel):
    status: Literal['identified', 'unavailable', 'ambiguous', 'unsupported']
    reason_code: str
    mode: Literal['standalone', 'labelled_alpha', 'none']
    alpha_number: int | None
    expression: str | None = Field(default=None, max_length=4096)
    citations: list[FormulaCitation] = Field(max_length=100)

class FormulaComparison(AuthorModel):
    status: Literal['exact', 'different', 'unknown']
    reason_code: str

class FormulaWindow(AuthorModel):
    operator: str = Field(max_length=64)
    rows: int = Field(ge=0, le=252)

class FormulaFeatures(AuthorModel):
    fields: list[str] = Field(max_length=256)
    operators: list[str] = Field(max_length=256)
    windows: list[FormulaWindow] = Field(max_length=256)
    constants: list[str] = Field(max_length=256)
    signature_digest: str = Field(pattern=SHA)

class FormulaDifferences(AuthorModel):
    reference: FormulaFeatures | None
    declared: FormulaFeatures | None
    added_fields: list[str] = Field(max_length=256)
    removed_fields: list[str] = Field(max_length=256)
    added_operators: list[str] = Field(max_length=256)
    removed_operators: list[str] = Field(max_length=256)
    window_changed: bool | None
    constants_changed: bool | None
    structure_changed: bool | None
    outer_transform: Literal['rank', 'negation'] | None

class FormulaCandidate(AuthorModel):
    hypothesis_id: str = Field(min_length=1, max_length=64)
    origin: Literal['paper_original', 'user_modification', 'model_conjecture']
    submitted_expression: str = Field(min_length=1, max_length=2048)
    executed_expression: str | None = Field(default=None, max_length=2048)
    declared_changes: list[str] = Field(max_length=100)

class FormulaFieldDeclarations(AuthorModel):
    used: list[str] = Field(max_length=256)
    required: list[str] = Field(max_length=100)
    undeclared: list[str] = Field(max_length=256)
    declared_unused: list[str] = Field(max_length=100)

class FormulaExecution(AuthorModel):
    status: Literal['available', 'unavailable']
    operator_semantics_version: str | None = Field(default=None, max_length=200)
    split: str | None = Field(default=None, max_length=64)
    signal: str | None = Field(default=None, max_length=4096)
    entry: str | None = Field(default=None, max_length=4096)
    exit: str | None = Field(default=None, max_length=4096)
    label: str | None = Field(default=None, max_length=4096)
    weights: str | None = Field(default=None, max_length=4096)
    rank_ic: str | None = Field(default=None, max_length=4096)
    data_kind: str | None = Field(default=None, max_length=64)
    price_units: str | None = Field(default=None, max_length=4096)
    volume_units: str | None = Field(default=None, max_length=4096)

class FormulaCheck(AuthorModel):
    dimension: Literal['expression_legality', 'formula_reference', 'field_declarations', 'execution_identity', 'attribution_declarations']
    status: Literal['passed', 'failed', 'warning', 'unknown']
    code: str
    message: str = Field(max_length=2000)

class FormulaAlignmentResponse(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['run_formula_alignment']
    target: LineageTarget
    policy_version: Literal['bounded-formula-structure-v1-decimal-literals']
    reference: FormulaReference
    candidate: FormulaCandidate
    reference_to_declared: FormulaComparison
    declared_to_executed: FormulaComparison
    features: FormulaDifferences
    field_declarations: FormulaFieldDeclarations
    execution: FormulaExecution
    checks: list[FormulaCheck] = Field(max_length=5)
    unassessed_semantics: list[Literal['economic_mechanism', 'natural_language_direction', 'field_units', 'calendar_and_missing_policy', 'algebraic_equivalence', 'overall_hypothesis_fidelity']] = Field(max_length=6)
    semantic_fidelity: Literal['unverified']
    semantic_quality_score: None
    human_review_written: Literal[False]
    llm_api_called: Literal[False]
    public_projection_redacted: bool
    public_view_digest: str = Field(pattern=SHA)
