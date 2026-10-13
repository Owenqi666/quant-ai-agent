"""Original local paper fixtures exercise the real queue and immutable history.

No seed_example, third-party paper, model adapter, old workspace or account is
used. Store registers its bundled synthetic market during normal initialization.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server.provider_runtime import ProviderRegistry
from paper_alpha.server.runner import Worker, worker_lock
from tests.public_fixtures import QUOTE, prepare_original_research


class PublicReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-reliability-')
        self.home = Path(self.temp.name) / 'workspace'
        self.app = create_app(self.home, provider_registry=ProviderRegistry({}))
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.pdf, self.body = prepare_original_research(self.store)
        paper = self.store.get_paper(self.body['paper_id'])
        self.assertIn(QUOTE, paper['pages'][0]['text'])
        self.task = self.body['task']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def create_research(self):
        response = self.client.post('/api/researches', json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def counts(self):
        return {table: self.store._read(f'SELECT COUNT(*) AS n FROM {table}')[0]['n']
                for table in ('researches', 'revisions', 'runs', 'attempts')}

    def snapshot(self, name, facts):
        destination = os.environ.get('PUBLIC_RELIABILITY_ARTIFACT_DIR')
        if not destination:
            return
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'original-fixture.pdf').write_bytes(self.pdf)
        (folder / f'{name}.json').write_text(json.dumps({
            'source': 'original_public_test_fixture', 'paper_sha256': sha256(self.pdf).hexdigest(),
            'quote': QUOTE, 'data_kind': 'synthetic', 'model_calls': 0,
            'human_review': 'pending_human', 'human_quality_scores': None, 'facts': facts},
            indent=2) + '\n')

    def test_research_request_replay_and_conflict_preserve_business_rows(self):
        first = self.create_research()
        before = self.counts()
        again = self.client.post('/api/researches', json=self.body)
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json(), first)
        conflict = self.client.post('/api/researches', json={**self.body, 'title': 'Different request'})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(self.counts(), before)
        self.snapshot('research-replay', {'before': before, 'after': self.counts(),
                                        'same_research_id': again.json()['id'] == first['id'],
                                        'changed_request_status': conflict.status_code})

    def test_concurrent_submission_is_one_run_and_completed_replay_does_not_resubmit(self):
        research = self.create_research()
        revision = research['latest_revision_id']
        with ThreadPoolExecutor(max_workers=4) as executor:
            submitted = list(executor.map(lambda _: self.store.submit_run(
                revision, 'normalized_fixed', 'same-run-request'), range(4)))
        self.assertEqual(len({item['id'] for item in submitted}), 1)
        self.assertEqual(self.counts()['runs'], 1)
        self.assertEqual(self.counts()['attempts'], 0)
        with worker_lock(self.home) as lock:
            worker = Worker(self.store, lock)
            self.assertTrue(worker.run_once())
        run = self.store.get_run(submitted[0]['id'])
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertTrue(run['verification']['verified'])
        before = self.counts()
        replay = self.store.submit_run(revision, 'normalized_fixed', 'same-run-request')
        self.assertEqual(replay['id'], run['id'])
        self.assertEqual(replay['status'], 'completed')
        self.assertEqual(self.counts(), before)
        self.snapshot('run-replay', {'run_status': run['status'], 'counts': before,
                                    'verified': run['verification']['verified']})

    def test_worker_stop_preserves_attempt_until_explicit_retry(self):
        research = self.create_research()
        run = self.store.submit_run(research['latest_revision_id'], 'normalized_fixed', 'recover-run')
        with worker_lock(self.home) as lock:
            worker = Worker(self.store, lock)
            job = self.store.claim(worker.worker_id)
            inputs = Path(job['task_path']).read_bytes()
            worker.stopping.set()  # Controlled stop before compute spawn, not a process-kill test.
            worker.execute(job)
            interrupted = self.store.get_run(run['id'])
            self.assertEqual(interrupted['status'], 'interrupted')
            self.assertEqual(interrupted['attempt_count'], 1)
            reopened = type(self.store)(self.home)
            self.assertEqual(reopened.get_run(run['id'])['status'], 'interrupted')
            self.assertIsNone(reopened.claim('no-implicit-retry'))
            queued = reopened.retry_run(run['id'])
            self.assertEqual(queued['status'], 'queued')
        # Use an unstopped Worker to execute the explicit retry.
        with worker_lock(self.home) as lock:
            worker = Worker(reopened, lock)
            self.assertTrue(worker.run_once())
        final = reopened.get_run(run['id'])
        self.assertEqual(final['status'], 'completed', final.get('error'))
        self.assertEqual(final['attempt_count'], 2)
        attempts = reopened._read('SELECT id,status FROM attempts WHERE run_id=? ORDER BY number', (run['id'],))
        self.assertEqual([row['status'] for row in attempts], ['interrupted', 'completed'])
        self.assertEqual(attempts[0]['id'], job['attempt_id'])
        self.assertEqual(Path(job['task_path']).read_bytes(), inputs)
        self.snapshot('explicit-recovery', {'attempt_statuses': [row['status'] for row in attempts],
                                          'original_inputs_unchanged': True, 'final_status': final['status']})

    def test_default_provider_and_status_reads_never_create_model_or_run_work(self):
        research = self.create_research()
        before = self.counts()
        for _ in range(3):
            self.assertFalse(self.client.get('/api/health').json()['ai_enabled'])
            self.assertFalse(self.client.get('/api/providers').json()['real_dispatch_enabled'])
            self.assertEqual(self.client.get('/api/researches/' + research['id']).status_code, 200)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM proposal_attempts')[0]['n'], 0)
        self.snapshot('read-only-status', {'business_counts_unchanged': True, 'proposal_attempts': 0})


if __name__ == '__main__':
    unittest.main()
