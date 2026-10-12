"""Observed local host bytes, never a Native permit or scientific approval."""
from typing import Literal, Annotated
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from ..storage import digest

SHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
ID = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')]
PROOF_ID = 'research-loop-host-observation-v1'


class ObservedFile(AuthorModel):
    role: ID
    sha256: SHA
    bytes: int = Field(ge=0, le=33554432)


class HostObservedProof(AuthorModel):
    schema_version: Literal[1] = 1
    proof_contract_id: Literal['research-loop-host-observation-v1'] = PROOF_ID
    publisher: Literal['host_observed_offline'] = 'host_observed_offline'
    scope: Literal['isolated_fixture_host_bytes_only'] = 'isolated_fixture_host_bytes_only'
    native_ready: Literal[False] = False
    dispatch_allowed: Literal[False] = False
    real_model_calls: Literal[0] = 0
    market_numeric_rows_read: Literal[0] = 0
    business_contract_id: Literal['research_provider_v2'] = 'research_provider_v2'
    operation: Literal['generate_candidates', 'repair_candidate', 'decide_next_action']
    source_kind: Literal['research_revision'] = 'research_revision'
    workspace_identity: ID
    session_id: ID
    proposal_id: Annotated[str, Field(pattern=r'^proposal_[a-f0-9]{64}$')]
    attempt_id: Annotated[str, Field(pattern=r'^proposal_attempt_[a-f0-9]{64}$')]
    fencing_token: SHA
    reservation_token: ID
    pre_reservation_state_digest: SHA
    dispatch_state_digest: SHA
    request_storage_digest: SHA
    source_storage_digest: SHA
    config_wire_digest: SHA
    context_wire_digest: SHA
    prepared_storage_digest: SHA
    anchor_storage_digest: SHA
    code_inventory_storage_digest: SHA
    catalog_storage_digest: SHA
    capability_storage_digest: SHA
    dataset_id: ID
    dataset_storage_digest: SHA
    target_binding_storage_digest: SHA | None
    observed_files: list[ObservedFile] = Field(min_length=7, max_length=10)
    proof_digest: SHA

    @model_validator(mode='before')
    @classmethod
    def exact_markers(cls, value):
        if isinstance(value, dict):
            for key in ('native_ready', 'dispatch_allowed'):
                if key in value and type(value[key]) is not bool:
                    raise ValueError('Proof readiness markers require actual booleans')
            for key in ('schema_version', 'real_model_calls', 'market_numeric_rows_read'):
                if key in value and type(value[key]) is not int:
                    raise ValueError('Proof count/version markers require actual integers')
        return value

    @model_validator(mode='after')
    def bound_digest(self):
        body = self.model_dump(); actual = body.pop('proof_digest')
        if digest(body) != actual or {item.role for item in self.observed_files} != {'request_json', 'context_json', 'config_json', 'prepared_json', 'paper_pdf', 'market_bytes', 'metadata_json'} or len(self.observed_files) != 7:
            raise ValueError('Observed proof digest or file roles differ')
        if (self.operation in {'repair_candidate', 'decide_next_action'}) != (self.target_binding_storage_digest is not None):
            raise ValueError('Target operations require an observed target binding')
        return self
