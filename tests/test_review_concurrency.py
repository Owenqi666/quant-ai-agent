"""Review publication must preserve frozen identity without blocking other writers."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha.server.db import transaction
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.workflow import run_task


class ReviewConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name).resolve() / 'workspace')
        example = self.store.seed_example()
        submitted = self.store.submit_run(example['revision_id'], 'normalized_fixed', 'run')
        claim = self.store.claim('test-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        self.store.finish(submitted['id'], 'test-worker', claim['attempt_id'], state['status'])
        self.run = self.store.get_run(submitted['id'])
        self.target = next(t for t in self.run['review_targets'] if t['candidate_id'] == 'alpha101')
        self.args = dict(run_id=self.run['id'], candidate_id='alpha101', verdict='accepted', category='implementation',
            note='Automated concurrency fixture.', source='automation', idempotency_key='review',
            expected_attempt_id=self.target['attempt_id'], expected_result_digest=self.target['result_digest'])

    def tearDown(self):
        self.temp.cleanup()

    def action(self, approval=False):
        if not approval:
            return lambda: self.store.create_review(**self.args)
        review = self.store.create_review(**self.args)
        return lambda: self.store.approve_case(review['id'], 'evaluated', 'Automated expectation.', 'approve')

    def during_verification(self, action, concurrent):
        entered, release = threading.Event(), threading.Event()
        original = self.store._check_integrity
        def slow(connection, row):
            original(connection, row)
            entered.set()
            if not release.wait(8):
                raise RuntimeError('Test failed to release verification')
        with patch.object(self.store, '_check_integrity', side_effect=slow), ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(action)
            try:
                self.assertTrue(entered.wait(8))
                writer = pool.submit(concurrent)
                writer.result(timeout=3)
            finally:
                release.set()
            return future.result(timeout=8)

    def test_review_validation_allows_queue_and_heartbeat_writes(self):
        def writers():
            self.store.heartbeat('parallel')
            self.store.submit_run(self.run['revision_id'], 'fixed', 'parallel-run')
        result = self.during_verification(self.action(), writers)
        self.assertEqual(result['attempt_id'], self.target['attempt_id'])
        self.assertEqual(len(self.store._read('SELECT * FROM reviews')), 1)

    def test_approval_validation_allows_heartbeat_write(self):
        result = self.during_verification(self.action(True), lambda: self.store.heartbeat('parallel'))
        self.assertTrue(result['approved'])

    def test_changed_attempt_state_blocks_review_publication(self):
        def change():
            with transaction(self.store.db_path) as connection:
                connection.execute("UPDATE runs SET status='interrupted' WHERE id=?", (self.run['id'],))
        with self.assertRaisesRegex(ServiceError, 'changed during verification'):
            self.during_verification(self.action(), change)
        self.assertEqual(self.store._read('SELECT * FROM reviews'), [])

    def test_changed_export_even_with_restored_mtime_blocks_review(self):
        artifact = next(a for a in self.store._read('SELECT * FROM artifacts') if a['name'].endswith('/result.json'))
        path = Path(artifact['path'])
        def change():
            old = path.stat()
            value = path.read_bytes()
            path.write_bytes(value[:-1] + (b' ' if value[-1:] != b' ' else b'\n'))
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaisesRegex(ServiceError, 'changed during verification'):
            self.during_verification(self.action(), change)
        self.assertEqual(self.store._read('SELECT * FROM reviews'), [])

    def test_changed_approved_review_blocks_case_publication(self):
        action = self.action(True)
        def change():
            with transaction(self.store.db_path) as connection:
                connection.execute("UPDATE reviews SET result_digest='changed'")
        with self.assertRaisesRegex(ServiceError, 'changed during verification'):
            self.during_verification(action, change)
        self.assertEqual(self.store._read('SELECT * FROM regression_cases'), [])

    def test_added_file_during_verification_is_detected(self):
        base = self.store.root / 'runs' / self.run['id'] / 'attempts' / self.target['attempt_id']
        with self.assertRaisesRegex(ServiceError, 'changed during verification'):
            self.during_verification(self.action(), lambda: (base / 'unexpected').write_text('new'))

    def test_queued_run_remains_a_conflict_not_internal_error(self):
        queued = self.store.submit_run(self.run['revision_id'], 'fixed', 'queued')
        with self.assertRaises(ServiceError) as raised:
            self.store.create_review(**{**self.args, 'run_id': queued['id']})
        self.assertEqual(raised.exception.status, 409)


if __name__ == '__main__':
    unittest.main()
