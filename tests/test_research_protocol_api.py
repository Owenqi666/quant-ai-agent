"""HTTP, genuine schema-eight migration, and immutable diagnostic integration."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from paper_alpha.research_protocol import presets
from paper_alpha.server import db
from paper_alpha.server.api import create_app
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.service import Store
from scripts.preview_research_protocol import freeze, verify
from paper_alpha.storage import read_json, atomic_json


class ResearchProtocolApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = create_app(self.root / 'workspace')
        self.client = TestClient(self.app)
        self.config = presets()[1]['config']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_http_preview_save_parent_diff_and_backup_replay(self):
        result = self.client.post('/api/research-protocols/preview', json={'config': self.config, 'target_month': '2026-07'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['windows']['momentum'], {'start': '2025-07-01', 'end': '2026-05-31'})
        body = {'title': 'Explicit project convention', 'note': 'Fixture only', 'config': self.config}
        saved = self.client.post('/api/research-protocols', json=body)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()['config_digest'], result.json()['config_digest'])
        self.assertEqual(self.client.post('/api/research-protocols', json=body).json(), saved.json())
        child = self.client.post('/api/research-protocols', json={**body, 'parent_id': saved.json()['id'],
                            'config': {**self.config, 'mom_window_months': 6}}).json()
        self.assertEqual(child['changes'], [{'field': 'mom_window_months', 'before': 11, 'after': 6}])
        self.assertEqual(self.client.get('/api/research-protocols').json()['total'], 2)
        create_backup(self.root / 'workspace', self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = ResearchProtocols(Store(self.root / 'restored'))
        self.assertEqual(restored.get(child['id']), child)
        self.assertEqual(restored.create(**body), saved.json())

    def test_paper_blocked_and_request_validation_is_strict(self):
        paper = self.client.get('/api/research-protocols/presets').json()['presets'][0]['config']
        report = self.client.post('/api/research-protocols/preview', json={'config': paper, 'target_month': '2026-07'})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()['status'], 'blocked')
        self.assertTrue(all(row['id'] is None for row in report.json()['assets']))
        for config in ({**self.config, 'schema_version': True}, {**self.config, 'mom_skip_months': 1.0},
                       {**self.config, 'extra': 1}, {**paper, 'id_window_months': 11}):
            with self.subTest(config=config):
                self.assertEqual(self.client.post('/api/research-protocols/preview',
                    json={'config': config, 'target_month': '2026-07'}).status_code, 422)
        self.assertEqual(self.client.get('/api/research-protocols?limit=101').status_code, 422)
        invalid = self.client.post('/api/research-protocols/preview',
            json={'config': self.config, 'target_month': '1800-01'})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('invalid_month: invalid_month', invalid.text)

    def test_frozen_cli_diagnostic_replays_and_rejects_changed_input(self):
        out = self.root / 'diagnostic'
        self.assertTrue(freeze(out, self.config, '2026-07')['verified'])
        self.assertTrue(verify(out)['verified'])
        with self.assertRaises(FileExistsError):
            freeze(out, self.config, '2026-07')
        with (out / 'input.json').open('a') as stream:
            stream.write(' ')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            verify(out)

    def test_frozen_source_declaration_is_checked_against_bound_core(self):
        out = self.root / 'diagnostic'
        freeze(out, self.config, '2026-07')
        manifest = read_json(out / 'manifest.json')
        manifest['sources'] = ['Forged provenance']
        atomic_json(out / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'source declarations'):
            verify(out)

    def test_large_valid_bundle_roundtrips_without_pretty_json_expansion(self):
        from datetime import date, timedelta
        from paper_alpha.research_protocol import MAX_BUNDLE_BYTES
        import json
        sessions = [(date(2000, 1, 1) + timedelta(days=i)).isoformat() for i in range(5000)]
        assets = [{'id': str(i).zfill(90), 'history_start': sessions[0]} for i in range(20)]
        bundle = {'schema_version': 1, 'data_kind': 'controlled_fixture', 'source_id': 'size-boundary',
                  'return_semantics': 'daily_total_return_decimal',
                  'calendar': {'id': 'fictional-every-day', 'version': '1', 'start': sessions[0], 'end': sessions[-1], 'sessions': sessions},
                  'assets': assets, 'returns': [{'date': day, 'asset': asset['id'], 'value': 0.0} for asset in assets for day in sessions]}
        self.assertGreater(len(json.dumps(bundle, indent=2).encode()), MAX_BUNDLE_BYTES)
        out = self.root / 'large-diagnostic'
        self.assertTrue(freeze(out, self.config, '2013-07', bundle)['verified'])
        self.assertLessEqual((out / 'input.json').stat().st_size, MAX_BUNDLE_BYTES)


class ResearchProtocolMigrationTests(unittest.TestCase):
    def test_genuine_v8_failure_rolls_back_and_upgrade_preserves_payload_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workbench.sqlite3'
            with closing(db.connect(path)) as connection:
                connection.executescript(db.SCHEMA)
                connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
                for migration in (db._migrate_v1, db._migrate_v2, db._migrate_v3, db._migrate_v4,
                                  db._migrate_v5, db._migrate_v6, db._migrate_v7):
                    migration(connection)
                connection.execute("UPDATE settings SET value='8' WHERE key='schema_version'")
                connection.execute("INSERT INTO papers VALUES ('paper','Untouched','hash','now','{  \"raw\": true }','paper.pdf')")
                snapshot = '\n'.join(connection.iterdump())
            original = db._migrate_v8
            def fail(connection):
                original(connection)
                raise RuntimeError('Injected protocol migration failure')
            with patch.object(db, '_migrate_v8', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Injected'):
                db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual('\n'.join(connection.iterdump()), snapshot)
            db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT document FROM papers').fetchone()[0], '{  "raw": true }')
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM research_protocols').fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
                self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
