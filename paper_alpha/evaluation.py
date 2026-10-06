"""Strict fixture ingestion and validation-only, next-open research evaluation.

This is a transparent software-validation calculation, not a trading simulator.
The evaluator consumes a computed panel; causality of arbitrary caller-supplied
Python factors cannot be inferred here and must be tested at the computation layer.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import io
from pathlib import Path
import re

import numpy as np
import pandas as pd

from .contracts import require
from .market_diagnostics import (DataValidationError, SAMPLE_LIMIT, csv_record_rows,
                                 location, require_data)
from .storage import json_text, read_json


FIELDS = ("open", "high", "low", "close", "volume")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PANEL_CELLS = 2_000_000
MAX_EVALUATION_CELLS = 100_000
AVAILABILITY = {field: "after session close" for field in FIELDS}
AVAILABILITY["open"] = "session open"
EXECUTION = {
    "signal": "after session t close",
    "entry": "session t+1 open",
    "exit": "session t+2 open",
    "label": "open[t+2] / open[t+1] - 1",
    "split_boundary": "signal, entry and exit must all lie inside the selected split",
    "rank_ic": "Pearson correlation of average cross-sectional ranks (Spearman)",
    "rank_ic_aggregation": "arithmetic mean over evaluated days with nonconstant forward-return ranks; undefined days are excluded and counted",
    "gross_return_aggregation": "mean and sum over evaluated days only; insufficient or constant signal cross sections are skipped, not assigned zero returns",
    "cross_section": "finite signal observations only, subject to min_assets; coverage and missing assets are reported",
    "weights": "average signal ranks minus their mean, divided by sum of absolute centered ranks",
    "gross_exposure": 1.0,
    "net_exposure": 0.0,
    "costs": "excluded; all return statistics are gross",
    "tradability": "not modeled: no costs, slippage, borrow, fills or capacity",
}


def _iso_date(value, field):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
            f"{field} must be an ISO YYYY-MM-DD date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {value}") from exc


def _calendar(values):
    require(isinstance(values, (list, tuple, pd.Index)) and len(values) > 0,
            "calendar must contain independently declared sessions")
    if isinstance(values, pd.DatetimeIndex):
        require(values.tz is None and values.equals(values.normalize()),
                "calendar must contain timezone-free midnight sessions")
        values = values.strftime("%Y-%m-%d").tolist()
    dates = [_iso_date(value, "calendar date") for value in values]
    require(len(set(dates)) == len(dates), "Duplicate calendar dates")
    require(dates == sorted(dates), "Calendar dates must be strictly increasing")
    return dates


def load_market(data_path, metadata_path) -> dict[str, pd.DataFrame]:
    """Load the strict, complete OHLCV grid with bounded structured diagnostics.

    Original bytes and calculation semantics are preserved. The same authority
    is used by execution and import validation; absent rows are never repaired.
    """
    require_data(0 < Path(metadata_path).stat().st_size <= MAX_FILE_BYTES,
                 "metadata_file_limit", "Metadata file exceeds the 64 MiB limit or is empty")
    try:
        metadata = read_json(metadata_path)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DataValidationError("invalid_metadata_json", "Metadata must contain strict JSON without duplicate keys or nonfinite values") from exc
    require_data(isinstance(metadata, dict), "invalid_metadata", "Market metadata must be an object")
    with Path(data_path).open("rb") as stream:
        payload = stream.read(MAX_FILE_BYTES + 1)
    require_data(0 < len(payload) <= MAX_FILE_BYTES,
                 "market_file_limit", "Market file exceeds the 64 MiB limit or is empty")
    checksum = hashlib.sha256(payload).hexdigest()
    require_data(metadata.get("market_sha256") == checksum, "checksum_mismatch", "market_sha256 mismatch",
                 at=location(column="market_sha256", reason="metadata_digest"))
    declared_calendar = metadata.get("calendar_dates")
    universe = metadata.get("universe")
    require_data(isinstance(declared_calendar, list) and 0 < len(declared_calendar) <= MAX_PANEL_CELLS,
                 "invalid_calendar", "calendar_dates must be a nonempty bounded list",
                 at=location(column="calendar_dates", reason="metadata_shape"))
    require_data(isinstance(universe, list) and len(universe) >= 2
                 and len(universe) <= MAX_PANEL_CELLS
                 and all(isinstance(asset, str) and asset.strip() == asset and asset for asset in universe),
                 "invalid_universe", "universe must contain at least two nonempty asset identifiers",
                 at=location(column="universe", reason="metadata_shape"))
    require_data(len(declared_calendar) * len(universe) <= MAX_PANEL_CELLS,
                 "panel_cell_limit", "calendar_dates x universe exceeds the 2,000,000 panel-cell limit")
    calendar = []
    for value in declared_calendar:
        try:
            calendar.append(_iso_date(value, "calendar date"))
        except ValueError as exc:
            raise DataValidationError("invalid_date", "Invalid calendar date: expected ISO YYYY-MM-DD",
                                      samples=[location(column="calendar_dates", value=value, reason="metadata_date")]) from exc
    require_data(len(set(calendar)) == len(calendar), "duplicate_calendar", "Duplicate calendar dates",
                 at=location(column="calendar_dates", reason="duplicate_metadata_date"))
    require_data(calendar == sorted(calendar), "date_order", "Calendar dates must be strictly increasing",
                 at=location(column="calendar_dates", reason="metadata_order"))
    require_data(len(set(universe)) == len(universe), "duplicate_universe", "Duplicate universe assets",
                 at=location(column="universe", reason="duplicate_metadata_asset"))
    require_data(metadata.get("sessions") == len(calendar), "grid_mismatch", "Metadata sessions mismatch",
                 at=location(column="sessions", reason="metadata_count"))
    expected_rows = len(calendar) * len(universe)
    require_data(metadata.get("rows") == expected_rows, "grid_mismatch", "Metadata rows mismatch",
                 at=location(column="rows", reason="metadata_count"))
    expected_columns = ["date", "asset", *FIELDS]
    require_data(metadata.get("columns") == expected_columns, "columns_mismatch", "Unsupported metadata columns",
                 at=location(column="columns", reason="metadata_columns"))
    require_data(metadata.get("field_availability") == AVAILABILITY, "unsupported_field_availability",
                 "Unsupported field_availability: v1 requires daily OHLCV availability",
                 at=location(column="field_availability", reason="metadata_availability"))
    for key in ("version", "data_kind", "calendar_kind", "adjustment"):
        require_data(isinstance(metadata.get(key), str) and bool(metadata[key].strip()),
                     "invalid_metadata", f"Metadata {key} is required", at=location(column=key, reason="metadata_required"))
    require_data(metadata["data_kind"] == "synthetic", "unsupported_data_kind",
                 "v1 accepts synthetic fixtures only; real-market data semantics are not yet supported",
                 at=location(column="data_kind", reason="synthetic_only"))
    source_rows = csv_record_rows(payload, expected_columns, expected_rows)
    try:
        frame = pd.read_csv(io.BytesIO(payload), dtype={"date": str, "asset": str}, nrows=expected_rows + 1)
    except (ValueError, pd.errors.ParserError) as exc:
        raise DataValidationError("invalid_csv_structure", "CSV cannot be parsed as the declared table") from exc
    require_data(list(frame.columns) == expected_columns, "columns_mismatch", "CSV columns mismatch")
    require_data(len(frame) == len(source_rows), "invalid_csv_structure", "CSV record interpretation is inconsistent")

    def cell(index, column=None, *, value=None, reason="", related_rows=()):
        row = frame.iloc[index]
        return location(row=source_rows[index], column=column,
                        date=None if pd.isna(row["date"]) else row["date"],
                        asset=None if pd.isna(row["asset"]) else row["asset"],
                        value=value, reason=reason, related_rows=related_rows)

    missing = frame[["date", "asset"]].isna().to_numpy()
    require_data(not missing.any(), "missing_key", "Missing date/asset key",
                 samples=(cell(i, ["date", "asset"][j], reason="missing_key")
                          for i, j in zip(*np.where(missing))))
    for index, value in enumerate(frame["date"]):
        try:
            _iso_date(value, "CSV date")
        except ValueError as exc:
            raise DataValidationError("invalid_date", "Invalid CSV date: expected ISO YYYY-MM-DD",
                                      samples=[cell(index, "date", value=value, reason="invalid_date")]) from exc
    duplicates = frame.duplicated(["date", "asset"])
    if duplicates.any():
        # Keep the earliest occurrence as the reference, including when there
        # are more than five repetitions of one key.
        first_rows, samples = {}, []
        for index, (session, asset) in enumerate(zip(frame["date"], frame["asset"])):
            key = (session, asset)
            if key in first_rows:
                samples.append(cell(index, "date,asset", reason="duplicate_key", related_rows=[first_rows[key]]))
                if len(samples) == SAMPLE_LIMIT:
                    break
            else:
                first_rows[key] = source_rows[index]
        raise DataValidationError("duplicate_key", "Duplicate date/asset key", samples=samples)
    order = frame["date"].iloc[1:].to_numpy() < frame["date"].iloc[:-1].to_numpy()
    require_data(not order.any(), "date_order", "CSV sessions must be in increasing time order",
                 samples=(cell(int(i) + 1, "date", reason="date_precedes_previous_record", related_rows=[source_rows[int(i)]])
                          for i in np.flatnonzero(order)))
    observed = pd.MultiIndex.from_frame(frame[["date", "asset"]])
    expected = pd.MultiIndex.from_product([calendar, universe], names=["date", "asset"])
    missing_keys, unexpected = expected.difference(observed), observed.difference(expected)
    if len(frame) != expected_rows or len(missing_keys) or len(unexpected):
        samples = []
        unexpected_keys = set(unexpected)
        for index, key in enumerate(observed):
            if key in unexpected_keys:
                samples.append(cell(index, "date,asset", reason="unexpected_key"))
                if len(samples) == SAMPLE_LIMIT:
                    break
        for session, asset in missing_keys[:SAMPLE_LIMIT - len(samples)]:
            samples.append(location(column="date,asset", date=session, asset=asset, reason="missing_grid_key"))
        raise DataValidationError("grid_mismatch", "Incomplete calendar_dates x universe coverage (missing or unexpected observations)", samples=samples)
    try:
        values = frame[list(FIELDS)].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        samples = []
        for index, row in enumerate(frame[list(FIELDS)].itertuples(index=False, name=None)):
            for field, value in zip(FIELDS, row):
                try:
                    float(value)
                except (TypeError, ValueError, OverflowError):
                    samples.append(cell(index, field, value=value, reason="not_numeric"))
                    if len(samples) == SAMPLE_LIMIT:
                        break
            if len(samples) == SAMPLE_LIMIT:
                break
        raise DataValidationError("numeric_type", "OHLCV fields must be numeric", samples=samples) from exc
    finite = np.isfinite(values)
    require_data(finite.all(), "nonfinite_value", "OHLCV fields must be finite",
                 samples=(cell(int(i), FIELDS[int(j)], value=frame.iloc[int(i)][FIELDS[int(j)]], reason="nonfinite")
                          for i, j in zip(*np.where(~finite))))
    positive = values > 0
    require_data(positive.all(), "nonpositive_value", "OHLCV prices and volume must be positive",
                 samples=(cell(int(i), FIELDS[int(j)], value=values[i, j], reason="not_positive")
                          for i, j in zip(*np.where(~positive))))
    frame[list(FIELDS)] = values
    valid_bounds = ((frame["low"] <= frame[["open", "close"]].min(axis=1))
                    & (frame["high"] >= frame[["open", "close"]].max(axis=1))
                    & (frame["low"] <= frame["high"]))
    require_data(valid_bounds.all(), "ohlc_bounds", "Invalid OHLC bounds",
                 samples=(cell(int(i), "open,high,low,close", reason="ohlc_bounds") for i in np.flatnonzero(~valid_bounds)))
    dates = pd.DatetimeIndex(calendar, name="date")
    panels = {}
    for field in FIELDS:
        panel = frame.pivot(index="date", columns="asset", values=field).reindex(index=calendar, columns=universe)
        panel.index = dates
        panels[field] = panel.astype(float)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        panels["returns"] = panels["close"].pct_change(fill_method=None)
    finite_returns = np.isfinite(panels["returns"].iloc[1:].to_numpy())
    if not finite_returns.all():
        row_by_key = {key: index for index, key in enumerate(observed)}
        samples = []
        for i, j in zip(*np.where(~finite_returns)):
            current_key, previous_key = (calendar[int(i) + 1], universe[int(j)]), (calendar[int(i)], universe[int(j)])
            samples.append(cell(row_by_key[current_key], "close", reason="derived_return_nonfinite",
                                related_rows=[source_rows[row_by_key[previous_key]]]))
            if len(samples) == SAMPLE_LIMIT:
                break
        raise DataValidationError("nonfinite_derived_return", "Derived close-to-close returns are nonfinite", samples=samples)
    for field, panel in panels.items():
        panel.attrs["market_metadata"] = deepcopy(metadata)
        panel.attrs["field_availability"] = AVAILABILITY.get(field, "after session close")
    return panels

def validate_config(config, calendar):
    """Return normalized configuration, rejecting overlapping or off-calendar bounds."""
    dates = _calendar(calendar)
    require(isinstance(config, dict), "evaluation config must be an object")
    require(not set(config) - {"splits", "min_assets"}, "Unknown evaluation config fields")
    splits = config.get("splits")
    require(isinstance(splits, dict) and set(splits) == {"train", "validation", "test"},
            "splits must declare train, validation and test")
    minimum = config.get("min_assets", 3)
    require(type(minimum) is int and minimum >= 3, "min_assets must be an integer >= 3")
    normalized = {"splits": {}, "min_assets": minimum}
    previous_end = None
    for name in ("train", "validation", "test"):
        bounds = splits[name]
        require(isinstance(bounds, dict) and set(bounds) == {"start", "end"},
                f"{name} requires exactly start and end")
        start, end = (_iso_date(bounds[key], f"{name}.{key}") for key in ("start", "end"))
        require(start in dates and end in dates, f"{name} bounds must be declared calendar sessions")
        require(start <= end, f"Invalid {name} bounds")
        require(previous_end is None or previous_end < start,
                "Splits must be ordered train < validation < test and nonoverlapping")
        require(dates.index(end) - dates.index(start) >= 2,
                f"{name} must contain at least three sessions for delayed labels")
        normalized["splits"][name] = {"start": start, "end": end}
        previous_end = end
    return normalized


def _number(value):
    """Protect the strict JSON boundary; undefined statistics use explicit nulls."""
    value = float(value)
    require(np.isfinite(value), "Numerical overflow during evaluation")
    return value


def evaluate(factor, panels, config, split="validation") -> dict:
    """Evaluate only the development validation split; test is reserved in v1.

    NaN signals remain unavailable observations and are counted in coverage. An
    insufficient or constant cross section is recorded explicitly, not assigned a
    zero IC. Every selected session, including two purged tail sessions, is saved.
    """
    require(split == "validation", "Only validation evaluation is allowed; final test is locked")
    require(isinstance(panels, dict) and all(field in panels for field in FIELDS),
            "Missing OHLCV panels")
    opening = panels["open"]
    require(isinstance(opening, pd.DataFrame) and isinstance(opening.index, pd.DatetimeIndex),
            "Panels require a DatetimeIndex")
    require(opening.size <= MAX_PANEL_CELLS, "Market panel exceeds the 2,000,000 panel-cell limit")
    dates = _calendar(opening.index)
    require(opening.columns.is_unique and len(opening.columns) >= 3, "Invalid panel universe")
    for field in FIELDS:
        panel = panels[field]
        require(isinstance(panel, pd.DataFrame) and panel.index.equals(opening.index)
                and panel.columns.equals(opening.columns), f"Misaligned {field} panel")
    require(isinstance(factor, pd.DataFrame) and factor.index.equals(opening.index)
            and factor.columns.equals(opening.columns), "Factor panel is not aligned with market panels")
    config = validate_config(config, opening.index)
    require(config["min_assets"] <= len(opening.columns), "min_assets exceeds available universe")
    bounds = config["splits"][split]
    first, last = dates.index(bounds["start"]), dates.index(bounds["end"])
    eligible = last - first - 1
    require(eligible * len(opening.columns) <= MAX_EVALUATION_CELLS,
            "Validation exceeds the 100,000 evaluation-cell limit for in-memory daily records")
    try:
        factor_values = factor.to_numpy(dtype=float)
        prices = opening.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Factor and open panels must be numeric") from exc
    require(not np.isinf(factor_values).any(), "Infinite factor values must be diagnosed before evaluation")
    require(np.isfinite(prices).all() and (prices > 0).all(), "Open prices must be finite and positive")
    daily, ic_values, gross_values = [], [], []
    finite_observations = 0
    for position in range(first, last + 1):
        item = {"signal_date": dates[position], "entry_date": None, "exit_date": None,
                "status": "purged", "reason": "label_would_cross_split_boundary",
                "available_assets": 0, "missing_assets": [], "rank_ic": None,
                "rank_ic_state": "not_evaluated", "gross_return": None, "assets": []}
        if position + 2 > last:
            daily.append(item)
            continue
        item.update(entry_date=dates[position + 1], exit_date=dates[position + 2])
        row = factor_values[position]
        mask = np.isfinite(row)
        finite_observations += int(mask.sum())
        item["available_assets"] = int(mask.sum())
        item["missing_assets"] = [str(asset) for asset in opening.columns[~mask]]
        if mask.sum() < config["min_assets"]:
            item.update(status="skipped", reason="insufficient_finite_assets")
            daily.append(item)
            continue
        signals = row[mask]
        ranks = pd.Series(signals).rank(method="average").to_numpy()
        centered = ranks - ranks.mean()
        gross = float(np.abs(centered).sum())
        if gross == 0:
            item.update(status="skipped", reason="constant_factor")
            daily.append(item)
            continue
        weights = centered / gross
        entry, exit_ = prices[position + 1, mask], prices[position + 2, mask]
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            labels = exit_ / entry - 1
        require(np.isfinite(labels).all(), "Nonfinite forward return labels")
        ranked_returns = pd.Series(labels).rank(method="average").to_numpy()
        centered_returns = ranked_returns - ranked_returns.mean()
        if np.any(centered_returns != 0):
            ic = _number(np.dot(centered, centered_returns)
                         / np.sqrt(np.dot(centered, centered) * np.dot(centered_returns, centered_returns)))
            item.update(rank_ic=ic, rank_ic_state="defined")
            ic_values.append(ic)
        else:
            item["rank_ic_state"] = "constant_forward_returns"
        contributions = weights * labels
        gross_return = _number(contributions.sum())
        gross_values.append(gross_return)
        item.update(status="evaluated", reason=None, gross_return=gross_return)
        item["assets"] = [
            {"asset": str(asset), "signal": _number(signal), "weight": _number(weight),
             "entry_open": _number(buy), "exit_open": _number(sell),
             "forward_return": _number(label), "gross_contribution": _number(contribution)}
            for asset, signal, weight, buy, sell, label, contribution in zip(
                opening.columns[mask], signals, weights, entry, exit_, labels, contributions)
        ]
        daily.append(item)
    result = {
        "schema_version": 1,
        "status": "evaluated" if gross_values else "not_evaluable",
        "reason": None if gross_values else "No nonconstant factor cross section with sufficient finite assets",
        "split": split,
        "config": config,
        "execution": deepcopy(EXECUTION),
        "metrics": {
            "mean_rank_ic": _number(np.mean(ic_values)) if ic_values else None,
            "rank_ic_days": len(ic_values),
            "mean_gross_return": _number(np.mean(gross_values)) if gross_values else None,
            "sum_gross_return": _number(np.sum(gross_values)) if gross_values else None,
            "evaluated_days": len(gross_values),
            "eligible_days": eligible,
            "skipped_days": eligible - len(gross_values),
            "purged_days": 2,
            "finite_factor_observations": finite_observations,
            "possible_factor_observations": eligible * len(opening.columns),
            "factor_coverage": finite_observations / (eligible * len(opening.columns)),
        },
        "daily": daily,
        "factor_diagnostics": deepcopy(factor.attrs.get("expression_diagnostics", {})),
        "market_metadata": deepcopy(opening.attrs.get("market_metadata", {})),
        "limitations": [
            "Synthetic fixture metrics validate software behavior only; they are not investment performance evidence.",
            "Gross daily rank portfolio, without transaction costs, borrow, liquidity or execution simulation.",
            "Metric means are conditional on their reported valid-day counts; compare candidate coverage before comparing means.",
            "Train data may supply trailing lookback history; validation labels never cross into the reserved test interval.",
            "Caller-supplied factor causality requires separate prefix-invariance tests; alignment alone cannot establish it.",
        ],
    }
    json_text(result)  # Strict JSON serialization is part of the result contract.
    return result
