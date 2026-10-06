"""Preserving stored research and safe concurrent startup across schema upgrades."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha.server import db
from paper_alpha.storage import digest


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'workbench.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def legacy_database(self):
        connection = db.connect(self.path)
        try:
            connection.executescript(db.SCHEMA)
            connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
            connection.execute("INSERT INTO papers VALUES ('paper','Paper','hash','now','{}','paper.pdf')")
            connection.execute("INSERT INTO datasets VALUES ('data','Data','hash','{}','market.csv','metadata.json')")
            connection.execute("INSERT INTO researches VALUES ('research','Research','paper','data','now','revision')")
            connection.execute("INSERT INTO revisions VALUES ('revision','research',1,'{}','Original task','digest','now')")
            connection.execute("INSERT INTO runs(id,revision_id,research_id,mode,status,created_at,idempotency_key,request_digest) VALUES ('run','revision','research','fixed','completed','now','key','digest')")
            connection.execute("INSERT INTO attempts(id,run_id,number,worker_id,status,started_at,output_dir) VALUES ('attempt','run',1,'worker','completed','now','output')")
            connection.execute("INSERT INTO reviews VALUES ('review','run','revision','attempt','alpha006','accepted','implementation','Original review','result-digest','now')")
            connection.execute("INSERT INTO regression_cases VALUES ('case','review','research','paper','data','alpha006','evaluated','Original approval','now',1,1)")
            connection.execute("INSERT INTO regression_checks VALUES ('check','run','now','{\"passed\":true,\"scope\":\"status only\"}')")
        finally:
            connection.close()

    def snapshot(self):
        connection = sqlite3.connect(self.path)
        try:
            return '\n'.join(connection.iterdump())
        finally:
            connection.close()

    def test_concurrent_fresh_initialization(self):
        # API and worker start concurrently; the WAL pragma must also be fenced.
        for index in range(12):
            path = self.root / f'concurrent-{index}.sqlite3'
            barrier = threading.Barrier(4)
            def initialize(_):
                barrier.wait(timeout=10)
                db.initialize(path)
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(initialize, range(4)))
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone(), (str(db.SCHEMA_VERSION),))
                self.assertEqual(connection.execute('SELECT count(*) FROM schema_migrations').fetchone(), (db.SCHEMA_VERSION - 1,))
                self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            finally:
                connection.close()

    def test_v1_upgrade_preserves_records_and_requires_legacy_reapproval(self):
        self.legacy_database()
        db.initialize(self.path)
        connection = db.connect(self.path)
        try:
            case = dict(connection.execute('SELECT * FROM regression_cases').fetchone())
            self.assertEqual(case['id'], 'case')
            self.assertEqual(case['review_id'], 'review')
            self.assertEqual(case['note'], 'Original approval')
            self.assertEqual(case['version'], 1)
            self.assertIsNone(case['contract'])
            self.assertIsNone(case['contract_digest'])
            self.assertEqual(connection.execute('SELECT note FROM reviews').fetchone()[0], 'Original review')
            self.assertEqual(connection.execute('SELECT source FROM reviews').fetchone()[0], 'legacy_unknown')
            self.assertIsNone(connection.execute('SELECT assessment FROM reviews').fetchone()[0])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mutation_receipts').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT task FROM revisions').fetchone()[0], '{}')
            self.assertEqual(connection.execute('SELECT payload FROM regression_checks').fetchone()[0], '{"passed":true,"scope":"status only"}')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            connection.close()

    def test_unknown_future_schema_is_rejected_without_database_changes(self):
        self.legacy_database()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE settings SET value='999' WHERE key='schema_version'")
            connection.commit()
        finally:
            connection.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
            db.initialize(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_migration_rolls_back_ddl_and_history(self):
        self.legacy_database()
        before = self.snapshot()
        migrate = db._migrate_v1
        def fail_after_columns(connection):
            migrate(connection)
            raise RuntimeError('Injected upgrade failure')
        with patch.object(db, '_migrate_v1', side_effect=fail_after_columns):
            with self.assertRaisesRegex(RuntimeError, 'Injected upgrade failure'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        # Failure must release the migration fence and leave the database usable.
        db.initialize(self.path)

    def test_reinitialization_is_idempotent(self):
        self.legacy_database()
        db.initialize(self.path)
        before = self.snapshot()
        db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)

    def test_v2_upgrade_failure_is_atomic_and_retry_preserves_reviews(self):
        self.legacy_database()
        with db.transaction(self.path) as connection:
            db._migrate_v1(connection)
            connection.execute("UPDATE settings SET value='2' WHERE key='schema_version'")
        before = self.snapshot()
        migrate = db._migrate_v2
        def fail_after_schema(connection):
            migrate(connection)
            raise RuntimeError('Injected v3 migration failure')
        with patch.object(db, '_migrate_v2', side_effect=fail_after_schema):
            with self.assertRaisesRegex(RuntimeError, 'Injected v3'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT source FROM reviews').fetchone()[0], 'legacy_unknown')
            self.assertIsNone(connection.execute('SELECT assessment FROM reviews').fetchone()[0])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM dataset_imports').fetchone()[0], 0)


    def test_v3_assessment_migration_rolls_back_and_preserves_legacy_null(self):
        self.legacy_database()
        with db.transaction(self.path) as connection:
            db._migrate_v1(connection)
            db._migrate_v2(connection)
            connection.execute("UPDATE settings SET value='3' WHERE key='schema_version'")
        before = self.snapshot()
        migrate = db._migrate_v3
        def fail_after_column(connection):
            migrate(connection)
            raise RuntimeError('Injected v4 assessment failure')
        with patch.object(db, '_migrate_v3', side_effect=fail_after_column):
            with self.assertRaisesRegex(RuntimeError, 'Injected v4'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            review = dict(connection.execute('SELECT * FROM reviews').fetchone())
            self.assertEqual(review['note'], 'Original review')
            self.assertEqual(review['source'], 'legacy_unknown')
            self.assertIsNone(review['assessment'])
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))

    def test_v4_receipt_migration_rolls_back_then_preserves_legacy_without_fake_keys(self):
        self.legacy_database()
        with db.transaction(self.path) as connection:
            db._migrate_v1(connection)
            db._migrate_v2(connection)
            db._migrate_v3(connection)
            connection.execute("UPDATE settings SET value='4' WHERE key='schema_version'")
        before = self.snapshot()
        migrate = db._migrate_v4
        def fail_after_receipts(connection):
            migrate(connection)
            raise RuntimeError('Injected v5 receipt failure')
        with patch.object(db, '_migrate_v4', side_effect=fail_after_receipts):
            with self.assertRaisesRegex(RuntimeError, 'Injected v5'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mutation_receipts').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT id,note FROM reviews').fetchone()[:], ('review', 'Original review'))
            self.assertEqual(connection.execute('SELECT id,note FROM regression_cases').fetchone()[:], ('case', 'Original approval'))
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_v5_migration_preserves_receipt_bytes_and_rolls_back_both_new_tables(self):
        self.legacy_database()
        # Use genuine schema-five constraints, not a downgraded schema-six table.
        with db.transaction(self.path) as connection:
            db._migrate_v1(connection)
            db._migrate_v2(connection)
            db._migrate_v3(connection)
            db._migrate_v4(connection)
            connection.execute("UPDATE settings SET value='5' WHERE key='schema_version'")
            for operation, resource, review_id, case_id in (
                    ('review.create', 'review', 'review', None),
                    ('case.approve', 'case', None, 'case')):
                response = '{  "id": "' + resource + '", "note":"原始响应", "created_at": "now" }'
                connection.execute('INSERT INTO mutation_receipts VALUES (?,?,?,?,?,?,?,?)',
                    (operation, 'same-key', 'request-digest', response,
                     digest({'id': resource, 'note': '原始响应', 'created_at': 'now'}), review_id, case_id, 'now'))
            receipts = [dict(row) for row in connection.execute('SELECT * FROM mutation_receipts ORDER BY operation')]
        before = self.snapshot()
        migrate = db._migrate_v5
        def fail_after_both_tables(connection):
            migrate(connection)
            connection.execute('SELECT * FROM research_reports')
            raise RuntimeError('Injected v6 migration failure')
        with patch.object(db, '_migrate_v5', side_effect=fail_after_both_tables):
            with self.assertRaisesRegex(RuntimeError, 'Injected v6'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        # Simulate the reports DDL failing after the receipts table was rebuilt.
        with patch('paper_alpha.server.research_insights_schema.REPORT_SCHEMA', 'CREATE TABLE invalid syntax'):
            with self.assertRaises(sqlite3.OperationalError):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        db.initialize(self.path)
        from paper_alpha.server import mutations
        with closing(db.connect(self.path)) as connection:
            migrated = [dict(row) for row in connection.execute('SELECT * FROM mutation_receipts ORDER BY operation')]
            self.assertEqual(migrated, [{**row, 'research_id': None} for row in receipts])
            for row in receipts:
                replay = mutations.replay(connection, row['operation'], row['idempotency_key'], row['request_digest'])
                self.assertEqual(replay['id'], row['review_id'] or row['case_id'])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM research_reports').fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mutation_receipts WHERE operation='research.create'").fetchone()[0], 0)
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))


if __name__ == '__main__':
    unittest.main()
