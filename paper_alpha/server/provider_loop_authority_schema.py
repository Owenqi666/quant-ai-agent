"""Fixture-only declarations for the independent research-loop authority v1.

These typed projections are not proof that a host inspected actual artifacts.
Native qualification belongs to a later, separately reviewed adapter branch.
"""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from ..storage import digest
from .research_jobs_schema import DraftCandidate

AUTHORITY_ID = 'research-loop-authority-v1'
OPERATIONS = ('extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action')
SHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
ID = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')]
Operation = Literal['extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action']


class PaperSource(AuthorModel):
    kind: Literal['paper_selection']
    paper_id: ID
    paper_sha256: SHA
    document_digest: SHA
    selection_digest: SHA
    source_digest: SHA


class MaterialSource(AuthorModel):
    kind: Literal['accepted_material']
    paper_id: ID
    paper_sha256: SHA
    document_digest: SHA
    material_id: ID
    version_id: ID
    material_digest: SHA
    source_digest: SHA


class RevisionSource(AuthorModel):
    kind: Literal['research_revision']
    paper_id: ID
    paper_sha256: SHA
    document_digest: SHA
    research_id: ID
    revision_id: ID
    revision_digest: SHA
    dataset_id: ID
    dataset_digest: SHA
    source_digest: SHA


LoopSource = Annotated[PaperSource | MaterialSource | RevisionSource, Field(discriminator='kind')]


class CodeIdentity(AuthorModel):
    inventory_id: ID
    files: dict[str, SHA] = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def relative_names(self):
        import re
        if any(not re.fullmatch(r'[A-Za-z0-9_./-]{1,256}', name) or name.startswith('/')
               or any(part in {'', '.', '..'} for part in name.split('/')) for name in self.files):
            raise ValueError('Code inventory names must be bounded relative paths')
        return self


class CapabilityIdentity(AuthorModel):
    data_contract_id: ID
    capability_id: ID
    catalog_digest: SHA
    dataset_id: ID
    dataset_digest: SHA
    operator_semantics_id: ID
    fields: list[Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')]] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def unique_fields(self):
        if len(set(self.fields)) != len(self.fields):
            raise ValueError('Capability field names must be unique')
        return self


class OperationLimits(AuthorModel):
    extract_material: int = Field(ge=0, le=100)
    generate_candidates: int = Field(ge=0, le=100)
    repair_candidate: int = Field(ge=0, le=2)
    decide_next_action: int = Field(ge=0, le=100)


class LoopBudget(AuthorModel):
    max_dispatches: int = Field(ge=1, le=100)
    operations: OperationLimits
    max_failures: int = Field(ge=1, le=100)
    max_seconds: int = Field(ge=1, le=7200)
    max_attempt_seconds: int = Field(ge=1, le=600)
    max_context_bytes: int = Field(ge=1, le=131072)
    max_output_bytes: int = Field(ge=1, le=65536)
    max_capture_bytes: int = Field(ge=1, le=8388608)
    hard_token_limit: int | None = Field(default=None, ge=1)
    hard_cost_limit: float | None = Field(default=None, gt=0)
    hard_model_call_limit: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def supported(self):
        if any(value is not None for value in (self.hard_token_limit, self.hard_cost_limit, self.hard_model_call_limit)):
            raise ValueError('UNSUPPORTED_BUDGET: supplier hard limits are unavailable')
        if self.max_dispatches > sum(self.operations.model_dump().values()):
            raise ValueError('Shared allowance exceeds operation allowances')
        if self.max_attempt_seconds > self.max_seconds:
            raise ValueError('Attempt duration exceeds the session duration')
        return self


class LoopGrantPolicy(AuthorModel):
    schema_version: Literal[1] = 1
    authority_id: Literal['research-loop-authority-v1'] = AUTHORITY_ID
    authorization_kind: Literal['fixture'] = 'fixture'
    workspace_id: ID
    provider_config_id: ID
    provider_config_digest: SHA
    code: CodeIdentity
    capability: CapabilityIdentity
    initial_source: LoopSource
    budget: LoopBudget
    idempotency_key: ID


class ParentBinding(AuthorModel):
    session_id: ID
    parent_claim_id: ID
    parent_request_digest: SHA
    parent_source_digest: SHA
    parent_proposal_id: ID
    parent_attempt_id: ID
    parent_context_digest: SHA
    parent_prepared_digest: SHA
    parent_response_digest: SHA
    parent_raw_sha256: SHA
    parent_receipt_digest: SHA
    parent_validation_digest: SHA
    candidate_id: ID
    candidate_digest: SHA
    assertions_digest: SHA
    source_digest: SHA
    research_id: ID
    revision_id: ID
    revision_digest: SHA
    config_digest: SHA
    code_digest: SHA
    capability_digest: SHA


class RepairBinding(ParentBinding):
    kind: Literal['repair']
    repair_policy: Literal['integral-window-representation-v1']
    diagnostics_digest: SHA


class DecisionBinding(ParentBinding):
    kind: Literal['decision']
    experiment_id: ID
    research_job_id: ID
    run_id: ID
    execution_attempt_id: ID
    frozen_result_digest: SHA
    state_digest: SHA
    completion_step_id: ID
    completion_step_digest: SHA
    experiment_projection_digest: SHA


LoopTarget = Annotated[RepairBinding | DecisionBinding, Field(discriminator='kind')]


class LoopClaim(AuthorModel):
    schema_version: Literal[1] = 1
    authority_id: Literal['research-loop-authority-v1'] = AUTHORITY_ID
    session_id: ID
    expected_state_digest: SHA
    operation: Operation
    source_digest: SHA
    config_digest: SHA
    code_digest: SHA
    capability_digest: SHA
    proposal_id: ID
    attempt_id: ID
    context_digest: SHA
    prepared_digest: SHA
    assertions_digest: SHA
    target: LoopTarget | None = None
    context_bytes: int = Field(ge=1, le=131072)
    output_bytes: int = Field(ge=1, le=65536)
    capture_bytes: int = Field(ge=1, le=8388608)
    max_seconds: int = Field(ge=1, le=600)
    idempotency_key: ID

    @model_validator(mode='after')
    def target_scope(self):
        expected = {'repair_candidate': 'repair', 'decide_next_action': 'decision'}.get(self.operation)
        if (self.target.kind if self.target else None) != expected:
            raise ValueError('Exactly repair/decision operations require their corresponding target')
        return self


class CandidateProjection(AuthorModel):
    candidate: DraftCandidate
    candidate_digest: SHA
    assertions_digest: SHA

    @model_validator(mode='after')
    def candidate_identity(self):
        if digest(self.candidate.model_dump()) != self.candidate_digest:
            raise ValueError('Candidate projection digest differs')
        return self


class LoopOutcome(AuthorModel):
    publisher: Literal['fixture_tool_transport']
    session_id: ID
    claim_id: ID
    request_digest: SHA
    proposal_id: ID
    attempt_id: ID
    transport_status: Literal['succeeded', 'failed', 'cancelled', 'unknown', 'not_started']
    invocation_started: bool | None
    process_stopped: bool | None
    domain_status: Literal['accepted', 'rejected', 'abstained', 'not_checked']
    raw_sha256: SHA | None = None
    response_digest: SHA | None = None
    transport_receipt_digest: SHA | None = None
    validation_digest: SHA | None = None
    diagnostic_code: ID | None = None
    diagnostics_digest: SHA | None = None
    candidate: CandidateProjection | None = None
    context_bytes: int = Field(ge=0, le=131072)
    output_bytes: int = Field(ge=0, le=65536)
    capture_bytes: int = Field(ge=0, le=8388608)

    @model_validator(mode='after')
    def transport_branches(self):
        if self.transport_status == 'succeeded':
            if (self.invocation_started is not True or self.process_stopped is not True
                    or not all((self.raw_sha256, self.response_digest, self.transport_receipt_digest))
                    or self.domain_status == 'not_checked'):
                raise ValueError('Success requires explicit stopped transport and raw/response/receipt identities')
        else:
            if self.domain_status != 'not_checked' or self.candidate is not None:
                raise ValueError('Non-success cannot claim domain acceptance or a usable candidate')
            if self.transport_status == 'unknown':
                if self.process_stopped is True:
                    raise ValueError('Unknown outcome cannot assert a confirmed stop')
            elif self.process_stopped is not True:
                raise ValueError('Unconfirmed process stop must remain unknown')
            if self.transport_status == 'not_started' and self.invocation_started is not False:
                raise ValueError('Not-started requires explicit invocation_started=False')
        return self


class SourceAdvance(AuthorModel):
    kind: Literal['material_adoption', 'research_commit', 'experiment_commit']
    session_id: ID
    expected_state_digest: SHA
    from_source_digest: SHA
    parent_claim_id: ID
    parent_outcome_digest: SHA
    to_source: LoopSource
    reservation_token: ID | None = None
    experiment_id: ID | None = None
    research_job_id: ID | None = None
    commit_step_id: ID | None = None
    commit_receipt_digest: SHA | None = None
    candidate_digest: SHA | None = None
    idempotency_key: ID

    @model_validator(mode='after')
    def edge_scope(self):
        fields = (self.reservation_token, self.experiment_id, self.research_job_id,
                  self.commit_step_id, self.commit_receipt_digest, self.candidate_digest)
        if self.kind == 'material_adoption':
            if self.to_source.kind != 'accepted_material' or any(x is not None for x in fields):
                raise ValueError('Material adoption has no implicit experiment/commit authority')
        elif self.to_source.kind != 'research_revision' or not all(fields):
            raise ValueError('Revision advancement requires an explicit owned commit/experiment edge')
        return self


class ExperimentProjection(AuthorModel):
    publisher: Literal['fixture_tool_transport']
    session_id: ID
    parent_claim_id: ID
    source_digest: SHA
    research_id: ID
    revision_id: ID
    revision_digest: SHA
    config_digest: SHA
    code_digest: SHA
    capability_digest: SHA
    experiment_id: ID
    reservation_token: ID
    research_job_id: ID
    commit_step_id: ID
    commit_receipt_digest: SHA
    candidate_id: ID
    candidate_digest: SHA
    run_id: ID
    execution_attempt_id: ID
    completion_step_id: ID
    completion_step_digest: SHA
    frozen_result_digest: SHA
    state_digest: SHA
    evaluation_period: Literal['validation']
    execution_status: Literal['completed']
    idempotency_key: ID
