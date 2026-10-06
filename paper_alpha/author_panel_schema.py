"""Closed, bounded source-specific normalized panel and diagnostic contracts."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AuthorModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)

    @model_validator(mode='before')
    @classmethod
    def strict_literals(cls, value):
        if isinstance(value, dict):
            if 'schema_version' in value and type(value['schema_version']) is not int:
                raise ValueError('schema_version requires an integer')
            if 'returns_included_in_formation' in value and value['returns_included_in_formation'] is not False:
                raise ValueError('Future labels may not participate in formation')
        return value


class AuthorSource(AuthorModel):
    doi: str
    version: str
    file_id: int
    filename: Literal['IntnlData.mat', 'USData.mat']
    sha256: str
    repository_md5: str
    bytes: int
    assets: int
    periods: int
    first_month: str
    last_month: str


class AuthorSelection(AuthorModel):
    target_month: str
    row_offset: int = Field(ge=0)
    row_count: int = Field(ge=1, le=512)


ValueState = Literal['value', 'nan', 'posinf', 'neginf']


class AuthorRow(AuthorModel):
    source_row: int = Field(ge=1)
    asset: str
    country: str | None
    returns: list[float | None] = Field(min_length=13, max_length=13)
    return_states: list[ValueState] = Field(min_length=13, max_length=13)
    dgw: float | None
    dgw_state: ValueState
    market_cap: float | None
    market_cap_state: ValueState


class AuthorPanel(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['author_perturbed_monthly_panel']
    adapter_version: Literal['gjs-v2-monthly-panel-v1']
    source: AuthorSource
    selection: AuthorSelection
    months: list[str] = Field(min_length=13, max_length=13)
    source_observation_dates: list[str | None] = Field(min_length=13, max_length=13)
    rows: list[AuthorRow] = Field(min_length=1, max_length=512)


class AuthorWindows(AuthorModel):
    momentum_months: list[str]
    skip_month: str
    formation_month: str
    label_month: str


class AuthorDiagnosticRow(AuthorModel):
    asset: str
    source_row: int
    country: str | None
    momentum: float | None
    dgw: float | None
    market_cap: float | None
    label: float | None
    formation_ready: bool
    label_ready: bool
    reasons: list[str]
    missing_momentum_months: list[str]


class AuthorSummary(AuthorModel):
    assets: int
    formation_ready: int
    labels_available: int
    ready_with_label: int


class AuthorDiagnostic(AuthorModel):
    schema_version: Literal[1]
    semantics_version: Literal['gjs-v2-alignment-diagnostic-v1']
    data_kind: Literal['author_perturbed_monthly_panel']
    panel_digest: str
    source: AuthorSource
    selection: AuthorSelection
    windows: AuthorWindows
    rows: list[AuthorDiagnosticRow]
    summary: AuthorSummary
    limitations: list[str]
    returns_included_in_formation: Literal[False]


class AuthorReference(AuthorModel):
    schema_version: Literal[1]
    method: Literal['independent-fraction-reference-v1']
    passed: bool
    issues: list[str]
