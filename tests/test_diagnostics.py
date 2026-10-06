from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.server.diagnostics import capacity, diagnose, main
from paper_alpha.server.maintenance import workspace_lease
from paper_alpha.server.service import Store


def snapshot(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file() and not p.is_symlink()}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'workspace'

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_workspace_is_not_created(self):
        before = snapshot(self.base)
        self.assertEqual(diagnose(self.home)['status'], 'missing_or_unsafe_workspace')
        self.assertFalse(self.home.exists())
        self.assertEqual(snapshot(self.base), before)

    def test_quiescent_read_preserves_bytes_and_omits_private_content(self):
        store = Store(self.home)
        example = store.seed_example()
        run = store.submit_run(example['revision_id'], 'fixed', 'diagnosis')
        with closing(sqlite3.connect(store.db_path)) as db:
            db.execute("UPDATE runs SET status='failed',error=? WHERE id=?", ('/private/customer/token-secret', run['id']))
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = snapshot(self.base)
        result = diagnose(self.home)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['database']['run_counts'], {'failed': 1})
        self.assertEqual(result['database']['recent_failures'][0]['id'], run['id'])
        self.assertNotIn('token-secret', json.dumps(result))
        self.assertNotIn(str(self.home), json.dumps(result))
        self.assertEqual(snapshot(self.base), before)

    def test_live_read_includes_committed_wal(self):
        store = Store(self.home)
        with workspace_lease(self.home), closing(sqlite3.connect(store.db_path)) as writer:
            writer.execute("INSERT INTO workers VALUES ('diagnostic-worker', 1)")
            writer.commit()
            result = diagnose(self.home)
            self.assertEqual(result['database']['snapshot'], 'live_read_transaction')
            self.assertGreater(result['database']['worker']['heartbeat_age_seconds'], 30)
            self.assertFalse(result['database']['worker']['recent_heartbeat'])

    def test_capacity_is_bounded_and_never_follows_links(self):
        Store(self.home)
        outside = self.base / 'private'
        outside.write_text('secret')
        (self.home / 'private-link').symlink_to(outside)
        result = diagnose(self.home)
        self.assertEqual(result['capacity']['skipped_links_or_special_files'], 1)
        self.assertNotIn('private-link', json.dumps(result))
        limited = diagnose(self.home, max_entries=1)
        self.assertFalse(limited['capacity']['complete'])
        self.assertEqual(limited['status'], 'attention')

    def test_unknown_schema_not_migrated(self):
        store = Store(self.home)
        with closing(sqlite3.connect(store.db_path)) as db:
            db.execute("UPDATE settings SET value='999' WHERE key='schema_version'")
            db.commit()
        before = snapshot(self.base)
        result = diagnose(self.home)
        self.assertEqual(result['database']['status'], 'unsupported_schema')
        self.assertEqual(snapshot(self.base), before)

    def test_export_refuses_overwrite_or_workspace_destination(self):
        Store(self.home)
        with self.assertRaises(SystemExit):
            main(['--home', str(self.home), '--out', str(self.home / 'diagnostic.json')])
        output = self.base / 'report.json'
        output.write_text('old')
        with self.assertRaises(SystemExit):
            main(['--home', str(self.home), '--out', str(output)])
        self.assertEqual(output.read_text(), 'old')

    def test_fifo_lock_never_blocks_diagnostic(self):
        Store(self.home)
        os.mkfifo(self.home / '.worker.lock')
        done = subprocess.run([sys.executable, '-m', 'paper_alpha.server.diagnostics', '--home', str(self.home)],
                              capture_output=True, text=True, timeout=3)
        self.assertEqual(json.loads(done.stdout)['locks']['worker_exclusive_probe'], 'unsafe')

    def test_directory_replaced_by_symlink_during_scan_is_not_followed(self):
        self.home.mkdir()
        (self.home / 'runs').mkdir()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret').write_bytes(b'x' * 12345)
        real_open = os.open
        def replace_then_open(path, *args, **kwargs):
            if path == 'runs':
                (self.home / 'runs').rmdir()
                (self.home / 'runs').symlink_to(outside, target_is_directory=True)
            return real_open(path, *args, **kwargs)
        with patch('paper_alpha.server.diagnostics.os.open', side_effect=replace_then_open):
            result = capacity(self.home)
        self.assertEqual(result['files'], 0)
        self.assertFalse(result['complete'])

    def test_corrupt_heartbeat_returns_attention_without_echo(self):
        store = Store(self.home)
        with closing(sqlite3.connect(store.db_path)) as db:
            db.execute("INSERT INTO workers VALUES ('bad-clock', 'private-invalid-clock')")
            db.commit()
        result = diagnose(self.home)
        self.assertEqual(result['status'], 'attention')
        self.assertEqual(result['database']['status'], 'invalid_heartbeat')
        self.assertNotIn('private-invalid-clock', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
