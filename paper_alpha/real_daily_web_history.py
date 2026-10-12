"""Read-only, versioned integrity verification for the eleven legacy v1 artifacts.

The caller must obtain producer pins from the original durable host record.
This module cannot authenticate that caller or those pins. A successful result
is neither current source/permission approval nor main-queue execution proof.
It reads only the declared archive files, never market inputs, source code,
current authority, or a live method profile. CSV bytes are hashed, not parsed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import date, datetime

PRODUCER_KIND = "legacy_real_daily_local_development_v1"
JOB_VERSION = "real-daily-local-development-v1"
SCOPE = "local_current_vintage_development_only"
HISTORY_VERSION = "real-daily-archived-history-v1"
OUTPUT_FILES = frozenset({"attempt.json", "registration.json", "selection.json", "method.json", "factor.csv", "result.json", "scalar.json", "baseline.json", "report.json", "report.md", "receipt.json"})
PRODUCER_KEYS = frozenset({"schema_version", "producer_kind", "status", "job_id", "request_digest", "receipt_sha256", "frozen_request", "frozen_selection"})
REQUEST_KEYS = frozenset({"schema_version", "job_version", "scope", "registration_identity", "registration_resolved_digest", "selection_digest", "method", "code", "output_relative", "idempotency_key", "candidate", "budget", "final_execution_allowed", "final_numeric_rows_parsed", "test_freshness"})
IDENTITY_KEYS = frozenset({"id", "registration_digest", "approval_id", "approval_digest", "assessment_id", "assessment_digest", "reservation_id", "reservation_digest", "prefix_sha256", "receipt_sha256", "method_digest", "origin"})
# Frozen legacy v1 inventory names. They are metadata, never paths to open.
CODE_FILES_V1 = frozenset({"paper_alpha/real_daily_workflow.py", "paper_alpha/real_daily_admission.py", "paper_alpha/real_daily_normalizer.py", "paper_alpha/real_daily_method.py", "paper_alpha/worker.py", "paper_alpha/expressions.py", "paper_alpha/evaluation.py", "paper_alpha/storage.py", "paper_alpha/vendor/operators.py", "paper_alpha/daily_event_review.py", "paper_alpha/contracts.py", "paper_alpha/market_diagnostics.py", "paper_alpha/server/db.py", "paper_alpha/server/maintenance.py", "paper_alpha/server/daily_snapshot_guard.py"})
RECEIPT_KEYS = frozenset({"schema_version", "job_version", "job_id", "request_digest", "status", "scope", "registration_identity", "selection_digest", "method_digest", "code", "candidate", "files", "actual_result_digest", "result_file_sha256", "scalar_digest", "baseline_digest", "baseline_status", "candidate_computations", "evaluation_calls", "model_calls", "automatic_retries", "final_numeric_rows_parsed", "final_execution_allowed", "test_freshness", "source_authentication", "point_in_time", "scientific_score", "receipt_digest"})
RESULT_KEYS = frozenset({"config", "daily", "evaluation_scope", "execution", "factor_diagnostics", "final_numeric_rows_used", "limitations", "market_metadata", "method_digest", "method_id", "metrics", "reason", "return_convention", "schema_version", "selection_digest", "source_authentication", "split", "status", "test_freshness"})
SCALAR_KEYS = frozenset({"absolute_tolerance", "arithmetic", "asset_records_checked", "daily_records_checked", "final_numeric_rows_used", "max_absolute_error", "method_digest", "method_id", "metrics_checked", "numeric_values_checked", "oracle_version", "result_digest", "schema_version", "selection_digest", "status"})
METRIC_KEYS = frozenset({"eligible_days", "evaluated_days", "factor_coverage", "finite_factor_observations", "mean_gross_return", "mean_rank_ic", "possible_factor_observations", "purged_days", "rank_ic_days", "skipped_days", "sum_gross_return"})
METHOD_KEYS = frozenset({"candidate", "configuration_origin", "entry_offset_sessions", "event_policy", "exit_offset_sessions", "final_numeric_read_allowed", "method_id", "missing_policy", "price_adjustment", "price_scale", "price_unit", "required_fields", "return_convention", "schema_version", "signal_timing", "total_return", "unknown_event_coverage", "unknown_event_dates", "volume"})
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_STRING_CHARS = 65_536
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class RealDailyHistoryError(ValueError):
    """Bounded public error; no paths, input tokens or numeric payloads."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise RealDailyHistoryError(code)


def _text_v1(value):
    # Frozen storage.json_text v1 representation, independent of live helpers.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _digest_v1(value):
    return hashlib.sha256(_text_v1(value).encode("utf-8")).hexdigest()


def _same(left, right):
    # Canonical bytes preserve null/bool/int/float distinctions unlike ==.
    return _text_v1(left) == _text_v1(right)


def _shape(value, keys, code):
    _need(type(value) is dict and set(value) == keys, code)


def _sha(value, code):
    _need(type(value) is str and _SHA.fullmatch(value) is not None, code)
    return value


def _string(value, code, maximum=128):
    _need(type(value) is str and 0 < len(value) <= maximum and value.strip() == value and "\x00" not in value and not any(ord(c) < 32 for c in value), code)
    return value


def _integer(value, expected, code):
    _need(type(value) is int and value == expected, code)


def _encoded_size(value, code):
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise RealDailyHistoryError(code) from None


def _bounded(value, code):
    pending = [(value, 0)]
    count, text_bytes = 0, 0
    while pending:
        item, depth = pending.pop()
        count += 1
        _need(count <= MAX_JSON_NODES and depth <= MAX_JSON_DEPTH, code)
        if type(item) is dict:
            _need(len(item) <= MAX_JSON_NODES, code)
            for key, val in item.items():
                _need(type(key) is str and len(key) <= 256, code)
                text_bytes += _encoded_size(key, code)
                _need(text_bytes <= MAX_JSON_BYTES, code)
                pending.append((val, depth + 1))
        elif type(item) is list:
            _need(len(item) <= MAX_JSON_NODES, code)
            pending.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            _need(len(item) <= MAX_STRING_CHARS, code)
            text_bytes += _encoded_size(item, code)
            _need(text_bytes <= MAX_JSON_BYTES, code)
        elif type(item) is float:
            _need(math.isfinite(item), code)
        elif type(item) is int:
            _need(abs(item) <= 2**63 - 1, code)
        else:
            _need(item is None or type(item) is bool, code)



def _producer_snapshot(value, code):
    """Copy only exact JSON builtins; no user copy/serialization hooks.

    The copy has its own dict/list containers. It is not a concurrency lock:
    observable changes are detected by comparing new snapshots at the entry
    and final boundaries, while mutations restored between them are outside
    the observation guarantee.
    """
    count, text_bytes = 0, 0
    def text_size(item, maximum):
        nonlocal text_bytes
        _need(type(item) is str and len(item) <= maximum, code)
        text_bytes += _encoded_size(item, code)
        _need(text_bytes <= MAX_JSON_BYTES, code)
    def clone(item, depth):
        nonlocal count
        count += 1
        _need(count <= MAX_JSON_NODES and depth <= MAX_JSON_DEPTH, code)
        if type(item) is dict:
            _need(len(item) <= MAX_JSON_NODES, code)
            copied = {}
            # Exact dict/list checks precede their builtin iteration: custom
            # mappings, iterators and deepcopy hooks cannot be executed here.
            for key, val in tuple(item.items()):
                text_size(key, 256)
                copied[key] = clone(val, depth + 1)
            return copied
        if type(item) is list:
            _need(len(item) <= MAX_JSON_NODES, code)
            return [clone(val, depth + 1) for val in tuple(item)]
        if type(item) is str:
            text_size(item, MAX_STRING_CHARS)
        elif type(item) is int:
            _need(abs(item) <= 2**63 - 1, code)
        elif type(item) is float:
            _need(math.isfinite(item), code)
        else:
            _need(item is None or type(item) is bool, code)
        return item
    try:
        copied = clone(value, 0)
        canonical = _text_v1(copied).encode("utf-8")
        _need(len(canonical) <= MAX_JSON_BYTES, code)
        return copied, canonical
    except RealDailyHistoryError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, RuntimeError):
        raise RealDailyHistoryError(code) from None


def _check_producer_readback(caller, canonical):
    _, current = _producer_snapshot(caller, "HISTORY_READBACK_DRIFT")
    _need(current == canonical, "HISTORY_READBACK_DRIFT")


def _json_v1(raw, code):
    _need(len(raw) <= MAX_JSON_BYTES, code)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, code)
            result[key] = value
        return result
    def reject(_):
        raise RealDailyHistoryError(code)
    def finite(token):
        value = float(token)
        _need(math.isfinite(value), code)
        return value
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject, parse_float=finite)
        _bounded(value, code)
        return value
    except RealDailyHistoryError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RealDailyHistoryError(code) from None


def _scope(value, *, receipt=False):
    code = "HISTORY_SCOPE_DRIFT"
    _integer(value.get("schema_version"), 1, code)
    _need(value.get("job_version") == JOB_VERSION and value.get("scope") == SCOPE, code)
    _need(value.get("final_execution_allowed") is False and value.get("test_freshness") == "unknown", code)
    _integer(value.get("final_numeric_rows_parsed"), 0, code)
    if receipt:
        for name, expected in (("candidate_computations", 1), ("evaluation_calls", 2), ("model_calls", 0), ("automatic_retries", 0)):
            _integer(value.get(name), expected, code)
        _need(value.get("status") == "completed" and value.get("source_authentication") == "not_certified_by_this_execution" and value.get("point_in_time") == "not_certified" and value.get("scientific_score") is None, code)


def _validate_method(method):
    code = "HISTORY_BINDING_DRIFT"
    _shape(method, METHOD_KEYS, code)
    _integer(method["schema_version"], 1, code)
    _string(method["method_id"], code)
    _need(method["method_id"] == "daily-raw-price-ohlc-method-v1", code)
    _need(method["final_numeric_read_allowed"] is False and method["total_return"] is False, "HISTORY_SCOPE_DRIFT")
    _need(method["required_fields"] == ["open", "high", "low", "close"] and method["price_adjustment"] == "raw_unadjusted" and method["price_unit"] == "currency_major_unit", code)
    _integer(method["price_scale"], 1, code)
    _integer(method["entry_offset_sessions"], 1, code)
    _integer(method["exit_offset_sessions"], 2, code)
    # Inspect the frozen metadata, without comparing it to a live profile.
    _need(method["configuration_origin"] == "project_configuration" and method["missing_policy"] == "reject_no_fill_no_calendar_compression" and method["signal_timing"] == "after_session_t_close", code)
    _need(method["event_policy"] == "known_cash_dividends_excluded_all_other_development_events_rejected" and method["unknown_event_coverage"] == "blocked" and method["unknown_event_dates"] == "blocked" and method["volume"] == "unavailable_not_required_no_proxy", code)
    convention = method["return_convention"]
    _shape(convention, {"borrow", "cash_dividends", "costs", "slippage", "formula", "kind", "total_return"}, code)
    _need(all(convention[k] == "excluded" for k in ("borrow", "cash_dividends", "costs", "slippage")) and convention["kind"] == "gross_price_return" and convention["formula"] == "open[t+2] / open[t+1] - 1" and convention["total_return"] is False, code)
    candidate = method["candidate"]
    _shape(candidate, {"id", "expression", "epsilon"}, code)
    _need(candidate["id"] == "alpha101-101" and candidate["expression"] == "((close - open) / ((high - low) + .001))", code)
    _shape(candidate["epsilon"], {"formula_literal", "unit", "value"}, code)
    _need(_same(candidate["epsilon"], {"formula_literal": ".001", "unit": "currency_major_unit", "value": 0.001}), code)


def _validate_selection(selection):
    code = "HISTORY_BINDING_DRIFT"
    keys = {"assets", "sessions", "development_sessions", "config", "partitions"}
    _need(type(selection) is dict and keys <= set(selection) <= keys | {"configuration_origin", "point_in_time"}, code)
    if "configuration_origin" in selection:
        _need(selection["configuration_origin"] in ("project_configuration", "project_engineering_fixture"), code)
    if "point_in_time" in selection:
        _need(selection["point_in_time"] == "not_certified", "HISTORY_SCOPE_DRIFT")
    assets = selection["assets"]
    _need(type(assets) is list and 3 <= len(assets) <= 10_000, code)
    for asset in assets:
        _string(asset, code, 96)
    _need(len(set(assets)) == len(assets), code)
    def sessions(items):
        _need(type(items) is list and 0 < len(items) <= 20_000, code)
        for value in items:
            _need(type(value) is str and _DATE.fullmatch(value) is not None, code)
            try:
                date.fromisoformat(value)
            except ValueError:
                raise RealDailyHistoryError(code) from None
        _need(items == sorted(set(items)), code)
    sessions(selection["sessions"])
    sessions(selection["development_sessions"])
    _shape(selection["partitions"], {"train", "validation", "test"}, code)
    config = selection["config"]
    _shape(config, {"min_assets", "splits"}, code)
    _need(type(config["min_assets"]) is int and 3 <= config["min_assets"] <= len(assets), code)
    _shape(config["splits"], {"train", "validation", "test"}, code)
    parts = selection["partitions"]
    for name in ("train", "validation", "test"):
        sessions(parts[name])
        interval = config["splits"][name]
        _shape(interval, {"start", "end"}, code)
        for bound in interval.values():
            _need(type(bound) is str and _DATE.fullmatch(bound) is not None, code)
            try:
                date.fromisoformat(bound)
            except ValueError:
                raise RealDailyHistoryError(code) from None
        _need(interval["start"] <= interval["end"] and parts[name] == [s for s in selection["sessions"] if interval["start"] <= s <= interval["end"]], code)
    _need(config["splits"]["train"]["end"] < config["splits"]["validation"]["start"] and config["splits"]["validation"]["end"] < config["splits"]["test"]["start"], code)
    _need(selection["development_sessions"] == parts["train"] + parts["validation"] and selection["sessions"] == parts["train"] + parts["validation"] + parts["test"], code)


def _validate_producer(producer):
    code = "HISTORY_PRODUCER_INVALID"
    _bounded(producer, code)
    _shape(producer, PRODUCER_KEYS, code)
    _integer(producer["schema_version"], 1, code)
    _need(producer["producer_kind"] == PRODUCER_KIND and producer["status"] == "completed", code)
    _string(producer["job_id"], code)
    for name in ("request_digest", "receipt_sha256"):
        _sha(producer[name], code)
    request, selection = producer["frozen_request"], producer["frozen_selection"]
    _shape(request, REQUEST_KEYS, code)
    _need(_digest_v1(request) == producer["request_digest"] and _digest_v1(selection) == request["selection_digest"], "HISTORY_REQUEST_DRIFT")
    _scope(request)
    for name in ("registration_resolved_digest", "selection_digest"):
        _sha(request[name], "HISTORY_BINDING_DRIFT")
    _string(request["idempotency_key"], code)
    _shape(request["budget"], {"candidate_computations", "evaluation_calls", "model_calls", "automatic_retries", "max_seconds"}, "HISTORY_SCOPE_DRIFT")
    for name, expected in (("candidate_computations", 1), ("evaluation_calls", 2), ("model_calls", 0), ("automatic_retries", 0), ("max_seconds", 60)):
        _integer(request["budget"][name], expected, "HISTORY_SCOPE_DRIFT")
    identity = request["registration_identity"]
    _shape(identity, IDENTITY_KEYS, "HISTORY_BINDING_DRIFT")
    for name in IDENTITY_KEYS - {"id", "approval_id", "assessment_id", "reservation_id", "origin"}:
        _sha(identity[name], "HISTORY_BINDING_DRIFT")
    for name in ("id", "approval_id", "assessment_id", "reservation_id"):
        _string(identity[name], "HISTORY_BINDING_DRIFT")
    _need(identity["origin"] in ("engineering_fixture", "real_source_declaration"), "HISTORY_BINDING_DRIFT")
    _validate_method(request["method"])
    _need(_digest_v1(request["method"]) == identity["method_digest"], "HISTORY_BINDING_DRIFT")
    candidate = request["candidate"]
    _shape(candidate, {"id", "expression", "evidence", "project_modifications"}, "HISTORY_BINDING_DRIFT")
    _need(candidate["id"] == "fixed-alpha101-101" and candidate["expression"] == request["method"]["candidate"]["expression"], "HISTORY_BINDING_DRIFT")
    evidence = candidate["evidence"]
    _shape(evidence, {"kind", "paper_id", "document_sha256", "source_evidence_file_sha256", "evidence_id", "physical_pdf_page", "quote", "verification_scope"}, "HISTORY_BINDING_DRIFT")
    _need(evidence["kind"] == "maintained_paper_formula" and evidence["paper_id"] == "arxiv:1601.00991v3" and evidence["evidence_id"] == "alpha101-formula" and evidence["quote"] == candidate["expression"], "HISTORY_BINDING_DRIFT")
    _integer(evidence["physical_pdf_page"], 15, "HISTORY_BINDING_DRIFT")
    for name in ("document_sha256", "source_evidence_file_sha256"):
        _sha(evidence[name], "HISTORY_BINDING_DRIFT")
    _string(evidence["verification_scope"], "HISTORY_BINDING_DRIFT", 1024)
    _need(type(candidate["project_modifications"]) is list and 0 < len(candidate["project_modifications"]) <= 32, "HISTORY_BINDING_DRIFT")
    for item in candidate["project_modifications"]:
        _string(item, "HISTORY_BINDING_DRIFT", 1024)
    code_identity = request["code"]
    _shape(code_identity, {"job_version", "files", "digest"}, "HISTORY_BINDING_DRIFT")
    _need(code_identity["job_version"] == JOB_VERSION, "HISTORY_BINDING_DRIFT")
    _shape(code_identity["files"], CODE_FILES_V1, "HISTORY_BINDING_DRIFT")
    for pin in code_identity["files"].values():
        _sha(pin, "HISTORY_BINDING_DRIFT")
    _sha(code_identity["digest"], "HISTORY_BINDING_DRIFT")
    _need(code_identity["digest"] == _digest_v1({"job_version": JOB_VERSION, "files": code_identity["files"]}), "HISTORY_BINDING_DRIFT")
    _validate_selection(selection)
    return request, selection


def _root_path(workspace_root, relative):
    code = "HISTORY_PATH_INVALID"
    try:
        raw = os.fspath(workspace_root)
    except TypeError:
        raise RealDailyHistoryError(code) from None
    _need(type(raw) is str and 0 < len(raw) <= 4096 and raw.startswith("/") and "\\" not in raw and "\x00" not in raw, code)
    _need(raw == "/" or all(p and p not in (".", "..") for p in raw[1:].split("/")), code)
    _need(type(relative) is str and re.fullmatch(r"real_daily_runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", relative) is not None, code)
    _need(relative.split("/")[1] not in (".", ".."), code)
    return raw, relative


def _open_directory(root, relative):
    # Walk components from / with directory fds: no symlink resolution or race
    # can redirect a path component into the raw originals or code tree.
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open("/", flags)
        for component in ([] if root == "/" else root[1:].split("/")) + relative.split("/"):
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        code = "HISTORY_ARTIFACT_MISSING" if exc.errno == 2 else "HISTORY_PATH_INVALID"
        raise RealDailyHistoryError(code) from None


def _file_stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink, value.st_mode)


def _directory_stamp(value):
    return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns, value.st_mode)


def _inventory(fd, *, readback=False):
    code = "HISTORY_READBACK_DRIFT" if readback else "HISTORY_INVENTORY_INVALID"
    try:
        names = set(os.listdir(fd))
    except OSError:
        raise RealDailyHistoryError(code) from None
    if OUTPUT_FILES - names:
        raise RealDailyHistoryError("HISTORY_READBACK_DRIFT" if readback else "HISTORY_ARTIFACT_MISSING")
    _need(names == OUTPUT_FILES, code)
    try:
        for name in names:
            value = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _need(stat.S_ISREG(value.st_mode) and value.st_nlink == 1, code)
            _need(0 < value.st_size <= MAX_ARTIFACT_BYTES, code)
    except OSError:
        raise RealDailyHistoryError(code) from None



def _check_locator_readback(root, relative, expected_stamp):
    try:
        locator = _open_directory(root, relative)
    except RealDailyHistoryError:
        raise RealDailyHistoryError("HISTORY_READBACK_DRIFT") from None
    try:
        _need(_directory_stamp(os.fstat(locator)) == expected_stamp, "HISTORY_READBACK_DRIFT")
        _inventory(locator, readback=True)
        _need(_directory_stamp(os.fstat(locator)) == expected_stamp, "HISTORY_READBACK_DRIFT")
    finally:
        os.close(locator)


def _read_file(fd, name, *, readback=False):
    code = "HISTORY_READBACK_DRIFT" if readback else "HISTORY_INVENTORY_INVALID"
    stream = None
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= MAX_ARTIFACT_BYTES, code)
        stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        opened = os.fstat(stream)
        _need(_file_stamp(opened) == _file_stamp(before), "HISTORY_READBACK_DRIFT")
        pieces, size = [], 0
        while True:
            part = os.read(stream, min(1024 * 1024, MAX_ARTIFACT_BYTES + 1 - size))
            if not part:
                break
            pieces.append(part)
            size += len(part)
            _need(size <= MAX_ARTIFACT_BYTES, code)
        after = os.fstat(stream)
        named = os.stat(name, dir_fd=fd, follow_symlinks=False)
        _need(_file_stamp(before) == _file_stamp(after) == _file_stamp(named) and size == before.st_size, "HISTORY_READBACK_DRIFT")
        raw = b"".join(pieces)
        return raw, _file_stamp(before), hashlib.sha256(raw).hexdigest()
    except RealDailyHistoryError:
        raise
    except OSError as exc:
        if not readback and exc.errno == 2:
            code = "HISTORY_ARTIFACT_MISSING"
        raise RealDailyHistoryError(code) from None
    finally:
        if stream is not None:
            os.close(stream)


def _report_v1(request, result, scalar, baseline):
    """Frozen legacy v1 JSON projection; no arithmetic or live workflow helper."""
    return {"schema_version": 1, "scope": SCOPE, "origin": request["registration_identity"]["origin"],
            "candidate": request["candidate"], "method_digest": _digest_v1(request["method"]),
            "actual_result_digest": _digest_v1(result), "metrics": result["metrics"], "daily": result["daily"],
            "scalar_digest": _digest_v1(scalar), "scalar_status": scalar["status"],
            "baseline": {"name": "constant_zero_factor", "status": baseline["status"], "reason": baseline["reason"],
                         "metrics": baseline["metrics"], "result_digest": _digest_v1(baseline)},
            "scientific_score": None, "source_authentication": "not_certified_by_this_execution", "point_in_time": "not_certified",
            "final_execution_allowed": False, "final_numeric_rows_parsed": 0, "test_freshness": "unknown", "model_calls": 0}


def _markdown_v1(request, result, scalar, baseline):
    """Frozen legacy v1 Markdown bytes; metrics are copied, never recomputed."""
    return ("# Fixed Alpha101 #101 development review\n\nOrigin: " + request["registration_identity"]["origin"] + ". Current-vintage, static-universe engineering demonstration; PIT and investment performance are not certified.\n\nRaw gross price return: cash dividends are excluded; total return, costs, borrow and slippage are not modeled.\n\nMetrics below are copied from actual result.json. Final numeric rows: 0; final test freshness: unknown.\n\n```json\n" + _text_v1(result["metrics"]) + "```\n\nIndependent scalar check: " + scalar["status"] + ". Constant zero-factor baseline: " + baseline["status"] + "; undefined IC remains null.\n").encode("utf-8")


def _metrics(value):
    code = "HISTORY_BINDING_DRIFT"
    _shape(value, METRIC_KEYS, code)
    for name in ("eligible_days", "evaluated_days", "finite_factor_observations", "possible_factor_observations", "purged_days", "rank_ic_days", "skipped_days"):
        _need(type(value[name]) is int and value[name] >= 0, code)
    for name in ("mean_gross_return", "mean_rank_ic", "sum_gross_return"):
        _need(value[name] is None or type(value[name]) in (int, float), code)
    _need(type(value["factor_coverage"]) in (int, float) and 0 <= value["factor_coverage"] <= 1, code)


def _validate_objects(request, selection, producer, receipt, objects, markdown):
    code = "HISTORY_BINDING_DRIFT"
    _shape(receipt, RECEIPT_KEYS, code)
    _scope(receipt, receipt=True)
    _sha(receipt["receipt_digest"], code)
    _need(receipt["receipt_digest"] == _digest_v1({k: v for k, v in receipt.items() if k != "receipt_digest"}), code)
    for name, expected in (("job_id", producer["job_id"]), ("request_digest", producer["request_digest"]), ("registration_identity", request["registration_identity"]), ("selection_digest", request["selection_digest"]), ("method_digest", _digest_v1(request["method"])), ("code", request["code"]), ("candidate", request["candidate"])):
        _need(_same(receipt[name], expected), code)
    _need(_same(objects["registration"], request["registration_identity"]) and _same(objects["method"], request["method"]), code)
    _need(_same(objects["selection"], {k: selection[k] for k in ("assets", "sessions", "development_sessions", "config")}), code)
    attempt = objects["attempt"]
    _shape(attempt, {"schema_version", "job_id", "request_digest", "owner_token", "status_at_creation", "started_at_utc", "automatic_retries"}, code)
    _integer(attempt["schema_version"], 1, code)
    _need(attempt["job_id"] == producer["job_id"] and attempt["request_digest"] == producer["request_digest"] and attempt["status_at_creation"] == "running", code)
    _string(attempt["owner_token"], code)
    _string(attempt["started_at_utc"], code, 48)
    try:
        timestamp = datetime.fromisoformat(attempt["started_at_utc"])
        _need(timestamp.tzinfo is not None and timestamp.utcoffset().total_seconds() == 0, code)
    except ValueError:
        raise RealDailyHistoryError(code) from None
    _integer(attempt["automatic_retries"], 0, "HISTORY_SCOPE_DRIFT")
    result, scalar, baseline = objects["result"], objects["scalar"], objects["baseline"]
    for value in (result, baseline):
        _shape(value, RESULT_KEYS, code)
        _integer(value["schema_version"], 1, code)
        _need(value["method_digest"] == _digest_v1(request["method"]) and value["method_id"] == request["method"]["method_id"] and value["selection_digest"] == request["selection_digest"] and _same(value["config"], selection["config"]) and _same(value["return_convention"], request["method"]["return_convention"]), code)
        _need(value["evaluation_scope"] == "development_prefix_validation_only" and value["split"] == "validation" and value["test_freshness"] == "unknown" and value["source_authentication"] == "not_performed", "HISTORY_SCOPE_DRIFT")
        _integer(value["final_numeric_rows_used"], 0, "HISTORY_SCOPE_DRIFT")
        _need(type(value["daily"]) is list and type(value["execution"]) is dict and type(value["factor_diagnostics"]) is dict and type(value["market_metadata"]) is dict and type(value["limitations"]) is list and (value["reason"] is None or type(value["reason"]) is str), code)
        _metrics(value["metrics"])
    _need(result["status"] in ("evaluated", "not_evaluable"), code)
    _shape(scalar, SCALAR_KEYS, code)
    _integer(scalar["schema_version"], 1, code)
    _need(scalar["status"] == "verified" and scalar["method_id"] == request["method"]["method_id"] and scalar["method_digest"] == _digest_v1(request["method"]) and scalar["selection_digest"] == request["selection_digest"] and scalar["result_digest"] == _digest_v1(result), code)
    _need(scalar["arithmetic"] == "independent_python_stdlib_scalar" and scalar["oracle_version"] == "daily-scalar-alpha101-v1", code)
    _integer(scalar["final_numeric_rows_used"], 0, "HISTORY_SCOPE_DRIFT")
    for name in ("asset_records_checked", "daily_records_checked", "metrics_checked", "numeric_values_checked"):
        _need(type(scalar[name]) is int and scalar[name] >= 0, code)
    for name in ("absolute_tolerance", "max_absolute_error"):
        _need(type(scalar[name]) in (int, float) and scalar[name] >= 0, code)
    _need(baseline["status"] == "not_evaluable" and baseline["metrics"]["mean_rank_ic"] is None and receipt["baseline_status"] == "not_evaluable", code)
    for name, expected in (("actual_result_digest", _digest_v1(result)), ("scalar_digest", _digest_v1(scalar)), ("baseline_digest", _digest_v1(baseline)), ("result_file_sha256", receipt["files"]["result.json"])):
        _sha(receipt[name], code)
        _need(receipt[name] == expected, code)
    _need(_same(objects["report"], _report_v1(request, result, scalar, baseline)) and markdown == _markdown_v1(request, result, scalar, baseline), "HISTORY_REPORT_DRIFT")


def verify_archived_real_daily_run(workspace_root, *, producer):
    """Verify legacy archive bytes under host-supplied original external pins.

    This does not authenticate the producer, approve present execution, check
    present source files/permission, read code, or verify main-queue integration.
    Frozen metadata is bounded to 100k nodes / depth 64, JSON files to 8 MiB,
    each file to 64 MiB and total archive bytes to 128 MiB. The original eleven
    outputs are read twice with anchored directory fds and stable stat/hash checks.
    Producer inputs are copied as exact JSON builtins and all verification and
    returned pins use that snapshot. Observable caller drift is rejected. The
    lexical locator is checked again after the last artifact readback.
    No files are written and no repair/retry or tool execution is performed.
    """
    caller_producer = producer
    producer, producer_bytes = _producer_snapshot(caller_producer, "HISTORY_PRODUCER_INVALID")
    request, selection = _validate_producer(producer)
    _check_producer_readback(caller_producer, producer_bytes)
    root, relative = _root_path(workspace_root, request["output_relative"])
    fd = _open_directory(root, relative)
    try:
        directory_stamp = _directory_stamp(os.fstat(fd))
        _inventory(fd)
        contents, stamps, pins = {}, {}, {}
        raw, stamp, pin = _read_file(fd, "receipt.json")
        _need(pin == producer["receipt_sha256"], "HISTORY_RECEIPT_PIN_DRIFT")
        receipt = _json_v1(raw, "HISTORY_BINDING_DRIFT")
        _shape(receipt, RECEIPT_KEYS, "HISTORY_BINDING_DRIFT")
        _sha(receipt["receipt_digest"], "HISTORY_BINDING_DRIFT")
        _need(receipt["receipt_digest"] == _digest_v1({k: v for k, v in receipt.items() if k != "receipt_digest"}), "HISTORY_BINDING_DRIFT")
        _shape(receipt["files"], OUTPUT_FILES - {"receipt.json"}, "HISTORY_INVENTORY_INVALID")
        for expected_pin in receipt["files"].values():
            _sha(expected_pin, "HISTORY_BINDING_DRIFT")
        contents["receipt.json"], stamps["receipt.json"], pins["receipt.json"] = raw, stamp, pin
        total = len(raw)
        objects = {}
        for name in sorted(OUTPUT_FILES - {"receipt.json"}):
            content, stamp, pin = _read_file(fd, name)
            total += len(content)
            _need(total <= MAX_TOTAL_BYTES, "HISTORY_INVENTORY_INVALID")
            _need(pin == receipt["files"][name], "HISTORY_ARTIFACT_PIN_DRIFT")
            contents[name], stamps[name], pins[name] = content, stamp, pin
            if name.endswith(".json"):
                objects[name[:-5]] = _json_v1(content, "HISTORY_BINDING_DRIFT")
        _validate_objects(request, selection, producer, receipt, objects, contents["report.md"])
        _inventory(fd, readback=True)
        _need(_directory_stamp(os.fstat(fd)) == directory_stamp, "HISTORY_READBACK_DRIFT")
        # Re-open the lexical locator as well: replacing an ancestor directory
        # must not allow the still-open original fd to hide a restored-path drift.
        _check_locator_readback(root, relative, directory_stamp)
        for name in sorted(OUTPUT_FILES):
            _, stamp, pin = _read_file(fd, name, readback=True)
            _need(stamp == stamps[name] and pin == pins[name], "HISTORY_READBACK_DRIFT")
        _inventory(fd, readback=True)
        _need(_directory_stamp(os.fstat(fd)) == directory_stamp, "HISTORY_READBACK_DRIFT")
        # A later ancestor rename must also be detected after the second read.
        _check_locator_readback(root, relative, directory_stamp)
        _check_producer_readback(caller_producer, producer_bytes)
        return {"schema_version": 1, "history_version": HISTORY_VERSION, "verified": True,
                "scope": "legacy_real_daily_artifact_integrity_only", "producer_kind": PRODUCER_KIND,
                "job_id": producer["job_id"], "request_digest": producer["request_digest"], "receipt_sha256": producer["receipt_sha256"],
                "artifact_count": 11, "receipt": receipt, "current_source_matches": "not_checked", "current_permission": "not_checked",
                "authority_check_calls": 0, "source_file_reads": 0, "raw_original_reads": 0,
                "candidate_computations": 0, "worker_dispatches": 0, "model_calls": 0, "market_calls": 0,
                "final_numeric_rows_parsed": 0, "main_api_integration_verified": False, "main_queue_execution_verified": False}
    finally:
        os.close(fd)
