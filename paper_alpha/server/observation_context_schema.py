"""Read-only form context; no field asserts that a human observation happened."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .research_assessments import AssessmentDimensions, ActiveInterval
from .workflow_observations_schema import ObservationEnvironment, ObservationOutputs


class ContextModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ContextProtocol(ContextModel):
    id: str
    digest: str
    task_sha256: str
    candidate_id: str
    engine_mode: Literal['normalized_fixed']


class ContextCompatibility(ContextModel):
    compatible: bool
    reason: str | None


class ContextRuntime(ContextModel):
    version: str
    code_commit: str | None
    code_digest: str | None
    environment: ObservationEnvironment
    provenance: Literal['server_runtime']
    code_commit_verified: bool


class ContextRun(ContextModel):
    id: str
    revision_id: str
    status: str
    mode: str
    created_at: str
    attempt_count: int


class ContextAttempt(ContextModel):
    id: str
    number: int
    status: str
    started_at: str
    finished_at: str | None


class ContextReview(ContextModel):
    id: str
    candidate_id: str
    source: Literal['human', 'automation', 'imported', 'legacy_unknown']
    reviewer: str
    created_at: str
    dimensions: AssessmentDimensions
    active_intervals: list[ActiveInterval]
    result_digest: str


class ContextSelection(ContextModel):
    revision_id: str
    run_id: str
    attempt_id: str | None
    status: str
    verified: bool
    verification_error: str | None
    bound_outputs: ObservationOutputs
    attempts: list[ContextAttempt]
    attempts_total: int
    attempts_truncated: bool
    reviews: list[ContextReview]
    reviews_total: int
    reviews_truncated: bool


class ObservationContext(ContextModel):
    schema_version: Literal[1]
    research_id: str
    revision_id: str
    protocol: ContextProtocol
    compatibility: ContextCompatibility
    runtime: ContextRuntime
    runs: list[ContextRun]
    runs_total: int
    runs_truncated: bool
    selected: ContextSelection | None
    limitations: list[str]
