"""Explicit experiment handoff; tasks and result numbers are host-produced."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .research_jobs_schema import JobBudget


class ExperimentBudget(JobBudget):
    max_steps: int = Field(ge=1, le=10)


class ExperimentCreate(AuthorModel):
    schema_version: Literal[1] = 1
    session_id: str = Field(pattern=r'^research_session_[0-9a-f]{64}$')
    expected_session_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    proposal_id: str = Field(pattern=r'^proposal_[0-9a-f]{64}$')
    accepted_response_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    budget: ExperimentBudget
    note: str = Field(default='', max_length=4000)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


SCHEMA = """
CREATE TABLE research_session_experiments(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES research_sessions(id),reservation_token TEXT NOT NULL UNIQUE REFERENCES research_session_reservations(token),idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,bootstrap_effect_key TEXT NOT NULL UNIQUE,job_effect_key TEXT NOT NULL UNIQUE);
CREATE TABLE research_session_experiment_effects(experiment_id TEXT NOT NULL REFERENCES research_session_experiments(id),kind TEXT NOT NULL,effect_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(experiment_id,kind));
CREATE TABLE research_session_experiment_resources(kind TEXT NOT NULL,resource_id TEXT NOT NULL,experiment_id TEXT NOT NULL REFERENCES research_session_experiments(id),effect_key TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(kind,resource_id));
CREATE INDEX research_session_experiments_session ON research_session_experiments(session_id,created_at,id);
"""



# Additive explicit host-correction handoff. The original v1 type/DDL above
# keep their serialized behavior and their ready-only candidate contract.
class CorrectedCandidateSource(AuthorModel):
    kind: Literal['host_corrected_rejected_candidate']
    loop_id: str = Field(pattern=r'^research_loop_[0-9a-f]{64}$')
    correction_action_sequence: int = Field(ge=1, le=24)
    intent_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    correction_receipt_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent_proposal_id: str = Field(pattern=r'^proposal_[0-9a-f]{64}$')
    parent_attempt_id: str = Field(pattern=r'^proposal_attempt_[0-9a-f]{64}$')
    parent_response_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    accepted_task_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class ExperimentCreateV2(AuthorModel):
    schema_version: Literal[2] = Field(...)
    session_id: str = Field(pattern=r'^research_session_[0-9a-f]{64}$')
    expected_session_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_source: CorrectedCandidateSource
    budget: ExperimentBudget
    note: str = Field(default='', max_length=4000)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


def parse_experiment_create(value):
    """Closed request parsing only; receipt/artifact gates live in the service."""
    if type(value) is not dict:
        raise ValueError('Experiment request must be a JSON object')
    version = value.get('schema_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Experiment schema_version requires supported integer 1 or 2')
    return (ExperimentCreate if version == 1 else ExperimentCreateV2).model_validate(value)
