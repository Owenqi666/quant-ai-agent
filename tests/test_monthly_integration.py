"""HTTP result binding, immutable artifacts, and genuine v9 migration."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from paper_alpha import monthly_workflow
from paper_alpha.evidence import sha256
from paper_alpha.research_protocol import presets
from paper_alpha.server import db
from paper_alpha.server.api import create_app
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest, read_json


class MonthlyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'workspace'
        self.client = TestClient(create_app(self.home))
        self.store = Store(self.home)
        self.protocol = self.client.post('/api/research-protocols', json={
            'title': 'Fixture convention', 'note': 'Automated software check',
            'config': presets()[1]['config']}).json()
        self.config = self.client.get('/api/monthly-experiments/defaults').json()['config']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def submit(self, **overrides):
        body = {'protocol_id': self.protocol['id'], 'protocol_digest': self.protocol['digest'],
                'config': self.config, 'idempotency_key': 'create-1', **overrides}
        result = self.client.post('/api/monthly-experiments', json=body)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(self.client.post('/api/monthly-experiments', json=body).json(), result.json())
        return '/api/monthly-experiments/' + result.json()['experiment_id']

    def execute(self):
        with worker_lock(self.home) as fd:
            self.assertTrue(Worker(self.store, fd).run_once())

    def test_complete_http_review_report_and_backup_restore(self):
        url = self.submit()
        status = self.client.get(url + '/status').json()
        self.assertFalse(status['integrity_checked'])
        self.assertEqual(status['status'], 'queued')
        self.execute()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        self.assertEqual(detail['experiment']['status'], 'completed', detail)
        self.assertTrue(detail['verification']['reference_passed'])
        self.assertEqual(detail['result']['source_id'], self.client.get('/api/monthly-experiments/defaults').json()['source_id'])
        target = detail['review_target']
        review_body = {**target, 'verdict': 'needs_revision', 'note': 'Synthetic software acceptance only.',
                       'source': 'automation', 'idempotency_key': 'review-1'}
        review = self.client.post(url + '/reviews', json=review_body)
        self.assertEqual(review.status_code, 201, review.text)
        report_body = {**target, 'idempotency_key': 'report-1'}
        report = self.client.post(url + '/reports', json=report_body)
        self.assertEqual(report.status_code, 201, report.text)
        frozen = report.json()
        self.assertEqual(frozen['reviews'], [review.json()])
        self.assertEqual(self.client.post(url + '/reviews', json={**review_body, 'idempotency_key': 'review-2', 'note': 'Later observation'}).status_code, 201)
        self.assertEqual(self.client.post(url + '/reports', json=report_body).json(), frozen)
        self.assertEqual(self.client.post(url + '/reviews', json={**review_body, 'idempotency_key': 'stale', 'result_digest': '0'*64}).status_code, 412)
        report_url = url + '/reports/' + frozen['id']
        self.assertEqual(self.client.get(report_url + '/json').json(), frozen)
        self.assertEqual(self.client.get(report_url + '/markdown').text, frozen['markdown'])
        create_backup(self.home, self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = MonthlyExperiments(Store(self.root / 'restored'))
        identity = detail['experiment']['id']
        self.assertEqual(restored.get(identity), self.client.get(url).json())
        self.assertEqual(restored.get_report(identity, frozen['id']), frozen)
        from paper_alpha.server.diagnostics import diagnose
        diagnostics = diagnose(self.root / 'restored')
        self.assertEqual(diagnostics['database']['monthly_counts'], {'completed': 1})
        self.assertGreater(diagnostics['capacity']['groups']['monthly']['files'], 0)

    def test_strict_requests_and_paper_blocked(self):
        for config in ({**self.config, 'split': 'test'}, {**self.config, 'cost_bps': True},
                       {**self.config, 'end_month': '2028-01'}, {**self.config, 'min_assets': 8}):
            body = {'protocol_id': self.protocol['id'], 'protocol_digest': self.protocol['digest'], 'config': config, 'idempotency_key': 'invalid'}
            self.assertEqual(self.client.post('/api/monthly-experiments', json=body).status_code, 422)
        paper = self.client.post('/api/research-protocols', json={'title': 'Unresolved original paper',
                    'note': '', 'config': presets()[0]['config']}).json()
        url = self.submit(protocol_id=paper['id'], protocol_digest=paper['digest'])
        self.execute()
        detail = self.client.get(url).json()
        self.assertEqual(detail['experiment']['status'], 'completed', detail)
        self.assertEqual(detail['result']['status'], 'blocked')
        self.assertIsNone(detail['verification']['reference_passed'])
        self.assertTrue(all(x['terminal_nav_proxy'] is None for x in detail['result']['summary']))

    def test_artifact_tamper_rehashed_result_and_deleted_source_rejected(self):
        url = self.submit()
        self.execute()
        detail = self.client.get(url).json()
        output = self.home / 'monthly' / detail['experiment']['id'] / 'attempts' / detail['review_target']['attempt_id'] / 'output'
        with self.assertRaises(FileExistsError):
            monthly_workflow.run(output / 'input.json', output)
        original = read_json(output / 'manifest.json')
        result = read_json(output / 'result.json')
        result['summary'][0]['mean_gross_return'] += .01
        atomic_json(output / 'result.json', result)
        manifest = {**original, 'files': {**original['files'], 'result.json': sha256(output / 'result.json')}, 'result_digest': digest(result)}
        atomic_json(output / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'reference'):
            monthly_workflow.verify(output)
        self.assertEqual(self.client.get(url).status_code, 409)
        manifest['files'].pop('source/paper_alpha/monthly_evaluation.py')
        (output / 'source/paper_alpha/monthly_evaluation.py').unlink()
        atomic_json(output / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            monthly_workflow.verify(output)

    def test_paper_result_cannot_smuggle_numbers_through_unsupported_reference(self):
        paper = self.client.post('/api/research-protocols', json={'title': 'Paper blocked',
                    'note': '', 'config': presets()[0]['config']}).json()
        url = self.submit(protocol_id=paper['id'], protocol_digest=paper['digest'])
        self.execute()
        detail = self.client.get(url).json()
        output = self.home / 'monthly' / detail['experiment']['id'] / 'attempts' / detail['review_target']['attempt_id'] / 'output'
        result = read_json(output / 'result.json')
        result['summary'][0]['terminal_nav_proxy'] = 100.0
        atomic_json(output / 'result.json', result)
        (output / 'report.md').write_text(monthly_workflow.render_report(result))
        manifest = read_json(output / 'manifest.json')
        manifest['result_digest'] = digest(result)
        for name in ('result.json', 'report.md'):
            manifest['files'][name] = sha256(output / name)
        atomic_json(output / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'reference'):
            monthly_workflow.verify(output)

    def test_json_download_uses_response_validation(self):
        with patch.object(MonthlyExperiments, 'get_report', return_value={'unexpected': True}):
            response = self.client.get('/api/monthly-experiments/anything/reports/anything/json')
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('unexpected', response.text)

    def test_cli_rejects_rehashed_unknown_rule_semantics(self):
        from paper_alpha.monthly_evaluation import demo_bundle
        protocol = {**self.protocol, 'semantics_version': 'future-unknown-rules'}
        protocol['digest'] = digest({key: protocol[key] for key in monthly_workflow.PROTOCOL_BODY})
        protocol['id'] = 'protocol_' + protocol['digest']
        value = {'schema_version': 1, 'protocol': protocol, 'config': self.config,
                 'bundle': demo_bundle(self.config, protocol['config'])}
        atomic_json(self.root / 'input.json', value)
        with self.assertRaisesRegex(ValueError, 'protocol digest mismatch'):
            monthly_workflow.run(self.root / 'input.json', self.root / 'rejected')
        self.assertFalse((self.root / 'rejected').exists())


class MonthlyMigrationTests(unittest.TestCase):
    def test_genuine_v9_rollback_and_old_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workbench.sqlite3'
            with closing(db.connect(path)) as connection:
                connection.executescript(db.SCHEMA)
                connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
                for index in range(1, 9):
                    getattr(db, '_migrate_v' + str(index))(connection)
                connection.execute("UPDATE settings SET value='9' WHERE key='schema_version'")
                connection.execute("INSERT INTO papers VALUES ('old','Untouched','hash','now','{  \"raw\": true }','paper.pdf')")
                snapshot = '\n'.join(connection.iterdump())
            original = db._migrate_v9
            def fail(connection):
                original(connection)
                raise RuntimeError('Injected monthly migration failure')
            with patch.object(db, '_migrate_v9', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Injected'):
                db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual('\n'.join(connection.iterdump()), snapshot)
            db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT document FROM papers').fetchone()[0], '{  "raw": true }')
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM monthly_experiments').fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
                self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
