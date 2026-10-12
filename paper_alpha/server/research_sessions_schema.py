"""Closed orchestration contracts. Sessions own budgets, never child queues."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from .research_jobs_schema import DraftCandidate
from .provider_research_source_schema import (PaperSelectionSource as PaperSelectionRequest,
                                               AcceptedMaterialSource as AcceptedMaterialRequest)

Identity = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^\S(?:[^\r\n]*\S)?$')]
Sha256 = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
SessionAction = Literal['extract', 'generate', 'repair', 'decide', 'experiment', 'read']
SessionStatus = Literal['created', 'active', 'awaiting_input', 'awaiting_handoff',
                        'completed', 'stopped', 'cancelled', 'exhausted', 'interrupted']


class ResearchRevisionSource(AuthorModel):
    kind: Literal['research_revision']
    research_id: Identity
    revision_id: Identity
    revision_digest: Sha256
    source_digest: Sha256


class AcceptedMaterialSource(AuthorModel):
    kind: Literal['accepted_material']
    material_id: Identity
    version_id: Identity
    material_digest: Sha256
    source_digest: Sha256
    source_request: AcceptedMaterialRequest


class PaperSelectionSource(AuthorModel):
    kind: Literal['paper_selection']
    paper_id: Identity
    selection_digest: Sha256
    source_digest: Sha256
    source_request: PaperSelectionRequest


SessionSource = Annotated[ResearchRevisionSource | AcceptedMaterialSource | PaperSelectionSource, Field(discriminator='kind')]


class SessionConfig(AuthorModel):
    provider_config_id: Identity
    provider_config_digest: Sha256
    contract_id: Identity


class SessionBudget(AuthorModel):
    max_provider_dispatches: int = Field(ge=0, le=100)
    max_tool_actions: int = Field(ge=1, le=500)
    max_experiments: int = Field(ge=0, le=100)
    max_failures: int = Field(ge=1, le=100)
    max_seconds: int = Field(ge=1, le=86400)


class SessionCreate(AuthorModel):
    schema_version: Literal[1] = 1
    source: SessionSource
    purpose: str = Field(min_length=1, max_length=4000)
    config: SessionConfig
    budget: SessionBudget
    idempotency_key: Identity


class SessionUsage(AuthorModel):
    provider_dispatches: int = Field(ge=0)
    tool_actions: int = Field(ge=0)
    experiments: int = Field(ge=0)
    failures: int = Field(ge=0)


class SessionChildren(AuthorModel):
    proposal_id: Identity | None = None
    research_job_id: Identity | None = None
    run_id: Identity | None = None

    @model_validator(mode='after')
    def nonempty(self):
        if not any((self.proposal_id, self.research_job_id, self.run_id)):
            raise ValueError('At least one actual child identity is required')
        return self


class SessionReserve(AuthorModel):
    action: SessionAction
    effect_key: Identity
    action_digest: Sha256
    expected_digest: Sha256


class SessionOutcome(AuthorModel):
    status: Literal['completed', 'failed', 'awaiting_input', 'awaiting_handoff', 'interrupted']
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(default='', max_length=4000)
    result_digest: Sha256 | None = None


class SessionReservation(AuthorModel):
    token: Identity
    sequence: int = Field(ge=1, le=500)
    action: SessionAction
    effect_key: Identity
    action_digest: Sha256
    request_digest: Sha256
    created_at: str
    deadline_at: str
    source: SessionSource
    source_digest: Sha256
    charges: SessionUsage
    status: Literal['reserved', 'effect_recorded', 'finished']
    children: SessionChildren | None = None
    outcome: SessionOutcome | None = None


class SessionLedgerState(AuthorModel):
    version: int = Field(default=0, ge=0, le=3000)
    state: SessionStatus
    stop_reason: str | None = Field(default=None, max_length=128)
    current_source: SessionSource
    usage: SessionUsage


class SessionEvent(AuthorModel):
    sequence: int = Field(ge=1, le=3000)
    kind: Literal['created', 'reserved', 'effect_recorded', 'action_finished',
                  'source_advanced', 'session_finished', 'session_cancelled',
                  'experiment_created', 'experiment_effect']
    created_at: str
    previous_digest: Sha256 | None
    operation_key: Identity
    operation_digest: Sha256
    state_after: SessionLedgerState
    reservation: SessionReservation | None = None
    commit_receipt_digest: Sha256 | None = None


class RepairDiagnostic(AuthorModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    phase: Literal['contract', 'expression', 'domain', 'execution']
    candidate_id: Identity


class RepairTarget(AuthorModel):
    parent_proposal_id: Identity
    parent_attempt_id: Identity
    parent_response_digest: Sha256
    candidate: DraftCandidate
    candidate_digest: Sha256
    diagnostics: list[RepairDiagnostic] = Field(min_length=1, max_length=30)
    diagnostics_digest: Sha256
    allowed_changes: Literal['syntax_or_parameters_without_changing_the_research_hypothesis']


class DecisionResult(AuthorModel):
    candidate_id: Identity
    run_id: Identity
    result_digest: Sha256
    evaluation_period: Literal['validation']
    execution_status: Literal['completed', 'failed', 'blocked']
    metrics: dict[str, int | float | None] = Field(max_length=30)
    diagnostics: list[RepairDiagnostic] = Field(max_length=30)


class ResearchDecision(AuthorModel):
    action: Literal['repair', 'request_information', 'propose_experiment', 'stop']
    reason: str = Field(min_length=1, max_length=4000)
    candidate_id: Identity | None
    target_digest: Sha256 | None
    information_gaps: list[str] = Field(max_length=30)

    @model_validator(mode='after')
    def target_and_gaps(self):
        if self.action in {'repair', 'propose_experiment'} and not all((self.candidate_id, self.target_digest)):
            raise ValueError('An action affecting a candidate requires an exact target')
        if self.action == 'request_information' and not self.information_gaps:
            raise ValueError('Specify the information gap')
        return self
