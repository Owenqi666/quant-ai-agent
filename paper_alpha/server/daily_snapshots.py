"""Fixture-only typed snapshots: durable declaration before any market number read."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import csv
import fcntl
from functools import wraps
import hashlib
import io
import json
import math
import os
import stat
from pathlib import Path
import uuid

from ..daily_adapter import _open_regular, _stable, precheck_development
from ..daily_contract import CANONICAL_VERSION, COLUMNS, FIELDS, MAX_METADATA_BYTES, MAX_RAW_BYTES, parse_contract, parse_source_receipt
from ..storage import atomic_json, digest, json_text
from .db import connect, transaction
from .maintenance import workspace_lease
from .daily_snapshot_schema import (DATA_KIND, SNAPSHOT_VERSION, DailySnapshotSchema, declaration_payload,
    declaration_digest, development_index_digest, snapshot_registration_digest)

METADATA_KEYS = frozenset(("schema_version", "version", "data_kind", "origin", "calendar_dates", "universe",
    "columns", "field_availability", "market_sha256", "contract_digest", "contract_file_sha256",
    "receipt_file_sha256", "development_content_digest", "canonical_version", "snapshot_scope",
    "full_dataset_validated", "final_numeric_rows_parsed", "final_grid_validation", "final_execution_allowed",
    "test_freshness", "real_source_ready", "calendar_kind", "adjustment", "currency", "price_unit", "price_scale",
    "volume_unit", "volume_adjustment", "epsilon_101"))


class DailySnapshotError(ValueError):
    def __init__(self, message, status=409, code="daily_snapshot_error"):
        super().__init__(message)
        self.status, self.code = status, code


def _public_errors(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except DailySnapshotError:
            raise
        except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
            raise DailySnapshotError(self._safe_error(exc), getattr(exc, "status", 409),
                                     getattr(exc, "code", "snapshot_binding_error")) from exc
    return call


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _strict_json(payload):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("Nonfinite JSON number")
    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Nonfinite JSON number")
        return parsed
    return json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)


def _code_identity():
    package = Path(__file__).resolve().parents[1]
    names = ("daily_adapter.py", "daily_contract.py", "storage.py", "server/daily_snapshot_schema.py",
             "server/daily_snapshots.py", "server/daily_snapshot_guard.py")
    return digest({"version": "daily-snapshot-validator-v1",
                   "files": {name: _sha((package / name).read_bytes()) for name in names}})


class DailySnapshots:
    def __init__(self, store):
        self.store, self.root, self.db_path = store, Path(store.root), Path(store.db_path)
        if self.root != self.root.resolve() or not self.db_path.is_relative_to(self.root):
            raise DailySnapshotError("Snapshot workspace must have canonical local paths")

    def _path(self, *parts):
        if any(".." in Path(part).parts for part in parts):
            raise DailySnapshotError("Snapshot paths cannot traverse parents")
        path = self.root.joinpath(*parts)
        if not path.is_relative_to(self.root) or any(p.is_symlink() for p in (path, *path.parents)):
            raise DailySnapshotError("Snapshot paths must not contain symbolic links")
        return path

    @contextmanager
    def _lock(self, identity):
        path = self._path(".daily-snapshot-locks", identity + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DailySnapshotError("Unsafe snapshot ownership lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DailySnapshotError("Snapshot operation still owns this request; inspect its status") from exc
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _bounded(path, limit):
        path, stream, before = _open_regular(path, limit)
        with stream:
            raw = stream.read(limit + 1)
            if len(raw) != before.st_size or len(raw) > limit:
                raise DailySnapshotError("Snapshot input exceeded its bound while reading")
            _stable(path, stream, before)
        return raw

    def _immutable(self, path, payload):
        self._path(str(path.relative_to(self.root)))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self._bounded(path, max(len(payload), 1)) != payload:
                raise DailySnapshotError("Immutable snapshot artifact differs; no file overwritten")
            return
        tmp = self._path(str(path.relative_to(self.root)) + "." + str(uuid.uuid4()) + ".tmp")
        with tmp.open("xb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            raise DailySnapshotError("Snapshot artifact appeared during publication")
        os.rename(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _folder(self, identity):
        try:
            if type(identity) is not str or str(uuid.UUID(identity)) != identity:
                raise ValueError
        except (ValueError, AttributeError, TypeError) as exc:
            raise DailySnapshotError("Invalid registration identifier", 422) from exc
        return self._path("daily_snapshots", identity)

    def _row(self, identity, connection=None):
        self._folder(identity)
        if connection is None:
            with closing(connect(self.db_path)) as conn:
                return self._row(identity, conn)
        row = connection.execute("SELECT * FROM daily_snapshot_registrations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise DailySnapshotError("Daily snapshot registration not found", 404)
        return dict(row)

    def _safe_error(self, exc):
        return str(exc).replace(str(self.root), "[workbench]")[:1000]

    def _check_inputs(self, row):
        folder = self._folder(row["id"])
        paths = {"contract": folder / "inputs/contract.json", "csv": folder / "inputs/raw.csv",
                 "source_receipt": folder / "inputs/source_receipt.json"}
        for key, field, limit in (("contract", "contract_file_sha256", MAX_METADATA_BYTES),
                                  ("csv", "raw_sha256", MAX_RAW_BYTES),
                                  ("source_receipt", "receipt_file_sha256", MAX_METADATA_BYTES)):
            self._path(str(paths[key].relative_to(self.root)))
            raw = self._bounded(paths[key], limit)
            if _sha(raw) != row[field] or (key == "csv" and len(raw) != row["raw_bytes"]):
                raise DailySnapshotError("Frozen snapshot input identity changed")
        expected = {"declaration": declaration_payload(row), "declaration_digest": declaration_digest(row)}
        intent = self._bounded(folder / "intent.json", MAX_METADATA_BYTES)
        if digest(_strict_json(intent)) != digest(expected) or row["declaration_digest"] != expected["declaration_digest"]:
            raise DailySnapshotError("Independent snapshot intent binding changed")
        return paths

    @staticmethod
    def _metadata(row, contract, csv_sha, content_digest):
        fields = contract.to_dict()["fields"]
        value = {"schema_version": 1, "version": SNAPSHOT_VERSION, "data_kind": DATA_KIND,
                 "origin": "engineering_fixture", "calendar_dates": list(contract.development_calendar),
                 "universe": list(contract.assets), "columns": list(COLUMNS),
                 "field_availability": fields["field_availability"], "market_sha256": csv_sha,
                 "contract_digest": row["contract_digest"], "contract_file_sha256": row["contract_file_sha256"],
                 "receipt_file_sha256": row["receipt_file_sha256"], "development_content_digest": content_digest,
                 "canonical_version": CANONICAL_VERSION, "snapshot_scope": "development_only",
                 "full_dataset_validated": False, "final_numeric_rows_parsed": 0,
                 "final_grid_validation": "not_performed", "final_execution_allowed": False,
                 "test_freshness": "unknown", "real_source_ready": False,
                 "calendar_kind": "declared_development_prefix", "adjustment": fields["price_adjustment"]}
        value.update({key: fields[key] for key in ("currency", "price_unit", "price_scale", "volume_unit", "volume_adjustment", "epsilon_101")})
        assert set(value) == METADATA_KEYS
        return value

    def _publish(self, row, view):
        panels = view.panels
        stream = io.StringIO(newline=""); writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(COLUMNS)
        for day in view.contract.development_calendar:
            for asset in view.contract.assets:
                writer.writerow([day, asset, *[format(float(panels[field].loc[day, asset]), ".17g") for field in FIELDS]])
        raw = stream.getvalue().encode("utf-8")
        metadata = self._metadata(row, view.contract, _sha(raw), view.report["identity"]["development_content_digest"])
        fingerprint = digest({"data": _sha(raw), "metadata": metadata})
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-alpha:dataset:" + fingerprint))
        folder = self._path("datasets", identity)
        with self._lock("dataset-" + identity):
            self._immutable(folder / "market.csv", raw)
            self._immutable(folder / "metadata.json", json_text(metadata).encode())
        return {"id": identity, "title": row["title"], "sha256": fingerprint, "metadata": json_text(metadata),
                "data_path": str(folder / "market.csv"), "metadata_path": str(folder / "metadata.json")}

    @_public_errors
    def register_files(self, title, contract_path, csv_path, receipt_path, idempotency_key, allow_whole_byte_hash=False):
        if type(allow_whole_byte_hash) is not bool or not allow_whole_byte_hash:
            raise DailySnapshotError("Explicit whole-byte integrity permission is required", 422, "whole_byte_permission_required")
        try:
            inputs = [self._bounded(path, limit) for path, limit in ((contract_path, MAX_METADATA_BYTES),
                      (csv_path, MAX_RAW_BYTES), (receipt_path, MAX_METADATA_BYTES))]
            return self.register_bytes(title, *inputs, idempotency_key, allow_whole_byte_hash=True)
        except (OSError, ValueError) as exc:
            if isinstance(exc, DailySnapshotError):
                raise
            raise DailySnapshotError("Snapshot files are unsafe, changed or invalid", 422) from exc

    @_public_errors
    def register_bytes(self, title, contract_bytes, csv_bytes, receipt_bytes, idempotency_key, allow_whole_byte_hash=False):
        from . import daily_snapshot_guard as guard
        if type(allow_whole_byte_hash) is not bool or not allow_whole_byte_hash:
            raise DailySnapshotError("Explicit whole-byte integrity permission is required", 422, "whole_byte_permission_required")
        if type(title) is not str or not 0 < len(title.strip()) <= 200:
            raise DailySnapshotError("Title must contain 1..200 characters", 422)
        if type(idempotency_key) is not str or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise DailySnapshotError("Provide an idempotency key of 1..128 characters", 422)
        for raw, limit in ((contract_bytes, MAX_METADATA_BYTES), (csv_bytes, MAX_RAW_BYTES), (receipt_bytes, MAX_METADATA_BYTES)):
            if type(raw) is not bytes or not 0 < len(raw) <= limit:
                raise DailySnapshotError("Upload is empty, invalid or exceeds its bounded size", 413)
        try:
            contract = parse_contract(_strict_json(contract_bytes)); receipt = parse_source_receipt(_strict_json(receipt_bytes))
            c, r = contract.to_dict(), receipt.to_dict()
            if c["origin"] != "engineering_fixture":
                raise DailySnapshotError("Only engineering_fixture registration is supported; real-source qualification remains pending", 422)
            if len(contract.assets) < 3 or len(contract.assets) < c["evaluation"]["min_assets"]:
                raise DailySnapshotError("Development evaluation registration requires at least three eligible assets", 422)
            if any(e["date"] <= contract.cutoff for e in c["known_events"]):
                raise DailySnapshotError("Unsupported declared development event", 422)
            if (_sha(receipt_bytes) != c["source"]["receipt_sha256"] or c["source"]["source_id"] != r["source_id"]
                    or r["origin"] != c["origin"] or r["raw_sha256"] != _sha(csv_bytes) or r["raw_bytes"] != len(csv_bytes)):
                raise DailySnapshotError("Contract/source receipt/raw-byte identities disagree", 422)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, DailySnapshotError):
                raise
            raise DailySnapshotError("Invalid closed snapshot declaration", 422) from exc
        input_digest = digest({"contract_file_sha256": _sha(contract_bytes), "receipt_file_sha256": _sha(receipt_bytes), "raw_sha256": _sha(csv_bytes)})
        request_digest = digest({"title": title.strip(), "data_kind": DATA_KIND, "input_digest": input_digest, "allow_whole_byte_hash": True})
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-alpha:daily-snapshot:" + _sha(idempotency_key.encode())))
        with workspace_lease(self.root), self._lock(identity):
            with closing(connect(self.db_path)) as connection:
                found = connection.execute("SELECT * FROM daily_snapshot_registrations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if found is not None:
                row = dict(found)
                if row["request_digest"] != request_digest:
                    raise DailySnapshotError("Idempotency key was used for a different request")
                self._check_inputs(row)
                with transaction(self.db_path) as connection:
                    guard.status(connection, row)
                    if row["status"] in {"reserved", "prechecking"}:
                        message = "Prior owner stopped; attempt retained. Automatic numerical resume is not supported."
                        connection.execute("UPDATE daily_snapshot_registrations SET status='interrupted',updated_at=?,error=? WHERE id=?", (_now(), message, row["id"]))
                        connection.execute("UPDATE daily_snapshot_attempts SET status='interrupted',finished_at=?,error=? WHERE registration_id=? AND status='prechecking'", (_now(), message, row["id"]))
                return self.get(row["id"])
            created, token, attempt_id = _now(), str(uuid.uuid4()), str(uuid.uuid4())
            row = {"id": identity, "title": title.strip(), "status": "reserved", "created_at": created, "updated_at": created,
                   "idempotency_key": idempotency_key, "request_digest": request_digest, "input_digest": input_digest,
                   "contract_file_sha256": _sha(contract_bytes), "contract_digest": contract.contract_digest,
                   "receipt_file_sha256": _sha(receipt_bytes), "raw_sha256": _sha(csv_bytes), "raw_bytes": len(csv_bytes),
                   "contract_json": json_text(c), "receipt_json": json_text(r), "origin": c["origin"],
                   "owner_token": token, "latest_attempt_id": attempt_id, "registered_dataset_id": None,
                   "registration_digest": None, "development_content_digest": None, "development_index_digest": None, "error": None}
            row["declaration_digest"] = declaration_digest(row)
            folder = self._folder(identity)
            for name, payload in (("inputs/contract.json", contract_bytes), ("inputs/raw.csv", csv_bytes),
                                  ("inputs/source_receipt.json", receipt_bytes),
                                  ("intent.json", json_text({"declaration": declaration_payload(row), "declaration_digest": row["declaration_digest"]}).encode())):
                self._immutable(folder / name, payload)
            code = _code_identity()
            try:
                with transaction(self.db_path) as connection:
                    fields = tuple(row)
                    connection.execute("INSERT INTO daily_snapshot_registrations(" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")", tuple(row.values()))
                    guard.reserve_declared(connection, row)
                    connection.execute("INSERT INTO daily_snapshot_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, identity, 1, "prechecking", "numeric_precheck", created, None, token, code, None, None))
                    connection.execute("UPDATE daily_snapshot_registrations SET status='prechecking' WHERE id=?", (identity,))
            except ValueError as exc:
                rejection = {"schema_version": 1, "registration_id": identity, "status": "rejected_before_numeric",
                             "code": "declaration_conflict", "input_digest": input_digest,
                             "declaration_digest": row["declaration_digest"], "message": self._safe_error(exc),
                             "business_intent_committed": False, "new_reservation_committed": False,
                             "numeric_precheck_calls": 0, "test_freshness": "unknown", "real_source_ready": False}
                self._immutable(folder / "rejection.json", json_text(rejection).encode())
                raise DailySnapshotError(self._safe_error(exc), getattr(exc, "status", 409), "declaration_conflict") from exc
            try:
                paths = self._check_inputs(row)
                with closing(connect(self.db_path)) as check:
                    check.execute("BEGIN")
                    guard.status(check, row)
                if _code_identity() != code:
                    raise DailySnapshotError("Registration validator changed before numerical precheck")
                view = precheck_development(paths["contract"], paths["csv"], paths["source_receipt"], allow_whole_byte_hash=True,
                                           required_fields=("open", "high", "low", "close"))
                self._immutable(folder / "preflight.json", json_text(view.report).encode())
                dataset = self._publish(row, view)
                self._check_inputs(row)
                if _code_identity() != code:
                    raise DailySnapshotError("Registration validator code changed during the attempt")
                row.update(registered_dataset_id=dataset["id"], development_content_digest=view.report["identity"]["development_content_digest"], development_index_digest=development_index_digest(view))
                row["registration_digest"] = snapshot_registration_digest(row, dataset)
                with transaction(self.db_path) as connection:
                    current = self._row(identity, connection)
                    if current["owner_token"] != token or current["status"] != "prechecking":
                        raise DailySnapshotError("Snapshot owner lost its finalization fence")
                    existing = connection.execute("SELECT * FROM datasets WHERE id=?", (dataset["id"],)).fetchone()
                    if existing is None:
                        connection.execute("INSERT INTO datasets VALUES (?,?,?,?,?,?)", tuple(dataset[k] for k in ("id", "title", "sha256", "metadata", "data_path", "metadata_path")))
                    elif any(existing[k] != dataset[k] for k in ("sha256", "metadata", "data_path", "metadata_path")):
                        raise DailySnapshotError("Existing dataset version differs; no overwrite allowed")
                    connection.execute("UPDATE daily_snapshot_registrations SET registered_dataset_id=?,registration_digest=?,development_content_digest=?,development_index_digest=? WHERE id=?", (row["registered_dataset_id"], row["registration_digest"], row["development_content_digest"], row["development_index_digest"], identity))
                    guard.finalize_snapshot(connection, dataset, row, view)
                    connection.execute("UPDATE daily_snapshot_registrations SET status='registered',updated_at=? WHERE id=?", (_now(), identity))
                    connection.execute("UPDATE daily_snapshot_attempts SET status='registered',stage='finalized',finished_at=?,report=? WHERE id=?", (_now(), json_text(view.report), attempt_id))
                return self.get(identity)
            except (ValueError, TypeError, OSError, KeyError) as exc:
                message = self._safe_error(exc)
                with transaction(self.db_path) as connection:
                    connection.execute("UPDATE daily_snapshot_registrations SET status='failed',updated_at=?,error=? WHERE id=? AND owner_token=? AND status='prechecking'", (_now(), message, identity, token))
                    connection.execute("UPDATE daily_snapshot_attempts SET status='failed',stage='failed',finished_at=?,error=? WHERE id=? AND owner_token=? AND status='prechecking'", (_now(), message, attempt_id, token))
                failure = {"schema_version": 1, "registration_id": identity, "attempt_id": attempt_id,
                           "status": "failed", "code": getattr(exc, "code", "registration_failure"), "message": message}
                self._immutable(folder / "failure.json", json_text(failure).encode())
                return self.get(identity)

    @_public_errors
    def get(self, registration_id):
        row = self._row(registration_id)
        if declaration_digest(row) != row["declaration_digest"]:
            raise DailySnapshotError("Business snapshot declaration changed")
        with closing(connect(self.db_path)) as connection:
            attempts = [dict(item) for item in connection.execute("SELECT id,number,status,stage,started_at,finished_at,code_digest,error FROM daily_snapshot_attempts WHERE registration_id=? ORDER BY number LIMIT 100", (registration_id,))]
        keys = ("id", "title", "status", "created_at", "updated_at", "input_digest", "contract_digest", "declaration_digest",
                "registered_dataset_id", "registration_digest", "development_content_digest", "development_index_digest", "error")
        return DailySnapshotSchema(**{key: row[key] for key in keys}, attempts=attempts).model_dump()

    @_public_errors
    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise DailySnapshotError("Invalid bounded snapshot list range", 422)
        with closing(connect(self.db_path)) as connection:
            ids = [r[0] for r in connection.execute("SELECT id FROM daily_snapshot_registrations ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (limit, offset))]
        return [self.get(identity) for identity in ids]

    @_public_errors
    def verify(self, registration_id):
        from . import daily_snapshot_guard as guard
        with closing(connect(self.db_path)) as connection:
            connection.execute("BEGIN")
            row = self._row(registration_id, connection)
            if row["status"] != "registered" or row["origin"] != "engineering_fixture":
                raise DailySnapshotError("Only registered engineering snapshots can provide current calculation inputs")
            self._check_inputs(row)
            saved = connection.execute("SELECT * FROM datasets WHERE id=?", (row["registered_dataset_id"],)).fetchone()
            if saved is None:
                raise DailySnapshotError("Registered snapshot dataset is missing")
            dataset = dict(saved);folder = self._path("datasets", dataset["id"])
            if dataset["data_path"] != str(folder / "market.csv") or dataset["metadata_path"] != str(folder / "metadata.json"):
                raise DailySnapshotError("Registered snapshot paths differ from current workspace")
            raw = self._bounded(folder / "market.csv", MAX_RAW_BYTES)
            metadata_raw = self._bounded(folder / "metadata.json", MAX_METADATA_BYTES)
            metadata = _strict_json(metadata_raw)
            contract = parse_contract(json.loads(row["contract_json"]))
            expected = self._metadata(row, contract, _sha(raw), row["development_content_digest"])
            if (metadata != expected or json.loads(dataset["metadata"]) != expected
                    or digest({"data": _sha(raw), "metadata": expected}) != dataset["sha256"]
                    or snapshot_registration_digest(row, dataset) != row["registration_digest"]):
                raise DailySnapshotError("Registered snapshot content binding changed")
            guard.assert_snapshot_safe(connection, dataset, row)
            guard_result = guard.status(connection, row)
        return {"schema_version": 1, "verified": True, "registration_id": row["id"], "dataset_id": dataset["id"],
                "registration_digest": row["registration_digest"], "declaration_digest": row["declaration_digest"],
                "input_digest": row["input_digest"], "development_content_digest": row["development_content_digest"],
                "development_index_digest": row["development_index_digest"], "guard": guard_result,
                "test_freshness": "unknown", "final_execution_allowed": False, "real_source_ready": False}

    @_public_errors
    def registration_public(self, dataset_id):
        with closing(connect(self.db_path)) as connection:
            row = connection.execute("SELECT id FROM daily_snapshot_registrations WHERE registered_dataset_id=? AND status='registered' ORDER BY created_at,id LIMIT 1", (dataset_id,)).fetchone()
        if row is None:
            raise DailySnapshotError("Dataset has no registered daily snapshot", 404)
        return self.get(row[0])

    @_public_errors
    def compute_inputs(self, registration_id):
        verified = self.verify(registration_id)
        row = self._row(registration_id)
        if row["status"] != "registered" or row["origin"] != "engineering_fixture":
            raise DailySnapshotError("Snapshot changed after verification")
        for field in ("registration_digest", "declaration_digest", "input_digest", "development_content_digest", "development_index_digest"):
            if row[field] != verified[field]:
                raise DailySnapshotError("Snapshot identity changed after verification")
        if row["registered_dataset_id"] != verified["dataset_id"]:
            raise DailySnapshotError("Snapshot dataset changed after verification")
        return {"paths": self._check_inputs(row), "registration": row, "guard": verified["guard"]}

    private_snapshot_inputs = compute_inputs
