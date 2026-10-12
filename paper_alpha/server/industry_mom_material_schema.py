"""Bounded public contracts for explicitly reviewed fixed monthly MOM plans."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from .provider_industry_mom_bridge import MonthlyMomReview
from .paper_materials_schema import MaterialEvidence, AcceptedMaterialHypothesis
from .industry_mom_schema import IndustryMomVerification, Status

SHA = r'^[a-f0-9]{64}$'
PLAN_ID = r'^industry_mom_plan_[a-f0-9]{64}$'
SOURCE_ID = r'^industry_mom_source_[a-f0-9]{64}$'
Hash = Annotated[str, Field(pattern=SHA)]
PlanId = Annotated[str, Field(pattern=PLAN_ID)]
SmallText = Annotated[str, Field(min_length=1, max_length=4000)]
Key = Annotated[str, Field(min_length=1, max_length=128)]

class IndustryMomMaterialPreview(AuthorModel):
    review: MonthlyMomReview
    source_id: str = Field(pattern=SOURCE_ID)
    source_digest: Hash
    idempotency_key: Key | None = None

    @model_validator(mode='after')
    def key_shape(self):
        if self.idempotency_key is not None and self.idempotency_key.strip() != self.idempotency_key:
            raise ValueError('Idempotency key must not contain outer whitespace')
        return self

class IndustryMomMaterialCreate(IndustryMomMaterialPreview):
    expected_plan_id: PlanId
    expected_plan_digest: Hash
    idempotency_key: Key

class IndustryMomMaterialStart(AuthorModel):
    expected_plan_id: PlanId
    expected_plan_digest: Hash
    confirm_start: Literal[True]
    idempotency_key: Key

    @model_validator(mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if isinstance(value, dict) and value.get('confirm_start') is not True:
            raise ValueError('Starting an experiment requires explicit true confirmation')
        return value

    @model_validator(mode='after')
    def key_shape(self):
        if self.idempotency_key.strip() != self.idempotency_key:
            raise ValueError('Idempotency key must not contain outer whitespace')
        return self

class IndustryMomSignal(AuthorModel):
    window_months: Literal[11]
    skip_months: Literal[1]
    missing_policy: Literal['complete']
    fill_policy: Literal['none']

class IndustryMomPlanCandidate(AuthorModel):
    id: Literal['fixed_industry_mom_11_skip1']
    hypothesis_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    origin: Literal['user_modification']
    expression_kind: Literal['structured_monthly_plan']
    signal: IndustryMomSignal
    formation_offsets: list[int] = Field(min_length=11, max_length=11)
    frequency: Literal['calendar_month']
    return_semantics: Literal['monthly_total_return_decimal']
    changes: list[SmallText] = Field(min_length=3, max_length=3)

class IndustryMomPlanMaterialSource(AuthorModel):
    paper_id: str = Field(min_length=1, max_length=128)
    paper_sha256: Hash
    document_digest: Hash
    selection_digest: Hash
    proposal_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    accepted_response_digest: Hash

class IndustryMomPlanProviderOrigin(AuthorModel):
    proposal_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    accepted_response_digest: Hash
    context_digest: Hash
    raw_response_sha256: Hash
    receipt_digest: Hash
    fixture: bool
    model: str = Field(min_length=1, max_length=128)
    config_digest: Hash

class IndustryMomPlanChange(AuthorModel):
    path: str = Field(min_length=1, max_length=256)
    before_json: str = Field(max_length=262144)
    after_json: str = Field(max_length=262144)

class IndustryMomPlanMaterial(AuthorModel):
    id: str = Field(pattern=r'^material_[a-f0-9]{64}$')
    version_id: str = Field(pattern=r'^material_version_[a-f0-9]{64}$')
    version_digest: Hash
    draft_digest: Hash
    source: IndustryMomPlanMaterialSource
    provider_origin: IndustryMomPlanProviderOrigin
    hypothesis: AcceptedMaterialHypothesis
    evidence: list[MaterialEvidence] = Field(min_length=1, max_length=30)
    changes: list[IndustryMomPlanChange] = Field(max_length=100)
    semantic_fidelity: Literal['unverified']

class IndustryMomWindowAnchor(AuthorModel):
    id: str = Field(min_length=1, max_length=128)
    document_sha256: Hash
    origin: Literal['paper_original']
    page_1_based: int = Field(ge=1, le=500)
    printed_page: int | str | None = Field(default=None)
    quote: str = Field(min_length=1, max_length=4096)
    normalized_start: int = Field(ge=0, le=2_000_000)
    normalized_end_exclusive: int = Field(ge=1, le=2_000_000)
    literal_match: Literal[True]
    normalization: str = Field(min_length=1, max_length=300)
    semantic_fidelity: Literal['requires_human_review']
    visually_inspected_by: str = Field(min_length=1, max_length=128)
    section: str | None = Field(default=None, max_length=128)

class IndustryMomPlanSource(AuthorModel):
    id: str = Field(pattern=SOURCE_ID)
    digest: Hash
    archive_sha256: Hash
    method_digest: Hash
    config_digest: Hash
    input_digest: Hash
    panel_digest: Hash
    manifest_digest: Hash

class IndustryMomPlanMonthRange(AuthorModel):
    start: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    end: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')

class IndustryMomPlanConfig(AuthorModel):
    schema_version: Literal[1]
    study_id: Literal['ff49-industry-mom-project-v1']
    research_scope: Literal['project_modification']
    source_contract: Literal['kenneth-french-49-value-weighted-monthly-v1']
    development: IndustryMomPlanMonthRange
    reserved: IndustryMomPlanMonthRange
    signal: IndustryMomSignal
    min_formation_assets: Literal[30]
    portfolio: Literal['tercile_equal_weight_gross1_net0']
    baseline: Literal['same_sample_equal_weight_long_only']
    cost_model: Literal['gross_only_no_cost_model']

class IndustryMomMaterialPlan(AuthorModel):
    schema_version: Literal[1]
    plan_id: PlanId
    plan_digest: Hash
    bridge_version: Literal['reviewed-material-fixed-industry-mom-v1']
    construction: Literal['host_owned_known_monthly_method_from_explicitly_reviewed_model_material']
    candidate: IndustryMomPlanCandidate
    review: MonthlyMomReview
    review_digest: Hash
    material: IndustryMomPlanMaterial
    window_anchors: list[IndustryMomWindowAnchor] = Field(min_length=1, max_length=30)
    industry_source: IndustryMomPlanSource
    config: IndustryMomPlanConfig
    method_digest: Hash
    reserved_evaluated: Literal[False]
    original_single_stock_plan_unlocked: Literal[False]
    model_generated_monthly_dsl: Literal[False]
    human_satisfaction_measured: Literal[False]
    public_projection_redacted: bool
    public_view_digest: Hash

class IndustryMomMaterialPlanCreated(AuthorModel):
    plan: IndustryMomMaterialPlan
    replayed: bool

class IndustryMomMaterialStarted(AuthorModel):
    plan_id: PlanId
    plan_digest: Hash
    experiment_id: str = Field(min_length=1, max_length=128)
    replayed: bool

class IndustryMomMaterialPlanSummary(AuthorModel):
    plan_id: PlanId
    plan_digest: Hash
    material_id: str = Field(pattern=r'^material_[a-f0-9]{64}$')
    version_id: str = Field(pattern=r'^material_version_[a-f0-9]{64}$')
    hypothesis_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    source_id: str = Field(pattern=SOURCE_ID)
    reviewer_id: str = Field(min_length=1, max_length=100)
    review_source: Literal['codex_proxy', 'user']
    experiment_id: str | None = Field(max_length=128)
    status: Status | Literal['not_started', 'handoff_pending']

class IndustryMomMaterialPlanPage(AuthorModel):
    items: list[IndustryMomMaterialPlanSummary] = Field(max_length=20)
    total: int = Field(ge=0, le=256)
    limit: int = Field(ge=1, le=20)
    offset: int = Field(ge=0, le=255)

class IndustryMomMaterialLineageState(AuthorModel):
    experiment_id: str | None = Field(max_length=128)
    status: Status | Literal['not_started', 'handoff_pending']
    attempt_id: str | None = Field(max_length=128)
    result_digest: Hash | None
    report_sha256: Hash | None
    verification: IndustryMomVerification | None
    error: str | None = Field(max_length=4000)

class IndustryMomMaterialLineage(AuthorModel):
    plan: IndustryMomMaterialPlan
    lineage: IndustryMomMaterialLineageState

class IndustryMomExperimentMaterialLineage(AuthorModel):
    experiment_id: str = Field(min_length=1, max_length=128)
    linked: bool
    items: list[IndustryMomMaterialLineage] = Field(max_length=8)
