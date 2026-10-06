"""Immutable dataset receipts, real subprocess validation and crash boundaries."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from paper_alpha.server import dataset_imports as module
from paper_alpha.server.dataset_imports import DatasetImportError, DatasetImports
from paper_alpha.server.dataset_validation import MAX_CSV_BYTES, MAX_METADATA_BYTES
from paper_alpha.server.db import transaction
from paper_alpha.server.maintenance import workspace_lease
from paper_alpha.server.runner import stop_group
from paper_alpha.server.service import Store
from paper_alpha.storage import read_json

FIXTURE = Path(__file__).resolve().parent / "fixtures/datasets"


class DatasetImportsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.store = Store(self.base / "workbench")
        self.imports = DatasetImports(self.store.root, self.store.db_path)
        self.csv = (FIXTURE / "market.csv").read_bytes()
        self.metadata = (FIXTURE / "metadata.json").read_bytes()

    def tearDown(self):
        self.temporary.cleanup()

    def upload(self, key="upload", csv=None, metadata=None, title="Second controlled dataset"):
        return self.imports.create(title, self.csv if csv is None else csv,
                                   self.metadata if metadata is None else metadata, key)

    def validate(self, row=None, key="validate"):
        return self.imports.validate((row or self.upload())["id"], key)

    def register(self, row, key="register"):
        return self.imports.register(row["id"], row["input_digest"], row["latest_validation"]["id"], row["latest_validation"]["report_digest"], key)

    def encoded(self, metadata, csv=None):
        copy = deepcopy(metadata)
        if csv is not None:
            copy["market_sha256"] = hashlib.sha256(csv).hexdigest()
        return json.dumps(copy).encode()

    def test_complete_explicit_registration_and_immutable_receipts(self):
        uploaded = self.upload()
        self.assertEqual(uploaded["status"], "uploaded")
        self.assertEqual(len(self.store.list_datasets()), 1)
        self.assertEqual(self.upload()["id"], uploaded["id"])
        valid = self.validate(uploaded)
        self.assertEqual(valid["status"], "valid", valid)
        self.assertEqual(len(self.store.list_datasets()), 1)
        self.assertEqual(valid["latest_validation"]["report"]["summary"]["rows"], 672)
        self.assertTrue(all(check["outcome"] == "passed" for check in valid["latest_validation"]["report"]["checks"]))
        registered = self.register(valid)
        self.assertEqual(registered["status"], "registered")
        self.assertEqual(len(self.store.list_datasets()), 2)
        self.assertEqual(self.register(valid)["registered_dataset_id"], registered["registered_dataset_id"])
        self.assertEqual(len(self.imports.get(uploaded["id"])["registration_attempts"]), 1)
        source = self.store.root / "dataset_imports" / uploaded["id"] / "inputs"
        self.assertEqual((source / "market.csv").read_bytes(), self.csv)
        self.assertEqual((source / "metadata.json").read_bytes(), self.metadata)
        self.assertNotIn(str(self.store.root), json.dumps(registered))
        self.assertEqual(self.imports.list()[0]["id"], uploaded["id"])

    def test_duplicate_content_and_metadata_whitespace_keep_original_version(self):
        first = self.register(self.validate())
        original_dataset = self.store._fetch("datasets", first["registered_dataset_id"])
        metadata = json.dumps(json.loads(self.metadata), separators=(",", ":")).encode()
        second = self.upload("other-upload", metadata=metadata, title="Do not rename existing dataset")
        self.assertNotEqual(first["input_digest"], second["input_digest"])
        second = self.register(self.validate(second, "other-validation"), "other-registration")
        self.assertEqual(first["registered_dataset_id"], second["registered_dataset_id"])
        self.assertEqual(self.store._fetch("datasets", first["registered_dataset_id"]), original_dataset)
        self.assertEqual(len(self.store.list_datasets()), 2)

    def test_upload_and_operation_idempotency_conflicts_are_explicit(self):
        first = self.upload()
        with self.assertRaisesRegex(DatasetImportError, "different upload"):
            self.upload(title="different")
        other = self.upload("another-upload")
        valid = self.validate(first, "same-validation-key")
        self.assertEqual(len(self.validate(first, "same-validation-key")["validation_attempts"]), 1)
        with self.assertRaisesRegex(DatasetImportError, "different request"):
            self.validate(other, "same-validation-key")
        self.register(valid, "same-registration-key")
        other = self.validate(other, "other-validation-key")
        with self.assertRaisesRegex(DatasetImportError, "different request"):
            self.register(other, "same-registration-key")

    def test_invalid_metadata_is_retained_in_a_failed_validation_receipt(self):
        for index, payload in enumerate((b'{"x":1,"x":2}', b'{"x":NaN}', b'[1,2]', b'not JSON')):
            with self.subTest(payload=payload):
                row = self.upload("bad-json-" + str(index), metadata=payload)
                result = self.validate(row, "bad-json-validation-" + str(index))
                self.assertEqual(result["status"], "invalid")
                report = result["latest_validation"]["report"]
                self.assertEqual(report["checks"][1]["outcome"], "failed")
                self.assertEqual(report["checks"][2]["outcome"], "not_run")
                with self.assertRaises(DatasetImportError):
                    self.register(result, "bad-json-register-" + str(index))
        self.assertEqual(len(self.store.list_datasets()), 1)

    def test_loader_failures_and_grid_limit_are_not_silently_repaired(self):
        metadata = json.loads(self.metadata)
        lines = self.csv.splitlines(keepends=True)
        bad_cases = [
            (self.csv, {**metadata, "market_sha256": "0" * 64}, "checksum_mismatch"),
            (self.csv, {**metadata, "data_kind": "real"}, "unsupported_data_kind"),
            (b"".join(lines[:-1]), metadata, "grid_mismatch"),
            (b"".join([*lines[:-1], lines[1]]), metadata, "duplicate_key"),
            (self.csv.replace(b"open,high", b"opening,high", 1), metadata, "columns_mismatch"),
            (self.csv, {**metadata, "calendar_dates": ["2024-01-02"] * 100_001}, "import_cell_limit"),
        ]
        for index, (payload, meta, expected) in enumerate(bad_cases):
            with self.subTest(expected=expected):
                encoded = self.encoded(meta, payload) if payload != self.csv else self.encoded(meta)
                row = self.upload(f"invalid-data-{index}", csv=payload, metadata=encoded)
                result = self.validate(row, f"invalid-data-validation-{index}")
                self.assertEqual(result["status"], "invalid", result)
                report = result["latest_validation"]["report"]
                self.assertTrue(any(expected == check["code"] for check in report["checks"]), report)
                self.assertEqual(report["diagnostics"]["detected_error_count"], 1)
                self.assertFalse(report["diagnostics"]["all_errors_enumerated"])
                self.assertEqual((self.store.root / "dataset_imports" / row["id"] / "inputs/market.csv").read_bytes(), payload)

    def test_nonfinite_and_invalid_ohlc_are_rejected(self):
        lines = self.csv.decode().splitlines()
        for index, replacement in enumerate(("NaN", "0", "99999999")):
            cells = lines[1].split(",")
            cells[4] = replacement  # low must be finite, positive and <= open/close/high.
            payload = ("\n".join([lines[0], ",".join(cells), *lines[2:]]) + "\n").encode()
            row = self.upload(f"ohlc-{index}", csv=payload, metadata=self.encoded(json.loads(self.metadata), payload))
            self.assertEqual(self.validate(row, f"ohlc-validation-{index}")["status"], "invalid")

    def test_input_and_report_tampering_block_registration(self):
        valid = self.validate()
        report_path = self.imports._report_path(valid["id"], valid["latest_validation"]["id"])
        original = report_path.read_bytes()
        report = json.loads(original)
        report["summary"]["rows"] += 1
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(DatasetImportError, "report changed"):
            self.register(valid)
        report_path.write_bytes(original)
        data_path = self.store.root / "dataset_imports" / valid["id"] / "inputs/market.csv"
        data_path.write_bytes(self.csv + b"\n")
        with self.assertRaisesRegex(DatasetImportError, "digest mismatch"):
            self.register(valid)
        self.assertEqual(len(self.store.list_datasets()), 1)

    def test_stale_validator_requires_new_attempt_but_registered_versions_remain(self):
        valid = self.validate()
        with patch.object(module, "validator_digest", return_value="0" * 64):
            with self.assertRaisesRegex(DatasetImportError, "semantics or environment changed"):
                self.register(valid)
        registered = self.register(valid)
        with patch.object(module, "validator_digest", return_value="0" * 64):
            self.assertEqual(self.register(valid)["registered_dataset_id"], registered["registered_dataset_id"])
            replay = self.register(valid, "second-register-key")
            self.assertEqual(replay["status"], "registered")
            self.assertEqual(len(replay["registration_attempts"]), 2)

    def test_upload_crash_receipt_can_complete_only_the_original_request(self):
        original = self.imports._immutable
        def stop_before_metadata(path, payload):
            if path.name == "metadata.json":
                raise KeyboardInterrupt("simulated crash")
            return original(path, payload)
        with patch.object(self.imports, "_immutable", side_effect=stop_before_metadata):
            with self.assertRaises(KeyboardInterrupt):
                self.upload()
        self.assertEqual(self.imports.list(), [])
        self.assertEqual(len(list((self.store.root / "dataset_imports").glob("*/upload.json"))), 1)
        with self.assertRaisesRegex(DatasetImportError, "receipt conflicts"):
            self.upload(title="different request")
        result = self.upload()
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(self.validate(result)["status"], "valid")

    def test_dead_validation_is_recovered_on_query_and_retry_uses_new_attempt(self):
        row = self.upload()
        with patch.object(self.imports, "_execute_validation", side_effect=KeyboardInterrupt("simulated owner death")):
            with self.assertRaises(KeyboardInterrupt):
                self.validate(row)
        result = DatasetImports(self.store.root, self.store.db_path).get(row["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["latest_validation"]["report"]["checks"][0]["code"], "owner_lost")
        self.assertEqual(self.validate(row)["status"], "interrupted")
        result = self.validate(row, "explicit-new-attempt")
        self.assertEqual(result["status"], "valid")
        self.assertEqual([a["status"] for a in result["validation_attempts"]], ["interrupted", "valid"])

    def test_live_validation_is_not_recovered_and_does_not_hold_sqlite_write_lock(self):
        row = self.upload()
        other = self.upload("other")
        started, release = threading.Event(), threading.Event()
        original = self.imports._execute_validation
        def waiting(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return original(*args)
        with patch.object(self.imports, "_execute_validation", side_effect=waiting), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.validate, row)
            try:
                self.assertTrue(started.wait(3))
                Store(self.store.root)  # Initialization must not recover an active owner.
                self.assertEqual(self.imports.get(row["id"])["status"], "validating")
                self.assertEqual(self.imports.list()[1]["status"], "validating")
                with transaction(self.store.db_path) as connection:
                    connection.execute("INSERT INTO settings VALUES ('parallel-write','ok')")
                with self.assertRaises(DatasetImportError):
                    self.validate(other, "parallel-validation")
                with self.assertRaises(RuntimeError):
                    with workspace_lease(self.store.root, exclusive=True):
                        pass
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10)["status"], "valid")

    def test_orphan_child_keeps_operation_and_backup_leases_until_it_exits(self):
        row = self.upload()
        processes = []
        def orphan(current, attempt, descriptors):
            processes.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                              pass_fds=tuple(descriptors), start_new_session=True))
            raise KeyboardInterrupt("parent exits before child")
        try:
            with patch.object(self.imports, "_execute_validation", side_effect=orphan):
                with self.assertRaises(KeyboardInterrupt):
                    self.validate(row)
            self.assertEqual(self.imports.get(row["id"])["status"], "validating")
            with self.assertRaises(RuntimeError):
                with workspace_lease(self.store.root, exclusive=True):
                    pass
        finally:
            for process in processes:
                stop_group(process)
        self.assertEqual(self.imports.get(row["id"])["status"], "interrupted")

    def test_validation_deadline_stops_subprocess_and_preserves_receipt(self):
        row = self.upload()
        with patch.object(module, "VALIDATION_TIMEOUT_SECONDS", .1), patch.object(self.imports, "_validation_command", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]):
            before = time.monotonic()
            result = self.validate(row)
        self.assertLess(time.monotonic() - before, 5)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["latest_validation"]["report"]["checks"][0]["code"], "validation_timeout")
        self.assertEqual(self.validate(row, "after-timeout")["status"], "valid")

    def test_registration_crash_after_files_before_database_resumes_same_request(self):
        valid = self.validate()
        original = self.imports._publish_dataset
        def crash(*args):
            original(*args)
            raise KeyboardInterrupt("simulated crash after final files")
        with patch.object(self.imports, "_publish_dataset", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.register(valid)
        self.assertEqual(len(self.store.list_datasets()), 1)
        interrupted = self.imports.get(valid["id"])
        self.assertEqual(interrupted["status"], "interrupted")
        operation_id = interrupted["registration_attempts"][0]["id"]
        result = self.register(valid)
        self.assertEqual(result["status"], "registered")
        self.assertEqual(result["registration_attempts"][0]["id"], operation_id)
        self.assertEqual(len(self.store.list_datasets()), 2)
        self.assertTrue(any(event["kind"] == "registration_interrupted" for event in result["events"]))

    def test_existing_dataset_files_are_never_overwritten_on_registration(self):
        valid = self.validate()
        original = self.imports._publish_dataset
        def corrupt_published(*args):
            identity, folder = original(*args)
            (folder / "market.csv").write_text("damaged immutable publication")
            raise KeyboardInterrupt("simulated crash and disk damage")
        with patch.object(self.imports, "_publish_dataset", side_effect=corrupt_published):
            with self.assertRaises(KeyboardInterrupt):
                self.register(valid)
        self.imports.get(valid["id"])
        with self.assertRaisesRegex(DatasetImportError, "immutable dataset artifact"):
            self.register(valid)
        self.assertEqual(len(self.store.list_datasets()), 1)

    def test_rejects_size_path_and_symlink_boundaries(self):
        for csv, metadata in ((b"", self.metadata), (b"x" * (MAX_CSV_BYTES + 1), self.metadata), (self.csv, b"x" * (MAX_METADATA_BYTES + 1))):
            with self.assertRaises(DatasetImportError) as raised:
                self.upload(csv=csv, metadata=metadata)
            self.assertEqual(raised.exception.status, 413)
        with self.assertRaises(DatasetImportError) as raised:
            self.imports.get("../outside")
        self.assertEqual(raised.exception.status, 422)
        row = self.upload()
        inputs = self.store.root / "dataset_imports" / row["id"] / "inputs"
        (inputs / "market.csv").unlink()
        (inputs / "market.csv").symlink_to(FIXTURE / "market.csv")
        result = self.validate(row)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["latest_validation"]["report"]["checks"][0]["code"], "input_integrity_failed")

    def test_second_fixture_reproduces_exactly(self):
        output = self.base / "reproduced"
        subprocess.run([sys.executable, str(FIXTURE / "generate.py"), "--out", str(output)], check=True, capture_output=True)
        for name in ("market.csv", "metadata.json", "research_config.json"):
            self.assertEqual((output / name).read_bytes(), (FIXTURE / name).read_bytes())
        metadata = read_json(FIXTURE / "metadata.json")
        self.assertEqual(metadata["rows"], len(metadata["calendar_dates"]) * len(metadata["universe"]))


if __name__ == "__main__":
    unittest.main()
