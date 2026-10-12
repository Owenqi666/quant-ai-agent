"""Registered fixed #101 development execution; inner D v1 stays immutable."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from .daily_contract import CANONICAL_VERSION, parse_contract
from .daily_research import CODE_FILES as DAILY_CODE_FILES, EXPRESSION_101, RUN_VERSION as INNER_VERSION
from .daily_research import research_code_identity, run_daily_development
from .storage import atomic_json, digest, json_text
from .server.maintenance import workspace_lease
from .server.provider_catalog import catalog_from_daily_contract

RUN_VERSION = "registered-daily-development-v1"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
INNER_FILES = frozenset({"attempt.json", "preflight.json", "inputs.json", "contract.json", "config.json", "catalog.json", "candidate.json", "code.json", "factor.json", "evaluation.json", "report.json", "factor.csv", "development_input.csv", "report.md"})
CODE_FILES = tuple(sorted(set(DAILY_CODE_FILES) | {
    "paper_alpha/registered_daily_development.py", "scripts/run_registered_daily.py",
    "paper_alpha/server/daily_snapshot_schema.py", "paper_alpha/server/daily_snapshots.py",
    "paper_alpha/server/daily_snapshot_guard.py", "paper_alpha/server/db.py",
    "paper_alpha/server/service.py", "paper_alpha/server/maintenance.py",
}))
REGISTRATION_FIELDS = ("id", "registered_dataset_id", "input_digest", "contract_file_sha256", "contract_digest", "receipt_file_sha256", "raw_sha256", "raw_bytes", "origin", "declaration_digest", "registration_digest", "development_content_digest", "development_index_digest")


class RegisteredDailyError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _need(condition, code, message):
    if not condition:
        raise RegisteredDailyError(code, message)


def _path(value):
    path = Path(value).absolute()
    _need(".." not in path.parts and path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)), "UNSAFE_PATH", "Canonical paths without symbolic links are required")
    return path


def _sha(value):
    _need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "INVALID_PIN", "A trusted host SHA256 pin is required")
    return value


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _read(path, limit=MAX_JSON_BYTES):
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit, "UNSAFE_INPUT", "Input must be a bounded independent regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        _need(len(raw) == before.st_size and len(raw) <= limit and _signature(before) == _signature(os.fstat(fd)) == _signature(path.stat(follow_symlinks=False)), "INPUT_DRIFT", "Input changed during readback")
        return raw
    finally:
        os.close(fd)


def _hash(path):
    return hashlib.sha256(_read(path, MAX_FILE_BYTES)).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "INVALID_JSON", "Duplicate JSON keys are forbidden")
            result[key] = value
        return result
    def constant(value):
        raise RegisteredDailyError("INVALID_JSON", "Nonfinite JSON numbers are forbidden")
    def number(value):
        parsed = float(value)
        _need(math.isfinite(parsed), "INVALID_JSON", "Nonfinite JSON numbers are forbidden")
        return parsed
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
        stack = [(value, 0)]; count = 0
        while stack:
            item, depth = stack.pop(); count += 1
            _need(depth <= 64 and count <= 500_000, "INVALID_JSON", "JSON exceeds complexity limits")
            if isinstance(item, dict): stack.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list): stack.extend((v, depth + 1) for v in item)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise RegisteredDailyError("INVALID_JSON", "Invalid bounded JSON") from exc


def _object(value, keys, label):
    _need(type(value) is dict and set(value) == set(keys), "RECEIPT_SCHEMA", "Unexpected fields in " + label)


def code_identity():
    root = Path(__file__).resolve().parents[1]
    value = {"run_version": RUN_VERSION, "files": {name: _hash(root / name) for name in CODE_FILES}, "daily_engine": research_code_identity()}
    return {**value, "digest": digest(value)}


def _registered(store, registration_id):
    # Only current managed paths produced by D are visited; old inner paths are
    # historical text and never used as IO arguments.
    from .server.daily_snapshots import DailySnapshots
    current = DailySnapshots(store).compute_inputs(registration_id)
    _object(current, {"paths", "registration", "guard"}, "private registered input")
    paths, row, guard = current["paths"], current["registration"], current["guard"]
    _object(paths, {"contract", "csv", "source_receipt"}, "managed paths")
    _need(row["id"] == registration_id and row["status"] == "registered" and row["origin"] == "engineering_fixture" and row["registered_dataset_id"] is not None, "REGISTRATION_BLOCKED", "Only registered engineering fixtures may execute")
    root = _path(store.root)
    paths = {key: _path(path) for key, path in paths.items()}
    _need(all(path.is_relative_to(root / "daily_snapshots" / registration_id / "inputs") for path in paths.values()), "UNSAFE_INPUT", "Inputs must belong to the current managed registration")
    contract = parse_contract(_json(row["contract_json"]))
    _need(contract.to_dict()["origin"] == "engineering_fixture", "REGISTRATION_BLOCKED", "Real source execution remains pending")
    identity = {key: row[key] for key in REGISTRATION_FIELDS}
    identity["canonical_version"] = CANONICAL_VERSION
    identity["config_digest"] = digest(contract.to_dict()["evaluation"])
    identity["catalog_digest"] = catalog_from_daily_contract(contract)["catalog_digest"]
    identity["guard_digest"] = digest(guard)
    return paths, contract, identity, guard


def _output(store, out_dir):
    root, directory = _path(store.root), _path(out_dir)
    _need(directory.is_relative_to(root) and directory != root, "UNSAFE_OUTPUT", "Run output must be inside the managed workspace")
    relative = directory.relative_to(root)
    _need(relative.parts[0] == "registered_daily_runs" and len(relative.parts) == 2, "UNSAFE_OUTPUT", "Use a new direct directory under workspace/registered_daily_runs")
    return directory, relative.as_posix()


@contextmanager
def _operation_lock(store, key):
    folder = _path(store.root) / ".registered-daily-locks"
    _path(folder); folder.mkdir(exist_ok=True)
    path = folder / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "UNSAFE_LOCK", "Operation lock must be an independent regular file")
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise RegisteredDailyError("RUN_BUSY", "Original registered operation is still active") from exc
        yield
    finally:
        os.close(fd)


def _exclusive_json(path, value):
    """Exclusive durable intent; a partial file after death is never repaired."""
    fd = os.open(_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json_text(value).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)


def _effect_path(store, key):
    return _path(_path(store.root) / ".registered-daily-effects" / (hashlib.sha256(key.encode()).hexdigest() + ".json"))


def _inner(directory, expected_manifest_sha256, identity, contract):
    root = _path(directory)
    raw = _read(root / "manifest.json")
    _need(hashlib.sha256(raw).hexdigest() == _sha(expected_manifest_sha256), "INNER_PIN_DRIFT", "Original producer manifest pin differs")
    m = _json(raw)
    _object(m, {"schema_version", "run_version", "status", "scope", "attempt_id", "identity", "files", "test_freshness", "real_source_ready", "real_execution_readiness", "registered", "legacy_guard_connected", "model_calls", "final_numeric_rows_parsed", "manifest_digest"}, "inner manifest")
    _need(m["schema_version"] == 1 and m["run_version"] == INNER_VERSION and m["status"] == "completed" and m["scope"] == "engineering_fixture_development_validation", "INNER_SCOPE", "Only completed D v1 engineering output is accepted")
    _need(m["registered"] is False and m["legacy_guard_connected"] is False and m["test_freshness"] == "unknown" and m["real_source_ready"] is False and m["real_execution_readiness"] == "pending" and type(m["model_calls"]) is int and m["model_calls"] == 0 and type(m["final_numeric_rows_parsed"]) is int and m["final_numeric_rows_parsed"] == 0, "INNER_SCOPE", "Original D scope must remain unchanged")
    _need(m["manifest_digest"] == digest({k: v for k, v in m.items() if k != "manifest_digest"}), "INNER_DRIFT", "Inner manifest digest differs")
    _need(type(m["files"]) is dict and set(m["files"]) == INNER_FILES and {p.name for p in root.iterdir()} == INNER_FILES | {"manifest.json"}, "INNER_INVENTORY", "Inner artifact inventory differs")
    objects, total = {}, 0
    for name, expected in m["files"].items():
        content = _read(root / name, MAX_JSON_BYTES if name.endswith(".json") else MAX_FILE_BYTES)
        total += len(content)
        _need(total <= MAX_TOTAL_BYTES and hashlib.sha256(content).hexdigest() == _sha(expected), "INNER_DRIFT", "Actual inner artifact SHA differs")
        if name.endswith(".json"): objects[name[:-5]] = _json(content)
    expected_input = {key: identity[key] for key in ("contract_file_sha256", "receipt_file_sha256", "raw_sha256", "raw_bytes")}
    _need(objects["inputs"]["input_digests"] == expected_input and objects["contract"] == contract.to_dict() and objects["config"] == contract.to_dict()["evaluation"], "INNER_INPUT_DRIFT", "Inner input/config differs from the formal registration")
    # Never dereference objects['inputs']['paths']; backup keeps them unchanged.
    _need(objects["catalog"] == catalog_from_daily_contract(contract) and objects["code"] == research_code_identity(), "INNER_CODE_DRIFT", "Inner catalog/current engine identity differs")
    candidate = objects["candidate"]
    _need(candidate["id"] == "fixed-alpha101-101" and candidate["expression"] == EXPRESSION_101, "INNER_CANDIDATE", "Only the fixed #101 candidate is permitted")
    tool, report, tool_identity = objects["evaluation"], objects["report"], m["identity"]
    _need(objects["attempt"]["attempt_id"] == m["attempt_id"] and objects["attempt"]["status"] == "completed" and objects["attempt"]["identity"] == tool_identity, "INNER_ATTEMPT", "Inner attempt identity differs")
    for key in ("input_digest", "contract_digest", "development_content_digest", "canonical_version"):
        expected = identity.get(key, objects["preflight"]["identity"][key])
        _need(tool_identity[key] == expected, "INNER_IDENTITY", "Inner computed identity differs from the registered snapshot")
    _need(tool_identity["origin"] == "engineering_fixture" and tool_identity["config_digest"] == identity["config_digest"] and tool_identity["catalog_digest"] == identity["catalog_digest"] and tool_identity["code_digest"] == objects["code"]["digest"] and tool_identity["evaluation_digest"] == digest(tool) and tool_identity["evaluation_file_sha256"] == m["files"]["evaluation.json"], "INNER_IDENTITY", "Inner input/config/catalog/code/tool identity differs")
    _need(tool["config"] == objects["config"] and tool["evaluation_scope"] == "development_prefix_validation_only" and tool["test_freshness"] == "unknown" and type(tool["final_numeric_rows_used"]) is int and tool["final_numeric_rows_used"] == 0 and tool["source_authentication"] == "not_performed", "INNER_SCOPE", "Tool scope differs")
    _need(report["result_digest"] == digest(tool) and report["identity"] == tool_identity and report["reported_metrics"] == tool["metrics"] and report["reported_daily"] == tool["daily"], "INNER_REPORT_DRIFT", "Report differs from actual tool output")
    _need(_read(root / "manifest.json") == raw and all(_hash(root / name) == expected for name, expected in m["files"].items()), "INNER_DRIFT", "Inner output changed during readback")
    return {"manifest_file_sha256": expected_manifest_sha256, "manifest_digest": m["manifest_digest"], "attempt_id": m["attempt_id"], "files": m["files"], "actual_tool_digest": digest(tool), "evaluation_file_sha256": m["files"]["evaluation.json"], "config_digest": digest(objects["config"]), "catalog_digest": objects["catalog"]["catalog_digest"], "code_digest": objects["code"]["digest"], "factor_digest": digest(objects["factor"]), "candidate_digest": digest(candidate), "historical_input_paths_accessed": False}


def verify_registered_run(store, out_dir, *, expected_receipt_sha256):
    """Strict current authority and pinned historical bytes; zero computation."""
    directory, relative = _output(store, out_dir)
    _sha(expected_receipt_sha256)
    raw = _read(directory / "registered-result.json")
    _need(hashlib.sha256(raw).hexdigest() == expected_receipt_sha256, "OUTER_PIN_DRIFT", "Trusted original outer producer pin differs")
    result = _json(raw)
    _object(result, {"schema_version", "run_version", "logical_run_id", "request_digest", "idempotency_key", "output_relative", "status", "scope", "registration_identity", "guard", "code", "inner", "attempt_file_sha256", "effect_file_sha256", "test_freshness", "real_source_ready", "source_authentication", "point_in_time", "scientific_score", "final_numeric_rows_parsed", "model_calls", "result_digest"}, "registered result")
    _need(result["schema_version"] == 1 and result["run_version"] == RUN_VERSION and result["output_relative"] == relative and result["status"] == "completed", "OUTER_SCOPE", "Only original completed registered effects can resume")
    _need(result["result_digest"] == digest({k: v for k, v in result.items() if k != "result_digest"}) and result["code"] == code_identity(), "OUTER_CODE_DRIFT", "Outer receipt or current execution code differs")
    _need(_hash(_effect_path(store, result["idempotency_key"])) == result["effect_file_sha256"], "EFFECT_DRIFT", "Original immutable host effect differs")
    effect = _json(_read(_effect_path(store, result["idempotency_key"])))
    _need(effect["request_digest"] == result["request_digest"] and effect["logical_run_id"] == result["logical_run_id"] and effect["output_relative"] == relative, "EFFECT_DRIFT", "Original effect belongs to another request")
    paths, contract, identity, guard = _registered(store, result["registration_identity"]["id"])
    _need(identity == result["registration_identity"] and guard == result["guard"], "REGISTRATION_DRIFT", "Current registration or guard identity differs")
    _need(_hash(directory / "attempt.json") == result["attempt_file_sha256"], "OUTER_ATTEMPT_DRIFT", "Original attempt bytes differ")
    attempt = _json(_read(directory / "attempt.json"))
    request = {"run_version": RUN_VERSION, "registration_identity": identity, "output_relative": relative, "idempotency_key": result["idempotency_key"], "expression": EXPRESSION_101, "split": "validation"}
    _need(result["request_digest"] == digest(request) and attempt["request_digest"] == result["request_digest"] and attempt["logical_run_id"] == result["logical_run_id"] and attempt["status"] == "completed", "OUTER_REQUEST_DRIFT", "Original logical effect differs")
    _need(result["inner"] == _inner(directory / "inner", result["inner"]["manifest_file_sha256"], identity, contract), "INNER_DRIFT", "Pinned actual inner effect differs")
    _need(result["scope"] == "registered_engineering_development_validation" and result["test_freshness"] == "unknown" and result["real_source_ready"] is False and result["source_authentication"] == "declared_unverified" and result["point_in_time"] == "not_certified" and result["scientific_score"] is None and type(result["final_numeric_rows_parsed"]) is int and result["final_numeric_rows_parsed"] == 0 and type(result["model_calls"]) is int and result["model_calls"] == 0, "OUTER_SCOPE", "Registered engineering scope differs")
    _need({p.name for p in directory.iterdir()} == {"attempt.json", "registered-result.json", "inner"}, "OUTER_INVENTORY", "Unexpected outer artifacts")
    final_paths, final_contract, final_identity, final_guard = _registered(store, identity["id"])
    _need(final_paths == paths and final_contract == contract and final_identity == identity and final_guard == guard and code_identity() == result["code"] and _read(directory / "registered-result.json") == raw, "READBACK_DRIFT", "Current authority or receipt changed during readback")
    return result


def run_registered_daily(store, registration_id, out_dir, *, idempotency_key, allow_whole_byte_hash=False, resume=False, expected_receipt_sha256=None):
    """One new host effect or exact readback; partial attempts never auto-recompute."""
    _need(type(resume) is bool and type(allow_whole_byte_hash) is bool, "ARGUMENTS_INVALID", "Boolean execution flags are required")
    _need(type(idempotency_key) is str and 0 < len(idempotency_key) <= 128 and bool(idempotency_key.strip()), "ARGUMENTS_INVALID", "A bounded nonempty idempotency key is required")
    if resume:
        _sha(expected_receipt_sha256)  # Refuse missing pins before IO/lock writes.
    directory, relative = _output(store, out_dir)
    with workspace_lease(store.root), _operation_lock(store, idempotency_key):
        if directory.exists():
            _need(resume, "OUTPUT_EXISTS", "Use explicit history resume with the original host pin; output is never overwritten")
            _need((directory / "registered-result.json").is_file(), "PARTIAL_UNKNOWN", "Original attempt is partial or failed; preserved without automatic recomputation")
            result = verify_registered_run(store, directory, expected_receipt_sha256=expected_receipt_sha256)
            _need(result["idempotency_key"] == idempotency_key and result["registration_identity"]["id"] == registration_id, "IDEMPOTENCY_CONFLICT", "Original run belongs to a different request")
            return result
        _need(not resume, "RESUME_MISSING", "No original effect exists to resume")
        _need(expected_receipt_sha256 is None and allow_whole_byte_hash is True, "INPUT_PERMISSION_REQUIRED", "A new operation needs explicit whole-byte integrity permission")
        effect_path = _effect_path(store, idempotency_key)
        if effect_path.exists():
            effect = _json(_read(effect_path))
            _need(effect["output_relative"] == relative and effect["registration_id"] == registration_id, "IDEMPOTENCY_CONFLICT", "Original key is already bound to another logical request")
            raise RegisteredDailyError("PARTIAL_UNKNOWN", "Original host effect is retained; missing output is not permission to recompute")
        paths, contract, identity, guard = _registered(store, registration_id)
        code = code_identity()
        request = {"run_version": RUN_VERSION, "registration_identity": identity, "output_relative": relative, "idempotency_key": idempotency_key, "expression": EXPRESSION_101, "split": "validation"}
        logical_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-alpha:registered-daily:" + digest(request)))
        effect_path.parent.mkdir(exist_ok=True)
        effect = {"schema_version": 1, "run_version": RUN_VERSION, "logical_run_id": logical_id, "request_digest": digest(request), "idempotency_key": idempotency_key, "registration_id": registration_id, "output_relative": relative}
        _exclusive_json(effect_path, effect)
        directory.mkdir(parents=True, exist_ok=False)
        attempt = {"schema_version": 1, "run_version": RUN_VERSION, "logical_run_id": logical_id, "request_digest": digest(request), "idempotency_key": idempotency_key, "registration_id": registration_id, "status": "running", "created_at_utc": datetime.now(timezone.utc).isoformat(), "phase": "before_compute", "model_calls": 0, "final_numeric_rows_parsed": 0}
        atomic_json(directory / "attempt.json", attempt)
        try:
            inner = run_daily_development(paths["contract"], paths["csv"], paths["source_receipt"], directory / "inner", allow_whole_byte_hash=True)
            _need(inner["status"] == "completed", "INNER_FAILED", "Actual D development attempt failed; all outputs retained")
            after_paths, after_contract, after_identity, after_guard = _registered(store, registration_id)
            _need(after_paths == paths and after_contract == contract and after_identity == identity and after_guard == guard and code_identity() == code, "EXECUTION_DRIFT", "Current registration/guard/code changed during execution")
            actual = _inner(directory / "inner", _hash(directory / "inner" / "manifest.json"), identity, contract)
            final_paths, final_contract, final_identity, final_guard = _registered(store, registration_id)
            _need(final_paths == paths and final_contract == contract and final_identity == identity and final_guard == guard and code_identity() == code, "PUBLICATION_DRIFT", "Current authority changed before registered result publication")
            attempt.update(status="completed", phase="completed", finished_at_utc=datetime.now(timezone.utc).isoformat())
            atomic_json(directory / "attempt.json", attempt)
            result = {"schema_version": 1, "run_version": RUN_VERSION, "logical_run_id": logical_id, "request_digest": digest(request), "idempotency_key": idempotency_key, "output_relative": relative, "status": "completed", "scope": "registered_engineering_development_validation", "registration_identity": identity, "guard": guard, "code": code, "inner": actual, "attempt_file_sha256": _hash(directory / "attempt.json"), "effect_file_sha256": _hash(effect_path), "test_freshness": "unknown", "real_source_ready": False, "source_authentication": "declared_unverified", "point_in_time": "not_certified", "scientific_score": None, "final_numeric_rows_parsed": 0, "model_calls": 0}
            result["result_digest"] = digest(result)
            atomic_json(directory / "registered-result.json", result)
            return result
        except (ValueError, OSError, KeyError, TypeError) as exc:
            attempt.update(status="failed", phase="failed", finished_at_utc=datetime.now(timezone.utc).isoformat(), diagnostics={"code": getattr(exc, "code", "REGISTERED_RUN_FAILED"), "message": "Registered development effect failed; inspect retained typed inputs and outputs"})
            atomic_json(directory / "attempt.json", attempt)
            raise
