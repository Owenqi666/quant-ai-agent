"""Immutable synthetic-dataset imports with explicit validation and registration."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from .db import connect, transaction
from .dataset_validation import (MAX_CSV_BYTES, MAX_METADATA_BYTES, VALIDATION_TIMEOUT_SECONDS,
                                 bounded_bytes, empty_report, input_digest, validator_digest)
from .maintenance import workspace_lease
from .runner import stop_group
from ..storage import digest, json_text, read_json

PROJECT = Path(__file__).resolve().parents[2]
MAX_DIAGNOSTIC_BYTES = 256 * 1024

DATASET_IMPORT_SCHEMA = """
CREATE TABLE dataset_imports(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 csv_sha256 TEXT NOT NULL,metadata_sha256 TEXT NOT NULL,input_digest TEXT NOT NULL,
 upload_key TEXT NOT NULL UNIQUE,upload_request_digest TEXT NOT NULL,
 latest_validation_attempt_id TEXT,registered_dataset_id TEXT REFERENCES datasets(id),
 error TEXT);
CREATE TABLE dataset_validation_attempts(
 id TEXT PRIMARY KEY,import_id TEXT NOT NULL REFERENCES dataset_imports(id),
 number INTEGER NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
 idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,owner_token TEXT NOT NULL,
 validator_digest TEXT NOT NULL,report TEXT,report_digest TEXT,error TEXT,
 UNIQUE(import_id,number));
CREATE TABLE dataset_registration_attempts(
 id TEXT PRIMARY KEY,import_id TEXT NOT NULL REFERENCES dataset_imports(id),
 validation_attempt_id TEXT NOT NULL REFERENCES dataset_validation_attempts(id),
 input_digest TEXT NOT NULL,report_digest TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,finished_at TEXT,
 idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,
 dataset_id TEXT REFERENCES datasets(id),error TEXT);
CREATE TABLE dataset_import_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,import_id TEXT NOT NULL REFERENCES dataset_imports(id),
 kind TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX dataset_import_events_import ON dataset_import_events(import_id,id);
CREATE INDEX dataset_validation_import ON dataset_validation_attempts(import_id,number);
"""


class DatasetImportError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Busy(DatasetImportError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise DatasetImportError("Invalid dataset import identifier", 422) from None
    return value


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise DatasetImportError("Provide an idempotency key of 1..128 characters", 422)
    return value


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DatasetImports:
    """Operate on a migrated Store without changing its schema or existing data.

    No startup-wide recovery is performed. A read or explicit operation only
    recovers its own import after a nonblocking lock proves its owner has gone.
    """
    def __init__(self, root, db_path):
        self.root = Path(root).resolve()
        self.db_path = Path(db_path).resolve()
        if not self.db_path.is_relative_to(self.root):
            raise DatasetImportError("Database must belong to the workspace", 422)

    def _path(self, *parts):
        path = self.root.joinpath(*parts)
        if not path.is_relative_to(self.root):
            raise DatasetImportError("Unsafe dataset artifact path", 409)
        current = self.root
        for name in path.relative_to(self.root).parts:
            if name in {"", ".", ".."}:
                raise DatasetImportError("Unsafe dataset artifact path", 409)
            current = current / name
            if current.is_symlink():
                raise DatasetImportError("Dataset artifact symlinks are forbidden", 409)
        return path

    @contextmanager
    def _lease(self):
        try:
            with workspace_lease(self.root) as descriptor:
                yield descriptor
        except RuntimeError as exc:
            raise DatasetImportError(str(exc), 409) from exc

    @contextmanager
    def _lock(self, name):
        directory = self._path(".dataset-locks")
        directory.mkdir(exist_ok=True)
        path = self._path(".dataset-locks", name + ".lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DatasetImportError("Dataset lock is not an independent regular file", 409)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _Busy("A dataset operation still owns this lock; query its state before retrying", 409) from exc
            yield descriptor
        finally:
            # Children inherit these descriptors. No explicit LOCK_UN: closing
            # the final inherited descriptor proves all work has stopped.
            os.close(descriptor)

    def _rows(self, sql, values=()):
        connection = connect(self.db_path)
        try:
            return [dict(row) for row in connection.execute(sql, values)]
        finally:
            connection.close()

    def _row(self, identity):
        _uuid(identity)
        rows = self._rows("SELECT * FROM dataset_imports WHERE id=?", (identity,))
        if not rows:
            raise DatasetImportError("Dataset import not found", 404)
        return rows[0]

    @staticmethod
    def _event(connection, identity, kind, payload=None):
        connection.execute("INSERT INTO dataset_import_events(import_id,kind,created_at,payload) VALUES (?,?,?,?)",
                           (identity, kind, _now(), json_text(payload or {})))

    def _immutable(self, path, payload):
        self._path(*path.relative_to(self.root).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                same = bounded_bytes(path, max(len(payload), 1)) == payload
            except (OSError, ValueError):
                same = False
            if not same:
                raise DatasetImportError("An immutable dataset artifact differs from its expected contents", 409)
            return
        temporary = path.with_name("." + path.name + "." + str(uuid.uuid4()) + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # All publishers hold the import/content lock. Retain unfinished temp
        # files on abrupt process death; never alter an existing final artifact.
        if path.exists() or path.is_symlink():
            raise DatasetImportError("Dataset artifact appeared during publication", 409)
        os.rename(temporary, path)
        _sync(path.parent)

    def _inputs(self, row):
        csv = self._path("dataset_imports", row["id"], "inputs", "market.csv")
        metadata = self._path("dataset_imports", row["id"], "inputs", "metadata.json")
        try:
            csv_raw = bounded_bytes(csv, MAX_CSV_BYTES)
            metadata_raw = bounded_bytes(metadata, MAX_METADATA_BYTES)
        except (OSError, ValueError) as exc:
            raise DatasetImportError("Frozen dataset input is missing, unsafe or exceeds its size limit", 409) from exc
        if (_sha(csv_raw) != row["csv_sha256"] or _sha(metadata_raw) != row["metadata_sha256"]
                or input_digest(_sha(csv_raw), _sha(metadata_raw)) != row["input_digest"]):
            raise DatasetImportError("Frozen dataset input digest mismatch", 409)
        return csv, metadata

    def _operation(self, table, key, request_digest):
        rows = self._rows(f"SELECT * FROM {table} WHERE idempotency_key=?", (key,))
        if rows and rows[0]["request_digest"] != request_digest:
            raise DatasetImportError("Idempotency key was already used for a different request", 409)
        return rows[0] if rows else None

    def create(self, title, csv_bytes, metadata_bytes, idempotency_key):
        _key(idempotency_key)
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise DatasetImportError("Dataset title must contain 1..200 characters", 422)
        for payload, limit, label in ((csv_bytes, MAX_CSV_BYTES, "CSV"), (metadata_bytes, MAX_METADATA_BYTES, "Metadata")):
            if not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
                raise DatasetImportError(f"{label} must contain 1..{limit} bytes", 413)
        title = title.strip()
        csv_hash, metadata_hash = _sha(csv_bytes), _sha(metadata_bytes)
        fingerprint = input_digest(csv_hash, metadata_hash)
        request = digest({"title": title, "input_digest": fingerprint})
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-alpha:dataset-import:" + _sha(idempotency_key.encode())))
        with self._lease(), self._lock(identity):
            existing = self._rows("SELECT * FROM dataset_imports WHERE upload_key=?", (idempotency_key,))
            if existing:
                if existing[0]["upload_request_digest"] != request:
                    raise DatasetImportError("Idempotency key was already used for a different upload", 409)
                return self._result(existing[0]["id"])
            folder = self._path("dataset_imports", identity)
            receipt_path = self._path("dataset_imports", identity, "upload.json")
            # A durable receipt predates the DB row. A retry after a crash can
            # complete the same upload; a different body cannot reuse its ID.
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                if receipt.get("request_digest") != request or receipt.get("id") != identity:
                    raise DatasetImportError("Upload receipt conflicts with this idempotency key", 409)
                created = receipt["created_at"]
            else:
                created = _now()
                receipt = {"schema_version": 1, "id": identity, "title": title, "request_digest": request,
                           "created_at": created, "csv_sha256": csv_hash, "metadata_sha256": metadata_hash,
                           "input_digest": fingerprint}
                self._immutable(receipt_path, json_text(receipt).encode())
            self._immutable(folder / "inputs/market.csv", csv_bytes)
            self._immutable(folder / "inputs/metadata.json", metadata_bytes)
            with transaction(self.db_path) as connection:
                connection.execute("INSERT INTO dataset_imports(id,title,status,created_at,updated_at,csv_sha256,metadata_sha256,input_digest,upload_key,upload_request_digest) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (identity, title, "uploaded", created, _now(), csv_hash, metadata_hash, fingerprint, idempotency_key, request))
                self._event(connection, identity, "uploaded", {"input_digest": fingerprint})
            return self._result(identity)

    def _result(self, identity):
        _uuid(identity)
        connection = connect(self.db_path)
        try:
            # Polls may race the live owner's final commit. One read snapshot
            # keeps status, latest attempt, history and receipts consistent.
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM dataset_imports WHERE id=?", (identity,)).fetchone()
            if not row:
                raise DatasetImportError("Dataset import not found", 404)
            row = dict(row)
            attempts = [dict(item) for item in connection.execute("SELECT id,number,status,started_at,finished_at,validator_digest,report,report_digest,error FROM dataset_validation_attempts WHERE import_id=? ORDER BY number", (identity,))]
            registrations = [dict(item) for item in connection.execute("SELECT id,validation_attempt_id,status,created_at,finished_at,dataset_id,error FROM dataset_registration_attempts WHERE import_id=? ORDER BY created_at,id", (identity,))]
            events = [dict(item) for item in connection.execute("SELECT id AS sequence,kind,created_at,payload FROM dataset_import_events WHERE import_id=? ORDER BY id", (identity,))]
            connection.commit()
        finally:
            connection.close()
        public = {name: value for name, value in row.items() if name not in {"upload_key", "upload_request_digest"}}
        for attempt in attempts:
            attempt["report"] = json.loads(attempt["report"]) if attempt["report"] else None
        public["validation_attempts"] = attempts
        public["latest_validation"] = next((a for a in attempts if a["id"] == row["latest_validation_attempt_id"]), None)
        public["registration_attempts"] = registrations
        for event in events:
            event["payload"] = json.loads(event["payload"])
        public["events"] = events
        return public

    def get(self, identity):
        self._row(identity)
        with self._lease():
            try:
                with self._lock(identity):
                    self._recover_locked(identity)
            except _Busy:
                pass  # The actual owner (or its child) is still alive.
            return self._result(identity)

    def list(self):
        return [self.get(row["id"]) for row in self._rows("SELECT id FROM dataset_imports ORDER BY created_at DESC,id")]

    def _report_path(self, identity, attempt_id, name="report.json"):
        return self._path("dataset_imports", _uuid(identity), "validation", _uuid(attempt_id), name)

    def _finish_validation(self, identity, attempt, report, *, status=None, error=None):
        status = status or report["status"]
        report_path = self._report_path(identity, attempt["id"])
        self._immutable(report_path, json_text(report).encode())
        fingerprint = digest(report)
        with transaction(self.db_path) as connection:
            row = connection.execute("SELECT status,latest_validation_attempt_id FROM dataset_imports WHERE id=?", (identity,)).fetchone()
            current = connection.execute("SELECT status,owner_token FROM dataset_validation_attempts WHERE id=?", (attempt["id"],)).fetchone()
            if not row or row["status"] != "validating" or row["latest_validation_attempt_id"] != attempt["id"] or not current or current["status"] != "validating" or current["owner_token"] != attempt["owner_token"]:
                raise DatasetImportError("Validation attempt no longer owns this import", 409)
            connection.execute("UPDATE dataset_validation_attempts SET status=?,finished_at=?,report=?,report_digest=?,error=? WHERE id=?",
                               (status, _now(), json_text(report), fingerprint, error, attempt["id"]))
            connection.execute("UPDATE dataset_imports SET status=?,updated_at=?,error=? WHERE id=?", (status, _now(), error, identity))
            self._event(connection, identity, "validation_" + status, {"validation_attempt_id": attempt["id"], "report_digest": fingerprint, "error": error})

    def _recover_locked(self, identity):
        row = self._row(identity)
        if row["status"] == "validating":
            attempts = self._rows("SELECT * FROM dataset_validation_attempts WHERE id=?", (row["latest_validation_attempt_id"],))
            if not attempts:
                raise DatasetImportError("Validation ownership record is missing", 409)
            attempt = attempts[0]
            message = "Validation owner and child have exited; prior files retained. Retry with a new key."
            final_path = self._report_path(identity, attempt["id"])
            # If computation published a report but the DB commit did not finish,
            # retain that immutable evidence while marking the attempt interrupted.
            report = read_json(final_path) if final_path.exists() else empty_report(row["input_digest"], attempt["validator_digest"], code="owner_lost", message=message)
            self._finish_validation(identity, attempt, report, status="interrupted", error=message)
        elif row["status"] == "registering":
            message = "Registration owner exited; frozen inputs and any published files are retained. Retry the original request."
            with transaction(self.db_path) as connection:
                connection.execute("UPDATE dataset_registration_attempts SET status='interrupted',finished_at=?,error=? WHERE import_id=? AND status='registering'", (_now(), message, identity))
                connection.execute("UPDATE dataset_imports SET status='interrupted',updated_at=?,error=? WHERE id=?", (_now(), message, identity))
                self._event(connection, identity, "registration_interrupted", {"error": message})

    def _validation_command(self, row, attempt, result_path):
        csv, metadata = self._inputs(row)
        return [sys.executable, "-m", "paper_alpha.server.dataset_validation", "--csv", str(csv),
                "--metadata", str(metadata), "--out", str(result_path), "--input-digest", row["input_digest"],
                "--validator-digest", attempt["validator_digest"]]

    def _execute_validation(self, row, attempt, descriptors):
        pending = self._report_path(row["id"], attempt["id"], "computed-report.json")
        pending.parent.mkdir(parents=True, exist_ok=True)
        process = None
        started = time.monotonic()
        try:
            command = self._validation_command(row, attempt, pending)
            stdout_path, stderr_path = pending.parent / "stdout.log", pending.parent / "stderr.log"
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                process = subprocess.Popen(command, cwd=PROJECT, stdout=stdout, stderr=stderr,
                                           start_new_session=True, pass_fds=tuple(descriptors),
                                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                while process.poll() is None:
                    if time.monotonic() - started >= VALIDATION_TIMEOUT_SECONDS:
                        raise TimeoutError("Validation exceeded the 30-second limit")
                    if max(stdout_path.stat().st_size, stderr_path.stat().st_size) > MAX_DIAGNOSTIC_BYTES:
                        raise RuntimeError("Validation diagnostic output exceeded its limit")
                    time.sleep(.025)
            if process.returncode != 0:
                raise RuntimeError("Validation subprocess failed; bounded diagnostic files retained")
            self._inputs(row)  # No file may change between validation and publication.
            bounded_bytes(pending, MAX_METADATA_BYTES)
            report = read_json(pending)
            if (report.get("input_digest") != row["input_digest"] or report.get("validator", {}).get("digest") != attempt["validator_digest"]
                    or report.get("status") not in {"valid", "invalid"}):
                raise RuntimeError("Validation subprocess returned an inconsistent receipt")
            return report, None
        except DatasetImportError as exc:
            return empty_report(row["input_digest"], attempt["validator_digest"], status="invalid", code="input_integrity_failed", message=str(exc)), str(exc)
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            code = "validation_timeout" if isinstance(exc, TimeoutError) else "validation_process_failed"
            message = str(exc).replace(str(self.root), "[workbench]").replace(str(PROJECT), "[project]")[:1500]
            report = empty_report(row["input_digest"], attempt["validator_digest"], code=code, message=message)
            report["duration_seconds"] = round(time.monotonic() - started, 6)
            return report, message
        finally:
            if process is not None:
                stop_group(process)

    def validate(self, identity, idempotency_key):
        _uuid(identity)
        _key(idempotency_key)
        request = digest({"import_id": identity, "operation": "validate"})
        if self._operation("dataset_validation_attempts", idempotency_key, request):
            return self.get(identity)
        with self._lease() as lease, self._lock("validation-global") as global_lock, self._lock(identity) as import_lock:
            if self._operation("dataset_validation_attempts", idempotency_key, request):
                return self._result(identity)
            self._recover_locked(identity)
            row = self._row(identity)
            if row["status"] == "registered":
                raise DatasetImportError("Registered versions are immutable; create another import to validate again", 409)
            identity_attempt = str(uuid.uuid4())
            attempt = {"id": identity_attempt, "import_id": identity, "status": "validating", "started_at": _now(),
                       "idempotency_key": idempotency_key, "request_digest": request, "owner_token": str(uuid.uuid4()),
                       "validator_digest": validator_digest()}
            with transaction(self.db_path) as connection:
                attempt["number"] = connection.execute("SELECT COALESCE(MAX(number),0)+1 FROM dataset_validation_attempts WHERE import_id=?", (identity,)).fetchone()[0]
                connection.execute("INSERT INTO dataset_validation_attempts(id,import_id,number,status,started_at,idempotency_key,request_digest,owner_token,validator_digest) VALUES (:id,:import_id,:number,:status,:started_at,:idempotency_key,:request_digest,:owner_token,:validator_digest)", attempt)
                connection.execute("UPDATE dataset_imports SET status='validating',latest_validation_attempt_id=?,updated_at=?,error=NULL WHERE id=?", (identity_attempt, _now(), identity))
                self._event(connection, identity, "validation_started", {"validation_attempt_id": identity_attempt, "number": attempt["number"], "validator_digest": attempt["validator_digest"]})
            report, error = self._execute_validation(row, attempt, (lease, global_lock, import_lock))
            self._finish_validation(identity, attempt, report, error=error)
            return self._result(identity)

    def _registration_binding(self, row, validation_attempt_id, expected_report):
        if row["latest_validation_attempt_id"] != validation_attempt_id:
            raise DatasetImportError("Register the latest explicitly validated attempt", 409)
        attempts = self._rows("SELECT * FROM dataset_validation_attempts WHERE id=? AND import_id=?", (validation_attempt_id, row["id"]))
        if not attempts or attempts[0]["status"] != "valid":
            raise DatasetImportError("A successfully completed validation attempt is required", 409)
        attempt = attempts[0]
        if attempt["report_digest"] != expected_report:
            raise DatasetImportError("Registration report digest does not match the approved validation", 409)
        report_path = self._report_path(row["id"], validation_attempt_id)
        try:
            bounded_bytes(report_path, MAX_METADATA_BYTES)
            report = read_json(report_path)
        except (OSError, ValueError) as exc:
            raise DatasetImportError("Frozen validation report is missing or unsafe", 409) from exc
        if (digest(report) != expected_report or report != json.loads(attempt["report"])
                or report.get("status") != "valid" or report.get("input_digest") != row["input_digest"]
                or report.get("validator", {}).get("digest") != attempt["validator_digest"]):
            raise DatasetImportError("Frozen validation report changed or belongs to different inputs", 409)
        # Existing registered versions remain usable across validator changes.
        # Only a fresh registration decision requires the current validator.
        if row["status"] != "registered" and attempt["validator_digest"] != validator_digest():
            raise DatasetImportError("Validation semantics or environment changed; validate with a new attempt before registration", 409)
        csv, metadata_path = self._inputs(row)
        metadata = read_json(metadata_path)
        fingerprint = digest({"data": row["csv_sha256"], "metadata": metadata})
        if report.get("summary", {}).get("dataset_sha256") != fingerprint:
            raise DatasetImportError("Validation report and dataset version disagree", 409)
        return csv, metadata, fingerprint

    def _publish_dataset(self, row, csv, metadata, fingerprint):
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper-alpha:dataset:" + fingerprint))
        folder = self._path("datasets", identity)
        # Files appear before the database row. A crash leaves an unregistered
        # directory which the same request can verify and complete safely.
        self._immutable(folder / "market.csv", bounded_bytes(csv, MAX_CSV_BYTES))
        self._immutable(folder / "metadata.json", json_text(metadata).encode())
        if (_sha(bounded_bytes(folder / "market.csv", MAX_CSV_BYTES)) != row["csv_sha256"]
                or read_json(folder / "metadata.json") != metadata):
            raise DatasetImportError("Published dataset does not match its validated input", 409)
        return identity, folder

    def _registration_interrupted(self, identity, attempt_id, error):
        with transaction(self.db_path) as connection:
            connection.execute("UPDATE dataset_registration_attempts SET status='interrupted',finished_at=?,error=? WHERE id=? AND status='registering'", (_now(), error, attempt_id))
            connection.execute("UPDATE dataset_imports SET status='interrupted',updated_at=?,error=? WHERE id=? AND status='registering'", (_now(), error, identity))
            self._event(connection, identity, "registration_interrupted", {"registration_attempt_id": attempt_id, "error": error})

    def register(self, identity, input_digest, validation_attempt_id, report_digest, idempotency_key):
        _uuid(identity)
        _uuid(validation_attempt_id)
        _key(idempotency_key)
        for value in (input_digest, report_digest):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise DatasetImportError("Registration requires SHA-256 input and report digests", 422)
        request = digest({"import_id": identity, "input_digest": input_digest,
                          "validation_attempt_id": validation_attempt_id, "report_digest": report_digest})
        operation = self._operation("dataset_registration_attempts", idempotency_key, request)
        if operation and operation["status"] == "registered":
            return self.get(identity)
        with self._lease(), self._lock("registration-key-" + _sha(idempotency_key.encode())), self._lock(identity):
            self._recover_locked(identity)
            operation = self._operation("dataset_registration_attempts", idempotency_key, request)
            row = self._row(identity)
            if row["input_digest"] != input_digest:
                raise DatasetImportError("Registration inputs differ from the uploaded receipt", 409)
            csv, metadata, fingerprint = self._registration_binding(row, validation_attempt_id, report_digest)
            if row["status"] not in {"valid", "interrupted", "registered"}:
                raise DatasetImportError("Import must be valid before explicit registration", 409)
            attempt_id = operation["id"] if operation else str(uuid.uuid4())
            if row["status"] == "registered":
                # A different key repeating an already approved registration is
                # recorded without changing the import or re-publishing files.
                with transaction(self.db_path) as connection:
                    connection.execute("INSERT INTO dataset_registration_attempts(id,import_id,validation_attempt_id,input_digest,report_digest,status,created_at,finished_at,idempotency_key,request_digest,dataset_id) VALUES (?,?,?,?,?,'registered',?,?,?,?,?)",
                                       (attempt_id, identity, validation_attempt_id, input_digest, report_digest, _now(), _now(), idempotency_key, request, row["registered_dataset_id"]))
                    self._event(connection, identity, "registration_replayed", {"registration_attempt_id": attempt_id, "dataset_id": row["registered_dataset_id"]})
                return self._result(identity)
            with transaction(self.db_path) as connection:
                if operation:
                    connection.execute("UPDATE dataset_registration_attempts SET status='registering',finished_at=NULL,error=NULL WHERE id=?", (attempt_id,))
                else:
                    connection.execute("INSERT INTO dataset_registration_attempts(id,import_id,validation_attempt_id,input_digest,report_digest,status,created_at,idempotency_key,request_digest) VALUES (?,?,?,?,?,'registering',?,?,?)",
                                       (attempt_id, identity, validation_attempt_id, input_digest, report_digest, _now(), idempotency_key, request))
                connection.execute("UPDATE dataset_imports SET status='registering',updated_at=?,error=NULL WHERE id=?", (_now(), identity))
                self._event(connection, identity, "registration_started", {"registration_attempt_id": attempt_id, "validation_attempt_id": validation_attempt_id, "report_digest": report_digest})
            try:
                with self._lock("dataset-" + fingerprint):
                    dataset_id, folder = self._publish_dataset(row, csv, metadata, fingerprint)
                    # Verify original bytes again after copying; no claim can be
                    # committed from a report whose inputs changed in the meantime.
                    self._inputs(row)
                    with transaction(self.db_path) as connection:
                        existing = connection.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
                        if existing:
                            if (existing["sha256"] != fingerprint or json.loads(existing["metadata"]) != metadata
                                    or Path(existing["data_path"]) != folder / "market.csv"
                                    or Path(existing["metadata_path"]) != folder / "metadata.json"):
                                raise DatasetImportError("Existing dataset identity or stored paths conflict; no files were overwritten", 409)
                        else:
                            connection.execute("INSERT INTO datasets(id,title,sha256,metadata,data_path,metadata_path) VALUES (?,?,?,?,?,?)",
                                               (dataset_id, row["title"], fingerprint, json_text(metadata), str(folder / "market.csv"), str(folder / "metadata.json")))
                        connection.execute("UPDATE dataset_registration_attempts SET status='registered',finished_at=?,dataset_id=?,error=NULL WHERE id=?", (_now(), dataset_id, attempt_id))
                        connection.execute("UPDATE dataset_imports SET status='registered',registered_dataset_id=?,updated_at=?,error=NULL WHERE id=?", (dataset_id, _now(), identity))
                        self._event(connection, identity, "registered", {"registration_attempt_id": attempt_id, "dataset_id": dataset_id, "dataset_sha256": fingerprint, "existing_version": bool(existing)})
            except Exception as exc:
                message = str(exc).replace(str(self.root), "[workbench]").replace(str(PROJECT), "[project]")[:1500]
                self._registration_interrupted(identity, attempt_id, message)
                if isinstance(exc, DatasetImportError):
                    raise
                raise DatasetImportError("Dataset registration interrupted; frozen inputs and files retained", 409) from exc
            return self._result(identity)
