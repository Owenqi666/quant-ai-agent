"""Bounded monthly MOM/ID signal diagnostics on declared controlled fixtures.

This module does not grant market-data admission or calculate portfolio returns.
Calendar completeness is an input declaration, never inferred from return rows.
Project ID rules are explicit adaptations; the paper's unresolved daily rules
remain unresolved even when the project profile can produce a number.
"""
from __future__ import annotations

import calendar as _calendar
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import math
import re

SEMANTICS_VERSION = "monthly-mom-id-v1"
VERSION = SEMANTICS_VERSION
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_ASSETS = 128
MAX_SESSIONS = 6000
MAX_RETURNS = 100000
MAX_CALENDAR_DAYS = 7320
MAX_PANEL_CELLS = 300000
EXACT_COMPOUND_MAX_BITS = 100000

_FIELDS = {"schema_version", "mode", "mom_window_months", "mom_skip_months",
           "id_window_months", "id_skip_months", "missing_policy", "min_coverage",
           "max_missing_run", "zero_policy", "fill_policy"}
_UNRESOLVED = [
    "id_daily_window_unresolved: The author code selects DGW at target month minus one, but its internal daily window is not published.",
    "id_missing_policy_unresolved: Daily missing-value and minimum-observation rules are not confirmed.",
]
_SOURCES = [
    "Goyal, Jegadeesh and Subrahmanyam; Review of Finance 29(1), 2025, online 2024-09-24; "
    "https://doi.org/10.1093/rof/rfae038; PDF pp. 4-6, 23-25; "
    "PDF SHA256 07b8dad425b23328588a9668e8fccb58f18f79a4d1665d8fc49680198b0fbc2e",
    "Author archive V2; https://doi.org/10.7910/DVN/R1UI1J; "
    "SetupDataA.m lines 9-10 and 28; Table8B.m lines 11-23; precomputed DGW construction absent.",
]


class ProtocolError(ValueError):
    """Invalid declarations are distinct from valid but unavailable signals."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code, message):
    raise ProtocolError(code, message)


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail("invalid_fields", f"{label} must contain exactly the documented fields.")


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        _fail("invalid_integer", f"{label} must be an integer between {low} and {high}.")
    return value


def _number(value, label):
    if type(value) not in (int, float):
        _fail("invalid_number", f"{label} must be a finite number, not a boolean.")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        _fail("invalid_number", f"{label} must be finite.")
    if not math.isfinite(result):
        _fail("invalid_number", f"{label} must be finite.")
    return result


def _text(value, label, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail("invalid_text", f"{label} must be nonempty bounded text.")
    return value


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("invalid_date", f"{label} must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail("invalid_date", f"{label} must be a real calendar date.")


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        _fail("invalid_month", "target_month must use YYYY-MM.")
    try:
        result = date.fromisoformat(value + "-01")
    except ValueError:
        _fail("invalid_month", "target_month must be a real calendar month.")
    if not 1905 <= result.year <= 2099:
        _fail("invalid_month", "target_month year must be between 1905 and 2099.")
    return result


def _shift_month(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _last_day(value):
    return value.replace(day=_calendar.monthrange(value.year, value.month)[1])


def _canonical(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        _fail("invalid_json", "Input must be finite JSON data.")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def presets() -> list[dict]:
    common = {"schema_version": 1, "mom_window_months": 11, "mom_skip_months": 1,
              "zero_policy": "include", "fill_policy": "none"}
    return [
        {"id": "paper_rof_id_v1", "title": "论文原文（ID 规则待确认）",
         "description": "MOM 月份可确认；ID 内部日频窗口与缺失规则仍未决，不执行信号计算。",
         "config": {**common, "mode": "paper", "id_window_months": None,
                    "id_skip_months": None, "missing_policy": "unresolved",
                    "min_coverage": None, "max_missing_run": None},
         "sources": list(_SOURCES), "unresolved": list(_UNRESOLVED)},
        {"id": "project_id_11m_skip1_complete_v1", "title": "项目约定：11 个月、跳过 1 个月、完整覆盖",
         "description": "MOM 与 ID 使用同一十一自然月窗口，零收益保留、缺失不填；这是项目约定，未声称论文原式复现。",
         "config": {**common, "mode": "project", "id_window_months": 11,
                    "id_skip_months": 1, "missing_policy": "complete",
                    "min_coverage": 1.0, "max_missing_run": 0},
         "sources": list(_SOURCES) + ["Project adaptation monthly-mom-id-v1: project-defined daily-window and missing-data rules; see frozen config and changes; not an original-paper replication."],
         "unresolved": list(_UNRESOLVED)},
    ]


def validate_config(config) -> dict:
    _object(config, _FIELDS, "config")
    _integer(config["schema_version"], 1, 1, "schema_version")
    if config["mode"] not in ("paper", "project"):
        _fail("invalid_mode", "mode must be paper or project.")
    if config["zero_policy"] != "include" or config["fill_policy"] != "none":
        _fail("unsupported_policy", "Zero returns must be retained and missing returns must not be filled.")
    _integer(config["mom_window_months"], 1, 36, "mom_window_months")
    _integer(config["mom_skip_months"], 0, 12, "mom_skip_months")
    if config["mode"] == "paper":
        if config != presets()[0]["config"]:
            _fail("paper_profile_modified", "Unresolved paper rules cannot be replaced under paper identity; use project mode.")
        return deepcopy(config)
    result = deepcopy(config)
    _integer(config["id_window_months"], 1, 36, "id_window_months")
    _integer(config["id_skip_months"], 0, 12, "id_skip_months")
    if config["missing_policy"] not in ("complete", "available"):
        _fail("unsupported_policy", "Project missing_policy must be complete or available.")
    result["min_coverage"] = _number(config["min_coverage"], "min_coverage")
    if not 0 < result["min_coverage"] <= 1:
        _fail("invalid_coverage", "min_coverage must be greater than zero and at most one.")
    _integer(config["max_missing_run"], 0, 366, "max_missing_run")
    if config["missing_policy"] == "complete" and (result["min_coverage"] != 1 or config["max_missing_run"] != 0):
        _fail("inconsistent_complete_policy", "complete requires min_coverage=1 and max_missing_run=0.")
    return result


def config_digest(config) -> str:
    """Canonical normalized configuration identity shared by preview and storage."""
    return _digest(validate_config(config))


def resolve_windows(config, target_month) -> dict:
    config = validate_config(config)
    target = _month(target_month)

    def window(length, skip):
        if length is None:
            return None
        end_month = _shift_month(target, -1 - skip)
        return {"start": _shift_month(end_month, 1 - length).isoformat(),
                "end": _last_day(end_month).isoformat()}

    return {"target_month": target_month, "as_of": (target - timedelta(days=1)).isoformat(),
            "momentum": window(config["mom_window_months"], config["mom_skip_months"]),
            "id": window(config["id_window_months"], config["id_skip_months"])}


def demo_bundle(target_month) -> dict:
    """A fictional Monday-Friday calendar, not any exchange's trading schedule."""
    target = _month(target_month)
    start = _shift_month(target, -48)
    end = _last_day(target)
    sessions = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    assets = [{"id": name, "history_start": start.isoformat()}
              for name in ("COMPLETE", "MISSING_ROW", "NULL_VALUE", "ASYMMETRIC")]
    recent = _shift_month(target, -2).isoformat()
    assets.append({"id": "SHORT_HISTORY", "history_start": recent})
    rows = []
    # In every month: +10%, -5%, then zeros, giving a full-month return of 4.5%.
    monthly_position = {}
    for session in sessions:
        month = session[:7]
        position = monthly_position.get(month, 0)
        monthly_position[month] = position + 1
        value = 0.1 if position == 0 else (-0.05 if position == 1 else 0.0)
        for asset in assets:
            if session < asset["history_start"]:
                continue
            if asset["id"] == "MISSING_ROW" and position == 2:
                continue
            rows.append({"date": session, "asset": asset["id"],
                         "value": (None if asset["id"] == "NULL_VALUE" and position == 2
                                   else 0.01 if asset["id"] == "ASYMMETRIC" and position == 2 else value)})
    return {"schema_version": 1, "data_kind": "controlled_fixture",
            "source_id": "fictional-monthly-demo-v1", "return_semantics": "daily_total_return_decimal",
            "calendar": {"id": "fictional-weekdays-not-exchange-calendar", "version": "1",
                         "start": start.isoformat(), "end": end.isoformat(), "sessions": sessions},
            "assets": assets, "returns": rows}


def _validate_bundle(bundle, windows):
    _object(bundle, {"schema_version", "data_kind", "source_id", "return_semantics", "calendar", "assets", "returns"}, "bundle")
    _integer(bundle["schema_version"], 1, 1, "bundle.schema_version")
    if bundle["data_kind"] != "controlled_fixture":
        _fail("data_kind_not_supported", "Only controlled_fixture data is admitted by this tool.")
    if bundle["return_semantics"] != "daily_total_return_decimal":
        _fail("return_semantics_not_supported", "Use declared daily total returns in decimal units, not prices or percentages.")
    _text(bundle["source_id"], "source_id")
    cal = bundle["calendar"]
    _object(cal, {"id", "version", "start", "end", "sessions"}, "calendar")
    _text(cal["id"], "calendar.id")
    _text(cal["version"], "calendar.version")
    start, end = _date(cal["start"], "calendar.start"), _date(cal["end"], "calendar.end")
    if end < start or (end - start).days + 1 > MAX_CALENDAR_DAYS:
        _fail("calendar_bounds", "Calendar coverage must be ordered and within the bounded date span.")
    sessions = cal["sessions"]
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= MAX_SESSIONS:
        _fail("session_limit", "Provide a nonempty bounded complete declared session list.")
    previous = None
    for session in sessions:
        parsed = _date(session, "session")
        if parsed < start or parsed > end or (previous is not None and session <= previous):
            _fail("invalid_sessions", "Sessions must be unique, strictly ordered, and within declared calendar coverage.")
        previous = session
    for name in ("momentum", "id"):
        window = windows[name]
        if window is not None and (cal["start"] > window["start"] or cal["end"] < window["end"]):
            _fail("calendar_coverage_insufficient", "Calendar must cover each complete required window, not only its observed rows.")
    assets = bundle["assets"]
    rows = bundle["returns"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_ASSETS:
        _fail("asset_limit", "Provide a bounded nonempty asset list.")
    if len(assets) * len(sessions) > MAX_PANEL_CELLS:
        _fail("panel_limit", "Declared calendar by asset product is too large.")
    if not isinstance(rows, list) or len(rows) > MAX_RETURNS:
        _fail("return_limit", "Return row count exceeds the bounded input limit.")
    asset_map = {}
    for asset in assets:
        _object(asset, {"id", "history_start"}, "asset")
        name = _text(asset["id"], "asset.id", 128)
        if name in asset_map:
            _fail("duplicate_asset", "Asset IDs must be unique.")
        _date(asset["history_start"], "asset.history_start")
        asset_map[name] = asset["history_start"]
    session_set = set(sessions)
    by_asset = {name: {} for name in asset_map}
    for row in rows:
        _object(row, {"date", "asset", "value"}, "return row")
        day = row["date"]
        _date(day, "return.date")
        name = row["asset"]
        if not isinstance(name, str) or name not in asset_map:
            _fail("unknown_asset", "Every return row must reference a declared asset.")
        if day not in session_set:
            _fail("non_session_return", "Return rows must occur on declared sessions.")
        if day < asset_map[name]:
            _fail("return_before_history", "Return rows cannot precede declared history_start.")
        if day in by_asset[name]:
            _fail("duplicate_return", "Asset/date return keys must be unique.")
        value = row["value"]
        if value is not None:
            value = _number(value, "return.value")
            if value < -1:
                _fail("invalid_return", "Total return cannot be less than -100%.")
        by_asset[name][day] = value
    raw = _canonical(bundle)
    if len(raw) > MAX_BUNDLE_BYTES:
        _fail("bundle_size_limit", "Bundle exceeds maximum encoded size.")
    return sessions, asset_map, by_asset, hashlib.sha256(raw).hexdigest()


def _coverage(sessions, values, window):
    required = [day for day in sessions if window["start"] <= day <= window["end"]]
    valid_values, missing = [], []
    positive = negative = zero = missing_rows = null_values = run = longest = 0
    for day in required:
        if day not in values or values[day] is None:
            reason = "missing_row" if day not in values else "null_value"
            missing_rows += reason == "missing_row"
            null_values += reason == "null_value"
            missing.append({"date": day, "reason": reason})
            run += 1
            longest = max(longest, run)
        else:
            value = values[day]
            valid_values.append(value)
            positive += value > 0
            negative += value < 0
            zero += value == 0
            run = 0
    expected, valid = len(required), len(valid_values)
    return {"expected": expected, "valid": valid, "positive": positive,
            "negative": negative, "zero": zero, "missing_rows": missing_rows,
            "null_values": null_values, "coverage": valid / expected if expected else 0.0,
            "max_missing_run": longest, "missing_dates": missing}, valid_values


def _exact_near_zero_product(values):
    """Exact product of (1 + binary64 return), with an explicit integer budget.

    Float ratios have power-of-two denominators. Cancel powers of two first,
    then compare the integer numerator to its denominator. This resolves true
    zero/sign boundaries without treating a tiny legitimate signal as zero.
    """
    numerator, exponent, estimated_bits = 1, 0, 0
    for value in values:
        if value == 0:
            continue
        n, d = value.as_integer_ratio()
        factor = n + d
        trailing = (factor & -factor).bit_length() - 1
        factor >>= trailing
        exponent += trailing - (d.bit_length() - 1)
        if factor != 1:
            estimated_bits += factor.bit_length()
        if max(estimated_bits, abs(exponent)) > EXACT_COMPOUND_MAX_BITS:
            return None, "compound_precision_budget_exceeded"
        numerator *= factor
    if exponent >= 0:
        difference, denominator = (numerator << exponent) - 1, 1
    else:
        denominator = 1 << -exponent
        difference = numerator - denominator
    try:
        result = difference / denominator
    except OverflowError:
        return None, "nonfinite_compound"
    if result == 0 and difference != 0:
        return None, "compound_precision_underflow"
    return result, None


def _compound(values):
    # Stable binary64 log compounding preserves small nonzero returns instead of
    # rounding every (1+r) to one. No global epsilon, clipping, or fill is applied.
    # A genuine -100% observation makes the entire compounded gross return zero.
    if -1.0 in values:
        return -1.0, None
    try:
        logs = [math.log1p(value) for value in values]
        log_return = math.fsum(logs)
        # This is a trigger for exact arithmetic, never a threshold for zeroing
        # the answer. It covers accumulated log rounding near the ID sign edge.
        rounding_bound = 4 * math.fsum(math.ulp(term) for term in logs)
        if abs(log_return) <= rounding_bound:
            return _exact_near_zero_product(values)
        result = math.expm1(log_return)
    except (OverflowError, ValueError):
        return None, "nonfinite_compound"
    return (result, None) if math.isfinite(result) else (None, "nonfinite_compound")


def _signal(config, sessions, values, window, history_start, field):
    coverage, observed = _coverage(sessions, values, window)
    reasons = []
    if window["start"] < history_start:
        reasons.append(f"{field}:history_insufficient")
    if coverage["expected"] == 0:
        reasons.append(f"{field}:no_expected_sessions")
    if coverage["valid"] == 0:
        reasons.append(f"{field}:no_valid_returns")
    if coverage["coverage"] < config["min_coverage"]:
        reasons.append(f"{field}:coverage_below_minimum")
    if coverage["max_missing_run"] > config["max_missing_run"]:
        reasons.append(f"{field}:missing_run_exceeded")
    if reasons:
        return None, coverage, reasons
    value, numerical_reason = _compound(observed)
    if numerical_reason:
        reasons.append(f"{field}:{numerical_reason}")
    return value, coverage, reasons


def preview(config, target_month, bundle=None) -> dict:
    config = validate_config(config)
    windows = resolve_windows(config, target_month)
    if bundle is None:
        bundle = demo_bundle(target_month)
    sessions, assets, returns, input_digest = _validate_bundle(bundle, windows)
    warnings = ["controlled_fixture_only: Signal and coverage diagnostics; no real-market backtest or portfolio performance is calculated.",
                "calendar_declared: Complete sessions and return adjustment semantics are input declarations, not externally verified market facts."]
    result = {"schema_version": 1, "semantics_version": SEMANTICS_VERSION,
              "config": config, "config_digest": config_digest(config), "input_digest": input_digest,
              "data_kind": bundle["data_kind"], "source_id": bundle["source_id"],
              "windows": windows, "status": "blocked", "warnings": warnings, "assets": []}
    if config["mode"] == "paper":
        warnings.extend(_UNRESOLVED)
        for name in sorted(assets):
            result["assets"].append({"asset": name, "status": "blocked", "momentum": None,
                                     "pret": None, "id": None, "momentum_coverage": None,
                                     "id_coverage": None, "reasons": list(_UNRESOLVED)})
        return result
    warnings.append("project_adaptation: The executable daily-window and missing-data choices are project rules, not verified original-paper rules.")
    if config["missing_policy"] == "available":
        warnings.append("observed_returns_only: available compounds only observed valid daily returns; with missing observations this is not the complete-window return. Missing returns are not filled, and missing days do not enter the ID denominator.")
    for name in sorted(assets):
        momentum, mom_coverage, mom_reasons = _signal(config, sessions, returns[name], windows["momentum"], assets[name], "momentum")
        pret, id_coverage, id_reasons = _signal(config, sessions, returns[name], windows["id"], assets[name], "id")
        identifier = None
        if pret is not None:
            sign = (pret > 0) - (pret < 0)
            identifier = sign * (id_coverage["negative"] - id_coverage["positive"]) / id_coverage["valid"]
        result["assets"].append({"asset": name,
                                 "status": "ready" if momentum is not None and identifier is not None else "unavailable",
                                 "momentum": momentum, "pret": pret, "id": identifier,
                                 "momentum_coverage": mom_coverage, "id_coverage": id_coverage,
                                 "reasons": mom_reasons + id_reasons})
    if all(asset["status"] == "ready" for asset in result["assets"]):
        result["status"] = "ready"
    elif any(asset["momentum"] is not None or asset["id"] is not None for asset in result["assets"]):
        result["status"] = "partial"
    return result
