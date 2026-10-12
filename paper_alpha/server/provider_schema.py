"""Provider v1 contracts. Models own drafts; the host owns execution and receipts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator, model_serializer

from ..author_panel_schema import AuthorModel
from .research_jobs_schema import JobBudget, ResearchDraft

CONTRACT_VERSION = 1
CONTRACT_ID = 'codex_cli_provider_v1'
DIGEST_PATTERN = r'^[a-f0-9]{64}$'
ID_PATTERN = r'^[A-Za-z0-9_-]{1,128}$'


class ProposalBudget(AuthorModel):
    max_provider_invocations: int = Field(default=1, ge=1, le=3)
    max_seconds: int = Field(default=60, ge=1, le=600)
    max_context_bytes: int = Field(default=131072, ge=1024, le=131072)
    max_output_bytes: int = Field(default=65536, ge=1024, le=65536)
    max_log_bytes: int = Field(default=8388608, ge=1024, le=8388608)
    token_scheduling_threshold: int = Field(default=12000, ge=1, le=12000)
    output_token_target: int = Field(default=2000, ge=1, le=2000)
    # Unsupported hard caps are rejected rather than silently weakened.
    hard_token_limit: int | None = Field(default=None, ge=1)
    hard_model_call_limit: int | None = Field(default=None, ge=1)
    hard_cost_limit: float | None = Field(default=None, gt=0)


class ProposalCreate(AuthorModel):
    schema_version: Literal[1] = 1
    research_id: str = Field(min_length=1, max_length=64)
    base_revision_id: str = Field(min_length=1, max_length=64)
    expected_source_digest: str = Field(pattern=DIGEST_PATTERN)
    purpose: Literal['generate_from_evidence', 'repair_candidate'] = 'generate_from_evidence'
    instruction: str = Field(default='', max_length=4000)
    candidate_limit: int = Field(default=3, ge=1, le=3)
    provider_config_id: str = Field(pattern=ID_PATTERN)
    budget: ProposalBudget = Field(default_factory=ProposalBudget)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def nonblank_key(self):
        if not self.idempotency_key.strip():
            raise ValueError('idempotency_key must not be blank')
        return self


class ProviderConfig(AuthorModel):
    """Trusted host configuration, never accepted as client/model output."""
    config_id: str = Field(pattern=ID_PATTERN)
    mode: Literal['codex_cli', 'fixture'] = 'codex_cli'
    enabled: bool = False
    executable: str = Field(default='codex', min_length=1, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Literal['low', 'medium', 'high', 'xhigh'] = 'low'
    expected_cli_version: str = '0.145.0'
    compatibility_verified: bool = False
    tool_boundary_verified: bool = False
    inference_verified: bool = False
    # Trusted host-only transport selection. The default is omitted on the
    # wire so historical exec configuration snapshots keep their exact shape.
    native_transport: Literal['exec', 'app_server'] = 'exec'

    @model_validator(mode='after')
    def supported_transport(self):
        if self.native_transport == 'app_server' and self.mode != 'codex_cli':
            raise ValueError('App Server requires the native host mode')
        return self

    @model_serializer(mode='wrap')
    def transport_snapshot(self, handler):
        result = handler(self)
        if self.native_transport == 'exec':
            result.pop('native_transport', None)
        return result


class ProviderError(AuthorModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=4000)
    retryable: bool = False


class ProviderReadiness(AuthorModel):
    provider: Literal['codex_cli', 'disabled', 'fixture']
    configured: bool = False
    authenticated: bool | None = None
    inference_verified: bool = False
    tool_boundary_verified: bool = False
    cli_version: str | None = None
    checked_at: str
    errors: list[ProviderError] = Field(default_factory=list)


class ProposalAbstention(AuthorModel):
    reason_code: Literal['EVIDENCE_INSUFFICIENT', 'FIELD_UNAVAILABLE', 'RULES_UNCLEAR', 'OPERATOR_UNSUPPORTED']
    reason: str = Field(min_length=1, max_length=4000)
    # Mechanically checkable entries use field:<name>, operator:<name>,
    # configuration:<key>. Other explanations remain semantically unverified.
    missing_requirements: list[str] = Field(max_length=30)
    evidence_ids: list[str] = Field(max_length=100)


class ProposalModelOutput(AuthorModel):
    schema_version: Literal[1]
    proposal_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    context_digest: str = Field(pattern=DIGEST_PATTERN)
    outcome: Literal['draft', 'abstain']
    draft: ResearchDraft | None
    abstention: ProposalAbstention | None

    @model_validator(mode='after')
    def consistent_branch(self):
        if self.outcome == 'draft':
            if self.draft is None or self.abstention is not None:
                raise ValueError('draft outcome requires only a draft')
            if len(self.draft.candidates) > 3:
                raise ValueError('provider v1 permits at most three candidates')
        elif self.draft is not None or self.abstention is None:
            raise ValueError('abstain outcome requires only an abstention')
        return self


class ProviderUsage(AuthorModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    complete: bool = False
    observed_turns: int = Field(default=0, ge=0)
    tool_events: int = Field(default=0, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    raw: list[dict] = Field(default_factory=list)


class ProviderReceipt(AuthorModel):
    schema_version: Literal[1] = 1
    proposal_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    started_at: str
    finished_at: str
    elapsed_seconds: float = Field(ge=0)
    exit_code: int | None = None
    requested_model: str | None = None
    reported_model: str | None = None
    invocation_started: bool = False
    fixture: bool = False
    cli_version: str | None = None
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    response_sha256: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    process_stopped: bool = True


class ProposalHandoff(AuthorModel):
    accepted_response_digest: str = Field(pattern=DIGEST_PATTERN)
    budget: JobBudget
    idempotency_key: str = Field(min_length=1, max_length=128)
    note: str = Field(default='', max_length=4000)
    allow_partial_execution: bool = False
    method_status: Literal['resolved', 'unresolved'] = 'resolved'
    unresolved_rules: list[str] = Field(default_factory=list, max_length=30)


@dataclass(frozen=True)
class PreparedProposalRequest:
    proposal_id: str
    attempt_id: str
    context_digest: str
    input_text: str
    output_schema: dict
    config: ProviderConfig
    budget: ProposalBudget


@dataclass
class ProviderCallControl:
    deadline_monotonic: float
    artifact_dir: Path
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    pass_fds: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProviderAttemptResult:
    raw_response: bytes | None
    error: ProviderError | None
    receipt: ProviderReceipt
