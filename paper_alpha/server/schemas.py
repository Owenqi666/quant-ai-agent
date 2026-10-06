"""HTTP inputs accept identifiers and structured data, never server-side paths."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .mutations import validate_key, validate_review_target
from .research_assessments import ResearchAssessment


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class OptionalMutationKey(StrictModel):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator('idempotency_key')
    @classmethod
    def nonblank_key(cls, value):
        return validate_key(value)


class ResearchCreate(OptionalMutationKey):
    title: str = Field(min_length=1, max_length=200)
    paper_id: str = Field(min_length=1, max_length=64)
    dataset_id: str = Field(min_length=1, max_length=64)
    task: dict[str, Any]


class RevisionCreate(OptionalMutationKey):
    base_revision_id: str = Field(min_length=1, max_length=64)
    task: dict[str, Any]
    note: str = Field(default='', max_length=4000)


class RunCreate(StrictModel):
    revision_id: str = Field(min_length=1, max_length=64)
    mode: Literal['agent', 'fixed', 'normalized_fixed'] = 'agent'
    idempotency_key: str = Field(min_length=1, max_length=128)


class ReviewCreate(OptionalMutationKey):
    candidate_id: str = Field(min_length=1, max_length=64)
    verdict: Literal['accepted', 'needs_changes', 'rejected']
    category: Literal['evidence', 'hypothesis', 'implementation', 'data', 'evaluation', 'other']
    note: str = Field(min_length=1, max_length=4000)
    source: Literal['human', 'automation', 'imported', 'legacy_unknown'] = 'legacy_unknown'
    assessment: ResearchAssessment | None = None
    expected_attempt_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_result_digest: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def coherent_target(self):
        validate_review_target(self.expected_attempt_id, self.expected_result_digest,
                               self.assessment.model_dump() if self.assessment is not None else None)
        return self


class ImportValidate(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=128)


class ImportRegister(ImportValidate):
    input_digest: str = Field(min_length=64, max_length=64)
    validation_attempt_id: str = Field(min_length=1, max_length=64)
    report_digest: str = Field(min_length=64, max_length=64)


class IssueCreate(ImportValidate):
    review_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=4000)
    source: Literal['human', 'automation', 'imported', 'legacy_unknown'] = 'legacy_unknown'
    disposition: Literal['implementation_fix', 'hypothesis_change', 'data_change', 'accept_limitation'] = 'implementation_fix'


class IssueUpdate(IssueCreate):
    review_id: str | None = None
    base_event_id: str = Field(min_length=1, max_length=64)
    state: Literal['open', 'proposed', 'awaiting_review', 'resolved', 'deferred']
    revision_id: str | None = None
    target_run_id: str | None = None
    check_id: str | None = None


class CaseCreate(OptionalMutationKey):
    review_id: str = Field(min_length=1, max_length=64)
    expected_status: Literal['evaluated', 'blocked', 'failed', 'rejected', 'not_evaluable', 'budget_stopped']
    note: str = Field(min_length=1, max_length=4000)


class CheckCreate(OptionalMutationKey):
    run_id: str = Field(min_length=1, max_length=64)
    case_ids: list[str] = Field(min_length=1, max_length=100)
    expected_attempt_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_result_digest: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def coherent_target(self):
        validate_review_target(self.expected_attempt_id, self.expected_result_digest)
        return self
