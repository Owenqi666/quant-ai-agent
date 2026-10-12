"""Closed public correction DTOs; display records never grant host authority.

Legacy request/response classes remain unchanged. Requests validate their exact
JSON discriminator before Pydantic can normalize it; omitted version stays v1.
"""
from typing import Annotated, Literal, Union

from pydantic import BeforeValidator, Field, model_validator

from ..author_panel_schema import AuthorModel
from .provider_loop_api_schema import (
    LoopActionKind, LoopActionView, PublicResearchLoop,
)
from .provider_experiment_api_schema import PublicExperiment, ExperimentBootstrap
from .provider_research_api_schema import PublicResearchSession, PublicSessionReservation, ResearchV2ProposalSummary
from .provider_host_schema import HostCapability
from .research_correction_schema import (
    PolicyId, RuleId, NormalizationRuleId, Sha256, Identity, ProviderCorrectionAssociation,
)
from .research_loops_schema import LoopCreate, LoopCreateV2, parse_loop_create
from .research_session_experiments_schema import (
    ExperimentBudget, ExperimentCreate, ExperimentCreateV2, CorrectedCandidateSource, parse_experiment_create,
)
from .research_jobs_schema import ResearchJobSummary, JobCandidateCheck
from .research_sessions_schema import ResearchDecision
from .provider_api_schema import ProposalStatus

LoopCreateRequest = Annotated[Union[LoopCreate, LoopCreateV2], BeforeValidator(parse_loop_create)]
ExperimentCreateRequest = Annotated[Union[ExperimentCreate, ExperimentCreateV2], BeforeValidator(parse_experiment_create)]
LoopActionKindV2 = Union[LoopActionKind, Literal['correction.apply']]
LoopState = PublicResearchLoop.model_fields['state'].annotation
ExperimentAction = LoopActionView.model_fields['experiment_action'].annotation
UtcTime = Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$')]


class PublicCorrectionPolicyReference(AuthorModel):
    policy_id: PolicyId
    policy_digest: Sha256


class PublicAcceptedProviderCandidate(AuthorModel):
    kind: Literal['provider_accepted_candidate']
    proposal_id: str = Field(pattern=r'^proposal_[0-9a-f]{64}$')
    accepted_response_digest: Sha256


class PublicCorrectedCandidateSource(CorrectedCandidateSource):
    """Exact nine source keys; artifact authority is verified by the service."""


PublicCandidateSource = Annotated[Union[PublicAcceptedProviderCandidate, PublicCorrectedCandidateSource], Field(discriminator='kind')]


class PublicCorrectionParent(AuthorModel):
    proposal_id: str = Field(pattern=r'^proposal_[0-9a-f]{64}$')
    attempt_id: str = Field(pattern=r'^proposal_attempt_[0-9a-f]{64}$')
    response_digest: Sha256
    candidate_id: Identity
    status: Literal['rejected']


class PublicCorrectionDiagnostic(AuthorModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    phase: Literal['expression']
    candidate_id: Identity


class PublicCorrectionReceipt(PublicCorrectionPolicyReference):
    """Sanitized subset; original digests bind original, unsanitized records."""
    schema_version: Literal[1]
    receipt_digest: Sha256
    candidate_id: Identity
    method: Literal['deterministic', 'provider']
    status: Literal['completed', 'blocked']
    source_digest: Sha256
    parent_binding_digest: Sha256
    diagnostics_digest: Sha256
    validation_digest: Sha256
    accepted_task_digest: Sha256 | None
    submitted_expression: str
    accepted_expression: str | None
    expression_before_digest: Sha256
    expression_after_digest: Sha256 | None
    changed_paths: list[str]
    rule_ids: list[RuleId]
    created_at: UtcTime
    provider_association: ProviderCorrectionAssociation | None


class PublicCorrectionAction(AuthorModel):
    sequence: int = Field(ge=1, le=24)
    effect_status: Literal['planned', 'started', 'unknown', 'completed', 'blocked']
    started_at: UtcTime | None
    intent_digest: Sha256
    parent: PublicCorrectionParent
    source_digest: Sha256
    capability_digest: Sha256
    diagnostics_digest: Sha256
    diagnostics: list[PublicCorrectionDiagnostic]
    receipt: PublicCorrectionReceipt | None
    normalization_rule_ids: list[NormalizationRuleId]
    correction_bundle_digest: Sha256 | None
    equivalence_status: Literal['not_verified', 'same_value_representation_verified']

    @model_validator(mode='after')
    def consistent_effect(self):
        terminal = self.effect_status in {'completed', 'blocked'}
        if (self.effect_status == 'planned') != (self.started_at is None):
            raise ValueError('Correction start display differs from its saved effect')
        if terminal != (self.receipt is not None and self.correction_bundle_digest is not None):
            raise ValueError('Correction terminal display needs its saved receipt')
        if not terminal and (self.receipt is not None or self.correction_bundle_digest is not None):
            raise ValueError('Unresolved correction has no terminal receipt')
        if self.receipt is not None:
            if (self.receipt.status != self.effect_status or self.receipt.candidate_id != self.parent.candidate_id
                    or self.receipt.source_digest != self.source_digest
                    or self.receipt.diagnostics_digest != self.diagnostics_digest):
                raise ValueError('Correction receipt display differs from its saved parent')
            accepted = self.receipt.accepted_expression is not None and self.receipt.accepted_task_digest is not None
            if (self.effect_status == 'completed') != accepted:
                raise ValueError('Correction accepted display differs from its terminal status')
        if (self.effect_status == 'completed') != (self.equivalence_status == 'same_value_representation_verified'):
            raise ValueError('Only completed host correction declares verified representation')
        if any(item.candidate_id != self.parent.candidate_id for item in self.diagnostics):
            raise ValueError('Correction diagnostic refers to another candidate')
        return self


class LoopPolicyV2(AuthorModel):
    max_repairs: int = Field(ge=0, le=2)
    allow_experiment: bool
    experiment_budget: ExperimentBudget | None
    max_experiments: Literal[1]
    max_actions: int
    correction_policy_id: PolicyId
    correction_policy_digest: Sha256
    correction_mode: Literal['deterministic', 'provider_optional']
    max_deterministic_passes: Literal[1]


class PublicProviderCorrectionIntent(PublicCorrectionPolicyReference):
    schema_version: Literal[1]
    intent_digest: Sha256
    input_digest: Sha256
    effect_key: str = Field(pattern=r'^loop-effect-[0-9a-f]{64}$')
    parent: PublicCorrectionParent
    source_digest: Sha256
    capability_digest: Sha256
    diagnostics_digest: Sha256
    parent_binding_digest: Sha256
    proposal_id: str | None
    proposal_status: ProposalStatus | None


class LoopActionViewV2(AuthorModel):
    sequence: int
    idempotency_key: str
    kind: LoopActionKindV2
    experiment_action: ExperimentAction
    planned_at: str
    status: Literal['pending', 'acknowledged']
    request_digest: Sha256
    receipt_digest: Sha256 | None
    proposal_id: str | None
    experiment_id: str | None
    step_id: str | None
    correction: PublicCorrectionAction | None
    provider_correction: PublicProviderCorrectionIntent | None = None
    provider_correction_receipt: PublicCorrectionReceipt | None = None

    @model_validator(mode='after')
    def correction_kind(self):
        if (self.kind == 'correction.apply') != (self.correction is not None):
            raise ValueError('Only correction action carries correction display')
        if self.provider_correction is not None and self.kind != 'proposal.create':
            raise ValueError('Provider correction intent belongs to its original create action')
        if self.provider_correction_receipt is not None and (self.kind != 'proposal.reconcile'
                or self.provider_correction_receipt.method != 'provider'):
            raise ValueError('Provider correction receipt belongs to original reconciliation')
        return self


class LoopExperimentViewV2(AuthorModel):
    id: str
    candidate_source: PublicCandidateSource
    task_digest: Sha256
    job_id: str | None
    state: str | None
    revision_id: str | None
    run_id: str | None


class PublicResearchLoopV2(AuthorModel):
    schema_version: Literal[2]
    id: str
    session_id: str
    created_at: str
    request_digest: Sha256
    state: LoopState
    reason: str | None
    next_action: LoopActionKindV2 | None
    policy: LoopPolicyV2
    actions: list[LoopActionViewV2]
    proposals: list[ResearchV2ProposalSummary]
    experiment: LoopExperimentViewV2 | None
    decision: ResearchDecision | None
    decision_applied: bool
    session: PublicResearchSession
    semantic_fidelity: Literal['unverified']
    real_dispatch_qualified: Literal[False]
    public_projection_redacted: bool


class PublicExperimentV2(AuthorModel):
    schema_version: Literal[2]
    id: str
    session_id: str
    candidate_source: PublicCorrectedCandidateSource
    request_digest: Sha256
    task_digest: Sha256
    source_kind: Literal['research_revision']
    created_at: str
    budget: ExperimentBudget
    bootstrap: ExperimentBootstrap | None
    job: ResearchJobSummary | None
    session: PublicResearchSession
    reservation: PublicSessionReservation
    candidate_checks: list[JobCandidateCheck]
    semantic_fidelity: Literal['unverified']
    budget_scope: Literal['session_top_level_actions_and_experiments_with_separate_job_steps']
    public_projection_redacted: bool


PublicLoopResponse = Annotated[Union[PublicResearchLoop, PublicResearchLoopV2], Field(discriminator='schema_version')]
PublicExperimentResponse = Annotated[Union[PublicExperiment, PublicExperimentV2], Field(discriminator='schema_version')]


class PublicResearchLoopPage(AuthorModel):
    items: list[PublicLoopResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)
    offset: int = Field(ge=0, le=10000)


class LoopSourceSupportRequest(AuthorModel):
    research_id: Identity
    base_revision_id: Identity
    provider_config_id: str = Field(min_length=1, max_length=128)
    host_grant_id: str | None = Field(pattern=r'^host_grant_[0-9a-f]{64}$')


class LoopSourceSupport(AuthorModel):
    schema_version: Literal[2]
    research_id: Identity
    base_revision_id: Identity
    source_digest: Sha256 | None
    correction_supported: bool
    support_status: Literal['supported', 'unsupported', 'unavailable']
    reason_code: str
    message: str
    accepted_source_kind: Literal['research_revision']
    accepted_task_schema_version: Literal[1]
    accepted_data_kind: Literal['synthetic']
    candidate_limit: Literal[1]
    max_deterministic_passes: Literal[1]
    correction_modes: list[Literal['deterministic']]
    policy_ids: list[PolicyId]
    policy_references: list[PublicCorrectionPolicyReference]
    provider_generation_capability: HostCapability | None
    provider_config_digest: Sha256 | None
    generation_available: bool
    provider_optional_qualified: Literal[False]
    real_dispatch_qualified: bool
    public_projection_redacted: bool
