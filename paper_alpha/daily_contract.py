"""Independent declarations for a bounded daily OHLCV development precheck.

A parsed declaration is not source authentication or execution authorization.
The legacy synthetic loader, research guard and Provider contexts are unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .evaluation import AVAILABILITY, EXECUTION, FIELDS, validate_config
from .expressions import MAX_WINDOW, OPERATOR_SEMANTICS_VERSION
from .market_diagnostics import DataValidationError, location, require_data
from .storage import digest, json_text

CONTRACT_ID = "real-daily-ohlcv-v1"
CAPABILITY_ID = "real-daily-capabilities-v1"
SOURCE_RECEIPT_VERSION = "real-daily-source-receipt-v1"
CANONICAL_VERSION = "daily-ohlcv-float64-content-v1"
LAYOUT = "calendar-major-asset-lexicographic-single-line-v1"
DATA_KIND = "real_daily_ohlcv"
MAX_GRID_CELLS = 100_000
MAX_RAW_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
ORIGINS = ("engineering_fixture", "real_source_declaration")
SUPPORTED_CURRENCIES = ("USD", "GBP", "EUR", "JPY", "CHF", "CAD", "AUD", "HKD", "CNY")
COLUMNS = ("date", "asset", *FIELDS)
EVENTS = ("halt", "no_quote", "listing", "delisting", "split", "dividend")
LIMITATIONS = {
    "point_in_time": False,
    "universe_history": "static_current_vintage",
    "supplier_adjustment_vintage": "current_vintage_unverified",
    "costs": "gross_only_not_modeled",
}


def _fail(code, message, path, *, value=None):
    raise DataValidationError(code, message, at=location(column=path, value=value, reason=code))


def _object(value, keys, path):
    if not isinstance(value, dict):
        _fail("invalid_metadata", "Expected an object", path)
    extras, missing = set(value) - set(keys), set(keys) - set(value)
    if extras:
        _fail("unknown_field", "Unknown declaration field", f"{path}.{sorted(extras)[0]}")
    if missing:
        _fail("missing_field", "Required declaration field is missing", f"{path}.{sorted(missing)[0]}")
    return value


def _text(value, path, *, nullable=False, identifier=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 2000:
        _fail("invalid_text", "Expected a bounded nonempty string", path)
    if identifier and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", value):
        _fail("invalid_identifier", "Identifiers cannot contain paths, URLs or whitespace", path)
    return value


def _sha(value, path, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail("invalid_digest", "Expected a lower-case SHA-256 digest", path)
    return value


def _iso_date(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("invalid_date", "Expected ISO YYYY-MM-DD", path)
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail("invalid_date", "Invalid calendar date", path)
    return value


def _utc(value, path, *, nullable=False):
    if nullable and value is None:
        return None
    _text(value, path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("invalid_timestamp", "Expected an ISO UTC acquisition/selection timestamp", path)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail("invalid_timestamp", "Timestamp must include UTC offset", path)
    return value


def _constant(value, expected, path, code="unsupported_semantics"):
    if type(value) is not type(expected) or value != expected:
        _fail(code, "Unsupported first-version declaration", path)


def _strict_copy(value):
    stack = [(value, "declaration", 0)]
    visited = 0
    while stack:
        item, path, depth = stack.pop()
        visited += 1
        if depth > 64 or visited > 500_000:
            _fail("invalid_metadata_json", "Declaration exceeds bounded JSON complexity", path)
        if type(item) is float and not math.isfinite(item):
            _fail("invalid_metadata_json", "Declaration numbers must be finite", path)
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    _fail("invalid_metadata_json", "JSON object keys must be strings", path)
                stack.append((child, path + "." + key, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, f"{path}[{index}]", depth + 1) for index, child in enumerate(item))
    try:
        return json.loads(json_text(value))
    except (ValueError, TypeError, RecursionError) as exc:
        raise DataValidationError("invalid_metadata_json", "Declaration must be finite JSON", at=location(column="declaration", reason="json_type")) from exc


@dataclass(frozen=True)
class DailyContract:
    """Immutable canonical JSON; accessors return fresh values."""
    _json: str

    def to_dict(self):
        return json.loads(self._json)

    @property
    def contract_digest(self):
        return digest(self.to_dict())

    @property
    def calendar(self):
        return tuple(self.to_dict()["calendar"]["sessions"])

    @property
    def assets(self):
        return tuple(sorted(a["id"] for a in self.to_dict()["universe"]["assets"]))

    @property
    def cutoff(self):
        return self.to_dict()["evaluation"]["splits"]["validation"]["end"]

    @property
    def development_calendar(self):
        dates = self.calendar
        return dates[:dates.index(self.cutoff) + 1]


@dataclass(frozen=True)
class SourceReceipt:
    """An independent, pinned supplier/use declaration, never authentication."""
    _json: str

    def to_dict(self):
        return json.loads(self._json)

    @property
    def declaration_digest(self):
        return digest(self.to_dict())

    @property
    def pending_facts(self):
        value = self.to_dict()
        missing = [name for name in ("provider", "source_version", "retrieved_at_utc") if value[name] is None]
        if value["use_scope"]["status"] == "pending":
            missing.append("source_use_authorization")
        return tuple(missing + ["independent_source_authentication", "approved_execution_integration"])


def parse_source_receipt(value) -> SourceReceipt:
    d = _strict_copy(value)
    _object(d, ("schema_version", "receipt_version", "origin", "source_id", "provider", "source_version",
                "retrieved_at_utc", "use_scope", "raw_sha256", "raw_bytes", "verification"), "receipt")
    _constant(d["schema_version"], 1, "receipt.schema_version")
    _constant(d["receipt_version"], SOURCE_RECEIPT_VERSION, "receipt.receipt_version")
    if d["origin"] not in ORIGINS:
        _fail("invalid_origin", "Unknown source origin", "receipt.origin")
    _text(d["source_id"], "receipt.source_id", identifier=True)
    for key in ("provider", "source_version"):
        _text(d[key], "receipt." + key, nullable=True)
    _utc(d["retrieved_at_utc"], "receipt.retrieved_at_utc", nullable=True)
    _sha(d["raw_sha256"], "receipt.raw_sha256")
    if type(d["raw_bytes"]) is not int or not 0 < d["raw_bytes"] <= MAX_RAW_BYTES:
        _fail("raw_file_limit", "Declared raw bytes must be within the bounded limit", "receipt.raw_bytes")
    use = _object(d["use_scope"], ("status", "statement", "evidence_sha256"), "receipt.use_scope")
    if use["status"] not in ("pending", "declared_unverified"):
        _fail("invalid_authorization_status", "A declaration cannot certify authorization", "receipt.use_scope.status")
    _text(use["statement"], "receipt.use_scope.statement", nullable=True)
    _sha(use["evidence_sha256"], "receipt.use_scope.evidence_sha256", nullable=True)
    if use["status"] == "declared_unverified" and (use["statement"] is None or use["evidence_sha256"] is None):
        _fail("missing_authorization_evidence", "Declared use scope needs statement and evidence digest", "receipt.use_scope")
    _constant(d["verification"], "declared_unverified", "receipt.verification")
    return SourceReceipt(json_text(d))


def parse_contract(value) -> DailyContract:
    d = _strict_copy(value)
    _object(d, ("schema_version", "contract_id", "capability_id", "data_kind", "origin", "source", "calendar",
                "universe", "fields", "evaluation", "layout", "known_events", "limitations"), "contract")
    for key, expected in (("schema_version", 1), ("contract_id", CONTRACT_ID), ("capability_id", CAPABILITY_ID),
                          ("data_kind", DATA_KIND), ("layout", LAYOUT)):
        _constant(d[key], expected, "contract." + key)
    if d["origin"] not in ORIGINS:
        _fail("invalid_origin", "Unknown contract origin", "contract.origin")
    source = _object(d["source"], ("source_id", "receipt_sha256"), "source")
    _text(source["source_id"], "source.source_id", identifier=True)
    _sha(source["receipt_sha256"], "source.receipt_sha256")
    cal = _object(d["calendar"], ("origin", "source_id", "version", "exchange", "timezone", "sessions", "evidence_sha256"), "calendar")
    expected_origin = "engineering_fixture" if d["origin"] == "engineering_fixture" else "independently_declared_exchange_calendar"
    _constant(cal["origin"], expected_origin, "calendar.origin", "calendar_origin_mismatch")
    for key in ("source_id", "version", "exchange"):
        _text(cal[key], "calendar." + key, identifier=True)
    _text(cal["timezone"], "calendar.timezone")
    try:
        ZoneInfo(cal["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        _fail("unknown_timezone", "Unknown IANA calendar timezone", "calendar.timezone")
    _sha(cal["evidence_sha256"], "calendar.evidence_sha256")
    dates = cal["sessions"]
    if not isinstance(dates, list) or not 9 <= len(dates) <= MAX_GRID_CELLS:
        _fail("invalid_calendar", "Calendar needs bounded independent session declarations", "calendar.sessions")
    for i, value in enumerate(dates):
        _iso_date(value, f"calendar.sessions[{i}]")
    if dates != sorted(set(dates)):
        _fail("invalid_calendar", "Calendar sessions must be unique and increasing", "calendar.sessions")
    uni = _object(d["universe"], ("kind", "selected_as_of_utc", "assets"), "universe")
    _constant(uni["kind"], "static_demonstration", "universe.kind")
    _utc(uni["selected_as_of_utc"], "universe.selected_as_of_utc")
    assets = uni["assets"]
    if not isinstance(assets, list) or len(assets) < 2 or len(assets) * len(dates) > MAX_GRID_CELLS:
        _fail("invalid_universe", "Declare at least two assets within the grid limit", "universe.assets")
    fields = _object(d["fields"], ("columns", "currency", "price_unit", "price_scale", "price_adjustment", "volume_unit",
                                 "volume_adjustment", "field_availability", "epsilon_101"), "fields")
    _constant(fields["columns"], list(COLUMNS), "fields.columns", "columns_mismatch")
    if not isinstance(fields["currency"], str) or fields["currency"] not in SUPPORTED_CURRENCIES:
        _fail("unknown_currency", "Declare one first-version supported currency code", "fields.currency")
    for key, expected in (("price_unit", "currency_major_unit"), ("price_adjustment", "raw_unadjusted"),
                          ("volume_unit", "shares"), ("volume_adjustment", "raw_unadjusted")):
        _constant(fields[key], expected, "fields." + key, "unsupported_units_or_adjustment")
    scale = fields["price_scale"]
    if type(scale) not in (int, float) or scale != 1:
        _fail("unsupported_price_scale", "Only unscaled major currency prices are supported", "fields.price_scale")
    _constant(fields["field_availability"], AVAILABILITY, "fields.field_availability", "unsupported_field_availability")
    eps = _object(fields["epsilon_101"], ("value", "unit", "formula_literal"), "fields.epsilon_101")
    if type(eps["value"]) not in (int, float) or eps["value"] != .001:
        _fail("unsupported_epsilon", "Preserve Alpha101 literal .001 in the declared price unit", "fields.epsilon_101.value")
    _constant(eps["unit"], fields["price_unit"], "fields.epsilon_101.unit", "epsilon_unit_mismatch")
    _constant(eps["formula_literal"], ".001", "fields.epsilon_101.formula_literal")
    ids = []
    for i, asset in enumerate(assets):
        path = f"universe.assets[{i}]"
        _object(asset, ("id", "exchange", "currency"), path)
        ids.append(_text(asset["id"], path + ".id", identifier=True))
        _constant(asset["exchange"], cal["exchange"], path + ".exchange", "mixed_exchange")
        _constant(asset["currency"], fields["currency"], path + ".currency", "mixed_currency")
    if len(set(ids)) != len(ids):
        _fail("duplicate_asset", "Duplicate asset identifier", "universe.assets")
    evaluation = _object(d["evaluation"], ("splits", "min_assets"), "evaluation")
    splits = _object(evaluation["splits"], ("train", "validation", "test"), "evaluation.splits")
    for split, bounds in splits.items():
        _object(bounds, ("start", "end"), "evaluation.splits." + split)
    try:
        config = validate_config(evaluation, dates)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise DataValidationError("invalid_evaluation", str(exc), at=location(column="evaluation", reason="split_config")) from exc
    d["evaluation"] = config
    events = d["known_events"]
    if not isinstance(events, list) or len(events) > MAX_GRID_CELLS:
        _fail("invalid_events", "Known events must be a bounded list", "known_events")
    for i, event in enumerate(events):
        path = f"known_events[{i}]"
        _object(event, ("date", "asset", "kind"), path)
        _iso_date(event["date"], path + ".date")
        if event["date"] not in dates or event["asset"] not in ids or event["kind"] not in EVENTS:
            _fail("invalid_event", "Unknown event session, asset or kind", path)
    _object(d["limitations"], LIMITATIONS, "limitations")
    for key, expected in LIMITATIONS.items():
        _constant(d["limitations"][key], expected, "limitations." + key)
    return DailyContract(json_text(d))


def require_fields(contract: DailyContract, required):
    if not isinstance(required, (list, tuple)) or not all(isinstance(x, str) for x in required):
        _fail("invalid_required_fields", "Required fields must be names", "required_fields")
    allowed = {*FIELDS, "returns"}
    for field in required:
        if field not in allowed:
            _fail("unavailable_field", "Field is unavailable; no proxy or substitution is permitted", "required_fields", value=field)
    return tuple(required)


def public_capabilities(contract: DailyContract):
    """Fixed-value allowlist: never copy provenance/free text, rows or final bounds."""
    d = parse_contract(contract.to_dict()).to_dict()
    raw = [{"name": name, "kind": "raw", "unit": "shares" if name == "volume" else "currency_major_unit",
            "currency": None if name == "volume" else d["fields"]["currency"],
            "available_at": AVAILABILITY[name], "adjustment": "raw_unadjusted"} for name in FIELDS]
    raw.append({"name": "returns", "kind": "derived", "unit": "dimensionless", "currency": None,
                "available_at": "after session close", "definition": "close[t]/close[t-1]-1; declared sessions; no fill; first row missing"})
    value = {"schema_version": 1, "capability_id": CAPABILITY_ID, "contract_id": CONTRACT_ID,
             "contract_digest": contract.contract_digest, "data_kind": DATA_KIND, "origin": d["origin"],
             "operator_semantics_version": OPERATOR_SEMANTICS_VERSION, "maximum_window": MAX_WINDOW,
             "fields": raw, "window_unit": "declared_calendar_sessions", "missing_policy": "reject_incomplete_or_nonpositive_grid",
             "alpha101_epsilon": {"value": .001, "unit": "currency_major_unit"},
             "development": {"train": deepcopy(d["evaluation"]["splits"]["train"]),
                             "validation": deepcopy(d["evaluation"]["splits"]["validation"]),
                             "min_assets": d["evaluation"]["min_assets"], "evaluation_period": "validation"},
             "execution": deepcopy(EXECUTION),
             "limitations": ["Declaration/precheck only; source authentication and execution integration remain pending.",
                             "Static current-vintage universe; supplier PIT and corporate-action completeness are not certified.",
                             "Only unadjusted OHLC and share volume; no real VWAP or proxy.",
                             "Engineering fixtures never establish real-source readiness.",
                             "Final-period values/results and private oracle/review labels are unavailable.",
                             "Legacy synthetic contexts and reuse guard are not connected by this module."]}
    value["capability_digest"] = digest(value)
    return value
