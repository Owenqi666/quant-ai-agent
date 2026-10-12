"""Server-controlled metadata handles; original bytes remain unopened here.

Trusted offline capture producers prepare a packet in a fixed managed folder.
HTTP may select these handles, but cannot supply paths, raw bytes or authority.
The FD helper is integrity plumbing, not permission to read any original.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from ..real_daily_main_metadata import snapshot, path_literal, pin, parse_main_assessment_request
from ..storage import digest, json_text
from .db import connect
from .real_daily_workspace_authority import RealDailyWorkspaceAuthority, _now
from .real_daily_prefix_schema import CAPTURE_VERSION, HANDLE_VERSION

MAX_METADATA = 2 * 1024 * 1024
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
HANDLE_KEYS = frozenset({"schema_version", "handle_version", "id", "capture_id", "capture_relative",
    "capture_sha256", "request", "request_digest", "identity_status", "created_at_utc"})


class RealDailySourceHandleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(condition, code):
    if not condition:
        raise RealDailySourceHandleError(code)


def identity(value):
    need(type(value) is str and _IDENTITY.fullmatch(value) is not None, "invalid_source_identity")
    return value


def relative_literal(value):
    need(type(value) is str and 0 < len(value) <= 4096 and not value.startswith("/") and
         all(component and component not in (".", "..") and "\\" not in component and
             not any(ord(c) < 32 or ord(c) == 127 for c in component)
             for component in value.split("/")), "unsafe_managed_locator")
    return value


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_nlink, info.st_mode)


def _directory_chain(root, components):
    """Open from / without following any ancestor; return FD and inode chain."""
    path_literal(str(root))
    current = None
    chain = []
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        chain.append((os.fstat(current).st_dev, os.fstat(current).st_ino))
        for name in (*Path(root).parts[1:], *components):
            following = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
            info = os.fstat(current)
            chain.append((info.st_dev, info.st_ino))
        return current, tuple(chain)
    except BaseException:
        if current is not None:
            os.close(current)
        raise


def read_managed(root, relative, *, maximum, expected_sha256=None, expected_size=None,
                 on_open=None, on_hash=None):
    """Bounded FD-safe integrity read. Caller must enforce consumption authority."""
    relative_literal(relative)
    if expected_sha256 is not None:
        pin(expected_sha256)
    need(type(maximum) is int and 0 < maximum <= 32 * 1024 * 1024, "invalid_read_budget")
    if expected_size is not None:
        need(type(expected_size) is int and 0 < expected_size <= maximum, "invalid_read_budget")
    parts = relative.split("/")
    directory = stream = reopened = None
    try:
        directory, chain = _directory_chain(root, parts[:-1])
        before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= maximum,
             "unsafe_managed_file")
        if expected_size is not None:
            need(before.st_size == expected_size, "managed_file_drift")
        stream = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if on_open is not None:
            on_open()
        need(_stamp(os.fstat(stream)) == _stamp(before), "managed_file_drift")
        chunks = []
        length = 0
        while True:
            chunk = os.read(stream, min(65_536, maximum + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            need(length <= maximum, "managed_file_drift")
        raw = b"".join(chunks)
        need(length == before.st_size and _stamp(os.fstat(stream)) == _stamp(before) ==
             _stamp(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)), "managed_file_drift")
        actual_sha = hashlib.sha256(raw).hexdigest()
        if on_hash is not None:
            on_hash()
        if expected_sha256 is not None:
            need(actual_sha == expected_sha256, "managed_file_drift")
        reopened, current_chain = _directory_chain(root, parts[:-1])
        need(current_chain == chain and _stamp(os.stat(parts[-1], dir_fd=reopened, follow_symlinks=False)) ==
             _stamp(before), "managed_file_drift")
        return raw, {"sha256": actual_sha, "size_bytes": len(raw), "device": before.st_dev, "inode": before.st_ino}
    except OSError:
        raise RealDailySourceHandleError("managed_file_unavailable") from None
    finally:
        for descriptor in (stream, reopened, directory):
            if descriptor is not None:
                os.close(descriptor)


def parse_json(raw):
    need(type(raw) in (bytes, str) and len(raw) <= MAX_METADATA, "bounded_metadata_required")

    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out, "duplicate_metadata_key")
            out[key] = value
        return out

    try:
        return snapshot(json.loads(raw, object_pairs_hook=pairs))
    except RealDailySourceHandleError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise RealDailySourceHandleError("invalid_source_metadata") from None


def _row(connection, table, identity_value):
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        value = cursor.execute("SELECT * FROM " + table + " WHERE id=?", (identity(identity_value),)).fetchone()
        need(value is not None, "source_handle_missing")
        return dict(value)
    finally:
        cursor.close()


def _capture_request(request, capture_id, captured_root):
    parsed = parse_main_assessment_request(request)
    need(parsed["input_pins"]["code"] == [], "external_code_descriptor_refused")
    base = str(captured_root) + "/real_daily_sources/" + capture_id + "/"
    path_literal(str(captured_root))
    for item in parsed["input_pins"]["raw"]:
        need(item["path"] == base + "raw/" + item["asset"] + ".raw.json", "controlled_source_locator_required")
    for item in parsed["input_pins"]["metadata"]:
        need(item["path"].startswith(base) and "/" not in item["path"][len(base):] and
             item["path"] != base + "capture.json", "controlled_metadata_locator_required")
    return parsed


def validate_handle(row):
    value = parse_json(row["payload_json"])
    need(type(value) is dict and set(value) == HANDLE_KEYS and type(value["schema_version"]) is int and
         value["schema_version"] == 1 and value["handle_version"] == HANDLE_VERSION and
         value["identity_status"] == "declared_not_raw_verified", "source_handle_drift")
    identity(value["id"])
    capture_id = identity(value["capture_id"])
    pin(value["capture_sha256"])
    need(value["capture_relative"] == "real_daily_sources/" + capture_id + "/capture.json", "source_handle_drift")
    raw = value["request"]["input_pins"]["raw"]
    need(type(raw) is list and bool(raw), "source_handle_drift")
    suffix = "/real_daily_sources/" + capture_id + "/raw/" + raw[0]["asset"] + ".raw.json"
    need(raw[0]["path"].endswith(suffix), "source_handle_drift")
    captured_root = raw[0]["path"][:-len(suffix)]
    parsed = _capture_request(value["request"], capture_id, captured_root)
    need(json_text(parsed) == json_text(value["request"]) and value["request_digest"] == digest(parsed),
         "source_handle_drift")
    request_pin = digest({"operation": "capture_source", "capture_id": capture_id,
                          "capture_sha256": value["capture_sha256"]})
    need(value["id"] == row["id"] and capture_id == row["capture_id"] and
         value["created_at_utc"] == row["created_at"] and row["request_digest"] == request_pin and
         row["handle_digest"] == digest(value), "source_handle_drift")
    from .real_daily_workspace_authority import _utc, _text
    _utc(value["created_at_utc"])
    _text(row["idempotency_key"])
    return value


def resolve_handle(connection, root, handle_id, expected_digest):
    """Saved metadata only; root is not compared as current grant here."""
    pin(expected_digest)
    row = _row(connection, "rdw_source_handles", handle_id)
    value = validate_handle(row)
    need(row["handle_digest"] == expected_digest, "source_handle_pin_mismatch")
    return {**value, "handle_digest": row["handle_digest"]}


def assert_current_handle_metadata(root, value):
    """Capture/evidence integrity only; no raw path is opened or stated."""
    raw, _ = read_managed(root, value["capture_relative"], maximum=MAX_METADATA,
                          expected_sha256=value["capture_sha256"])
    capture = parse_json(raw)
    need(type(capture) is dict and set(capture) == {"schema_version", "capture_version", "capture_id", "request"} and
         type(capture["schema_version"]) is int and capture["schema_version"] == 1 and
         capture["capture_version"] == CAPTURE_VERSION and capture["capture_id"] == value["capture_id"],
         "source_capture_drift")
    request = _capture_request(capture["request"], value["capture_id"], root)
    need(json_text(request) == json_text(value["request"]), "source_capture_drift")
    for descriptor in request["input_pins"]["metadata"]:
        relative = descriptor["path"][len(str(root)) + 1:]
        read_managed(root, relative, maximum=MAX_METADATA, expected_sha256=descriptor["sha256"])
    return request


def public_handle(value):
    request = value["request"]
    facts = request["source_facts"]
    return {"schema_version": 1, "id": value["id"], "handle_id": value["id"],
            "handle_digest": value["handle_digest"], "origin": request["input_pins"]["origin"],
            "identity_status": "declared_not_raw_verified", "selection": deepcopy(request["selection"]),
            "method_digest": request["method_digest"],
            "source_fact_counts": {status: sum(fact["status"] == status for fact in facts.values())
                                   for status in ("supported", "unknown", "rejected")},
            "created_at_utc": value["created_at_utc"], "source_authenticated": False,
            "real_source_ready": False, "point_in_time": "not_certified", "final_numeric_access_allowed": False}


class RealDailySourceHandles:
    def __init__(self, existing_store):
        self.store = existing_store
        self.root = Path(existing_store.root)
        self.authority = RealDailyWorkspaceAuthority(existing_store)

    def capture(self, capture_id, *, idempotency_key):
        """Trusted offline Python import, never exposed as an HTTP mutation."""
        identity(capture_id)
        from .real_daily_workspace_authority import _text, _rows
        _text(idempotency_key)
        from .real_daily_prefix_registration import assert_prefix_history
        with self.authority._transaction() as (connection, _):
            assert_prefix_history(connection)
            existing = _rows(connection, "SELECT * FROM rdw_source_handles WHERE idempotency_key=? OR capture_id=?",
                             (idempotency_key, capture_id))
            if existing:
                need(len(existing) == 1 and existing[0]["idempotency_key"] == idempotency_key and
                     existing[0]["capture_id"] == capture_id, "idempotency_conflict")
                value = validate_handle(existing[0])
                return public_handle({**value, "handle_digest": existing[0]["handle_digest"]})
            relative = "real_daily_sources/" + capture_id + "/capture.json"
            raw, info = read_managed(self.root, relative, maximum=MAX_METADATA)
            capture = parse_json(raw)
            need(type(capture) is dict and set(capture) == {"schema_version", "capture_version", "capture_id", "request"} and
                 type(capture["schema_version"]) is int and capture["schema_version"] == 1 and
                 capture["capture_version"] == CAPTURE_VERSION and capture["capture_id"] == capture_id,
                 "invalid_source_capture")
            request = _capture_request(capture["request"], capture_id, self.root)
            value = {"schema_version": 1, "handle_version": HANDLE_VERSION, "id": uuid.uuid4().hex,
                     "capture_id": capture_id, "capture_relative": relative, "capture_sha256": info["sha256"],
                     "request": request, "request_digest": digest(request),
                     "identity_status": "declared_not_raw_verified", "created_at_utc": _now()}
            # Only metadata/evidence is verified at this phase.
            assert_current_handle_metadata(self.root, value)
            handle_pin = digest(value)
            command_pin = digest({"operation": "capture_source", "capture_id": capture_id,
                                  "capture_sha256": info["sha256"]})
            connection.execute("INSERT INTO rdw_source_handles VALUES (?,?,?,?,?,?,?)",
                (value["id"], capture_id, idempotency_key, command_pin, handle_pin, json_text(value), value["created_at_utc"]))
            assert_prefix_history(connection)
            return public_handle({**value, "handle_digest": handle_pin})

    def get(self, handle_id):
        from .real_daily_prefix_registration import assert_prefix_history
        with self.authority._transaction() as (connection, _):
            assert_prefix_history(connection)
            row = _row(connection, "rdw_source_handles", handle_id)
            return public_handle({**validate_handle(row), "handle_digest": row["handle_digest"]})

    def list(self):
        from .real_daily_workspace_authority import _rows
        from .real_daily_prefix_registration import assert_prefix_history
        with self.authority._transaction() as (connection, _):
            assert_prefix_history(connection)
            rows = _rows(connection, "SELECT * FROM rdw_source_handles ORDER BY created_at,id")
            return [public_handle({**validate_handle(row), "handle_digest": row["handle_digest"]}) for row in rows]
