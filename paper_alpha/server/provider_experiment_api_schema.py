"""Public experiment views omit private plans and raw child context."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .research_jobs_schema import ResearchJobSummary, JobAction, JobCandidateCheck
from .research_session_experiments_schema import ExperimentBudget
from .provider_research_api_schema import PublicResearchSession, PublicSessionReservation


class ExperimentBootstrap(AuthorModel):
    research_id: str
    revision_id: str
    receipt_digest: str | None


class PublicExperiment(AuthorModel):
    schema_version: Literal[1]
    id: str
    session_id: str
    proposal_id: str
    accepted_response_digest: str
    request_digest: str
    task_digest: str
    source_kind: Literal['accepted_material', 'research_revision']
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


class ExperimentAdvance(AuthorModel):
    action: JobAction
    idempotency_key: str = Field(min_length=1, max_length=128)


class ExperimentCancel(AuthorModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
