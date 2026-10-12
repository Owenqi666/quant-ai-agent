"""Closed DTOs for attaching one host-prepared original Native loop.

The catalog is a locator and integrity manifest, never a permission ledger.
Public callers cannot provide slots, budgets, file paths or qualification.
"""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .research_sessions_schema import Identity, Sha256
from .provider_correction_public_schema import PublicResearchLoopV2

class NativeCorrectionIdentity(AuthorModel):
    schema_version: Literal[1]
    research_id: Identity
    base_revision_id: Identity
    provider_config_id: Identity


class NativeCorrectionStartRequest(NativeCorrectionIdentity):
    start_id: str = Field(pattern=r'^native_start_[a-f0-9]{64}$')
    start_digest: Sha256
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')


class NativeCorrectionAuthorization(AuthorModel):
    authority_id: Literal['research_loop_native_v1']
    grant_id: str = Field(pattern=r'^loop_native_grant_[a-f0-9]{64}$')
    kind: Literal['fixture', 'bounded_qualification']
    human_authorization_digest: Sha256 | None
    deadline_at: str
    max_provider_dispatches: Literal[2]
    max_repairs: Literal[1]
    candidate_limit: Literal[1]


class NativeCorrectionEvidence(AuthorModel):
    profile_digest: Sha256
    profile_status: Literal['pinned_static']
    boundary_status: Literal['fixture_only', 'per_attempt_verification_required']
    inference_status: Literal['unverified', 'fixture_observed', 'native_observed']
    semantic_fidelity: Literal['unverified']


class NativeCorrectionStartSummary(AuthorModel):
    schema_version: Literal[1]
    start_id: str = Field(pattern=r'^native_start_[a-f0-9]{64}$')
    start_digest: Sha256
    research_id: Identity
    base_revision_id: Identity
    provider_config_id: Identity
    provider_config_digest: Sha256
    source_digest: Sha256
    session_id: Identity
    loop_id: str = Field(pattern=r'^research_loop_[a-f0-9]{64}$')
    loop_request_digest: Sha256
    correction_policy_id: Literal['equivalent-expression-representation-v2']
    correction_policy_digest: Sha256
    state: Literal['prepared', 'active', 'unknown', 'terminal']
    available: bool
    reason_code: str
    message: str
    authorization: NativeCorrectionAuthorization
    evidence: NativeCorrectionEvidence
    development_scope: Literal['ordinary_synthetic_task1_generation1_eligible_repair1']
    public_projection_redacted: bool


class NativeCorrectionSupport(NativeCorrectionIdentity):
    available: bool
    reason_code: str
    message: str
    plans: list[NativeCorrectionStartSummary] = Field(max_length=32)
    public_projection_redacted: bool


class NativeCorrectionStartResponse(AuthorModel):
    schema_version: Literal[1]
    start_id: str = Field(pattern=r'^native_start_[a-f0-9]{64}$')
    start_digest: Sha256
    idempotency_key: str
    attached_existing: Literal[True]
    plan: NativeCorrectionStartSummary
    loop: PublicResearchLoopV2


class ManagedNativePlan(AuthorModel):
    schema_version: Literal[1]
    program_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,96}$')
    research_id: Identity
    base_revision_id: Identity
    provider_config_id: Identity
    provider_config_digest: Sha256
    source_digest: Sha256
    session_id: Identity
    loop_id: str = Field(pattern=r'^research_loop_[a-f0-9]{64}$')
    loop_request_digest: Sha256
    grant_id: str = Field(pattern=r'^loop_native_grant_[a-f0-9]{64}$')
    grant_request_digest: Sha256
    grant_profile_digest: Sha256
    workspace_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    correction_policy_id: Literal['equivalent-expression-representation-v2']
    correction_policy_digest: Sha256
    service_source_digest: Sha256
    read_qualification_digest: Sha256 | None


class ManagedNativePlanEntry(AuthorModel):
    start_id: str = Field(pattern=r'^native_start_[a-f0-9]{64}$')
    start_digest: Sha256


class ManagedNativeCatalog(AuthorModel):
    schema_version: Literal[1]
    plans: list[ManagedNativePlanEntry] = Field(max_length=32)


class NativeCorrectionPrepare(AuthorModel):
    schema_version: Literal[1]
    program_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,96}$')
    research_id: Identity
    base_revision_id: Identity
    provider_config_id: Identity
    generation_instruction: str = Field(default='', max_length=4000)
    purpose: str = Field(default='Bounded Native correction development acceptance', min_length=1, max_length=4000)
    max_seconds: int = Field(default=600, ge=60, le=600)
    proposal_seconds: int = Field(default=120, ge=1, le=180)
    # Host-only App Server whole-attempt cap, including fresh qualification.
    # The original exec bound and browser attachment DTO remain unchanged.
    app_server_attempt_seconds: int | None = Field(default=None, ge=1, le=240)
    allow_experiment: bool = True
