"""Schema-six report storage and closed HTTP projections for research insights."""
from typing import Any, Literal

from pydantic import Field, field_validator

from .mutations import validate_key
from .response_schemas import ResponseModel, RunSummary


REPORT_SCHEMA = """
CREATE TABLE research_reports(
 id TEXT PRIMARY KEY,
 research_id TEXT NOT NULL REFERENCES researches(id),
 created_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,
 request_digest TEXT NOT NULL,
 payload TEXT NOT NULL,
 payload_digest TEXT NOT NULL
);
CREATE INDEX research_reports_research ON research_reports(research_id,created_at,id);
"""


class ReportCreate(ResponseModel):
    run_ids: list[str] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator('idempotency_key')
    @classmethod
    def validate_request_key(cls, value):
        return validate_key(value)

    @field_validator('run_ids')
    @classmethod
    def distinct_run_ids(cls, value):
        if len(set(value)) != len(value) or any(not item.strip() or len(item) > 64 for item in value):
            raise ValueError('Select 1..20 distinct run identifiers of 1..64 characters')
        return value


class ComparisonCondition(ResponseModel):
    name: str
    status: Literal['equal', 'changed', 'unknown']
    blocking: bool
    baseline: Any
    candidate: Any


class MetricSource(ResponseModel):
    run_id: str
    attempt_id: str
    candidate_id: str
    result_digest: str
    artifact_id: str
    artifact_name: str
    artifact_sha256: str
    original_sha256: str
    json_pointer: str


class MetricDifference(ResponseModel):
    name: str
    baseline: float | int | None
    candidate: float | int | None
    delta: float | int | None
    reason: str | None
    baseline_source: MetricSource | None
    candidate_source: MetricSource | None


class ComparisonCandidate(ResponseModel):
    candidate_id: str
    presence: Literal['both', 'baseline_only', 'candidate_only']
    baseline_status: str | None
    candidate_status: str | None
    baseline_expression: str | None
    candidate_expression: str | None
    expression_changed: bool | None
    comparable: bool
    reasons: list[str]
    metrics: list[MetricDifference]


class ComparisonRun(ResponseModel):
    run: RunSummary
    source_verified: bool
    verification_error: str | None
    attempt_id: str | None
    state_digest: str | None
    revision_digest: str
    paper_sha256: str
    dataset_sha256: str
    data_metadata: dict[str, Any]
    evaluation: dict[str, Any]
    budget: dict[str, Any]
    recorded_semantics: dict[str, Any] | None
    code_digest: str | None
    environment: dict[str, Any] | None
    tool_calls: int | None
    elapsed_seconds: float | None


class RunComparison(ResponseModel):
    schema_version: Literal[1]
    compared_at: str
    delta_direction: Literal['candidate_minus_baseline']
    baseline: ComparisonRun
    candidate: ComparisonRun
    comparable: bool
    conditions: list[ComparisonCondition]
    candidates: list[ComparisonCandidate]
    limitations: list[str]


class ReportSummary(ResponseModel):
    id: str
    research_id: str
    created_at: str
    payload_digest: str
    selected_run_ids: list[str]
    counts: dict[str, int]
    integrity: Literal['verified']


class ResearchReport(ReportSummary):
    # This is the versioned, hashed export payload, including historical engine,
    # assessment and regression records whose schemas may differ by source age.
    payload: dict[str, Any]


class ReportList(ResponseModel):
    items: list[ReportSummary]
    total: int
    limit: int
    offset: int
