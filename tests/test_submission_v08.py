"""Research submission acknowledgements remain bound to the original commit."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server import mutations
from paper_alpha.server.api import create_app
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import SCHEMA_VERSION, transaction
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import digest, json_text


class ResearchSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'workspace'
        self.app = create_app(self.home)
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        example = self.store.seed_example()
        research = self.store.get_research(example['research_id'])
        self.body = {'title': 'Research creation retry fixture', 'paper_id': research['paper_id'],
                     'dataset_id': research['dataset_id'], 'task': research['revisions'][0]['task'],
                     'idempotency_key': 'create-research-key'}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def counts(self, store=None):
        store = store or self.store
        return {table: store._read(f'SELECT COUNT(*) AS n FROM {table}')[0]['n']
                for table in ('researches', 'revisions', 'mutation_receipts')}

    def test_http_repeat_returns_original_detail_after_later_revision(self):
        first = self.client.post('/api/researches', json=self.body)
        self.assertEqual(first.status_code, 201, first.text)
        original = first.json()
        task = deepcopy(self.body['task'])
        task['candidates'][0]['expression'] = '-ts_corr(open, volume, 11)'
        revision = self.store.create_revision(original['id'], original['latest_revision_id'], task, 'Later revision')
        before = self.counts()
        reordered = dict(reversed(list(self.body.items())))
        reordered['task'] = dict(reversed(list(self.body['task'].items())))
        repeated = self.client.post('/api/researches', json=reordered)
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(repeated.json(), original)
        self.assertEqual(self.counts(), before)
        current = self.store.get_research(original['id'])
        self.assertEqual(current['latest_revision_id'], revision['id'])
        self.assertEqual(len(current['revisions']), 2)
        self.assertEqual(len(original['revisions']), 1)

    def test_concurrent_same_key_creates_one_research_and_revision(self):
        before = self.counts()
        barrier = threading.Barrier(6)
        def create(_):
            barrier.wait(timeout=10)
            return self.store.create_research(**self.body)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(create, range(6)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(self.counts(), {table: count + 1 for table, count in before.items()})

    def test_changed_request_conflicts_before_resource_or_task_lookup(self):
        self.store.create_research(**self.body)
        before = self.counts()
        for change in ({'title': 'Different title'}, {'title': ' ' + self.body['title']},
                       {'paper_id': 'missing'}, {'dataset_id': 'missing'}, {'task': {}}):
            with self.subTest(change=change):
                response = self.client.post('/api/researches', json={**self.body, **change})
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.counts(), before)

    def test_failed_validation_does_not_reserve_key_and_rollback_is_atomic(self):
        before = self.counts()
        for change, status in (({'paper_id': 'missing'}, 404), ({'dataset_id': 'missing'}, 404),
                               ({'task': {}}, 422)):
            response = self.client.post('/api/researches', json={**self.body, **change})
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(self.counts(), before)
        record = mutations.record
        def fail_after_receipt(*args):
            record(*args)
            raise RuntimeError('Injected after research receipt')
        with patch.object(mutations, 'record', side_effect=fail_after_receipt):
            with self.assertRaisesRegex(RuntimeError, 'Injected'):
                self.store.create_research(**self.body)
        self.assertEqual(self.counts(), before)
        self.store.create_research(**self.body)
        self.assertEqual(self.counts(), {table: count + 1 for table, count in before.items()})

    def test_failed_response_construction_rolls_back_initial_revision(self):
        before = self.counts()
        with patch.object(self.store, '_research_detail', side_effect=RuntimeError('Response construction failure')):
            with self.assertRaisesRegex(RuntimeError, 'Response construction'):
                self.store.create_research(**self.body)
        self.assertEqual(self.counts(), before)

    def test_original_receipt_replays_without_revalidating_current_metadata(self):
        first = self.store.create_research(**self.body)
        with patch.object(self.store, '_task', side_effect=RuntimeError('Must not recalculate acknowledgement')):
            self.assertEqual(self.store.create_research(**self.body), first)

    def test_legacy_calls_and_deliberate_new_keys_keep_existing_behavior(self):
        legacy = {key: value for key, value in self.body.items() if key != 'idempotency_key'}
        old = self.client.post('/api/researches', json=legacy)
        explicit_null = self.client.post('/api/researches', json={**legacy, 'idempotency_key': None})
        first = self.client.post('/api/researches', json=self.body)
        another = self.client.post('/api/researches', json={**self.body, 'idempotency_key': 'intentional-new-operation'})
        for response in (old, explicit_null, first, another):
            self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len({item.json()['id'] for item in (old, explicit_null, first, another)}), 4)
        self.assertEqual(self.counts()['mutation_receipts'], 2)

    def test_invalid_keys_rejected_by_api_and_service(self):
        before = self.counts()
        for key in ('', ' \t\n', 'x' * 129, 1, True, {}, []):
            with self.subTest(key=key):
                response = self.client.post('/api/researches', json={**self.body, 'idempotency_key': key})
                self.assertEqual(response.status_code, 422, response.text)
                with self.assertRaises(ServiceError) as raised:
                    self.store.create_research(**{**self.body, 'idempotency_key': key})
                self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.counts(), before)

    def test_damaged_receipt_blocks_duplicate_creation(self):
        self.store.create_research(**self.body)
        before = self.counts()
        with transaction(self.store.db_path) as connection:
            connection.execute("UPDATE mutation_receipts SET response='{}' WHERE operation='research.create'")
        response = self.client.post('/api/researches', json=self.body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('receipt is invalid', response.json()['detail'])
        self.assertEqual(self.counts(), before)

    def test_backup_restore_preserves_creation_receipt_and_frozen_report_bytes(self):
        original = self.store.create_research(**self.body)
        legacy = self.store.create_research(**{k: v for k, v in self.body.items() if k != 'idempotency_key'})
        payload = {'kind': 'storage-only fixture; not an exported research report', 'research_id': original['id']}
        with transaction(self.store.db_path) as connection:
            connection.execute('INSERT INTO research_reports VALUES (?,?,?,?,?,?,?)',
                ('storage-fixture', original['id'], original['created_at'], 'storage-key', 'request-digest',
                 json_text(payload), digest(payload)))
        receipts = self.store._read('SELECT * FROM mutation_receipts')
        reports = self.store._read('SELECT * FROM research_reports')
        before = self.counts()
        manifest = create_backup(self.home, self.base / 'backup')
        self.assertEqual(manifest['database_schema'], str(SCHEMA_VERSION))
        restore_backup(self.base / 'backup', self.base / 'restored')
        self.home.rename(self.base / 'original-offline')
        restored = Store(self.base / 'restored')
        self.assertEqual(restored.create_research(**self.body), original)
        self.assertEqual(self.counts(restored), before)
        self.assertEqual(restored._read('SELECT * FROM mutation_receipts'), receipts)
        self.assertEqual(restored._read('SELECT * FROM research_reports'), reports)
        self.assertEqual(restored.get_research(legacy['id']), legacy)

    def test_run_retry_acknowledges_same_run_with_current_status(self):
        research = self.store.create_research(**self.body)
        # Research and run operation keys are independent namespaces.
        args = {'revision_id': research['latest_revision_id'], 'mode': 'normalized_fixed',
                'idempotency_key': self.body['idempotency_key']}
        first = self.store.submit_run(**args)
        self.assertEqual(first['status'], 'queued')
        self.store.cancel_run(first['id'])
        before_events = self.store.events(first['id'])
        replay = self.store.submit_run(**args)
        self.assertEqual(replay['id'], first['id'])
        self.assertEqual(replay['status'], 'cancelled')
        self.assertEqual(self.store.events(first['id']), before_events)
        self.assertEqual(len(self.store.list_runs()), 1)


if __name__ == '__main__':
    unittest.main()
