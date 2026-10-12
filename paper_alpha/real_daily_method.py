"""Versioned OHLC price-return method and independent Alpha101 scalar oracle.

This module performs no I/O or authority operations. Cash distributions remain
evidence metadata and are never added to raw-price labels. Existing OHLCV and
fixture-only contracts retain their original behavior.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import math
import re

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype

from . import evaluation
from .daily_event_review import DailyEventReviewError, assess_event_policy, parse_event_ledger
from .storage import digest, json_text

METHOD_ID = "daily-raw-price-ohlc-method-v1"
EVENT_REVIEW_VERSION = "daily-raw-price-event-assessment-v1"
ORACLE_VERSION = "daily-scalar-alpha101-v1"
REQUIRED_FIELDS = ("open", "high", "low", "close")
EXPRESSION_101 = "((close - open) / ((high - low) + .001))"
MAX_SESSIONS = 1024
MAX_ASSETS = 128


class RealDailyMethodError(ValueError):
    """Bounded machine-readable failure; never copies market or evidence text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise RealDailyMethodError(code)


def _return_convention():
    return {"formula": "open[t+2] / open[t+1] - 1", "kind": "gross_price_return",
            "cash_dividends": "excluded", "costs": "excluded", "slippage": "excluded",
            "borrow": "excluded", "total_return": False}


def method_profile():
    """Fresh closed JSON profile; caller mutations never modify method semantics."""
    return {"schema_version": 1, "method_id": METHOD_ID,
            "required_fields": list(REQUIRED_FIELDS), "price_adjustment": "raw_unadjusted",
            "price_unit": "currency_major_unit", "price_scale": 1,
            "signal_timing": "after_session_t_close", "entry_offset_sessions": 1,
            "exit_offset_sessions": 2, "return_convention": _return_convention(),
            "event_policy": "known_cash_dividends_excluded_all_other_development_events_rejected",
            "unknown_event_dates": "blocked", "unknown_event_coverage": "blocked",
            "missing_policy": "reject_no_fill_no_calendar_compression",
            "candidate": {"id": "alpha101-101", "expression": EXPRESSION_101,
                          "epsilon": {"value": .001, "unit": "currency_major_unit", "formula_literal": ".001"}},
            "volume": "unavailable_not_required_no_proxy", "total_return": False,
            "configuration_origin": "project_configuration", "final_numeric_read_allowed": False}


def method_digest():
    return digest(method_profile())


def _date(value):
    _need(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
          "invalid_selection_date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise RealDailyMethodError("invalid_selection_date") from None
    return value


def _selection(selection):
    """Check core frozen selection without relying on evaluator arithmetic."""
    _need(type(selection) is dict and
          {"assets", "sessions", "development_sessions", "config"} <= set(selection),
          "invalid_selection")
    assets, sessions, development, config = (selection[name] for name in
                                            ("assets", "sessions", "development_sessions", "config"))
    _need(type(assets) is list and 3 <= len(assets) <= MAX_ASSETS and
          all(type(asset) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", asset)
              for asset in assets) and assets == sorted(set(assets)), "invalid_selection_assets")
    _need(type(sessions) is list and 9 <= len(sessions) <= MAX_SESSIONS, "invalid_selection_sessions")
    for day in sessions:
        _date(day)
    _need(sessions == sorted(set(sessions)), "invalid_selection_sessions")
    _need(type(config) is dict and set(config) == {"splits", "min_assets"} and
          type(config["min_assets"]) is int and 3 <= config["min_assets"] <= len(assets),
          "invalid_selection_config")
    splits = config["splits"]
    _need(type(splits) is dict and set(splits) == {"train", "validation", "test"},
          "invalid_selection_config")
    cursor, partitions = 0, {}
    for name in ("train", "validation", "test"):
        bounds = splits[name]
        _need(type(bounds) is dict and set(bounds) == {"start", "end"}, "invalid_selection_config")
        start, end = _date(bounds["start"]), _date(bounds["end"])
        _need(start in sessions and end in sessions, "invalid_selection_config")
        first, last = sessions.index(start), sessions.index(end)
        _need(first == cursor and last - first >= 2, "selection_partitions_not_contiguous")
        partitions[name] = sessions[first:last + 1]
        cursor = last + 1
    _need(cursor == len(sessions), "selection_partitions_not_contiguous")
    expected = partitions["train"] + partitions["validation"]
    _need(type(development) is list and development == expected, "development_prefix_required")
    _need(len(expected) * len(assets) <= evaluation.MAX_PANEL_CELLS, "panel_cell_limit")
    _need((len(partitions["validation"]) - 2) * len(assets) <= evaluation.MAX_EVALUATION_CELLS,
          "evaluation_cell_limit")
    try:
        json_text(selection)
    except (TypeError, ValueError, RecursionError):
        raise RealDailyMethodError("invalid_selection_json") from None
    # Never mutate source metadata, final partition status, or acquisition times.
    return deepcopy(assets), deepcopy(sessions), deepcopy(expected), deepcopy(config)


def assess_events(ledger, *, selection, cash_annotations):
    """Retain all coverage/date blockers; allow only known, bound cash exceptions.

    An annotation is an evidence declaration, not independent authentication.
    Its source refs must already belong to the exact event and asset/type.
    """
    assets, sessions, development, _ = _selection(selection)
    try:
        declared = parse_event_ledger(ledger, assets=assets, sessions=sessions)
        existing = assess_event_policy(declared, assets=assets, sessions=sessions,
                                       development_sessions=development)
    except DailyEventReviewError as exc:
        raise RealDailyMethodError(exc.code) from None
    _need(type(cash_annotations) is list and len(cash_annotations) <= len(declared["events"]),
          "invalid_cash_annotations")
    events = {event["id"]: event for event in declared["events"]}
    annotations, annotated_ids = [], set()
    for item in cash_annotations:
        _need(type(item) is dict and set(item) == {"event_id", "subtype", "statement", "evidence_refs"},
              "invalid_cash_annotation_schema")
        event_id = item["event_id"]
        _need(type(event_id) is str and event_id in events and event_id not in annotated_ids,
              "unbound_or_duplicate_cash_event")
        _need(type(item["subtype"]) is str and item["subtype"] == "cash_dividend",
              "unsupported_dividend_subtype")
        statement = item["statement"]
        _need(type(statement) is str and 0 < len(statement) <= 2000 and statement.strip() == statement and
              not any(ord(c) < 32 or ord(c) == 127 for c in statement), "invalid_cash_statement")
        event = events[event_id]
        refs = item["evidence_refs"]
        _need(event["event_type"] == "dividend", "cash_annotation_requires_dividend")
        _need(type(refs) is list and 0 < len(refs) <= len(event["evidence_refs"]) and
              all(type(ref) is str for ref in refs) and len(refs) == len(set(refs)) and
              set(refs) <= set(event["evidence_refs"]), "cash_reference_not_bound_to_event")
        annotated_ids.add(event_id)
        annotations.append(deepcopy(item))
    accepted = [dict(deepcopy(event), cash_annotation=deepcopy(next(
                    item for item in annotations if item["event_id"] == event["id"])),
                    treatment="cash_distribution_excluded_from_raw_price_return")
                for event in existing["known_events"]
                if event["location"] == "development" and event["id"] in annotated_ids]
    accepted_ids = {event["id"] for event in accepted}
    blockers = [deepcopy(blocker) for blocker in existing["blockers"]
                if not (blocker["code"] == "unsupported_development_event" and
                        blocker["event_id"] in accepted_ids)]
    report = {"schema_version": 1, "review_version": EVENT_REVIEW_VERSION,
              "method_id": METHOD_ID, "method_digest": method_digest(),
              "ledger_digest": digest(declared), "canonical_ledger_digest": existing["canonical_ledger_digest"],
              "selection_digest": digest(selection), "cash_annotations_digest": digest(cash_annotations),
              "coverage_summary": deepcopy(existing["coverage_summary"]), "coverage": deepcopy(existing["coverage"]),
              "known_events": deepcopy(existing["known_events"]), "unknown_events": deepcopy(existing["unknown_events"]),
              "accepted_cash": accepted, "cash_annotations": annotations,
              "unsupported_development_events": [deepcopy(event) for event in existing["unsupported_development_events"]
                                                   if event["id"] not in accepted_ids],
              "blockers": blockers, "status": "blocked" if blockers else "method_clear_pending_authority",
              "source_admission": "pending", "execution_allowed": False,
              "independent_authentication": "pending", "numeric_market_rows_parsed": 0,
              "final_numeric_rows_parsed": 0}
    json_text(report)
    return report


def _validated_panels(panels, selection):
    assets, sessions, development, config = _selection(selection)
    _need(type(panels) is dict and set(panels) == set(REQUIRED_FIELDS), "exact_ohlc_fields_required")
    expected_index = pd.DatetimeIndex(development, name="date")
    values = {}
    for field in REQUIRED_FIELDS:
        panel = panels[field]
        _need(type(panel) is pd.DataFrame and type(panel.index) is pd.DatetimeIndex and
              panel.index.tz is None and panel.index.equals(expected_index) and
              list(panel.columns) == assets and panel.columns.is_unique, "unaligned_development_panel")
        _need(all(is_numeric_dtype(dtype) and not is_bool_dtype(dtype) and not is_complex_dtype(dtype)
                  for dtype in panel.dtypes),
              "nonnumeric_ohlc")
        try:
            array = panel.to_numpy(dtype=np.float64)
        except (ValueError, TypeError, OverflowError):
            raise RealDailyMethodError("nonnumeric_ohlc") from None
        _need(np.isfinite(array).all() and (array > 0).all(), "nonfinite_or_nonpositive_ohlc")
        values[field] = array
    _need((values["low"] <= np.minimum(values["open"], values["close"])).all() and
          (np.maximum(values["open"], values["close"]) <= values["high"]).all(), "invalid_ohlc_bounds")
    return assets, sessions, development, config, values


def evaluate_ohlc(factor, panels, *, selection):
    """Validate real four-field prefix and reuse established validation arithmetic.

    This numerical boundary has no authority to admit a source or unlock final
    values; the host must validate admission and registration independently.
    """
    assets, sessions, development, config, _ = _validated_panels(panels, selection)
    expected_index = panels["open"].index
    _need(type(factor) is pd.DataFrame and factor.index.equals(expected_index) and
          list(factor.columns) == assets and factor.columns.is_unique, "unaligned_factor")
    _need(all(is_numeric_dtype(dtype) and not is_bool_dtype(dtype) and not is_complex_dtype(dtype)
              for dtype in factor.dtypes), "nonnumeric_factor")
    try:
        values = factor.to_numpy(dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        raise RealDailyMethodError("nonnumeric_factor") from None
    _need(not np.isinf(values).any(), "infinite_factor")
    # NaN factor observations retain the shared evaluator's coverage semantics.
    try:
        evaluation.validate_config(config, sessions)
        result = evaluation._evaluate_validation(factor, panels, config, "validation", development)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        raise RealDailyMethodError("invalid_price_evaluation") from None
    result.update(method_id=METHOD_ID, method_digest=method_digest(), return_convention=_return_convention(),
                  selection_digest=digest(selection), evaluation_scope="development_prefix_validation_only",
                  test_freshness="unknown", final_numeric_rows_used=0, source_authentication="not_performed")
    result["limitations"][0] = "Source-neutral engineering price-return calculation; source/use/PIT authority must be checked separately."
    result["limitations"].extend([
        "Raw unadjusted OHLC; cash distributions are excluded. This is not total return or net trading P&L.",
        "This four-field method provides no volume field or proxy; final market values are unavailable."])
    json_text(result)
    return result


def _average_ranks(values):
    """Pure Python ranks with exact ties, independent of pandas/evaluator ranks."""
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    first = 0
    while first < len(ordered):
        last = first + 1
        while last < len(ordered) and values[ordered[last]] == values[ordered[first]]:
            last += 1
        average = ((first + 1) + last) / 2.0
        for position in range(first, last):
            ranks[ordered[position]] = average
        first = last
    return ranks


def _scalar101(values, assets, development, config):
    """Compute factor/labels/weights/IC/metrics using stdlib scalar arithmetic."""
    first = development.index(config["splits"]["validation"]["start"])
    last = development.index(config["splits"]["validation"]["end"])
    eligible = last - first - 1
    daily, ics, gross_values = [], [], []
    for position in range(first, last + 1):
        item = {"signal_date": development[position], "entry_date": None, "exit_date": None,
                "status": "purged", "reason": "label_would_cross_split_boundary",
                "available_assets": 0, "missing_assets": [], "rank_ic": None,
                "rank_ic_state": "not_evaluated", "gross_return": None, "assets": []}
        if position + 2 > last:
            daily.append(item)
            continue
        item.update(entry_date=development[position + 1], exit_date=development[position + 2], available_assets=len(assets))
        signals = [(float(values["close"][position, column]) - float(values["open"][position, column])) /
                   ((float(values["high"][position, column]) - float(values["low"][position, column])) + .001)
                   for column in range(len(assets))]
        _need(all(math.isfinite(signal) for signal in signals), "nonfinite_scalar_signal")
        ranks = _average_ranks(signals)
        mean_rank = math.fsum(ranks) / len(ranks)
        centered = [rank - mean_rank for rank in ranks]
        gross = math.fsum(abs(rank) for rank in centered)
        if gross == 0:
            item.update(status="skipped", reason="constant_factor")
            daily.append(item)
            continue
        weights = [rank / gross for rank in centered]
        entry = [float(values["open"][position + 1, column]) for column in range(len(assets))]
        exit_prices = [float(values["open"][position + 2, column]) for column in range(len(assets))]
        labels = [sell / buy - 1 for buy, sell in zip(entry, exit_prices)]
        _need(all(math.isfinite(label) for label in labels), "nonfinite_scalar_label")
        return_ranks = _average_ranks(labels)
        mean_return_rank = math.fsum(return_ranks) / len(return_ranks)
        centered_returns = [rank - mean_return_rank for rank in return_ranks]
        if any(rank != 0 for rank in centered_returns):
            ic = math.fsum(x * y for x, y in zip(centered, centered_returns)) / math.sqrt(
                math.fsum(x * x for x in centered) * math.fsum(y * y for y in centered_returns))
            item.update(rank_ic=ic, rank_ic_state="defined")
            ics.append(ic)
        else:
            item["rank_ic_state"] = "constant_forward_returns"
        contributions = [weight * label for weight, label in zip(weights, labels)]
        gross_return = math.fsum(contributions)
        _need(math.isfinite(gross_return), "nonfinite_scalar_return")
        gross_values.append(gross_return)
        item.update(status="evaluated", reason=None, gross_return=gross_return)
        item["assets"] = [{"asset": asset, "signal": signal, "weight": weight, "entry_open": buy,
                           "exit_open": sell, "forward_return": label, "gross_contribution": contribution}
                          for asset, signal, weight, buy, sell, label, contribution in
                          zip(assets, signals, weights, entry, exit_prices, labels, contributions)]
        daily.append(item)
    metrics = {"mean_rank_ic": math.fsum(ics) / len(ics) if ics else None,
               "rank_ic_days": len(ics), "mean_gross_return": math.fsum(gross_values) / len(gross_values) if gross_values else None,
               "sum_gross_return": math.fsum(gross_values) if gross_values else None,
               "evaluated_days": len(gross_values), "eligible_days": eligible,
               "skipped_days": eligible - len(gross_values), "purged_days": 2,
               "finite_factor_observations": eligible * len(assets),
               "possible_factor_observations": eligible * len(assets), "factor_coverage": 1.0}
    return {"status": "evaluated" if gross_values else "not_evaluable",
            "reason": None if gross_values else "No nonconstant factor cross section with sufficient finite assets",
            "metrics": metrics, "daily": daily}


def verify_scalar101(panels, *, selection, result, tolerance=1e-12):
    """Cross-check tool output against independently calculated stdlib scalars.

    No expression engine or evaluator is called. NaN/Inf, missing/extra metric
    fields, reordered records, bool-as-number and configuration drift fail.
    """
    _need(type(tolerance) in (int, float) and math.isfinite(tolerance) and 0 <= tolerance <= 1e-9,
          "invalid_scalar_tolerance")
    assets, _sessions, development, config, values = _validated_panels(panels, selection)
    _need(type(result) is dict, "invalid_scalar_result")
    try:
        json_text(result)
    except (ValueError, TypeError, RecursionError):
        raise RealDailyMethodError("invalid_scalar_result_json") from None
    identity = {"method_id": METHOD_ID, "method_digest": method_digest(),
                "return_convention": _return_convention(), "selection_digest": digest(selection),
                "config": config, "split": "validation", "execution": deepcopy(evaluation.EXECUTION),
                "evaluation_scope": "development_prefix_validation_only", "final_numeric_rows_used": 0,
                "test_freshness": "unknown", "source_authentication": "not_performed"}
    for key, expected in identity.items():
        _need(key in result and type(result[key]) is type(expected) and
              json_text(result[key]) == json_text(expected),
              "scalar_identity_mismatch")
    expected = _scalar101(values, assets, development, config)
    maximum, checked_numbers = 0.0, 0

    def compare(actual, wanted):
        nonlocal maximum, checked_numbers
        if type(wanted) is dict:
            _need(type(actual) is dict and set(actual) == set(wanted), "scalar_result_shape_mismatch")
            for key, value in wanted.items():
                compare(actual[key], value)
        elif type(wanted) is list:
            _need(type(actual) is list and len(actual) == len(wanted), "scalar_result_shape_mismatch")
            for got, value in zip(actual, wanted):
                compare(got, value)
        elif type(wanted) is float:
            _need(type(actual) in (int, float) and math.isfinite(actual), "scalar_numeric_type_mismatch")
            error = abs(float(actual) - wanted)
            _need(error <= tolerance, "scalar_numeric_mismatch")
            maximum = max(maximum, error)
            checked_numbers += 1
        else:
            _need(type(actual) is type(wanted) and actual == wanted, "scalar_value_mismatch")

    for key in ("status", "reason", "metrics", "daily"):
        _need(key in result, "scalar_result_shape_mismatch")
        compare(result[key], expected[key])
    return {"schema_version": 1, "oracle_version": ORACLE_VERSION, "status": "verified",
            "method_id": METHOD_ID, "method_digest": method_digest(), "selection_digest": digest(selection),
            "result_digest": digest(result), "metrics_checked": len(expected["metrics"]),
            "daily_records_checked": len(expected["daily"]),
            "asset_records_checked": sum(len(day["assets"]) for day in expected["daily"]),
            "numeric_values_checked": checked_numbers, "max_absolute_error": maximum,
            "absolute_tolerance": tolerance, "arithmetic": "independent_python_stdlib_scalar",
            "final_numeric_rows_used": 0}
