"""Closed HTTP projections for monthly experiments, distinct from daily tasks."""
from typing import Literal
from pydantic import Field, model_validator
from .research_protocol_schema import ProtocolModel, ProtocolConfig, ProtocolWindows, ProtocolCoverage, ProtocolRecord

RunStatus = Literal['queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted']
StrategyId = Literal['momentum', 'mom_id']


class MonthlyConfig(ProtocolModel):
    schema_version: Literal[1]
    start_month: str
    end_month: str
    cost_bps: float = Field(ge=0, le=100)
    min_assets: int = Field(ge=9, le=128)

    @model_validator(mode='before')
    @classmethod
    def semantic_config(cls, value):
        from ..monthly_evaluation import validate_config
        return validate_config(value)


class MonthlyExclusion(ProtocolModel):
    asset: str
    reasons: list[str]


class MonthlySignal(ProtocolModel):
    asset: str
    momentum: float
    pret: float
    id: float
    mom_group: int
    id_group: int


class MonthlyGroup(ProtocolModel):
    mom_group: int
    id_group: int
    assets: list[str]
    gross_return: float | None
    reasons: list[str]


class MonthlyLabel(ProtocolModel):
    asset: str
    return_value: float | None
    coverage: ProtocolCoverage
    reasons: list[str]


class MonthlyWeight(ProtocolModel):
    asset: str
    weight: float


class MonthlyStrategy(ProtocolModel):
    id: StrategyId
    status: Literal['evaluated', 'unavailable']
    weights: list[MonthlyWeight]
    gross_return: float | None
    traded_weight: float | None
    turnover_proxy: float | None
    estimated_cost: float | None
    net_return_proxy: float | None
    nav_proxy: float | None
    drawdown_proxy: float | None
    reasons: list[str]


class MonthlyPeriod(ProtocolModel):
    month: str
    as_of: str
    windows: ProtocolWindows
    status: Literal['evaluated', 'partial', 'unavailable', 'blocked']
    eligible_assets: list[str]
    exclusions: list[MonthlyExclusion]
    signals: list[MonthlySignal]
    groups: list[MonthlyGroup]
    labels: list[MonthlyLabel]
    strategies: list[MonthlyStrategy]
    difference_net_proxy: float | None
    warnings: list[str]


class MonthlyMetric(ProtocolModel):
    strategy_id: StrategyId
    months_total: int
    months_evaluated: int
    gross_months: int
    turnover_months: int
    mean_gross_return: float | None
    mean_net_return_proxy: float | None
    volatility_annualized_proxy: float | None
    sharpe_annualized_proxy: float | None
    max_drawdown_proxy: float | None
    terminal_nav_proxy: float | None
    mean_turnover_proxy: float | None
    cumulative_complete: bool


class MonthlyResult(ProtocolModel):
    schema_version: Literal[1]
    semantics_version: str
    data_kind: Literal['controlled_fixture']
    source_id: str
    rules: ProtocolConfig
    config: MonthlyConfig
    config_digest: str
    input_digest: str
    status: Literal['evaluated', 'partial', 'not_evaluable', 'blocked']
    warnings: list[str]
    months: list[MonthlyPeriod]
    summary: list[MonthlyMetric]


class MonthlyReference(ProtocolModel):
    supported: bool
    passed: bool | None
    checks: int
    issues: list[str]


class MonthlySummary(ProtocolModel):
    id: str
    protocol_id: str
    created_at: str
    updated_at: str
    status: RunStatus
    config: MonthlyConfig
    attempt_count: int
    attempt_id: str | None
    error: str | None
    phase: str | None


class MonthlyStatus(ProtocolModel):
    id: str
    status: RunStatus
    attempt_id: str | None
    attempt_count: int
    phase: str | None
    change_token: str
    integrity_checked: Literal[False]


class MonthlyAttempt(ProtocolModel):
    id: str
    number: int
    status: RunStatus
    started_at: str
    finished_at: str | None
    error: str | None


class MonthlyReview(ProtocolModel):
    id: str
    experiment_id: str
    attempt_id: str
    result_digest: str
    verdict: Literal['accepted', 'needs_revision', 'rejected']
    note: str
    source: Literal['human', 'automation']
    created_at: str
    digest: str


class MonthlyReport(ProtocolModel):
    id: str
    experiment_id: str
    attempt_id: str
    result_digest: str
    created_at: str
    digest: str
    result: MonthlyResult
    reviews: list[MonthlyReview]
    protocol: ProtocolRecord
    markdown: str


class MonthlyReportSummary(ProtocolModel):
    id: str
    created_at: str
    digest: str
    attempt_id: str
    result_digest: str


class MonthlyVerification(ProtocolModel):
    verified: bool
    result_digest: str | None
    input_digest: str | None
    reference_passed: bool | None


class MonthlyReviewTarget(ProtocolModel):
    attempt_id: str
    result_digest: str


class MonthlyDetail(ProtocolModel):
    experiment: MonthlySummary
    protocol: ProtocolRecord
    attempts: list[MonthlyAttempt]
    result: MonthlyResult | None
    verification: MonthlyVerification
    review_target: MonthlyReviewTarget | None
    reviews: list[MonthlyReview]
    reports: list[MonthlyReportSummary]


class MonthlyPage(ProtocolModel):
    items: list[MonthlySummary]
    total: int
    limit: int
    offset: int


class MonthlyDefaults(ProtocolModel):
    config: MonthlyConfig
    data_kind: Literal['controlled_fixture']
    source_id: str
    warnings: list[str]


class MonthlyAck(ProtocolModel):
    experiment_id: str


class MonthlyCreate(ProtocolModel):
    protocol_id: str = Field(pattern=r'^protocol_[0-9a-f]{64}$')
    protocol_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    config: MonthlyConfig
    idempotency_key: str = Field(min_length=1, max_length=128)


class MonthlyCancel(ProtocolModel):
    expected_attempt_id: str | None


class MonthlyRetry(ProtocolModel):
    expected_attempt_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MonthlyReportCreate(ProtocolModel):
    attempt_id: str = Field(min_length=1, max_length=128)
    result_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: str = Field(min_length=1, max_length=128)


class MonthlyReviewCreate(MonthlyReportCreate):
    verdict: Literal['accepted', 'needs_revision', 'rejected']
    note: str = Field(max_length=4000)
    source: Literal['human', 'automation']
