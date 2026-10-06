"""Genuine schema-six upgrade, preserving bytes and rolling back schema seven."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.server import db
from paper_alpha.storage import digest


class ObservationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'workbench.sqlite3'
        with closing(db.connect(self.path)) as connection:
            connection.executescript(db.SCHEMA)
            connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
            for migrate in (db._migrate_v1, db._migrate_v2, db._migrate_v3, db._migrate_v4, db._migrate_v5):
                migrate(connection)
            connection.execute("UPDATE settings SET value='6' WHERE key='schema_version'")
            connection.execute("INSERT INTO papers VALUES ('paper','Paper','hash','now','{}','paper.pdf')")
            connection.execute("INSERT INTO datasets VALUES ('data','Data','hash','{}','market.csv','metadata.json')")
            connection.execute("INSERT INTO researches VALUES ('research','Research','paper','data','now','revision')")
            connection.execute("INSERT INTO revisions VALUES ('revision','research',1,'{}','Unchanged task','digest','now')")
            payload = '{  "frozen": true, "note": "原始字节"  }'
            connection.execute('INSERT INTO research_reports VALUES (?,?,?,?,?,?,?)',
                               ('report','research','now','report-key','request',payload,digest({'frozen':True,'note':'原始字节'})))
            response = '{ "id":"research", "legacy": true }'
            connection.execute('INSERT INTO mutation_receipts VALUES (?,?,?,?,?,?,?,?,?)',
                ('research.create','create-key','request',response,digest({'id':'research','legacy':True}),None,None,'research','now'))
            self.report = dict(connection.execute('SELECT * FROM research_reports').fetchone())
            self.receipt = dict(connection.execute('SELECT * FROM mutation_receipts').fetchone())

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return '\n'.join(connection.iterdump())

    def test_upgrade_preserves_existing_bytes_and_makes_no_observations(self):
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            self.assertEqual(dict(connection.execute('SELECT * FROM research_reports').fetchone()), self.report)
            self.assertEqual(dict(connection.execute('SELECT * FROM mutation_receipts').fetchone()), self.receipt)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM workflow_observations').fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        before = self.snapshot()
        db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)

    def test_failed_migration_rolls_back_new_table_index_and_version_then_retries(self):
        before = self.snapshot()
        migrate = db._migrate_v6
        def fail(connection):
            migrate(connection)
            raise RuntimeError('Injected observation migration failure')
        with patch.object(db, '_migrate_v6', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'Injected observation'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM workflow_observations').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
