"""Main-store, single-owner normalization and immutable development prefixes.

Only an exact active host grant plus a committed reservation/owner permits raw
IO. No second research scheduler is created. Historical reads use frozen DB
metadata independently of raw originals, code changes and present permission.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import csv
import hashlib
import io
import math
import os
from pathlib import Path
import sqlite3
import uuid

from ..real_daily_main_metadata import snapshot, pin
from ..real_daily_normalizer import decode_development_bytes, FIELDS
from ..real_daily_web_contract import catalog_from_selection, validate_execution
from ..storage import digest, json_text
from .db import connect
from .maintenance import workspace_lease
from .real_daily_workspace_authority import RealDailyWorkspaceAuthority, _history, _rows, _record, _now, _utc, _text
from .real_daily_prefix_schema import REQUIRED_COLUMNS, NORMALIZATION_VERSION, REGISTRATION_VERSION
from .real_daily_source_handles import (identity, validate_handle, resolve_handle, assert_current_handle_metadata,
    read_managed, relative_literal, parse_json, _directory_chain)

MAX_HISTORY = 1000
MAX_HISTORY_METADATA = 16 * 1024 * 1024
MAX_REGISTRATION_BYTES = 128 * 1024
MAX_OUTCOME_BYTES = 64 * 1024
RESERVED_COMPLETION_BYTES = 256 * 1024
RESERVED_RESULT_BYTES = 16 * 1024
MAX_PREFIX_BYTES = 32 * 1024 * 1024
MAX_GRID_CELLS = 100_000
INTENT_KEYS = frozenset({"schema_version", "normalization_version", "id", "reservation_id", "reservation_digest",
    "handle_id", "handle_digest", "approval_id", "approval_digest", "assessment_id", "assessment_digest",
    "authority_id", "epoch_id", "owner_digest", "request_digest", "output_relative", "created_at_utc"})
OUTCOME_KEYS = frozenset({"schema_version", "normalization_version", "intent_id", "intent_digest", "owner_digest",
    "status", "registration_id", "registration_digest", "error_code", "counters", "finished_at_utc", "automatic_retry_allowed"})
REGISTRATION_KEYS = frozenset({"schema_version", "registration_version", "id", "intent_id", "intent_digest",
    "dataset_id", "dataset_sha256", "metadata_digest", "handle_id", "handle_digest", "approval_id", "approval_digest",
    "assessment_id", "assessment_digest", "reservation_id", "reservation_digest", "authority_id", "epoch_id",
    "prefix_relative", "prefix_sha256", "receipt_relative", "receipt_sha256", "metadata_relative", "metadata_sha256",
    "selection", "selection_digest", "method", "method_digest", "catalog", "catalog_digest", "origin",
    "development_content_digest", "development_index_digest", "development_row_count", "created_at_utc",
    "source_authenticated", "real_source_ready", "point_in_time", "final_numeric_access_allowed"})
COUNTER_KEYS = frozenset({"raw_originals_opened", "raw_originals_whole_byte_hashed", "development_price_values_decoded",
    "development_decode_completed_assets", "decode_count_certainty", "final_value_tokens_decoded",
    "final_numeric_rows_parsed", "volume_value_tokens_decoded", "full_dataset_validated"})


class RealDailyPrefixError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(condition, code):
    if not condition:
        raise RealDailyPrefixError(code)


def _closed(value, keys):
    need(type(value) is dict and set(value) == keys, "prefix_history_invalid")


def _payload(row, field, keys, maximum=2 * 1024 * 1024):
    value = parse_json(row["payload_json"])
    snapshot(value, maximum=maximum)
    _closed(value, keys)
    need(row[field] == digest(value), "prefix_record_drift")
    return value


def _counter_check(value):
    _closed(value, COUNTER_KEYS)
    for key in COUNTER_KEYS - {"development_price_values_decoded", "decode_count_certainty", "full_dataset_validated"}:
        need(type(value[key]) is int and value[key] >= 0, "prefix_counter_drift")
    for key in ("final_value_tokens_decoded", "final_numeric_rows_parsed", "volume_value_tokens_decoded"):
        need(value[key] == 0, "prefix_scope_drift")
    need(value["full_dataset_validated"] is False and value["decode_count_certainty"] in ("exact", "partial_unknown"),
         "prefix_scope_drift")
    count = value["development_price_values_decoded"]
    need((count is None and value["decode_count_certainty"] == "partial_unknown") or
         (type(count) is int and 0 <= count <= 4 * MAX_GRID_CELLS and value["decode_count_certainty"] == "exact"),
         "prefix_counter_drift")
    need(value["development_decode_completed_assets"] <= value["raw_originals_whole_byte_hashed"] <=
         value["raw_originals_opened"] <= 100, "prefix_counter_drift")


def _table_state(connection):
    from .db import verify_audit_triggers
    from .real_daily_workspace_authority import _setting
    names = {row["name"] for row in _rows(connection, "SELECT name FROM sqlite_master WHERE type='table'")}
    present = names & set(REQUIRED_COLUMNS)
    version = _setting(connection, "schema_version") if "settings" in names else None
    if not present and version in (None, "27", "28"):
        return False
    need(version == "29" and present == set(REQUIRED_COLUMNS), "prefix_schema_invalid")
    for table, expected in REQUIRED_COLUMNS.items():
        need({row["name"] for row in _rows(connection, "PRAGMA table_info(" + table + ")")} == expected,
             "prefix_schema_invalid")
    try:
        verify_audit_triggers(connection)
    except RuntimeError:
        raise RealDailyPrefixError("prefix_trigger_drift") from None
    return True


def _assert_prefix_history(connection):
    if not _table_state(connection):
        return {"handles": {}, "intents": {}, "outcomes": {}, "registrations": {}, "total_bytes": 0}
    tables = {}
    total = 0
    for table in REQUIRED_COLUMNS:
        population = _rows(connection, "SELECT 1 AS p FROM " + table + " LIMIT ?", (MAX_HISTORY + 1,))
        need(len(population) <= MAX_HISTORY, "prefix_history_bound_exceeded")
        total += _rows(connection, "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) AS n FROM " + table)[0]["n"]
        need(total <= MAX_HISTORY_METADATA, "prefix_history_bound_exceeded")
        tables[table] = _rows(connection, "SELECT * FROM " + table + " LIMIT ?", (MAX_HISTORY + 1,))
    _, authority = _history(connection)
    need(authority is not None or not any(tables.values()), "authority_not_initialized")
    handles = {}
    for row in tables["rdw_source_handles"]:
        handles[row["id"]] = {**validate_handle(row), "handle_digest": row["handle_digest"]}
    intents = {}
    for row in tables["rdw_normalization_intents"]:
        value = _payload(row, "intent_digest", INTENT_KEYS)
        need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
             value["normalization_version"] == NORMALIZATION_VERSION and value["id"] == row["id"] and
             value["created_at_utc"] == row["created_at"], "prefix_intent_drift")
        _utc(value["created_at_utc"])
        _text(row["idempotency_key"])
        for key in ("reservation_id", "handle_id", "approval_id"):
            need(value[key] == row[key], "prefix_intent_drift")
        for key in ("owner_digest", "request_digest"):
            need(value[key] == row[key], "prefix_intent_drift")
            pin(value[key])
        need(value["handle_id"] in handles and handles[value["handle_id"]]["handle_digest"] == value["handle_digest"],
             "prefix_handle_binding_drift")
        handle = handles[value["handle_id"]]
        approval = authority["approvals"].get(value["approval_id"])
        need(approval is not None, "prefix_authority_binding_drift")
        approval_row, approval_value = approval
        reservation_rows = _rows(connection, "SELECT * FROM rdw_reservations WHERE id=?", (value["reservation_id"],))
        need(len(reservation_rows) == 1, "prefix_authority_binding_drift")
        reservation = _record(reservation_rows[0], "reservation_digest")
        need(approval_row["approval_digest"] == value["approval_digest"] and
             reservation_rows[0]["reservation_digest"] == value["reservation_digest"] and
             all(value[key] == reservation[key] for key in ("approval_id", "approval_digest", "assessment_id",
                 "assessment_digest", "authority_id", "epoch_id")), "prefix_authority_binding_drift")
        assessment = authority["assessments"][value["assessment_id"]]
        request = {"schema_version": 1, **{key: assessment[key] for key in
            ("selection", "ledger", "cash_annotations", "source_facts", "input_pins", "method_digest")}}
        assessment_rows = _rows(connection, "SELECT request_digest FROM rdw_assessments WHERE id=?", (value["assessment_id"],))
        source_request_pin = digest({"operation": "submit_source_handle", "handle_id": handle["id"],
                                     "handle_digest": handle["handle_digest"], "request": handle["request"]})
        need(json_text(request) == json_text(handle["request"]) and assessment_rows[0]["request_digest"] == source_request_pin,
             "prefix_assessment_handle_drift")
        need(value["output_relative"] == "real_daily_prefixes/" + value["id"] + "/", "prefix_locator_drift")
        command = {"operation": "normalize", **{key: value[key] for key in
            ("handle_id", "handle_digest", "approval_id", "approval_digest", "reservation_id", "reservation_digest")}}
        need(value["request_digest"] == digest(command), "prefix_intent_drift")
        intents[value["id"]] = {**value, "intent_digest": row["intent_digest"], "idempotency_key": row["idempotency_key"]}
    registrations = {}
    for row in tables["rdw_prefix_registrations"]:
        value = _payload(row, "registration_digest", REGISTRATION_KEYS, MAX_REGISTRATION_BYTES)
        need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
             value["registration_version"] == REGISTRATION_VERSION and value["id"] == row["id"] and
             value["intent_id"] == row["intent_id"] and value["dataset_id"] == row["dataset_id"] and
             value["created_at_utc"] == row["created_at"] and value["intent_id"] in intents,
             "prefix_registration_drift")
        intent = intents[value["intent_id"]]
        for key in ("intent_digest", "handle_id", "handle_digest", "approval_id", "approval_digest", "assessment_id",
                    "assessment_digest", "reservation_id", "reservation_digest", "authority_id", "epoch_id"):
            need(value[key] == intent[key], "prefix_registration_drift")
        assessment = authority["assessments"][value["assessment_id"]]
        need(json_text(value["selection"]) == json_text(assessment["selection"]) and
             json_text(value["method"]) == json_text(assessment["method"]) and
             value["selection_digest"] == digest(value["selection"]) and value["method_digest"] == assessment["method_digest"] and
             value["origin"] == assessment["origin"], "prefix_registration_drift")
        catalog = catalog_from_selection(value["selection"], origin=value["origin"])
        need(json_text(value["catalog"]) == json_text(catalog) and value["catalog_digest"] == catalog["catalog_digest"],
             "prefix_registration_drift")
        for key, name in (("prefix_relative", "prefix.csv"), ("receipt_relative", "receipt.json"), ("metadata_relative", "metadata.json")):
            need(value[key] == intent["output_relative"] + name, "prefix_locator_drift")
        for key in ("dataset_sha256", "metadata_digest", "prefix_sha256", "receipt_sha256", "metadata_sha256",
                    "development_content_digest", "development_index_digest"):
            pin(value[key])
        selection = value["selection"]
        grid = [[day, asset] for day in selection["development_sessions"] for asset in selection["assets"]]
        need(type(value["development_row_count"]) is int and value["development_row_count"] == len(grid) <= MAX_GRID_CELLS and
             value["development_index_digest"] == digest(grid), "prefix_grid_drift")
        need(value["source_authenticated"] is False and value["real_source_ready"] is False and
             value["point_in_time"] == "not_certified" and value["final_numeric_access_allowed"] is False, "prefix_scope_drift")
        datasets = _rows(connection, "SELECT * FROM datasets WHERE id=?", (value["dataset_id"],))
        need(len(datasets) == 1, "prefix_dataset_missing")
        metadata = parse_json(datasets[0]["metadata"])
        need(digest(metadata) == value["metadata_digest"] and
             hashlib.sha256(json_text(metadata).encode()).hexdigest() == value["metadata_sha256"] and
             datasets[0]["sha256"] == value["dataset_sha256"] == digest({"data": value["prefix_sha256"], "metadata": metadata}),
             "prefix_dataset_drift")
        need(metadata["data_kind"] == "real_daily_ohlc_development" and metadata["columns"] == ["date", "asset", *FIELDS] and
             metadata["calendar_dates"] == selection["development_sessions"] and metadata["selection_digest"] == value["selection_digest"],
             "prefix_dataset_drift")
        _utc(value["created_at_utc"])
        registrations[value["id"]] = {**value, "registration_digest": row["registration_digest"]}
    outcomes = {}
    for row in tables["rdw_normalization_outcomes"]:
        value = _payload(row, "outcome_digest", OUTCOME_KEYS, MAX_OUTCOME_BYTES)
        need(type(value["schema_version"]) is int and value["schema_version"] == 1 and value["normalization_version"] == NORMALIZATION_VERSION and
             value["intent_id"] == row["intent_id"] and value["intent_id"] in intents and value["status"] == row["status"] and
             value["finished_at_utc"] == row["created_at"] and value["automatic_retry_allowed"] is False, "prefix_outcome_drift")
        intent = intents[value["intent_id"]]
        need(value["intent_digest"] == intent["intent_digest"] and value["owner_digest"] == intent["owner_digest"], "prefix_owner_drift")
        _utc(value["finished_at_utc"])
        _counter_check(value["counters"])
        matched = [item for item in registrations.values() if item["intent_id"] == value["intent_id"]]
        if value["status"] == "completed":
            need(len(matched) == 1 and value["registration_id"] == matched[0]["id"] and
                 value["registration_digest"] == matched[0]["registration_digest"] and value["error_code"] is None,
                 "prefix_outcome_drift")
            counters = value["counters"]
            need(counters["raw_originals_opened"] == counters["raw_originals_whole_byte_hashed"] ==
                 counters["development_decode_completed_assets"] == len(matched[0]["selection"]["assets"]) and
                 counters["development_price_values_decoded"] == 4 * matched[0]["development_row_count"], "prefix_counter_drift")
        else:
            need(not matched and value["registration_id"] is None and value["registration_digest"] is None and
                 type(value["error_code"]) is str and 0 < len(value["error_code"]) <= 128 and
                 all(c.isalnum() or c == "_" for c in value["error_code"]), "prefix_outcome_drift")
        outcomes[value["intent_id"]] = {**value, "outcome_digest": row["outcome_digest"]}
    need(all(item["intent_id"] in outcomes and outcomes[item["intent_id"]]["status"] == "completed"
             for item in registrations.values()), "prefix_registration_uncommitted")
    pending = len(intents) - len(outcomes)
    # Original runs/attempts are the only execution authority. Every active
    # typed run holds one future result-binding row plus bounded producer bytes;
    # this is independent of a normalization's terminal-record reservation.
    pending_results = len(_rows(connection, "SELECT r.id FROM runs r JOIN rdw_revision_bindings b "
        "ON b.revision_id=r.revision_id WHERE r.status IN ('queued','running','cancelling') LIMIT ?", (MAX_HISTORY + 1,)))
    need(len(registrations) + pending <= MAX_HISTORY and len(outcomes) + pending <= MAX_HISTORY and
         len(tables['rdw_result_bindings']) + pending_results <= MAX_HISTORY and
         total + pending * RESERVED_COMPLETION_BYTES + pending_results * RESERVED_RESULT_BYTES <= MAX_HISTORY_METADATA,
         "prefix_history_bound_exceeded")
    # These immutable tables are filled at M05/M09. Their dedicated closed
    # parsers must be available before a nonempty population is admitted.
    for table, module_name, function in (
        ("rdw_revision_bindings", "real_daily_main_research", "assert_revision_binding_history"),
        ("rdw_result_bindings", "real_daily_main_research", "assert_result_binding_history")):
        try:
            import importlib
            parser = getattr(importlib.import_module("paper_alpha.server." + module_name), function)
            parser(connection, tables[table], registrations)
        except (ImportError, AttributeError):
            if tables[table]:
                raise RealDailyPrefixError("prefix_future_binding_unsupported") from None
    return {"handles": handles, "intents": intents, "outcomes": outcomes, "registrations": registrations,
            "total_bytes": total, "pending_count": pending, "pending_result_count": pending_results}


def assert_prefix_history(connection):
    """Frozen metadata only; never reads prefixes, originals or current code."""
    try:
        return _assert_prefix_history(connection)
    except RealDailyPrefixError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, OverflowError):
        raise RealDailyPrefixError("prefix_history_invalid") from None


def main_execution_binding(registration):
    value = {"schema_version": 1, "kind": "real_daily_ohlc_development_v1", "data_kind": "real_daily_ohlc_development",
        "scope": "development_only", "origin": registration["origin"], "dataset_id": registration["dataset_id"],
        "registration_id": registration["id"], "assessment_id": registration["assessment_id"],
        **{key: registration[key] for key in ("registration_digest", "assessment_digest", "method_digest",
            "selection_digest", "catalog_digest", "prefix_sha256")}, "config_digest": digest(registration["selection"]["config"])}
    value["source_digest"] = digest(value)
    return validate_execution(value)


def _new_output(root, intent_id):
    directory = output = None
    try:
        directory, _ = _directory_chain(root, ())
        try:
            os.mkdir("real_daily_prefixes", mode=0o700, dir_fd=directory)
        except FileExistsError:
            pass
        parent = os.open("real_daily_prefixes", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            os.mkdir(intent_id, mode=0o700, dir_fd=parent)
            output = os.open(intent_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
        return output
    except OSError:
        raise RealDailyPrefixError("prefix_output_unavailable") from None
    finally:
        if directory is not None:
            os.close(directory)


def _immutable_file(directory, name, raw):
    need(name in ("prefix.csv", "receipt.json", "metadata.json", "registration.json", "failure.json"), "invalid_prefix_file")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)


def _initial_counters():
    return {"raw_originals_opened": 0, "raw_originals_whole_byte_hashed": 0,
        "development_price_values_decoded": 0, "development_decode_completed_assets": 0,
        "decode_count_certainty": "exact", "final_value_tokens_decoded": 0, "final_numeric_rows_parsed": 0,
        "volume_value_tokens_decoded": 0, "full_dataset_validated": False}


class RealDailyPrefixes:
    def __init__(self, existing_store):
        self.store = existing_store
        self.root = Path(existing_store.root)
        self.authority = RealDailyWorkspaceAuthority(existing_store)

    def _current(self, connection, authority_state, command):
        handle = resolve_handle(connection, self.root, command["handle_id"], command["handle_digest"])
        approval_row = self.authority._saved(connection, "rdw_approvals", command["approval_id"])
        need(approval_row["approval_digest"] == command["approval_digest"], "approval_pin_mismatch")
        current = self.authority._current(connection, authority_state, approval_row)
        need(current["local_development_execution_allowed"], "approval_" + current["current_permission"])
        reservation_row = self.authority._saved(connection, "rdw_reservations", command["reservation_id"])
        need(reservation_row["reservation_digest"] == command["reservation_digest"], "reservation_pin_mismatch")
        reservation = _record(reservation_row, "reservation_digest")
        need(reservation["approval_id"] == command["approval_id"] and reservation["approval_digest"] == command["approval_digest"],
             "reservation_approval_mismatch")
        assessment = authority_state["assessments"][reservation["assessment_id"]]
        saved = self.authority._saved(connection, "rdw_assessments", reservation["assessment_id"])
        request = {"schema_version": 1, **{key: assessment[key] for key in
            ("selection", "ledger", "cash_annotations", "source_facts", "input_pins", "method_digest")}}
        expected = digest({"operation": "submit_source_handle", "handle_id": handle["id"],
                           "handle_digest": handle["handle_digest"], "request": handle["request"]})
        need(saved["request_digest"] == expected and json_text(request) == json_text(handle["request"]), "assessment_handle_mismatch")
        assert_current_handle_metadata(self.root, handle)
        return handle, reservation, assessment

    @staticmethod
    def _summary(intent, outcome=None, registration=None):
        value = {"schema_version": 1, "id": intent["id"], "intent_id": intent["id"], "intent_digest": intent["intent_digest"],
            "reservation_id": intent["reservation_id"], "handle_id": intent["handle_id"],
            "status": outcome["status"] if outcome else "started_unknown", "automatic_retry_allowed": False,
            "error_code": outcome["error_code"] if outcome else "normalization_started_or_interrupted",
            "registration_id": outcome["registration_id"] if outcome else None,
            "registration_digest": outcome["registration_digest"] if outcome else None,
            "dataset_id": registration["dataset_id"] if registration else None,
            "counters": deepcopy(outcome["counters"]) if outcome else None,
            "source_authenticated": False, "real_source_ready": False, "point_in_time": "not_certified",
            "final_numeric_access_allowed": False}
        return value

    def get_normalization(self, intent_id):
        identity(intent_id)
        with self.authority._transaction() as (connection, _):
            state = assert_prefix_history(connection)
            need(intent_id in state["intents"], "normalization_missing")
            intent = state["intents"][intent_id]
            outcome = state["outcomes"].get(intent_id)
            registration = state["registrations"].get(outcome["registration_id"]) if outcome else None
            return self._summary(intent, outcome, registration)

    def get_normalization_by_key(self, idempotency_key):
        _text(idempotency_key)
        with self.authority._transaction() as (connection, _):
            state = assert_prefix_history(connection)
            matches = [item for item in state['intents'].values() if item['idempotency_key'] == idempotency_key]
            need(len(matches) == 1, 'normalization_missing')
            intent = matches[0]
            outcome = state['outcomes'].get(intent['id'])
            registration = state['registrations'].get(outcome['registration_id']) if outcome else None
            return self._summary(intent, outcome, registration)

    def get_registration(self, registration_id):
        identity(registration_id)
        with self.authority._transaction() as (connection, _):
            state = assert_prefix_history(connection)
            need(registration_id in state["registrations"], "registration_missing")
            reg = state["registrations"][registration_id]
            return {"schema_version": 1, "id": reg["id"], "registration_id": reg["id"],
                    "registration_digest": reg["registration_digest"], "dataset_id": reg["dataset_id"],
                    "data_kind": "real_daily_ohlc_development", "fields": list(FIELDS),
                    "assessment_id": reg["assessment_id"], "selection": deepcopy(reg["selection"]),
                    "method_digest": reg["method_digest"], "catalog_digest": reg["catalog_digest"],
                    "prefix_sha256": reg["prefix_sha256"], "origin": reg["origin"],
                    "source_authenticated": False, "real_source_ready": False, "point_in_time": "not_certified",
                    "final_numeric_access_allowed": False}

    def normalize(self, handle_id, *, expected_handle_digest, approval_id, expected_approval_digest,
                  reservation_id, expected_reservation_digest, idempotency_key):
        command = {"operation": "normalize", "handle_id": identity(handle_id), "handle_digest": pin(expected_handle_digest),
            "approval_id": identity(approval_id), "approval_digest": pin(expected_approval_digest),
            "reservation_id": identity(reservation_id), "reservation_digest": pin(expected_reservation_digest)}
        _text(idempotency_key)
        request_pin = digest(command)
        owner = uuid.uuid4().hex
        # Keep the shared lease across raw IO so exclusive recovery cannot mark
        # a still-running owner's intent interrupted.
        with workspace_lease(self.root):
            with self.authority._transaction() as (connection, authority_state):
                state = assert_prefix_history(connection)
                found = [item for item in state["intents"].values() if item["idempotency_key"] == idempotency_key]
                if found:
                    need(found[0]["request_digest"] == request_pin, "idempotency_conflict")
                    outcome = state["outcomes"].get(found[0]["id"])
                    registration = state["registrations"].get(outcome["registration_id"]) if outcome else None
                    return self._summary(found[0], outcome, registration)
                need(not any(item["reservation_id"] == reservation_id for item in state["intents"].values()), "normalization_not_retriable")
                handle, reservation, assessment = self._current(connection, authority_state, command)
                value = {"schema_version": 1, "normalization_version": NORMALIZATION_VERSION, "id": uuid.uuid4().hex,
                    **{key: command[key] for key in command if key != "operation"},
                    **{key: reservation[key] for key in ("assessment_id", "assessment_digest", "authority_id", "epoch_id")},
                    "owner_digest": digest(owner), "request_digest": request_pin, "output_relative": "", "created_at_utc": _now()}
                value["output_relative"] = "real_daily_prefixes/" + value["id"] + "/"
                intent_pin = digest(value)
                connection.execute("INSERT INTO rdw_normalization_intents VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (value["id"], reservation_id, handle_id, approval_id, value["owner_digest"], idempotency_key,
                     request_pin, intent_pin, json_text(value), value["created_at_utc"]))
                assert_prefix_history(connection)  # prospective completion capacity before any raw IO
                intent = {**value, "intent_digest": intent_pin, "idempotency_key": idempotency_key}
            return self._execute(intent, owner, command)

    def _assert_owner_current(self, intent, owner, command):
        with self.authority._transaction() as (connection, authority_state):
            state = assert_prefix_history(connection)
            need(intent["id"] in state["intents"] and intent["id"] not in state["outcomes"] and
                 state["intents"][intent["id"]]["intent_digest"] == intent["intent_digest"] and
                 state["intents"][intent["id"]]["owner_digest"] == digest(owner), "normalization_owner_lost")
            return self._current(connection, authority_state, command)

    def _record_outcome(self, connection, intent, status, counters, *, registration=None, error_code=None):
        value = {"schema_version": 1, "normalization_version": NORMALIZATION_VERSION, "intent_id": intent["id"],
            "intent_digest": intent["intent_digest"], "owner_digest": intent["owner_digest"], "status": status,
            "registration_id": registration["id"] if registration else None,
            "registration_digest": registration["registration_digest"] if registration else None,
            "error_code": error_code, "counters": deepcopy(counters), "finished_at_utc": _now(), "automatic_retry_allowed": False}
        snapshot(value, maximum=MAX_OUTCOME_BYTES)
        connection.execute("INSERT INTO rdw_normalization_outcomes VALUES (?,?,?,?,?)",
            (intent["id"], status, digest(value), json_text(value), value["finished_at_utc"]))
        return {**value, "outcome_digest": digest(value)}

    def _execute(self, intent, owner, command):
        counters = _initial_counters()
        directory = None
        try:
            _, _, assessment = self._assert_owner_current(intent, owner, command)
            selection = assessment["selection"]
            need(len(selection["development_sessions"]) * len(selection["assets"]) <= MAX_GRID_CELLS, "prefix_grid_limit")
            rows_by_asset = {}
            raw_identities = []
            inode_identities = set()
            for descriptor in assessment["input_pins"]["raw"]:
                self._assert_owner_current(intent, owner, command)
                relative = descriptor["path"][len(str(self.root)) + 1:]

                def opened():
                    counters["raw_originals_opened"] += 1

                def hashed():
                    counters["raw_originals_whole_byte_hashed"] += 1

                raw, info = read_managed(self.root, relative, maximum=2 * 1024 * 1024,
                    expected_sha256=descriptor["sha256"], expected_size=descriptor["size_bytes"], on_open=opened, on_hash=hashed)
                source_inode = (info["device"], info["inode"])
                need(source_inode not in inode_identities, "source_inode_alias")
                inode_identities.add(source_inode)
                self._assert_owner_current(intent, owner, command)
                previous = counters["development_price_values_decoded"]
                counters["development_price_values_decoded"] = None
                counters["decode_count_certainty"] = "partial_unknown"
                rows, scope = decode_development_bytes(raw, asset=descriptor["asset"], selection=selection)
                counters["development_price_values_decoded"] = previous + scope["development_price_values_decoded"]
                counters["decode_count_certainty"] = "exact"
                counters["development_decode_completed_assets"] += 1
                rows_by_asset[descriptor["asset"]] = rows
                raw_identities.append({"asset": descriptor["asset"], "sha256": info["sha256"], "size_bytes": info["size_bytes"]})
            self._assert_owner_current(intent, owner, command)
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["date", "asset", *FIELDS])
            observations = []
            grid = []
            for day in selection["development_sessions"]:
                for asset in selection["assets"]:
                    values = [rows_by_asset[asset][day][field] for field in FIELDS]
                    writer.writerow([day, asset, *[format(value, ".17g") for value in values]])
                    observations.append({"date": day, "asset": asset, "values_hex": [value.hex() for value in values]})
                    grid.append([day, asset])
            prefix = stream.getvalue().encode()
            need(0 < len(prefix) <= MAX_PREFIX_BYTES, "prefix_size_limit")
            receipt = {"schema_version": 1, "normalization_version": NORMALIZATION_VERSION,
                "intent_id": intent["id"], "intent_digest": intent["intent_digest"], "handle_digest": intent["handle_digest"],
                "assessment_digest": intent["assessment_digest"], "approval_digest": intent["approval_digest"],
                "reservation_digest": intent["reservation_digest"], "selection_digest": digest(selection),
                "method_digest": assessment["method_digest"], "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
                "development_content_digest": digest(observations), "development_index_digest": digest(grid),
                "development_row_count": len(observations), "raw_identities": raw_identities, "counters": deepcopy(counters),
                "code_identity": deepcopy(assessment["code_identity"]), "source_authenticated": False,
                "real_source_ready": False, "point_in_time": "not_certified", "final_numeric_access_allowed": False}
            receipt_bytes = json_text(receipt).encode()
            registration_id = uuid.uuid4().hex
            dataset_id = uuid.uuid4().hex
            metadata = {"schema_version": 1, "version": REGISTRATION_VERSION, "data_kind": "real_daily_ohlc_development",
                "columns": ["date", "asset", *FIELDS], "calendar_dates": deepcopy(selection["development_sessions"]),
                "assets": deepcopy(selection["assets"]), "declared_calendar_dates": deepcopy(selection["sessions"]),
                "selection_digest": digest(selection), "method_digest": assessment["method_digest"],
                "registration_id": registration_id, "dataset_id": dataset_id, "intent_id": intent["id"],
                "prefix_sha256": receipt["prefix_sha256"], "development_content_digest": receipt["development_content_digest"],
                "development_index_digest": receipt["development_index_digest"], "evaluation": deepcopy(selection["config"]),
                "price_adjustment": "raw_unadjusted", "price_unit": "currency_major_unit", "price_scale": 1,
                "missing_policy": "reject_no_fill_no_calendar_compression", "origin": assessment["origin"],
                "source_authenticated": False, "real_source_ready": False, "point_in_time": "not_certified",
                "final_numeric_access_allowed": False}
            metadata_bytes = json_text(metadata).encode()
            catalog = catalog_from_selection(selection, origin=assessment["origin"])
            reg = {"schema_version": 1, "registration_version": REGISTRATION_VERSION, "id": registration_id,
                "intent_id": intent["id"], "intent_digest": intent["intent_digest"], "dataset_id": dataset_id,
                "dataset_sha256": digest({"data": receipt["prefix_sha256"], "metadata": metadata}),
                "metadata_digest": digest(metadata), **{key: intent[key] for key in
                    ("handle_id", "handle_digest", "approval_id", "approval_digest", "assessment_id", "assessment_digest",
                     "reservation_id", "reservation_digest", "authority_id", "epoch_id")},
                "prefix_relative": intent["output_relative"] + "prefix.csv", "prefix_sha256": receipt["prefix_sha256"],
                "receipt_relative": intent["output_relative"] + "receipt.json", "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "metadata_relative": intent["output_relative"] + "metadata.json", "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                "selection": deepcopy(selection), "selection_digest": digest(selection), "method": deepcopy(assessment["method"]),
                "method_digest": assessment["method_digest"], "catalog": catalog, "catalog_digest": catalog["catalog_digest"],
                "origin": assessment["origin"], **{key: receipt[key] for key in
                    ("development_content_digest", "development_index_digest", "development_row_count")},
                "created_at_utc": _now(), "source_authenticated": False, "real_source_ready": False,
                "point_in_time": "not_certified", "final_numeric_access_allowed": False}
            snapshot(reg, maximum=MAX_REGISTRATION_BYTES)
            registration = {**reg, "registration_digest": digest(reg)}
            directory = _new_output(self.root, intent["id"])
            for name, raw in (("prefix.csv", prefix), ("receipt.json", receipt_bytes), ("metadata.json", metadata_bytes),
                              ("registration.json", json_text(registration).encode())):
                _immutable_file(directory, name, raw)
            with self.authority._transaction() as (connection, authority_state):
                state = assert_prefix_history(connection)
                need(intent["id"] in state["intents"] and intent["id"] not in state["outcomes"] and
                     state["intents"][intent["id"]]["owner_digest"] == digest(owner), "normalization_owner_lost")
                self._current(connection, authority_state, command)
                for key, expected in (("prefix_relative", receipt["prefix_sha256"]), ("receipt_relative", reg["receipt_sha256"]),
                                      ("metadata_relative", reg["metadata_sha256"])):
                    read_managed(self.root, reg[key], maximum=MAX_PREFIX_BYTES if key == "prefix_relative" else 2 * 1024 * 1024,
                                 expected_sha256=expected)
                saved_reg, _ = read_managed(self.root, intent["output_relative"] + "registration.json", maximum=MAX_REGISTRATION_BYTES + 1024)
                need(json_text(parse_json(saved_reg)) == json_text(registration), "prefix_registration_file_drift")
                title = "Synthetic OHLC engineering development" if assessment["origin"] == "engineering_fixture" else "Declared OHLC local development"
                connection.execute("INSERT INTO datasets VALUES (?,?,?,?,?,?)", (dataset_id, title, reg["dataset_sha256"],
                    json_text(metadata), str(self.root / reg["prefix_relative"]), str(self.root / reg["metadata_relative"])))
                connection.execute("INSERT INTO rdw_prefix_registrations VALUES (?,?,?,?,?,?)",
                    (registration_id, intent["id"], dataset_id, registration["registration_digest"], json_text(reg), reg["created_at_utc"]))
                outcome = self._record_outcome(connection, intent, "completed", counters, registration=registration)
                assert_prefix_history(connection)
            return self._summary(intent, outcome, registration)
        except Exception as exc:
            error = getattr(exc, "code", "normalization_failure")
            if type(error) is not str or len(error) > 128 or not all(c.isalnum() or c == "_" for c in error):
                error = "normalization_failure"
            with self.authority._transaction() as (connection, _):
                state = assert_prefix_history(connection)
                need(intent["id"] in state["intents"] and intent["id"] not in state["outcomes"] and
                     state["intents"][intent["id"]]["owner_digest"] == digest(owner), "normalization_owner_lost")
                self._record_outcome(connection, intent, "failed", counters, error_code=error)
                assert_prefix_history(connection)
            if directory is not None:
                try:
                    _immutable_file(directory, "failure.json", json_text({"schema_version": 1, "intent_id": intent["id"],
                        "error_code": error, "counters": counters, "automatic_retry_allowed": False}).encode())
                except OSError:
                    pass
            raise RealDailyPrefixError(error) from None
        finally:
            if directory is not None:
                os.close(directory)

    def source_for_dataset(self, dataset_id, *, require_current_permission=False, read_prefix=False, connection=None):
        identity(dataset_id)
        if connection is None:
            with self.authority._transaction() as (opened, _):
                return self.source_for_dataset(dataset_id, require_current_permission=require_current_permission,
                                               read_prefix=read_prefix, connection=opened)
        state = assert_prefix_history(connection)
        matches = [reg for reg in state["registrations"].values() if reg["dataset_id"] == dataset_id]
        need(len(matches) == 1, "registered_ohlc_dataset_missing")
        registration = matches[0]
        intent = state["intents"][registration["intent_id"]]
        _, authority_state = _history(connection)
        if require_current_permission:
            command = {key: intent[key] for key in ("handle_id", "handle_digest", "approval_id", "approval_digest", "reservation_id", "reservation_digest")}
            self._current(connection, authority_state, command)
        dataset = _rows(connection, "SELECT * FROM datasets WHERE id=?", (dataset_id,))[0]
        paths = {"prefix": str(self.root / registration["prefix_relative"]), "receipt": str(self.root / registration["receipt_relative"]),
                 "metadata": str(self.root / registration["metadata_relative"]),
                 "registration": str(self.root / intent["output_relative"] / "registration.json")}
        source = {"registration": deepcopy(registration), "assessment": deepcopy(authority_state["assessments"][registration["assessment_id"]]),
            "dataset": dataset, "catalog": deepcopy(registration["catalog"]), "execution": main_execution_binding(registration), "paths": paths}
        if read_prefix:
            source["panels"] = self._load_panels(registration)
        return source

    def _load_panels(self, registration):
        import pandas as pd
        selection = registration["selection"]
        raw, _ = read_managed(self.root, registration["prefix_relative"], maximum=MAX_PREFIX_BYTES,
                              expected_sha256=registration["prefix_sha256"])
        reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""))
        need(next(reader) == ["date", "asset", *FIELDS], "prefix_columns_mismatch")
        grid = [(day, asset) for day in selection["development_sessions"] for asset in selection["assets"]]
        values = {field: [] for field in FIELDS}
        observations = []
        for index, row in enumerate(reader):
            need(index < len(grid) and len(row) == 6 and row[:2] == list(grid[index]), "prefix_grid_mismatch")
            numbers = [float(value) for value in row[2:]]
            need(all(math.isfinite(value) and value > 0 for value in numbers), "invalid_prefix_numeric")
            o, h, l, c = numbers
            need(l <= min(o, c) <= max(o, c) <= h, "invalid_prefix_numeric")
            for field, value in zip(FIELDS, numbers):
                values[field].append(value)
            observations.append({"date": row[0], "asset": row[1], "values_hex": [value.hex() for value in numbers]})
        need(len(observations) == len(grid) and digest(observations) == registration["development_content_digest"], "prefix_content_drift")
        return {field: pd.DataFrame([items[index:index + len(selection["assets"])]
                    for index in range(0, len(items), len(selection["assets"]))],
                    index=selection["development_sessions"], columns=selection["assets"]) for field, items in values.items()}
