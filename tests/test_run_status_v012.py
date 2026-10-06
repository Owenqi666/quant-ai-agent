"""Light probes do not claim verification; full reads use one WAL snapshot."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server.execution_lifecycle import record_phase
from paper_alpha.workflow import run_task


class RunStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / 'workspace')
        self.store = self.app.state.store
        self.client = TestClient(self.app)
        example = self.store.seed_example()
        self.run = self.store.submit_run(example['revision_id'], 'normalized_fixed', 'fixture')
        self.url = '/api/runs/' + self.run['id']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def execute(self):
        claim = self.store.claim('fixture-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        return self.store.finish(self.run['id'], 'fixture-worker', claim['attempt_id'], state['status'])

    def test_probe_reports_phase_without_heartbeat_churn_or_integrity_read(self):
        claim = self.store.claim('fixture-worker')
        with patch.object(self.store, '_check_integrity', side_effect=AssertionError('probe hashed files')):
            preparing = self.client.get(self.url + '/status')
            self.assertEqual(preparing.status_code, 200)
            self.assertEqual(preparing.json()['phase'], 'preparing')
            self.assertFalse(preparing.json()['integrity_checked'])
            self.store.heartbeat('fixture-worker', self.run['id'])
            self.assertEqual(self.client.get(self.url + '/status').json(), preparing.json())
            record_phase(self.store, self.run['id'], 'fixture-worker', claim['attempt_id'], 'executing')
            executing = self.client.get(self.url + '/status').json()
            self.assertEqual(executing['phase'], 'executing')
            self.assertNotEqual(executing['change_token'], preparing.json()['change_token'])
        self.assertEqual(self.client.get('/api/runs/missing/status').status_code, 404)

    def test_probe_does_not_detect_external_file_changes_but_full_read_does(self):
        run = self.execute()
        probe = self.client.get(self.url + '/status').json()
        self.assertEqual(run['status_token'], probe['change_token'])
        artifact = next(a for a in self.store._read('SELECT * FROM artifacts') if a['name'] == 'report.md')
        Path(artifact['path']).write_text('External modification fixture')
        with patch.object(self.store, '_check_integrity', side_effect=AssertionError('probe hashed files')):
            unchanged = self.client.get(self.url + '/status').json()
        self.assertEqual(probe, unchanged)
        detail = self.client.get(self.url).json()
        self.assertFalse(detail['verification']['verified'])
        self.assertIsNone(detail['regression_target'])
        self.assertEqual(detail['review_targets'], [])

    def test_full_detail_and_token_share_snapshot_during_concurrent_review(self):
        run = self.execute()
        old_token = run['status_token']
        target = next(t for t in run['review_targets'] if t['candidate_id'] == 'alpha101')
        original = self.store._check_integrity
        inserted = False
        def checking(connection, row):
            nonlocal inserted
            original(connection, row)
            if not inserted:
                inserted = True
                self.store.create_review(run['id'], 'alpha101', 'accepted', 'implementation',
                    'Automated WAL snapshot fixture.', source='automation', idempotency_key='review',
                    expected_attempt_id=target['attempt_id'], expected_result_digest=target['result_digest'])
        with patch.object(self.store, '_check_integrity', side_effect=checking):
            snapshot = self.store.get_run(run['id'])
        self.assertEqual(snapshot['reviews'], [])
        self.assertEqual(snapshot['status_token'], old_token)
        self.assertNotEqual(self.store.get_run_status(run['id'])['change_token'], old_token)
        self.assertEqual(len(self.store.get_run(run['id'])['reviews']), 1)
