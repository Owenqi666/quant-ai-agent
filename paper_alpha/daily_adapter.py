"""Bounded byte integrity plus a strictly development-only numeric CSV view.

The final tail is opaque: hashing and counting LF bytes are not CSV/numeric
validation of that tail. Content row identities have no source namespace.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat

import numpy as np
import pandas as pd

from .daily_contract import (CANONICAL_VERSION, COLUMNS, FIELDS, MAX_METADATA_BYTES,
                             MAX_RAW_BYTES, DailyContract, parse_contract,
                             parse_source_receipt, public_capabilities, require_fields)
from .market_diagnostics import DataValidationError, location, require_data
from .storage import digest

ADAPTER_VERSION = "real-daily-development-prefix-v1"
MAX_LINE_BYTES = 8192
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
CODE_FILES = ("daily_contract.py", "daily_adapter.py", "storage.py", "market_diagnostics.py", "evaluation.py", "expressions.py")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _open_regular(path, limit):
    path = Path(path).absolute()
    try:
        if path.is_symlink() or path.resolve() != path:
            raise OSError("symbolic path")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
            raise OSError("file type or limit")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        stream = os.fdopen(fd, "rb")
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            stream.close()
            raise OSError("changed while opening")
        return path, stream, before
    except OSError as exc:
        raise DataValidationError("unsafe_or_bounded_file", "Input must be an unchanged regular file within its size limit") from exc


def _stable(path, stream, before):
    try:
        same = _signature(path.stat()) == _signature(before) == _signature(os.fstat(stream.fileno()))
    except OSError:
        same = False
    require_data(same, "input_changed", "Input changed during the precheck")


def _json_input(path):
    path, stream, before = _open_regular(path, MAX_METADATA_BYTES)
    with stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
        try:
            require_data(len(raw) <= MAX_METADATA_BYTES, "metadata_file_limit", "Metadata snapshot exceeds the bounded limit")
            def reject_constant(_value):
                raise ValueError("Nonfinite JSON constant")
            def unique_pairs(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("Duplicate JSON key")
                    value[key] = item
                return value
            def finite_float(value):
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("Nonfinite JSON number")
                return number
            value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                               object_pairs_hook=unique_pairs, parse_float=finite_float)
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise DataValidationError("invalid_metadata_json", "Metadata/receipt must be strict finite JSON without duplicate keys", at=location(column="declaration_json", reason="json_parse")) from exc
        _stable(path, stream, before)
    return value, hashlib.sha256(raw).hexdigest()


def code_identity():
    package = Path(__file__).resolve().parent
    files = {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in CODE_FILES}
    value = {"adapter_version": ADAPTER_VERSION, "canonical_version": CANONICAL_VERSION,
             "files": files, "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__}
    return {**value, "digest": digest(value)}


def content_row_fingerprint(session, asset, values):
    """Exact engine float64 values; no source/provider/contract identity exemption."""
    require_data(isinstance(session, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", session)),
                 "invalid_observation_key", "Observation date must be ISO YYYY-MM-DD")
    try:
        date.fromisoformat(session)
    except ValueError as exc:
        raise DataValidationError("invalid_observation_key", "Observation date is invalid") from exc
    require_data(isinstance(asset, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", asset)),
                 "invalid_observation_key", "Observation asset must be an exact bounded identifier")
    require_data(isinstance(values, (tuple, list)) and len(values) == len(FIELDS), "invalid_observation", "An OHLCV row is required")
    require_data(all(isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, (bool, np.bool_)) for value in values),
                 "invalid_observation", "Observation values must be numbers, not booleans or strings")
    try:
        converted = [float(value) for value in values]
    except (ValueError, TypeError, OverflowError) as exc:
        raise DataValidationError("invalid_observation", "Observation values must be finite numbers") from exc
    require_data(all(math.isfinite(x) and x > 0 for x in converted), "invalid_observation", "Observation OHLCV must be finite and positive")
    opening, high, low, close, _volume = converted
    require_data(low <= min(opening, close) <= max(opening, close) <= high,
                 "invalid_observation", "Observation has invalid OHLC bounds")
    return digest({"version": CANONICAL_VERSION, "date": session, "asset": asset,
                   "fields": list(FIELDS), "values": [x.hex() for x in converted]})


@dataclass(frozen=True)
class Observation:
    date: str
    asset: str
    row_sha256: str


@dataclass(frozen=True)
class DevelopmentView:
    contract: DailyContract
    observations: tuple[Observation, ...]
    _panels: tuple
    _report_json: str

    @property
    def panels(self):
        return {name: frame.copy(deep=True) for name, frame in self._panels}

    @property
    def report(self):
        import json
        return json.loads(self._report_json)


def _number(value, field, record, session, asset):
    at = location(row=record, column=field, date=session, asset=asset, reason="numeric_cell")
    require_data(value.lower() not in ("nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"),
                 "nonfinite_value", "OHLCV fields must be finite", at=at)
    require_data(bool(NUMBER.fullmatch(value)), "invalid_numeric", "OHLCV cell must contain a finite decimal number", at=at)
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise DataValidationError("invalid_numeric", "OHLCV number cannot be represented", at=at) from exc
    require_data(math.isfinite(number), "nonfinite_value", "OHLCV fields must be finite", at=at)
    require_data(number > 0, "nonpositive_value", "Prices and volume must be positive; zero-volume/no-quote events are unsupported", at=at)
    return number


def precheck_development(contract_path, csv_path, receipt_path, *, allow_whole_byte_hash=False, required_fields=()):
    """No implicit whole-byte access. Numeric conversions stop at validation.end."""
    require_data(type(allow_whole_byte_hash) is bool and allow_whole_byte_hash,
                 "whole_byte_permission_required", "Explicit whole-byte integrity permission is required; this is not final numeric permission")
    raw_contract, contract_file_sha = _json_input(contract_path)
    contract = parse_contract(raw_contract)
    raw_receipt, receipt_file_sha = _json_input(receipt_path)
    receipt = parse_source_receipt(raw_receipt)
    d, source = contract.to_dict(), receipt.to_dict()
    require_fields(contract, required_fields)
    require_data(d["source"]["receipt_sha256"] == receipt_file_sha, "source_receipt_mismatch", "Source receipt bytes do not match the contract binding")
    require_data(d["source"]["source_id"] == source["source_id"] and d["origin"] == source["origin"],
                 "source_identity_mismatch", "Contract and independent source receipt identities disagree")
    for event in d["known_events"]:
        if event["date"] <= contract.cutoff:
            raise DataValidationError("unsupported_event", "Declared development event is unsupported by this first version",
                                      at=location(column="known_events", date=event["date"], asset=event["asset"], reason=event["kind"]))
    path, stream, before = _open_regular(csv_path, MAX_RAW_BYTES)
    dates, assets = contract.development_calendar, contract.assets
    observations, values, seen = [], [], set()
    with stream:
        raw_hash, size, newlines, last = hashlib.sha256(), 0, 0, b""
        while True:
            block = stream.read(64 * 1024)
            if not block:
                break
            raw_hash.update(block)
            size += len(block)
            newlines += block.count(b"\n")
            last = block[-1:]
        actual_sha = raw_hash.hexdigest()
        require_data(size == source["raw_bytes"] and actual_sha == source["raw_sha256"], "source_bytes_mismatch", "Whole raw bytes do not match the independent source receipt")
        # Count delimiters only; no final CSV row is decoded or numerically parsed.
        require_data(last == b"\n" and newlines == 1 + len(contract.calendar) * len(assets),
                     "physical_record_count", "First-version layout needs one terminated physical line per declared grid cell plus header")
        stream.seek(0)
        header = stream.readline(MAX_LINE_BYTES + 1)
        require_data(header.rstrip(b"\r\n") == ",".join(COLUMNS).encode("ascii"), "columns_mismatch", "CSV columns must be exactly date,asset,open,high,low,close,volume")
        for index, (session, asset) in enumerate((day, name) for day in dates for name in assets):
            record = index + 2
            line = stream.readline(MAX_LINE_BYTES + 1)
            at = location(row=record, date=session, asset=asset, reason="expected_grid_key")
            require_data(bool(line) and len(line) <= MAX_LINE_BYTES and line.endswith(b"\n"), "invalid_csv_structure", "Development CSV needs bounded single physical lines", at=at)
            try:
                text = line.rstrip(b"\r\n").decode("ascii")
            except UnicodeDecodeError as exc:
                raise DataValidationError("invalid_csv_encoding", "Development CSV uses ASCII identifiers/numbers", at=at) from exc
            require_data('"' not in text and "\x00" not in text and "\r" not in text,
                         "unsupported_csv_layout", "Quoting, multiline fields and NUL are unsupported in this first version", at=at)
            cells = text.split(",")
            require_data(len(cells) == len(COLUMNS), "columns_mismatch", "Every development line needs seven columns", at=at)
            key = tuple(cells[:2])
            if key in seen:
                raise DataValidationError("duplicate_key", "Duplicate development observation", at=location(row=record, column="date,asset", date=cells[0], asset=cells[1], reason="duplicate_key"))
            if cells[1] not in assets:
                raise DataValidationError("unexpected_asset", "Asset is outside the declared universe", at=location(row=record, column="asset", date=cells[0], asset=cells[1], reason="universe"))
            if cells[:2] != [session, asset]:
                related = [record - 1] if index else []
                raise DataValidationError("grid_key_mismatch", "Missing, duplicate, unexpected or reordered calendar/asset key; no sorting/filling is permitted",
                                          at=location(row=record, column="date,asset", date=session, asset=asset, related_rows=related, reason="expected_grid_key"))
            row = [_number(value, field, record, session, asset) for field, value in zip(FIELDS, cells[2:])]
            opening, high, low, close, _volume = row
            require_data(low <= min(opening, close) <= max(opening, close) <= high,
                         "ohlc_bounds", "Invalid OHLC bounds", at=location(row=record, column="open,high,low,close", date=session, asset=asset, reason="ohlc_bounds"))
            seen.add(key)
            values.append(row)
            observations.append(Observation(session, asset, content_row_fingerprint(session, asset, row)))
        _stable(path, stream, before)
    # Recheck all metadata/receipt identities before publishing a receipt.
    for input_path, expected in ((contract_path, contract_file_sha), (receipt_path, receipt_file_sha)):
        _unused, actual = _json_input(input_path)
        require_data(actual == expected, "input_changed", "Metadata or source receipt changed during precheck")
    grid = np.asarray(values, dtype=np.float64).reshape(len(dates), len(assets), len(FIELDS))
    panels = {field: pd.DataFrame(grid[:, :, i], index=pd.DatetimeIndex(dates, name="date"), columns=assets) for i, field in enumerate(FIELDS)}
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        returns = panels["close"].pct_change(fill_method=None)
    require_data(np.isfinite(returns.iloc[1:].to_numpy()).all(), "nonfinite_derived_return", "Derived development close returns are nonfinite")
    panels["returns"] = returns
    content_digest = digest({"version": CANONICAL_VERSION, "rows": [o.__dict__ for o in observations]})
    code = code_identity()
    capabilities = public_capabilities(contract)
    source_identity = digest({"receipt_declaration_digest": receipt.declaration_digest, "receipt_file_sha256": receipt_file_sha,
                              "raw_sha256": actual_sha, "raw_bytes": size})
    input_digest = digest({"contract_file_sha256": contract_file_sha, "receipt_file_sha256": receipt_file_sha, "raw_sha256": actual_sha})
    report = {"schema_version": 1, "report_version": "real-daily-preflight-v1", "status": "development_prechecked",
              "scope": "engineering_fixture_precheck_only" if d["origin"] == "engineering_fixture" else "declared_real_source_precheck_only",
              "origin": d["origin"], "real_source_ready": False, "real_execution_readiness": "pending",
              "pending_facts": list(receipt.pending_facts),
              "identity": {"input_digest": input_digest, "contract_digest": contract.contract_digest,
                           "source_identity_digest": source_identity, "development_content_digest": content_digest,
                           "canonical_version": CANONICAL_VERSION, "capability_digest": capabilities["capability_digest"],
                           "config_digest": digest(d["evaluation"]), "code_digest": code["digest"]},
              "inputs": {"contract_file_sha256": contract_file_sha, "receipt_file_sha256": receipt_file_sha,
                         "raw_sha256": actual_sha, "raw_bytes": size}, "code_identity": code,
              "read_scope": {"whole_byte_integrity_permission": True, "whole_bytes_hashed": size,
                             "development_start": dates[0], "development_end": contract.cutoff,
                             "development_sessions": len(dates), "development_rows_parsed": len(values),
                             "final_numeric_rows_parsed": 0, "final_grid_validation": "not_performed", "full_dataset_validated": False,
                             "whole_file_structure_check": "LF count and final newline only; opaque tail"},
              "grid": {"assets": len(assets), "development_complete": True, "missing_policy": "reject; no fill or calendar compression"},
              "evaluation_asset_eligibility": len(assets) >= d["evaluation"]["min_assets"],
              "public_capabilities": capabilities,
              "limitations": ["No factor, metric, market evaluation, Provider/Native call or service registration performed.",
                              "This truncated development view cannot be passed directly to the legacy full-calendar evaluator; D3 requires separate approved adaptation.",
                              "Whole-byte integrity and development numeric validation do not authenticate source/use/PIT.",
                              "Final tail is not CSV/numeric validated; it may still contain unsupported events or invalid records.",
                              "Content identity is source-independent, but no bridge to legacy research_guard is connected."]}
    report["report_digest"] = digest(report)
    from .storage import json_text
    return DevelopmentView(contract, tuple(observations), tuple(panels.items()), json_text(report))
