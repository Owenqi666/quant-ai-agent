"""Closed API projections for monthly research-rule preparation and diagnostics."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ProtocolConfig(ProtocolModel):
    schema_version: Literal[1]
    mode: Literal['paper', 'project']
    mom_window_months: int = Field(ge=1, le=36)
    mom_skip_months: int = Field(ge=0, le=12)
    id_window_months: int | None = Field(ge=1, le=36)
    id_skip_months: int | None = Field(ge=0, le=12)
    missing_policy: Literal['unresolved', 'complete', 'available']
    min_coverage: float | None = Field(gt=0, le=1)
    max_missing_run: int | None = Field(ge=0, le=366)
    zero_policy: Literal['include']
    fill_policy: Literal['none']

    @model_validator(mode='before')
    @classmethod
    def enforce_semantics(cls, value):
        # Validate before Pydantic can normalize Literal[1] from bool/float.
        from ..research_protocol import validate_config
        return validate_config(value)


class ProtocolPreset(ProtocolModel):
    id: str
    title: str
    description: str
    config: ProtocolConfig
    sources: list[str]
    unresolved: list[str]


class ProtocolPresets(ProtocolModel):
    presets: list[ProtocolPreset]


class ProtocolWindow(ProtocolModel):
    start: str
    end: str


class ProtocolWindows(ProtocolModel):
    target_month: str
    as_of: str
    momentum: ProtocolWindow
    id: ProtocolWindow | None


class ProtocolMissingDate(ProtocolModel):
    date: str
    reason: str


class ProtocolCoverage(ProtocolModel):
    expected: int
    valid: int
    positive: int
    negative: int
    zero: int
    missing_rows: int
    null_values: int
    coverage: float
    max_missing_run: int
    missing_dates: list[ProtocolMissingDate]


class ProtocolAsset(ProtocolModel):
    asset: str
    status: Literal['ready', 'unavailable', 'blocked']
    momentum: float | None
    pret: float | None
    id: float | None
    momentum_coverage: ProtocolCoverage | None
    id_coverage: ProtocolCoverage | None
    reasons: list[str]


class ProtocolPreview(ProtocolModel):
    schema_version: Literal[1]
    semantics_version: str
    config: ProtocolConfig
    config_digest: str
    input_digest: str
    data_kind: Literal['controlled_fixture']
    source_id: str
    windows: ProtocolWindows
    status: Literal['blocked', 'ready', 'partial']
    warnings: list[str]
    assets: list[ProtocolAsset]


class ProtocolPreviewCreate(ProtocolModel):
    config: ProtocolConfig
    target_month: str = Field(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$')
    bundle: dict[str, Any] | None = None


class ProtocolCreate(ProtocolModel):
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=4000)
    parent_id: str | None = Field(default=None, pattern=r'^protocol_[0-9a-f]{64}$')
    config: ProtocolConfig


class ProtocolChange(ProtocolModel):
    field: str
    before: str | int | float | None
    after: str | int | float | None


class ProtocolRecord(ProtocolModel):
    id: str
    title: str
    note: str
    parent_id: str | None
    config: ProtocolConfig
    config_digest: str
    digest: str
    created_at: str
    changes: list[ProtocolChange]
    semantics_version: str
    sources: list[str]
    unresolved: list[str]


class ProtocolPage(ProtocolModel):
    items: list[ProtocolRecord]
    total: int
    limit: int
    offset: int
