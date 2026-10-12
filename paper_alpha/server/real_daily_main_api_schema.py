"""Closed public metadata resources; no private paths or producer callbacks."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..real_daily_web_contract import Selection

Identity = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_-]{1,128}$')]
Pin = Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)]
Key = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Origin = Literal['engineering_fixture', 'real_source_declaration']


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)

    @model_validator(mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is dict and 'schema_version' in value and type(value['schema_version']) is not int:
            raise ValueError('Schema version must be an integer')
        return value


class MainDailySourceFactCounts(Closed):
    supported: int = Field(ge=0, le=8)
    unknown: int = Field(ge=0, le=8)
    rejected: int = Field(ge=0, le=8)


class MainDailySourceHandle(Closed):
    schema_version: Literal[1]
    id: Identity
    handle_id: Identity
    handle_digest: Pin
    origin: Origin
    identity_status: Literal['declared_not_raw_verified']
    selection: Selection
    method_digest: Pin
    source_fact_counts: MainDailySourceFactCounts
    created_at_utc: str
    source_authenticated: Literal[False]
    real_source_ready: Literal[False]
    point_in_time: Literal['not_certified']
    final_numeric_access_allowed: Literal[False]


class MainDailySourceHandlePage(Closed):
    items: list[MainDailySourceHandle] = Field(max_length=1000)
    total: int = Field(ge=0, le=1000)


class MainDailyAssessmentCreate(Closed):
    schema_version: Literal[1] = 1
    handle_id: Identity
    expected_handle_digest: Pin
    idempotency_key: Key


class MainDailyApprovalCreate(Closed):
    schema_version: Literal[1] = 1
    expected_digest: Pin
    reviewer: str = Field(min_length=1, max_length=200)
    expires_at_utc: str = Field(min_length=1, max_length=48)
    whole_byte_integrity_allowed: Literal[True]
    numeric_access: Literal['development_ohlc_only']
    final_numeric_access_allowed: Literal[False]
    idempotency_key: Key

    @model_validator(mode='before')
    @classmethod
    def exact_flags(cls, value):
        if type(value) is dict:
            if type(value.get('whole_byte_integrity_allowed')) is not bool or type(value.get('final_numeric_access_allowed')) is not bool:
                raise ValueError('Permission flags must be explicit booleans')
        return value


class MainDailyRevokeCreate(Closed):
    schema_version: Literal[1] = 1
    expected_digest: Pin
    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: Key


class MainDailyReserveCreate(Closed):
    schema_version: Literal[1] = 1
    expected_digest: Pin
    idempotency_key: Key


class MainDailyNormalizeCreate(Closed):
    schema_version: Literal[1] = 1
    handle_id: Identity
    expected_handle_digest: Pin
    approval_id: Identity
    expected_approval_digest: Pin
    reservation_id: Identity
    expected_reservation_digest: Pin
    idempotency_key: Key


class MainDailySourceFactBlocker(Closed):
    code: Literal['source_fact_unknown', 'source_fact_rejected']
    fact: Literal['provider', 'rights', 'calendar', 'units', 'universe', 'events', 'vintage', 'raw_identity']


class MainDailyEventBlocker(Closed):
    code: Literal['unknown_event_coverage', 'unknown_effective_date', 'unsupported_development_event']
    asset: str = Field(min_length=1, max_length=96)
    event_type: Literal['split', 'dividend', 'halt', 'no_quote', 'listing', 'delisting']
    event_id: str | None = Field(max_length=96)


class MainDailyAssessmentSummary(Closed):
    id: Identity
    assessment_id: Identity
    assessment_digest: Pin
    status: Literal['blocked', 'ready_for_host_review']
    blockers: list[MainDailySourceFactBlocker | MainDailyEventBlocker] = Field(max_length=2048)
    origin: Origin
    method_digest: Pin
    selection_digest: Pin
    source_authenticated: Literal[False]
    real_source_ready: Literal[False]
    point_in_time: Literal['not_certified']
    local_development_execution_allowed: Literal[False]
    final_numeric_access_allowed: Literal[False]
    final_numeric_rows_parsed: Literal[0]


class MainDailyApprovalSummary(Closed):
    id: Identity
    approval_id: Identity
    approval_digest: Pin
    assessment_id: Identity
    current_permission: Literal['active', 'revoked', 'expired', 'restored_inactive', 'code_or_input_changed']
    local_development_execution_allowed: bool
    source_authenticated: Literal[False]
    real_source_ready: Literal[False]
    point_in_time: Literal['not_certified']
    scope: Literal['local_development_only']
    numeric_access: Literal['development_ohlc_only']
    whole_byte_integrity_allowed: Literal[True]
    final_numeric_access_allowed: Literal[False]


class MainDailyReservationSummary(Closed):
    id: Identity
    reservation_id: Identity
    reservation_digest: Pin
    approval_id: Identity
    approval_digest: Pin
    assessment_id: Identity
    assessment_digest: Pin
    selection_digest: Pin
    scope_keys_digest: Pin
    status: Literal['reserved']
    final_numeric_access_allowed: Literal[False]
    final_numeric_rows_parsed: Literal[0]


class MainDailyRevocationSummary(Closed):
    schema_version: Literal[1]
    approval_id: Identity
    approval_digest: Pin
    reason: str
    revoked_at_utc: str
    revocation_digest: Pin


class MainDailyReadCounters(Closed):
    raw_originals_opened: int = Field(ge=0, le=100)
    raw_originals_whole_byte_hashed: int = Field(ge=0, le=100)
    development_price_values_decoded: int | None = Field(ge=0, le=400000)
    development_decode_completed_assets: int = Field(ge=0, le=100)
    decode_count_certainty: Literal['exact', 'partial_unknown']
    final_value_tokens_decoded: Literal[0]
    final_numeric_rows_parsed: Literal[0]
    volume_value_tokens_decoded: Literal[0]
    full_dataset_validated: Literal[False]


class MainDailyNormalizationSummary(Closed):
    schema_version: Literal[1]
    id: Identity
    intent_id: Identity
    intent_digest: Pin
    reservation_id: Identity
    handle_id: Identity
    status: Literal['started_unknown', 'completed', 'failed', 'interrupted']
    automatic_retry_allowed: Literal[False]
    error_code: str | None
    registration_id: Identity | None
    registration_digest: Pin | None
    dataset_id: Identity | None
    counters: MainDailyReadCounters | None
    source_authenticated: Literal[False]
    real_source_ready: Literal[False]
    point_in_time: Literal['not_certified']
    final_numeric_access_allowed: Literal[False]


class MainDailyRegistrationSummary(Closed):
    schema_version: Literal[1]
    id: Identity
    registration_id: Identity
    registration_digest: Pin
    dataset_id: Identity
    data_kind: Literal['real_daily_ohlc_development']
    fields: list[Literal['open', 'high', 'low', 'close']] = Field(min_length=4, max_length=4)
    assessment_id: Identity
    selection: Selection
    method_digest: Pin
    catalog_digest: Pin
    prefix_sha256: Pin
    origin: Origin
    source_authenticated: Literal[False]
    real_source_ready: Literal[False]
    point_in_time: Literal['not_certified']
    final_numeric_access_allowed: Literal[False]


class MainDailyMainCapabilities(Closed):
    schema_version: Literal[1]
    execution_kind: Literal['real_daily_ohlc_development_v1']
    data_kind: Literal['real_daily_ohlc_development']
    fields: list[Literal['open', 'high', 'low', 'close']] = Field(min_length=4, max_length=4)
    task_schema_version: Literal[3]
    supported_candidate: Literal['alpha101-101']
    max_development_cells: Literal[10000]
    max_validation_eligible_cells: Literal[4000]
    authority_initialized: bool
    offline_bootstrap_required: bool
    source_capture_mode: Literal['offline_controlled_metadata_handles']
    source_authenticated: Literal[False]
    real_source_ready: Literal[False]
    point_in_time: Literal['not_certified']
    final_numeric_access_allowed: Literal[False]
