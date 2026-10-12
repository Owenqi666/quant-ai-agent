"""Real SQLite/assets recovery, lock fencing and hostile backup boundaries."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server import backup as backup_module
from paper_alpha.server.api import create_app
from paper_alpha.server.backup import BackupError, create_backup, restore_backup, verify_backup
from paper_alpha.server.maintenance import workspace_lease
from paper_alpha.server.runner import worker_lock
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, read_json
from paper_alpha.workflow import run_task


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "original"
        self.backup = self.base / "backup"
        self.restored = self.base / "restored"
        self.store = Store(self.home)
        self.example = self.store.seed_example()

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, store, revision_id, key):
        submitted = store.submit_run(revision_id, "normalized_fixed", key)
        job = store.claim("backup-test-worker")
        state = run_task(job["task_path"], job["output_dir"], mode="normalized_fixed")
        store.finish(submitted["id"], "backup-test-worker", job["attempt_id"], state["status"])
        return store.get_run(submitted["id"]), job

    def test_portable_restore_preserves_verified_outputs_and_feedback(self):
        run, job = self.execute(self.store, self.example["revision_id"], "before-backup")
        self.assertTrue(run["verification"]["verified"])
        review = self.store.create_review(run["id"], "alpha006", "accepted", "implementation", "Automated backup fixture review")
        case = self.store.approve_case(review["id"], "evaluated", "Explicit backup fixture expectation")
        check = self.store.run_regression_check(run["id"], [case["id"]])
        self.assertTrue(check["passed"])
        events = self.store.events(run["id"])
        report = self.store.report_path(run["id"]).read_bytes()
        snapshot = {p.relative_to(Path(job["output_dir"])).as_posix(): p.read_bytes()
                    for p in Path(job["output_dir"]).rglob("*") if p.is_file()}
        manifest = create_backup(self.home, self.backup)
        self.assertGreater(len(manifest["files"]), 20)
        self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in manifest["files"]))
        restore = restore_backup(self.backup, self.restored)
        self.assertTrue(restore["restored"])
        restored = Store(self.restored)
        self.assertEqual(restored._safe_error(f"Historical input {self.home}/runs/example"), "Historical input [workbench]/runs/example")
        restored_run = restored.get_run(run["id"])
        self.assertTrue(restored_run["verification"]["verified"], restored_run["verification"])
        self.assertEqual(restored_run["reviews"], run["reviews"] + [review])
        self.assertEqual(restored.list_cases(), self.store.list_cases())
        self.assertEqual(restored.list_checks(), [check])
        self.assertEqual(restored.events(run["id"]), events)
        self.assertEqual(restored.report_path(run["id"]).read_bytes(), report)
        restored_output = self.restored / Path(job["output_dir"]).relative_to(self.home)
        self.assertEqual({p.relative_to(restored_output).as_posix(): p.read_bytes()
                          for p in restored_output.rglob("*") if p.is_file()}, snapshot)
        self.assertTrue(all(str(self.restored) in row["path"] for row in restored._read("SELECT path FROM artifacts")))
        # The restored workspace can conduct a new experiment using the original
        # reviewed case, without dereferencing paths in the original workspace.
        offline_original = self.base / "original-offline"
        self.home.rename(offline_original)
        next_run, _ = self.execute(restored, self.example["revision_id"], "after-backup")
        self.assertTrue(next_run["verification"]["verified"])
        self.assertTrue(restored.run_regression_check(next_run["id"], [case["id"]])["passed"])
        self.assertTrue(restored.get_run(run["id"])["verification"]["verified"])
        self.assertEqual(verify_backup(self.backup), manifest)

    def test_sqlite_snapshot_includes_committed_wal_records(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO settings VALUES ('wal_test','committed')")
            connection.commit()
            self.assertTrue(Path(str(self.store.db_path) + "-wal").exists())
            create_backup(self.home, self.backup)
        restore_backup(self.backup, self.restored)
        with closing(sqlite3.connect(self.restored / "workbench.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='wal_test'").fetchone(), ("committed",))

    def test_active_service_and_worker_leases_block_backup(self):
        with workspace_lease(self.home):
            with self.assertRaisesRegex(RuntimeError, "Stop the API"):
                create_backup(self.home, self.backup)
        with worker_lock(self.home):
            with self.assertRaisesRegex(BackupError, "worker or its engine child"):
                create_backup(self.home, self.backup)
        self.assertFalse(self.backup.exists())
        with workspace_lease(self.home, exclusive=True):
            with self.assertRaisesRegex(RuntimeError, "maintenance is active"):
                with workspace_lease(self.home):
                    pass

    def test_api_lifespan_and_store_initialization_respect_maintenance(self):
        with TestClient(create_app(self.home)) as client:
            self.assertEqual(client.get("/api/health").status_code, 200)
            with self.assertRaisesRegex(RuntimeError, "Stop the API"):
                create_backup(self.home, self.backup)
        with workspace_lease(self.home, exclusive=True):
            with self.assertRaisesRegex(RuntimeError, "maintenance is active"):
                Store(self.home)
        create_backup(self.home, self.backup)

    def test_partial_restoration_cannot_initialize_or_migrate_store(self):
        self.restored.mkdir()
        (self.restored / ".restore-incomplete").write_text("incomplete")
        with self.assertRaisesRegex(RuntimeError, "restore is incomplete"):
            Store(self.restored)
        self.assertFalse((self.restored / "workbench.sqlite3").exists())

    def test_restore_lease_fences_startup_before_marker_exists(self):
        create_backup(self.home, self.backup)
        new_directory = backup_module._new_directory
        def concurrent_start(path):
            new_directory(path)
            self.assertFalse((path / ".restore-incomplete").exists())
            with self.assertRaisesRegex(RuntimeError, "maintenance is active"):
                Store(path)
            self.assertFalse((path / "workbench.sqlite3").exists())
        with patch.object(backup_module, "_new_directory", side_effect=concurrent_start):
            restore_backup(self.backup, self.restored)
        self.assertEqual(len(Store(self.restored).list_researches()), 1)

    def test_backup_tampering_or_extra_files_reject_restore(self):
        create_backup(self.home, self.backup)
        manifest = read_json(self.backup / "manifest.json")
        name = next(name for name in manifest["files"] if name.endswith("market.csv"))
        asset = self.backup / "files" / name
        original = asset.read_bytes()
        asset.write_bytes(original + b"tampering")
        with self.assertRaisesRegex(BackupError, "checksum mismatch"):
            restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())
        asset.write_bytes(original)
        (self.backup / "untracked").write_text("untracked")
        with self.assertRaisesRegex(BackupError, "untracked"):
            verify_backup(self.backup)

    def test_manifest_traversal_and_symlinks_are_rejected(self):
        create_backup(self.home, self.backup)
        manifest = read_json(self.backup / "manifest.json")
        modified = {**manifest, "directories": [*manifest["directories"], "../outside"]}
        atomic_json(self.backup / "manifest.json", modified)
        with self.assertRaisesRegex(BackupError, "inventory path"):
            restore_backup(self.backup, self.restored)
        atomic_json(self.backup / "manifest.json", manifest)
        asset = next((self.backup / "files").rglob("market.csv"))
        asset.unlink()
        asset.symlink_to(self.home / asset.relative_to(self.backup / "files"))
        with self.assertRaisesRegex(BackupError, "regular files"):
            restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())

    def test_source_symlinks_special_files_and_path_aliases_are_rejected(self):
        link = self.home / "unexpected-link"
        link.symlink_to(self.base)
        with self.assertRaisesRegex(BackupError, "regular files"):
            create_backup(self.home, self.backup)
        link.unlink()
        fifo = self.home / "unexpected-fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(BackupError, "regular files"):
            create_backup(self.home, self.backup)
        fifo.unlink()
        alias = self.base / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(BackupError, "Symlinks"):
            create_backup(alias, self.backup)

    def test_existing_and_nested_destinations_are_never_overwritten(self):
        with self.assertRaisesRegex(BackupError, "non-nested"):
            create_backup(self.home, self.home / "backup")
        create_backup(self.home, self.backup)
        before = (self.backup / "manifest.json").read_bytes()
        with self.assertRaisesRegex(BackupError, "already exists"):
            create_backup(self.home, self.backup)
        with self.assertRaisesRegex(BackupError, "already exists"):
            restore_backup(self.backup, self.home)
        with self.assertRaisesRegex(BackupError, "non-nested"):
            restore_backup(self.backup, self.backup / "restored")
        self.assertEqual((self.backup / "manifest.json").read_bytes(), before)
        self.assertEqual(len(self.store.list_researches()), 1)

    def test_interrupt_cleans_only_new_destination(self):
        create_backup(self.home, self.backup)
        copy = backup_module._copy
        def interrupt_copy(source, destination=None):
            if destination is not None:
                self.assertTrue((self.restored / ".restore-incomplete").is_file())
                raise KeyboardInterrupt("simulated interruption")
            return copy(source, destination)
        with patch.object(backup_module, "_copy", side_effect=interrupt_copy):
            with self.assertRaises(KeyboardInterrupt):
                restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())
        self.assertTrue(verify_backup(self.backup))
        self.assertEqual(len(self.store.list_researches()), 1)

    def test_incomplete_restore_is_not_a_backup(self):
        (self.home / ".restore-incomplete").write_text("incomplete")
        with self.assertRaisesRegex(BackupError, "incomplete"):
            create_backup(self.home, self.backup)
        self.backup.mkdir()
        (self.backup / "files").mkdir()
        with self.assertRaisesRegex(BackupError, "incomplete"):
            restore_backup(self.backup, self.restored)

    def test_database_paths_outside_workspace_are_rejected(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute("UPDATE papers SET pdf_path='/outside/paper.pdf'")
            connection.commit()
        create_backup(self.home, self.backup)
        with self.assertRaisesRegex(BackupError, "outside the original workspace"):
            restore_backup(self.backup, self.restored)
        self.assertFalse(self.restored.exists())

    def test_unsupported_database_version_does_not_publish_backup(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute("UPDATE settings SET value='999' WHERE key='schema_version'")
            connection.commit()
        with self.assertRaisesRegex(BackupError, "Unsupported"):
            create_backup(self.home, self.backup)
        self.assertFalse(self.backup.exists())

    def test_cli_verify_and_missing_destination_errors(self):
        create_backup(self.home, self.backup)
        verified = subprocess.run([sys.executable, "-m", "paper_alpha.server.backup", "verify", "--backup", str(self.backup)],
                                  capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(verified.stdout)["verified"])
        failed = subprocess.run([sys.executable, "-m", "paper_alpha.server.backup", "restore", "--backup", str(self.backup), "--home", str(self.home)],
                                capture_output=True, text=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("never overwrites", failed.stderr)


if __name__ == "__main__":
    unittest.main()
