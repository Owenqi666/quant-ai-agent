"""Strict schema-seven observation contracts; declarations are not authentication."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_assessments import AssessmentDimensions, parse_timestamp

PHASES = ('reading', 'hypothesis', 'implementation', 'run_setup', 'review', 'report')
KINDS = ('active', 'waiting', 'away')

OBSERVATION_SCHEMA = """
CREATE TABLE workflow_observations(
 id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES researches(id),
 created_at TEXT NOT NULL,
 protocol_digest TEXT NOT NULL,
 participant TEXT NOT NULL,
 session_id TEXT NOT NULL,
 condition TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,
 request_digest TEXT NOT NULL,
 payload TEXT NOT NULL,
 payload_digest TEXT NOT NULL,
 UNIQUE(protocol_digest,participant,session_id,condition)
);
CREATE INDEX workflow_observations_research ON workflow_observations(research_id,created_at,id);
"""


class ObservationModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('schema_version', mode='before', check_fields=False)
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('Schema version must be an integer, not a boolean')
        return value


class ObservationEnvironment(ObservationModel):
    machine: str = Field(min_length=1, max_length=300)
    os: str = Field(min_length=1, max_length=300)
    python: str = Field(min_length=1, max_length=100)
    dependencies: str = Field(min_length=1, max_length=2000)

    @field_validator('*')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Environment declarations must not be blank')
        return value


class ObservationInterval(ObservationModel):
    phase: Literal['reading', 'hypothesis', 'implementation', 'run_setup', 'review', 'report']
    kind: Literal['active', 'waiting', 'away']
    started_at: str = Field(min_length=20, max_length=40)
    ended_at: str | None = Field(max_length=40)
    status: Literal['ended', 'interrupted']
    # A copied review interval identifies its actual review; null is a manually
    # recorded interval, never an instruction to add the review's whole timer.
    source_reference: str | None = Field(max_length=64)

    @field_validator('started_at', 'ended_at')
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            parse_timestamp(value)
        return value


class ObservationOutputs(ObservationModel):
    task_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    revision_id: str | None = Field(max_length=64)
    run_id: str | None = Field(max_length=64)
    attempt_id: str | None = Field(max_length=64)
    review_ids: list[str] = Field(max_length=20)
    external_run_reference: str | None = Field(max_length=1000)
    verification_evidence: str | None = Field(max_length=4000)
    report_reference: str | None = Field(max_length=1000)

    @field_validator('review_ids')
    @classmethod
    def distinct_reviews(cls, value):
        if len(set(value)) != len(value) or any(not x.strip() or len(x) > 64 for x in value):
            raise ValueError('Review identifiers must be nonblank, distinct and bounded')
        return value

    @field_validator('revision_id', 'run_id', 'attempt_id', 'external_run_reference',
                     'verification_evidence', 'report_reference')
    @classmethod
    def nonblank_optional(cls, value):
        if value is not None and not value.strip():
            raise ValueError('Use null for missing output declarations')
        return value


class WorkflowObservation(ObservationModel):
    schema_version: Literal[1]
    protocol_id: str = Field(min_length=1, max_length=120)
    protocol_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    record_status: Literal['observed']
    source: Literal['human', 'automation']
    participant: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    condition: Literal['manual_cli', 'workbench']
    execution_order: int = Field(ge=1, le=10000)
    code_commit: str = Field(pattern=r'^[0-9a-f]{40}$')
    environment: ObservationEnvironment
    allowed_tools: list[str] = Field(min_length=1, max_length=40)
    prior_familiarity: str = Field(min_length=1, max_length=2000)
    practice_session: bool
    predeclared_stop_condition: str = Field(min_length=1, max_length=2000)
    started_at: str = Field(min_length=20, max_length=40)
    finished_at: str | None = Field(max_length=40)
    completion: Literal['completed', 'incomplete', 'abandoned']
    incomplete_reason: str | None = Field(max_length=4000)
    intervals: list[ObservationInterval] = Field(max_length=300)
    errors_and_rework: list[str] = Field(max_length=100)
    template_reuse: list[str] = Field(max_length=100)
    help_received: list[str] = Field(max_length=100)
    bound_outputs: ObservationOutputs
    dimensions: AssessmentDimensions | None
    limitations: list[str] = Field(max_length=100)

    @field_validator('participant', 'session_id', 'protocol_id', 'prior_familiarity', 'predeclared_stop_condition')
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('Observation declarations must be nonblank without surrounding whitespace')
        return value

    @field_validator('started_at', 'finished_at')
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            parse_timestamp(value)
        return value

    @field_validator('allowed_tools', 'errors_and_rework', 'template_reuse', 'help_received', 'limitations')
    @classmethod
    def bounded_text(cls, value):
        if any(not item.strip() or len(item) > 2000 for item in value):
            raise ValueError('List declarations must be nonblank strings of at most 2000 characters')
        return value


class ObservationValidate(ObservationModel):
    observation: WorkflowObservation


class ObservationCreate(ObservationValidate):
    idempotency_key: str = Field(min_length=1, max_length=128)


class PhaseTiming(ObservationModel):
    phase: str
    active_seconds: float | None
    waiting_seconds: float | None
    away_seconds: float | None
    ended_intervals: int
    interrupted_intervals: int


class ObservationTiming(ObservationModel):
    phases: list[PhaseTiming]
    observed_active_seconds: float | None
    observed_waiting_seconds: float | None
    observed_away_seconds: float | None
    full_active_seconds: float | None
    measured_phases: list[str]
    unmeasured_phases: list[str]
    active_unmeasured_phases: list[str]
    interrupted_intervals: int
    complete_active_coverage: bool


class ObservationBinding(ObservationModel):
    protocol_verified: bool
    scientific_inputs_verified: bool
    outputs_verified: bool
    code_and_environment_verified: bool
    kind: Literal['workbench_bound', 'external_declared']
    revision_id: str | None
    run_id: str | None
    attempt_id: str | None
    review_ids: list[str]
    result_digests: dict[str, str]
    limitations: list[str]


class ObservationValidation(ObservationModel):
    valid: Literal[True]
    observation_digest: str
    timing: ObservationTiming
    binding: ObservationBinding
    warnings: list[str]


class ObservationDetail(ObservationModel):
    id: str
    research_id: str
    created_at: str
    payload_digest: str
    integrity: Literal['verified']
    observation: WorkflowObservation
    timing: ObservationTiming
    binding: ObservationBinding
    warnings: list[str]


class ObservationList(ObservationModel):
    items: list[ObservationDetail]
    total: int
    limit: int
    offset: int


class ObservationComparison(ObservationModel):
    group_digest: str
    observation_ids: list[str]
    manual_observation_id: str | None
    workbench_observation_id: str | None
    comparable: bool
    reasons: list[str]
    comparison_basis: Literal['same_declared_conditions_case_only']
    active_seconds_delta: float | None
    delta_direction: Literal['workbench_minus_manual_cli']


class ObservationConditionSummary(ObservationModel):
    condition: Literal['manual_cli', 'workbench']
    observation_ids: list[str]
    records: int
    completed: int
    incomplete: int
    abandoned: int
    completion_rate: float | None
    full_active_coverage_records: int
    partial_or_unmeasured_records: int
    interrupted_records: int
    error_and_rework_entries: int


class ObservationSummary(ObservationModel):
    schema_version: Literal[1]
    research_id: str
    generated_at: str
    total: int
    human_nonpractice_records: int
    human_completed_records: int
    human_completion_rate: float | None
    automation_records: int
    practice_records: int
    observations: list[ObservationDetail]
    by_condition: list[ObservationConditionSummary]
    comparisons: list[ObservationComparison]
    limitations: list[str]


class ObservationProtocol(ObservationModel):
    id: str
    digest: str
    inputs: dict[str, str]
    phases: list[str]
    conditions: list[str]


class ObservationTemplate(ObservationModel):
    schema_version: Literal[1]
    protocol: ObservationProtocol
    # This intentionally unfilled template is not a WorkflowObservation.
    observation: dict
