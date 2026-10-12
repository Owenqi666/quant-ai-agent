"""Closed public HTTP projections for the fixture-qualified research v2 chain."""
from typing import Literal
from pydantic import Field, model_serializer

from ..author_panel_schema import AuthorModel
from .paper_materials_schema import MaterialDraft, AcceptedMaterialDraft, PaperSelection
from .provider_api_schema import (ProposalStatus, PublicProviderError, PublicProviderReceipt,
                                  PublicProposalValidation, ProposalArtifactReference)
from .provider_research_schema import output_model, grounded_output_model
from .grounded_material_schema import GroundedMaterialDraft, AcceptedGroundedMaterialDraft
from .material_context import UnresolvedDisposition
from .provider_research_source_schema import ResearchSource
from .provider_host_schema import HostCapability
from .provider_schema import ProposalBudget
from .research_sessions_schema import (SessionBudget, SessionConfig, SessionUsage, SessionAction,
                                      SessionStatus, SessionChildren, SessionOutcome)

ResearchProviderOutput = output_model()
GroundedResearchProviderOutput = grounded_output_model()


class ResearchSourcePreviewRequest(AuthorModel):
    source: ResearchSource
    host_grant_id: str | None = Field(default=None, pattern=r'^host_grant_[a-f0-9]{64}$')
    provider_config_id: str = Field(min_length=1, max_length=128)


class ResearchV2SessionCreate(ResearchSourcePreviewRequest):
    expected_source_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_provider_config_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    purpose: str = Field(min_length=1, max_length=4000)
    budget: SessionBudget
    idempotency_key: str = Field(min_length=1, max_length=128)

class PublicResearchSource(AuthorModel):
    kind: Literal['paper_selection', 'accepted_material', 'research_revision']
    source_digest: str
    paper_id: str | None
    selection_digest: str | None
    material_id: str | None
    version_id: str | None
    material_digest: str | None
    research_id: str | None
    revision_id: str | None
    revision_digest: str | None


class ResearchSourcePreview(AuthorModel):
    schema_version: Literal[2]
    contract_id: Literal['research_provider_v2']
    source: PublicResearchSource
    config: SessionConfig
    allowed_operations: list[Literal['extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action']]
    real_dispatch_qualified: bool
    host_capability: HostCapability | None = None
    paper_selection: PaperSelection | None = None
    public_projection_redacted: bool


class PublicSessionReservation(AuthorModel):
    token: str
    sequence: int
    action: SessionAction
    effect_key: str
    action_digest: str
    source: PublicResearchSource
    created_at: str
    deadline_at: str
    charges: SessionUsage
    status: Literal['reserved', 'effect_recorded', 'finished']
    children: SessionChildren | None
    outcome: SessionOutcome | None


class PublicResearchSession(AuthorModel):
    schema_version: Literal[2]
    id: str
    state_digest: str
    version: int
    purpose: str
    config: SessionConfig
    created_at: str
    deadline_at: str
    root_source: PublicResearchSource
    current_source: PublicResearchSource
    budget: SessionBudget
    usage: SessionUsage
    state: SessionStatus
    effective_state: SessionStatus
    stop_reason: str | None
    remaining_seconds: float
    reservations: list[PublicSessionReservation]
    semantic_fidelity: Literal['unverified']
    execution_scope: Literal['provider_queue_gated', 'provider_and_experiment_gated']
    public_projection_redacted: bool


class ResearchSessionPage(AuthorModel):
    items: list[PublicResearchSession]
    total: int
    limit: int
    offset: int


class ResearchSessionCancel(AuthorModel):
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(min_length=1, max_length=128)


class ResearchSessionReconcile(AuthorModel):
    token: str = Field(min_length=1, max_length=128)


class PublicSessionReconciliation(AuthorModel):
    session: PublicResearchSession
    reservation: PublicSessionReservation
    replayed: bool
    observation: str | None


class ResearchV2ProposalSummary(AuthorModel):
    schema_version: Literal[2]
    id: str
    session_id: str
    operation: Literal['extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action']
    source_kind: Literal['paper_selection', 'accepted_material', 'research_revision']
    source_digest: str
    provider_config_id: str
    provider_mode: Literal['fixture', 'codex_cli']
    requested_model: str | None
    status: ProposalStatus
    candidate_limit: int
    created_at: str
    generation_deadline_at: str
    provider_invocations: int
    current_attempt_id: str | None
    accepted_attempt_id: str | None
    accepted_response_digest: str | None
    error: PublicProviderError | None


class ResearchV2ProposalPage(AuthorModel):
    items: list[ResearchV2ProposalSummary]
    total: int
    limit: int
    offset: int


class PublicResearchProposalAttempt(AuthorModel):
    id: str
    sequence: int
    status: ProposalStatus
    started_at: str
    finished_at: str | None
    error: PublicProviderError | None
    receipt: PublicProviderReceipt | None
    output: ResearchProviderOutput | GroundedResearchProviderOutput | None
    validation: PublicProposalValidation | None
    artifacts: list[ProposalArtifactReference]


class ResearchV2ProposalDetail(ResearchV2ProposalSummary):
    context_digest: str
    instruction: str
    budget: ProposalBudget
    accepted_response: ResearchProviderOutput | GroundedResearchProviderOutput | None
    attempts: list[PublicResearchProposalAttempt]
    artifacts: list[ProposalArtifactReference]
    public_projection_redacted: bool
    semantic_fidelity: Literal['unverified']


class MaterialAdoptRequest(AuthorModel):
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    edited_material: AcceptedMaterialDraft | AcceptedGroundedMaterialDraft | None = None
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class MaterialEngineeringRequest(AuthorModel):
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    # Explicit dispositions bind the original unresolved text. No source rule is deleted.
    unresolved_dispositions: list[UnresolvedDisposition] = Field(max_length=100)


class PublicMaterialChange(AuthorModel):
    path: str
    before_json: str
    after_json: str


class PublicMaterialVersion(AuthorModel):
    id: str
    material_id: str
    version: int
    digest: str
    draft_digest: str
    parent_version_id: str | None
    parent_digest: str
    material: AcceptedMaterialDraft | AcceptedGroundedMaterialDraft
    actor: Literal['user', 'automation']
    changes: list[PublicMaterialChange]
    citation_integrity: Literal['verified']
    semantic_fidelity: Literal['unverified']
    created_at: str
    public_projection_redacted: bool
    definition_repair: dict | None = None

    @model_serializer(mode='wrap')
    def preserve_legacy_projection(self, handler):
        value = handler(self)
        if self.definition_repair is None:
            value.pop('definition_repair', None)
        return value


class MaterialDefinitionRepairRequest(AuthorModel):
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class PublicMaterialDefinitionRepair(AuthorModel):
    version: PublicMaterialVersion | None
    status: str
    diagnostic: dict
    human_approval: Literal[False]
    human_review_status: Literal['pending_human']


class PublicMaterialEngineering(AuthorModel):
    version: PublicMaterialVersion
    method_policy: dict
    verification_scope: Literal['formula_engineering_only']
    human_review_status: Literal['pending_human']
    human_approval: Literal[False]


class PublicMaterialDiagnostic(AuthorModel):
    material_id: str
    material_digest: str
    source: dict
    status: str
    stop_reason: str | None
    details: dict
    automatic_checks: dict
    human_review: dict


class PublicMaterialSummary(AuthorModel):
    id: str
    paper_id: str
    title: str
    draft_digest: str
    current_digest: str
    latest_version_id: str | None
    semantic_fidelity: Literal['unverified']
    created_at: str


class PublicMaterialDetail(AuthorModel):
    id: str
    paper_id: str
    proposal_id: str
    attempt_id: str
    accepted_response_digest: str
    draft_digest: str
    current_digest: str
    selection: PaperSelection
    draft: MaterialDraft | GroundedMaterialDraft
    latest_version: PublicMaterialVersion | None
    citation_integrity: Literal['verified']
    semantic_fidelity: Literal['unverified']
    created_at: str
    public_projection_redacted: bool


class ResearchMaterialPage(AuthorModel):
    items: list[PublicMaterialSummary]
    total: int
    limit: int
    offset: int
