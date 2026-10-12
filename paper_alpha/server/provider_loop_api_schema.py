"""Closed public loop views; private prompts and action requests stay local."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .provider_research_api_schema import PublicResearchSession, ResearchV2ProposalSummary
from .research_session_experiments_schema import ExperimentBudget
from .research_sessions_schema import ResearchDecision

LoopActionKind = Literal['proposal.create', 'proposal.reconcile', 'experiment.create',
    'experiment.advance', 'experiment.reconcile', 'decision.apply', 'session.stop']


class LoopPolicy(AuthorModel):
    max_repairs: int
    allow_experiment: bool
    experiment_budget: ExperimentBudget | None
    max_experiments: Literal[1]
    max_actions: int


class LoopActionView(AuthorModel):
    sequence: int
    idempotency_key: str
    kind: LoopActionKind
    experiment_action: Literal['validate', 'commit', 'submit', 'observe', 'complete'] | None
    planned_at: str
    status: Literal['pending', 'acknowledged']
    request_digest: str
    receipt_digest: str | None
    proposal_id: str | None
    experiment_id: str | None
    step_id: str | None


class LoopExperimentView(AuthorModel):
    id: str
    proposal_id: str
    accepted_response_digest: str
    task_digest: str
    job_id: str | None
    state: str | None
    revision_id: str | None
    run_id: str | None


class PublicResearchLoop(AuthorModel):
    schema_version: Literal[1]
    id: str
    session_id: str
    created_at: str
    request_digest: str
    state: Literal['ready', 'recovering', 'waiting_provider', 'waiting_execution',
        'awaiting_input', 'awaiting_handoff', 'stopped', 'completed', 'cancelled',
        'exhausted', 'interrupted']
    reason: str | None
    next_action: LoopActionKind | None
    policy: LoopPolicy
    actions: list[LoopActionView]
    proposals: list[ResearchV2ProposalSummary]
    experiment: LoopExperimentView | None
    decision: ResearchDecision | None
    decision_applied: bool
    session: PublicResearchSession
    semantic_fidelity: Literal['unverified']
    real_dispatch_qualified: Literal[False]
    public_projection_redacted: bool


class LoopAdvance(AuthorModel):
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
