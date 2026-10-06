"""Real engine/queue integration, independent of the browser or a model."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid

from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import Store


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.example = self.store.seed_example()

    def execute(self):
        with worker_lock(self.root) as fd:
            Worker(self.store, fd).run(once=True)

    def submit(self, revision=None, mode='normalized_fixed'):
        return self.store.submit_run(revision or self.example['revision_id'], mode, str(uuid.uuid4()))

    def test_real_engine_review_revision_regression_loop(self):
        original = self.submit()
        self.execute()
        result = self.store.get_run(original['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['verification']['verified'])
        candidates = {c['id']: c['status'] for c in result['state']['candidates']}
        self.assertEqual(candidates, {'alpha006': 'evaluated', 'alpha101': 'evaluated', 'alpha005': 'blocked'})
        review = self.store.create_review(original['id'], 'alpha005', 'accepted', 'data',
                                         'Synthetic dataset does not contain vwap; stopping is expected.')
        case = self.store.approve_case(review['id'], 'blocked', 'Keep missing-field detection in development regression.')
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][-1]['task'])
        task['hypotheses'][0]['assumptions'].append('Human review recorded for this engineering fixture.')
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Record review')
        updated = self.submit(revision['id'])
        self.execute()
        check = self.store.run_regression_check(updated['id'], [case['id']])
        self.assertTrue(check['passed'])
        self.assertEqual(check['results'][0]['actual_status'], 'blocked')
        self.assertEqual(self.store.get_run(original['id'])['state'], result['state'])
        self.assertNotEqual(revision['id'], self.example['revision_id'])

    def test_budget_exhaustion_is_persisted_and_worker_can_continue(self):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][-1]['task'])
        task['budget']['max_seconds'] = .001
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Timeout probe')
        run = self.submit(revision['id'])
        self.execute()
        ended = self.store.get_run(run['id'])
        self.assertEqual(ended['status'], 'failed')
        self.assertEqual(ended['state']['status'], 'budget_exhausted')
        good = self.submit()
        self.execute()
        self.assertEqual(self.store.get_run(good['id'])['status'], 'completed')

    def test_restart_marks_abandoned_attempt_then_retry_retains_it(self):
        run = self.submit()
        claim = self.store.claim('dead-worker')
        self.assertEqual(claim['id'], run['id'])
        self.execute()  # lock-protected recovery, not heartbeat-only execution theft
        ended = self.store.get_run(run['id'])
        self.assertEqual(ended['status'], 'interrupted')
        self.store.retry_run(run['id'])
        self.execute()
        ended = self.store.get_run(run['id'])
        self.assertEqual(ended['status'], 'completed')
        self.assertEqual(ended['attempt_count'], 2)
        self.assertEqual([a['status'] for a in ended['attempts']], ['interrupted', 'completed'])


if __name__ == '__main__':
    unittest.main()
