"""Provider-free, closed contracts for bounded diagnostic tool sessions."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel
from .research_cases_schema import CaseAction


class ToolBudget(AuthorModel):
    max_calls: int = Field(ge=1, le=100)
    max_errors: int = Field(ge=1, le=20)
    max_seconds: int = Field(ge=1, le=600)


class ToolSessionCreate(AuthorModel):
    case_id: str = Field(pattern=r'^research_case_[0-9a-f]{64}$')
    case_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    budget: ToolBudget
    idempotency_key: str = Field(min_length=1, max_length=128)


class ToolArguments(AuthorModel):
    action: str | None = Field(default=None, max_length=100)
    rationale: str | None = Field(default=None, max_length=4000)
    evidence_ids: list[str] | None = Field(default=None, max_length=100)


class ToolCallCreate(AuthorModel):
    tool: str = Field(min_length=1, max_length=80)
    arguments: ToolArguments
    idempotency_key: str = Field(min_length=1, max_length=128)


class ToolError(AuthorModel):
    code: str
    message: str
    retryable: bool


class ToolProposal(AuthorModel):
    action: CaseAction
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(max_length=100)
    actor: Literal['automation']
    status: Literal['draft']
    semantic_fidelity: Literal['unverified']


class ToolResponse(AuthorModel):
    ok: bool
    error: ToolError | None
    case_json: str | None
    evidence_json: str | None
    result_json: str | None
    proposal: ToolProposal | None


class ToolCall(AuthorModel):
    id: str
    sequence: int
    tool: str
    arguments_json: str
    request_digest: str
    status: Literal['running', 'completed', 'failed', 'interrupted']
    started_at: str
    finished_at: str | None
    elapsed_seconds: float
    response: ToolResponse | None


class ToolUsage(AuthorModel):
    calls: int
    errors: int
    elapsed_seconds: float


class ToolSessionSummary(AuthorModel):
    id: str
    digest: str
    created_at: str
    case_id: str
    case_digest: str
    budget: ToolBudget
    usage: ToolUsage
    status: Literal['active', 'running', 'exhausted', 'blocked']


class ToolSession(ToolSessionSummary):
    calls: list[ToolCall]


class ToolSessionPage(AuthorModel):
    items: list[ToolSessionSummary]
    total: int
    limit: int
    offset: int


class ToolCapability(AuthorModel):
    name: str
    description: str
    authority: Literal['read_only', 'automation_draft']


class ToolCapabilities(AuthorModel):
    provider_connected: Literal[False]
    tools: list[ToolCapability]
    forbidden: list[str]
    budget_scope: str
    time_policy: str
    review_policy: str
