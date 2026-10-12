"""Host-owned, bounded research grants and their read-only public capability view."""
from typing import Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from .provider_research_source_schema import PaperSelectionSource, MaterialDestination

Operation = Literal['extract_material', 'generate_candidates']
SourceKind = Literal['paper_selection', 'accepted_material']

SCHEMA = """
CREATE TABLE provider_host_grants(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_host_sessions(session_id TEXT PRIMARY KEY REFERENCES research_sessions(id),grant_id TEXT NOT NULL REFERENCES provider_host_grants(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_host_attempts(id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES provider_host_grants(id),session_id TEXT NOT NULL UNIQUE REFERENCES research_sessions(id),proposal_id TEXT NOT NULL UNIQUE REFERENCES proposal_requests(id),attempt_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_host_revocations(grant_id TEXT PRIMARY KEY REFERENCES provider_host_grants(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
"""

class GenerationPolicy(AuthorModel):
    dataset_id: str = Field(min_length=1,max_length=64)
    evaluation: dict
    method_policy: dict
    destination: MaterialDestination
    title: str = Field(min_length=1,max_length=200)

    @model_validator(mode='after')
    def new_only(self):
        if self.destination.kind != 'new_research' or not self.title.strip():
            raise ValueError('Host material workflow requires a named new research destination')
        return self

class HostGrantCreate(AuthorModel):
    provider_config_id: str = Field(min_length=1,max_length=128)
    paper_selections: list[PaperSelectionSource] = Field(min_length=1,max_length=2)
    generation_policy: GenerationPolicy | None = None
    max_dispatches: int = Field(default=2,ge=1,le=3)
    max_extract: int = Field(default=1,ge=1,le=2)
    max_generate: int = Field(default=1,ge=0,le=1)
    max_proposal_seconds: int = Field(default=240,ge=1,le=240)
    max_seconds: int = Field(default=1800,ge=1,le=7200)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    authorization_kind: Literal['fixture','bounded_qualification','qualified'] = 'bounded_qualification'
    qualification_proposal_ids: list[str] = Field(default_factory=list,max_length=2)

    @model_validator(mode='after')
    def limits(self):
        if self.max_dispatches > self.max_extract + self.max_generate:
            raise ValueError('Total allowance exceeds operation allowances')
        if bool(self.max_generate) != (self.generation_policy is not None):
            raise ValueError('Generation allowance requires its exact configuration policy')
        if len({(s.paper_id,tuple(s.selected_pages)) for s in self.paper_selections}) != len(self.paper_selections):
            raise ValueError('Extraction selections must be distinct')
        return self

class PublicHostScope(AuthorModel):
    paper_selections: list[PaperSelectionSource]
    generation_policy: GenerationPolicy | None

class HostCapability(AuthorModel):
    provider_config_id: str
    provider_mode: Literal['fixture','codex_cli']
    operation: Operation
    source_kind: SourceKind
    supported: bool = True
    real_qualified: bool
    available: bool
    reason_code: str | None
    message: str
    host_grant_id: str | None
    authorization_kind: Literal['none','fixture','bounded_qualification','qualified']
    remaining_dispatches: int
    remaining_operation_dispatches: int
    deadline_at: str | None
    max_proposal_seconds: int
    scope: PublicHostScope | None

class HostCapabilities(AuthorModel):
    schema_version: Literal[1] = 1
    contract_id: Literal['research_provider_v2'] = 'research_provider_v2'
    items: list[HostCapability]
