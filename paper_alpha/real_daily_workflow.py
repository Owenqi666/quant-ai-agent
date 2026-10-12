"""Pinned host effects for one prefix-only, fixed #101 Worker execution.

The tool receives an identity, never an expression, path override or trusted
approved flag. A failed/unknown dispatch remains terminal; history is read-only.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

from .storage import digest, json_text

JOB_VERSION = "real-daily-local-development-v1"
SCOPE = "local_current_vintage_development_only"
PDF_EDITION_SHA256 = "1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9"
JOB_KEYS = frozenset({"job_version", "host_root", "job_id", "request_digest"})
OUTPUT_FILES = frozenset({"attempt.json", "registration.json", "selection.json", "method.json", "factor.csv", "result.json", "scalar.json", "baseline.json", "report.json", "report.md", "receipt.json"})
CODE_FILES = (
    "paper_alpha/real_daily_workflow.py", "paper_alpha/real_daily_admission.py",
    "paper_alpha/real_daily_normalizer.py", "paper_alpha/real_daily_method.py",
    "paper_alpha/worker.py", "paper_alpha/expressions.py", "paper_alpha/evaluation.py",
    "paper_alpha/storage.py", "paper_alpha/vendor/operators.py", "paper_alpha/daily_event_review.py",
    "paper_alpha/contracts.py", "paper_alpha/market_diagnostics.py", "paper_alpha/server/db.py",
    "paper_alpha/server/maintenance.py", "paper_alpha/server/daily_snapshot_guard.py",
)
SCHEMA = """CREATE TABLE IF NOT EXISTS cycle_worker_effects(
 id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,
 request_json TEXT NOT NULL,registration_id TEXT NOT NULL,registration_digest TEXT NOT NULL,
 output_relative TEXT NOT NULL,status TEXT NOT NULL,created_at_utc TEXT NOT NULL,
 dispatched_at_utc TEXT,started_at_utc TEXT,finished_at_utc TEXT,owner_token TEXT,
 receipt_sha256 TEXT,error_code TEXT);
"""
TABLE_COLUMNS = ("id", "idempotency_key", "request_digest", "request_json", "registration_id", "registration_digest", "output_relative", "status", "created_at_utc", "dispatched_at_utc", "started_at_utc", "finished_at_utc", "owner_token", "receipt_sha256", "error_code")
IDENTITY_FIELDS = ("id", "registration_digest", "approval_id", "approval_digest", "assessment_id", "assessment_digest", "reservation_id", "reservation_digest", "prefix_sha256", "receipt_sha256", "method_digest", "origin")
MAX_BYTES = 64 * 1024 * 1024


class RealDailyWorkflowError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _need(condition, code, message):
    if not condition:
        raise RealDailyWorkflowError(code, message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_path(value):
    path = Path(value).absolute()
    _need(".." not in path.parts and path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)), "UNSAFE_PATH", "Canonical paths without links are required")
    return path


def _sha(value):
    _need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "INVALID_PIN", "Explicit lowercase SHA256 required")
    return value


def _read(path, limit=MAX_BYTES):
    path = _safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit, "UNSAFE_FILE", "Bounded independent regular input required")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
        _need(len(raw) == before.st_size and identity(before) == identity(os.fstat(fd)) == identity(path.stat(follow_symlinks=False)), "FILE_DRIFT", "Input changed during readback")
        return raw
    finally:
        os.close(fd)


def _file_sha(path):
    return hashlib.sha256(_read(path)).hexdigest()


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _need(key not in value, "INVALID_JSON", "Duplicate JSON keys forbidden")
            value[key] = item
        return value
    def invalid(_):
        raise RealDailyWorkflowError("INVALID_JSON", "Nonfinite JSON forbidden")
    def number(value):
        item = float(value)
        _need(math.isfinite(item), "INVALID_JSON", "Nonfinite JSON forbidden")
        return item
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)
        stack = [(value, 0)]
        count = 0
        while stack:
            item, depth = stack.pop(); count += 1
            _need(depth <= 64 and count <= 500_000, "INVALID_JSON", "JSON complexity limit exceeded")
            if isinstance(item, dict): stack.extend((v, depth + 1) for v in item.values())
            if isinstance(item, list): stack.extend((v, depth + 1) for v in item)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise RealDailyWorkflowError("INVALID_JSON", "Invalid bounded JSON") from exc


def _publish(path, raw):
    path = _safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)


def _publish_json(path, value):
    _publish(path, json_text(value).encode("utf-8"))


def code_identity():
    root = Path(__file__).resolve().parents[1]
    value = {"job_version": JOB_VERSION, "files": {name: _file_sha(root / name) for name in CODE_FILES}}
    return {**value, "digest": digest(value)}


def _schema_anchor(host):
    return {"schema_version": 1, "job_version": JOB_VERSION, "table": "cycle_worker_effects", "columns": list(TABLE_COLUMNS), "host_root": str(_safe_path(host.root))}


def _check_schema(host, connection):
    anchor = _safe_path(host.root / ".real-daily-worker-schema.json")
    _need(anchor.is_file() and _json(_read(anchor)) == _schema_anchor(host), "EFFECT_SCHEMA_ANCHOR", "Original host Worker schema anchor required")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(cycle_worker_effects)"))
    _need(columns == TABLE_COLUMNS, "EFFECT_SCHEMA", "Original host Worker table is missing or changed")


def _ensure_schema(host, connection):
    anchor = _safe_path(host.root / ".real-daily-worker-schema.json")
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_worker_effects'").fetchone() is not None
    if anchor.exists():
        _check_schema(host, connection)
        return
    _need(not exists, "EFFECT_SCHEMA_ANCHOR", "Worker effect table has no original host schema anchor")
    # Anchor is durable before the transaction commits. Death leaves an explicit
    # missing-table condition, never permission to recreate forgotten effects.
    _publish_json(anchor, _schema_anchor(host))
    connection.execute(SCHEMA)
    _check_schema(host, connection)


def _effect_anchor_path(host, registration_id):
    name = hashlib.sha256(registration_id.encode("utf-8")).hexdigest() + ".json"
    return _safe_path(host.root / ".real-daily-worker-effects" / name)


def _effect_anchor(row):
    return {"schema_version": 1, "job_version": JOB_VERSION, **{k: row[k] for k in ("id", "idempotency_key", "request_digest", "registration_id", "registration_digest", "output_relative")}}


def _assert_effect_anchor(host, row):
    path = _effect_anchor_path(host, row["registration_id"])
    _need(path.is_file() and _json(_read(path)) == _effect_anchor(row), "HOST_EFFECT_ANCHOR", "Original immutable host effect anchor differs")


def _row(host, job_id, connection=None):
    _need(type(job_id) is str and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", job_id) is not None, "INVALID_JOB", "Bounded job identity required")
    if connection is None:
        with closing(host._connect()) as current:
            return _row(host, job_id, current)
    _check_schema(host, connection)
    try:
        found = connection.execute("SELECT * FROM cycle_worker_effects WHERE id=?", (job_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        raise RealDailyWorkflowError("JOB_MISSING", "Original host job is missing") from exc
    _need(found is not None, "JOB_MISSING", "Original host job is missing")
    row = dict(found)
    _assert_effect_anchor(host, row)
    return row


def _output(host, value):
    root = _safe_path(host.root)
    path = _safe_path(value)
    _need(path.is_relative_to(root) and path != root, "UNSAFE_OUTPUT", "Worker output must belong to the host")
    relative = path.relative_to(root)
    _need(len(relative.parts) == 2 and relative.parts[0] == "real_daily_runs", "UNSAFE_OUTPUT", "Use host/real_daily_runs/new-name")
    _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", relative.parts[1]) is not None, "UNSAFE_OUTPUT", "Invalid output name")
    return path, relative.as_posix()


def _resolved(host, registration_id, pin):
    from .real_daily_method import method_profile
    value = host.resolve_registration(registration_id, _sha(pin))
    _need(type(value) is dict and all(k in value for k in IDENTITY_FIELDS + ("selection", "assessment", "method")), "REGISTRATION_SHAPE", "Complete registered development identity required")
    _need(value["id"] == registration_id and value["registration_digest"] == pin and value["status"] == "registered", "REGISTRATION_DRIFT", "Registration identity changed")
    _need(value["method_digest"] == digest(method_profile()) and digest(value["method"]) == value["method_digest"], "METHOD_DRIFT", "Registered return method differs")
    _need(value["origin"] in ("engineering_fixture", "real_source_declaration"), "REGISTRATION_SCOPE", "Unsupported registered origin")
    return value


def _formula_evidence():
    from .real_daily_method import EXPRESSION_101
    root = Path(__file__).resolve().parents[1]
    evidence_path = root / "examples/alpha101/source_evidence.json"
    raw = _read(evidence_path, 2 * 1024 * 1024)
    source = _json(raw)
    matches = [e for e in source.get("evidence", []) if e.get("id") == "alpha101-formula"]
    _need(len(matches) == 1 and matches[0].get("quote") == EXPRESSION_101 and matches[0].get("page") == 15 and matches[0].get("visual_checked") is True, "FORMULA_EVIDENCE_DRIFT", "Maintained paper formula evidence differs")
    _need(source.get("paper_id") == "arxiv:1601.00991v3" and source.get("document_sha256") == PDF_EDITION_SHA256 and _file_sha(root / "examples/alpha101/paper.pdf") == _sha(source.get("document_sha256")), "FORMULA_DOCUMENT_DRIFT", "Actual retained paper differs from formula evidence")
    return {"kind": "maintained_paper_formula", "paper_id": source["paper_id"], "document_sha256": source["document_sha256"],
            "source_evidence_file_sha256": hashlib.sha256(raw).hexdigest(), "evidence_id": "alpha101-formula", "physical_pdf_page": 15,
            "quote": matches[0]["quote"], "verification_scope": "Retained visually checked formula reference; user-selected engineering material. No new economic or profitability validation."}


def _request(host, registration, relative, key):
    from .real_daily_method import EXPRESSION_101, method_profile
    return {"schema_version": 1, "job_version": JOB_VERSION, "scope": SCOPE,
            "registration_identity": {k: registration[k] for k in IDENTITY_FIELDS},
            "registration_resolved_digest": digest(registration), "selection_digest": digest(registration["selection"]),
            "method": method_profile(), "code": code_identity(), "output_relative": relative,
            "idempotency_key": key, "candidate": {"id": "fixed-alpha101-101", "expression": EXPRESSION_101,
                "evidence": _formula_evidence(),
                "project_modifications": ["Static demonstration universe; local current-vintage development validation; next-open gross price-return rank portfolio."]},
            "budget": {"candidate_computations": 1, "evaluation_calls": 2, "model_calls": 0, "automatic_retries": 0, "max_seconds": 60},
            "final_execution_allowed": False, "final_numeric_rows_parsed": 0, "test_freshness": "unknown"}


def _assert_request(host, row):
    request = _json(row["request_json"])
    _need(digest(request) == row["request_digest"], "HOST_EFFECT_DRIFT", "Original host request changed")
    registration = _resolved(host, row["registration_id"], row["registration_digest"])
    expected = _request(host, registration, row["output_relative"], row["idempotency_key"])
    _need(request == expected, "CURRENT_AUTHORITY_DRIFT", "Current authority, input, configuration or code differs")
    return request, registration


def create_worker_job(host, registration_id, expected_registration_digest, out_dir, *, idempotency_key):
    """Persist one immutable host effect; no market-number read or subprocess."""
    _need(type(idempotency_key) is str and 0 < len(idempotency_key) <= 128 and bool(idempotency_key.strip()), "INVALID_KEY", "Bounded nonempty idempotency key required")
    directory, relative = _output(host, out_dir)
    registration = _resolved(host, registration_id, expected_registration_digest)
    request = _request(host, registration, relative, idempotency_key)
    request_digest = digest(request)
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-alpha:real-daily-worker:" + digest({"key": idempotency_key})))
    with host._transaction() as connection:
        _ensure_schema(host, connection)
        existing = connection.execute("SELECT * FROM cycle_worker_effects WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None:
            row = dict(existing)
            _need(row["request_digest"] == request_digest and _json(row["request_json"]) == request, "IDEMPOTENCY_CONFLICT", "Original key belongs to a different request")
            _assert_effect_anchor(host, row)
            identity = row["id"]
        else:
            _need(not directory.exists(), "OUTPUT_EXISTS", "New output must not exist")
            _need(connection.execute("SELECT 1 FROM cycle_worker_effects WHERE output_relative=?", (relative,)).fetchone() is None, "OUTPUT_OWNED", "Output belongs to another original effect")
            _need(connection.execute("SELECT 1 FROM cycle_worker_effects WHERE registration_id=?", (registration_id,)).fetchone() is None, "REGISTRATION_EFFECT_EXISTS", "This registration already owns an original fixed-candidate effect")
            effect_path = _effect_anchor_path(host, registration_id)
            _need(not effect_path.exists(), "ORIGINAL_EFFECT_MISSING", "Original retained host effect cannot be recreated")
            effect_path.parent.mkdir(exist_ok=True)
            intent = {"id": identity, "idempotency_key": idempotency_key, "request_digest": request_digest, "registration_id": registration_id, "registration_digest": expected_registration_digest, "output_relative": relative}
            _publish_json(effect_path, _effect_anchor(intent))
            values = (identity, idempotency_key, request_digest, json_text(request), registration_id, expected_registration_digest, relative, "queued", _now())
            connection.execute("INSERT INTO cycle_worker_effects(id,idempotency_key,request_digest,request_json,registration_id,registration_digest,output_relative,status,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)", values)
    return {"job_version": JOB_VERSION, "host_root": str(_safe_path(host.root)), "job_id": identity, "request_digest": request_digest}


def _validate_job(job):
    _need(type(job) is dict and set(job) == JOB_KEYS and job.get("job_version") == JOB_VERSION, "INVALID_JOB", "Closed real-daily Worker job required")
    _sha(job["request_digest"])
    return job


def _fail(host, job_id, code, *, allowed=("dispatching", "running"), owner_token=None):
    with host._transaction() as connection:
        row = _row(host, job_id, connection)
        if row["status"] not in allowed or (owner_token is not None and row["owner_token"] != owner_token):
            return
        connection.execute("UPDATE cycle_worker_effects SET status='failed',finished_at_utc=?,error_code=? WHERE id=?", (_now(), code, job_id))


def _selection_public(selection):
    return {k: selection[k] for k in ("assets", "sessions", "development_sessions", "config")}


def _report(request, result, scalar, baseline):
    return {"schema_version": 1, "scope": SCOPE, "origin": request["registration_identity"]["origin"],
            "candidate": request["candidate"], "method_digest": digest(request["method"]),
            "actual_result_digest": digest(result), "metrics": result["metrics"], "daily": result["daily"],
            "scalar_digest": digest(scalar), "scalar_status": scalar["status"],
            "baseline": {"name": "constant_zero_factor", "status": baseline["status"], "reason": baseline["reason"],
                "metrics": baseline["metrics"], "result_digest": digest(baseline)},
            "scientific_score": None, "source_authentication": "not_certified_by_this_execution", "point_in_time": "not_certified",
            "final_execution_allowed": False, "final_numeric_rows_parsed": 0, "test_freshness": "unknown", "model_calls": 0}


def execute_worker_job(job):
    """Child entry: only a host-dispatched original effect may compute."""
    _validate_job(job)
    from .real_daily_admission import RealDailyHost
    from .real_daily_normalizer import load_registered_panels
    from .real_daily_method import evaluate_ohlc, verify_scalar101
    from .expressions import compute_expression
    import pandas as pd
    host = RealDailyHost(_safe_path(job["host_root"]))
    row = _row(host, job["job_id"])
    _need(row["request_digest"] == job["request_digest"], "HOST_EFFECT_DRIFT", "Job differs from original host effect")
    request, registration = _assert_request(host, row)
    owner = str(uuid.uuid4())
    directory, relative = _output(host, host.root / row["output_relative"])
    with host._transaction() as connection:
        current = _row(host, row["id"], connection)
        _need(current == row and row["status"] == "dispatching", "EXECUTION_NOT_DISPATCHED", "Only one original dispatched effect may start")
        _need(not directory.exists(), "OUTPUT_EXISTS", "Original output already exists")
        connection.execute("UPDATE cycle_worker_effects SET status='running',started_at_utc=?,owner_token=? WHERE id=?", (_now(), owner, row["id"]))
    started = time.monotonic()
    try:
        directory.parent.mkdir(exist_ok=True)
        directory.mkdir(exist_ok=False)
        _publish_json(directory / "attempt.json", {"schema_version": 1, "job_id": row["id"], "request_digest": row["request_digest"], "owner_token": owner, "status_at_creation": "running", "started_at_utc": _now(), "automatic_retries": 0})
        # Only managed registered prefix is opened here. Upstream raw, volume
        # tokens and final values are not input resources to this Worker.
        panels = load_registered_panels(host, row["registration_id"], row["registration_digest"])
        _need(digest(_resolved(host, row["registration_id"], row["registration_digest"])) == request["registration_resolved_digest"], "CURRENT_AUTHORITY_DRIFT", "Authority changed before computation")
        _need(code_identity() == request["code"], "CODE_DRIFT", "Current Worker code changed")
        factor = compute_expression(request["candidate"]["expression"], panels)
        result = evaluate_ohlc(factor, panels, selection=registration["selection"])
        _assert_request(host, _row(host, row["id"]))
        scalar = verify_scalar101(panels, selection=registration["selection"], result=result)
        zero = pd.DataFrame(0.0, index=factor.index, columns=factor.columns)
        baseline = evaluate_ohlc(zero, panels, selection=registration["selection"])
        _need(baseline["status"] == "not_evaluable" and baseline["metrics"]["mean_rank_ic"] is None, "BASELINE_DRIFT", "Constant baseline must preserve undefined IC")
        _need(time.monotonic() - started <= request["budget"]["max_seconds"], "WORKER_TIMEOUT", "Worker budget exhausted")
        current = _row(host, row["id"])
        _need(current["status"] == "running" and current["owner_token"] == owner, "OWNERSHIP_REVOKED", "Original Worker ownership changed")
        _assert_request(host, current)
        report = _report(request, result, scalar, baseline)
        artifacts = {"registration": request["registration_identity"], "selection": _selection_public(registration["selection"]), "method": request["method"], "result": result, "scalar": scalar, "baseline": baseline, "report": report}
        for name, value in artifacts.items(): _publish_json(directory / (name + ".json"), value)
        _publish(directory / "factor.csv", factor.to_csv(index_label="date", float_format="%.17g").encode("utf-8"))
        markdown = "# Fixed Alpha101 #101 development review\n\nOrigin: " + request["registration_identity"]["origin"] + ". Current-vintage, static-universe engineering demonstration; PIT and investment performance are not certified.\n\nRaw gross price return: cash dividends are excluded; total return, costs, borrow and slippage are not modeled.\n\nMetrics below are copied from actual result.json. Final numeric rows: 0; final test freshness: unknown.\n\n```json\n" + json_text(result["metrics"]) + "```\n\nIndependent scalar check: " + scalar["status"] + ". Constant zero-factor baseline: " + baseline["status"] + "; undefined IC remains null.\n"
        _publish(directory / "report.md", markdown.encode("utf-8"))
        _assert_request(host, _row(host, row["id"]))
        files = {name: _file_sha(directory / name) for name in sorted(OUTPUT_FILES - {"receipt.json"})}
        receipt = {"schema_version": 1, "job_version": JOB_VERSION, "job_id": row["id"], "request_digest": row["request_digest"], "status": "completed", "scope": SCOPE,
                   "registration_identity": request["registration_identity"], "selection_digest": request["selection_digest"], "method_digest": digest(request["method"]), "code": request["code"],
                   "candidate": request["candidate"], "files": files, "actual_result_digest": digest(result), "result_file_sha256": files["result.json"], "scalar_digest": digest(scalar), "baseline_digest": digest(baseline), "baseline_status": baseline["status"],
                   "candidate_computations": 1, "evaluation_calls": 2, "model_calls": 0, "automatic_retries": 0, "final_numeric_rows_parsed": 0, "final_execution_allowed": False, "test_freshness": "unknown", "source_authentication": "not_certified_by_this_execution", "point_in_time": "not_certified", "scientific_score": None}
        receipt["receipt_digest"] = digest(receipt)
        _publish_json(directory / "receipt.json", receipt)
        pin = _file_sha(directory / "receipt.json")
        with host._transaction() as connection:
            current = _row(host, row["id"], connection)
            _need(current["status"] == "running" and current["owner_token"] == owner, "OWNERSHIP_REVOKED", "Original Worker ownership changed before publication")
            _assert_request(host, current)
            connection.execute("UPDATE cycle_worker_effects SET status='completed',finished_at_utc=?,receipt_sha256=? WHERE id=?", (_now(), pin, row["id"]))
        return {"status": "completed", "job_id": row["id"], "request_digest": row["request_digest"], "receipt_sha256": pin, "candidate_computations": 1, "model_calls": 0, "final_numeric_rows_parsed": 0}
    except Exception as exc:
        code = getattr(exc, "code", "WORKER_COMPUTE_FAILED")
        _fail(host, row["id"], code, owner_token=owner)
        raise RealDailyWorkflowError(code, "Real daily Worker failed; original effect and outputs retained") from exc


def verify_worker_result(host, job_id, *, expected_receipt_sha256):
    """Original host producer pin + actual file/report readback; zero computation."""
    _sha(expected_receipt_sha256)  # Before inspecting output or creating anything.
    row = _row(host, job_id)
    _need(row["status"] == "completed" and row["receipt_sha256"] == expected_receipt_sha256, "HOST_RECEIPT_DRIFT", "Original completed host receipt pin required")
    request, registration = _assert_request(host, row)
    directory, _ = _output(host, host.root / row["output_relative"])
    raw = _read(directory / "receipt.json")
    _need(hashlib.sha256(raw).hexdigest() == expected_receipt_sha256, "RECEIPT_PIN_DRIFT", "Original producer receipt bytes differ")
    receipt = _json(raw)
    _need(receipt.get("receipt_digest") == digest({k: v for k, v in receipt.items() if k != "receipt_digest"}), "RECEIPT_DRIFT", "Receipt canonical digest differs")
    _need(receipt["job_id"] == job_id and receipt["request_digest"] == row["request_digest"] and receipt["code"] == request["code"] and receipt["registration_identity"] == request["registration_identity"] and receipt["selection_digest"] == request["selection_digest"] and receipt["method_digest"] == digest(request["method"]) and receipt["candidate"] == request["candidate"], "RECEIPT_BINDING", "Actual receipt does not bind the frozen host effect")
    _need(set(p.name for p in directory.iterdir()) == OUTPUT_FILES and set(receipt["files"]) == OUTPUT_FILES - {"receipt.json"}, "OUTPUT_INVENTORY", "Unexpected output inventory")
    objects = {}
    for name, pin in receipt["files"].items():
        content = _read(directory / name)
        _need(hashlib.sha256(content).hexdigest() == _sha(pin), "OUTPUT_PIN_DRIFT", "Actual output bytes changed")
        if name.endswith(".json"): objects[name[:-5]] = _json(content)
    _need(objects["registration"] == request["registration_identity"] and objects["selection"] == _selection_public(registration["selection"]) and objects["method"] == request["method"], "OUTPUT_BINDING", "Output input/configuration/method binding differs")
    result, scalar, baseline = objects["result"], objects["scalar"], objects["baseline"]
    _need(digest(result) == receipt["actual_result_digest"] and receipt["result_file_sha256"] == receipt["files"]["result.json"] and digest(scalar) == receipt["scalar_digest"] and digest(baseline) == receipt["baseline_digest"], "TOOL_DRIFT", "Actual tool/oracle/baseline identity differs")
    _need(objects["report"] == _report(request, result, scalar, baseline), "REPORT_DRIFT", "Report differs from actual tool output")
    _need(scalar["status"] == "verified" and baseline["status"] == "not_evaluable" and baseline["metrics"]["mean_rank_ic"] is None and receipt["baseline_status"] == "not_evaluable", "BASELINE_DRIFT", "Scalar or constant-baseline state differs")
    _need(receipt["status"] == "completed" and receipt["scope"] == SCOPE and receipt["candidate_computations"] == 1 and receipt["evaluation_calls"] == 2 and receipt["model_calls"] == 0 and receipt["automatic_retries"] == 0 and receipt["final_numeric_rows_parsed"] == 0 and receipt["final_execution_allowed"] is False and receipt["test_freshness"] == "unknown" and receipt["source_authentication"] == "not_certified_by_this_execution" and receipt["point_in_time"] == "not_certified" and receipt["scientific_score"] is None, "OUTPUT_SCOPE", "Execution scope changed")
    _assert_request(host, _row(host, job_id))
    _need(_row(host, job_id) == row and _read(directory / "receipt.json") == raw, "READBACK_DRIFT", "Host or receipt changed during readback")
    return {"verified": True, "status": "completed", "job_id": job_id, "receipt_sha256": expected_receipt_sha256, "receipt": receipt, "candidate_computations": 0, "model_calls": 0, "final_numeric_rows_parsed": 0}


def _stop(process):
    if process.poll() is None:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    process.wait(timeout=5)


def run_registered_development(host, registration_id, expected_registration_digest, out_dir, *, idempotency_key, timeout_seconds=60):
    """Dispatch once using the existing isolated Worker process, never retry."""
    _need(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 60, "INVALID_TIMEOUT", "Worker timeout must be positive and at most 60 seconds")
    job = create_worker_job(host, registration_id, expected_registration_digest, out_dir, idempotency_key=idempotency_key)
    row = _row(host, job["job_id"])
    if row["status"] == "completed":
        return verify_worker_result(host, row["id"], expected_receipt_sha256=row["receipt_sha256"])
    _need(row["status"] == "queued", "PARTIAL_UNKNOWN", "Original dispatch is incomplete or failed; automatic recomputation forbidden")
    _assert_request(host, row)
    with host._transaction() as connection:
        current = _row(host, row["id"], connection)
        _need(current == row and current["status"] == "queued", "RUN_BUSY", "Original dispatch already claimed")
        connection.execute("UPDATE cycle_worker_effects SET status='dispatching',dispatched_at_utc=? WHERE id=?", (_now(), row["id"]))
    folder = _safe_path(host.root / "real_daily_worker_io" / row["id"])
    process = None
    try:
        folder.parent.mkdir(exist_ok=True); folder.mkdir(exist_ok=False)
        job_path, response_path = folder / "job.json", folder / "response.json"
        _publish_json(job_path, job)
        project = Path(__file__).resolve().parents[1]
        # No credential/model-provider environment is passed to this tool.
        env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONPATH": str(project), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        with (folder / "stdout.log").open("xb") as stdout, (folder / "stderr.log").open("xb") as stderr:
            process = subprocess.Popen([sys.executable, "-B", "-m", "paper_alpha.worker", str(job_path), str(response_path)], cwd=project, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            try: process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _stop(process); _fail(host, row["id"], "WORKER_TIMEOUT")
                raise RealDailyWorkflowError("WORKER_TIMEOUT", "Worker timed out; retained without retry") from exc
        response = _json(_read(response_path))
        _need(process.returncode == 0 and type(response) is dict and set(response) == {"ok", "value"} and response["ok"] is True, "WORKER_FAILED", "Worker did not produce a successful original response")
        actual = _row(host, row["id"])
        _need(actual["status"] == "completed", "WORKER_FAILED", "Worker did not complete its original host effect")
        _need(response["value"] == {"status": "completed", "job_id": row["id"], "request_digest": row["request_digest"], "receipt_sha256": actual["receipt_sha256"], "candidate_computations": 1, "model_calls": 0, "final_numeric_rows_parsed": 0}, "WORKER_RESPONSE_DRIFT", "Worker response differs from original host completion")
        result = verify_worker_result(host, row["id"], expected_receipt_sha256=actual["receipt_sha256"])
        result["candidate_computations"] = 1
        return result
    except Exception as exc:
        if process is not None: _stop(process)
        _fail(host, row["id"], getattr(exc, "code", "WORKER_DISPATCH_FAILED"))
        if isinstance(exc, RealDailyWorkflowError): raise
        raise RealDailyWorkflowError("WORKER_DISPATCH_FAILED", "Worker dispatch failed; original effect retained") from exc
