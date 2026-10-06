"""Real local API/store workflows; no mocked numerical results or model calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import atomic_json, read_json
from paper_alpha.workflow import run_task


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(self.root)
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.example = self.store.seed_example()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def submit(self, key='run-key', mode='agent'):
        return self.store.submit_run(self.example['revision_id'], mode, key)

    def execute(self, key='run-key', mode='agent', revision_id=None):
        run = self.store.submit_run(revision_id or self.example['revision_id'], mode, key)
        claim = self.store.claim('test-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode=mode)
        self.store.heartbeat('test-worker', run['id'])
        self.store.finish(run['id'], 'test-worker', claim['attempt_id'],
                          'completed' if state['status'] == 'completed' else 'failed')
        return self.store.get_run(run['id']), claim

    def test_health_capabilities_and_seed_idempotency(self):
        health = self.client.get('/api/health').json()
        self.assertFalse(health['ai_enabled'])
        self.assertFalse(health['worker']['online'])
        self.store.heartbeat('test-worker')
        self.assertTrue(self.client.get('/api/health').json()['worker']['online'])
        self.assertEqual(self.client.post('/api/examples/alpha101').json(), self.example)
        self.assertEqual(len(self.store.list_researches()), 1)
        self.assertFalse(self.client.get('/api/capabilities').json()['rag_enabled'])
        self.assertEqual(self.client.get('/docs').status_code, 200)

    def test_concurrent_demo_import_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            imports = list(executor.map(lambda _: self.store.seed_example(), range(4)))
        self.assertTrue(all(item == self.example for item in imports))
        self.assertEqual(len(self.store.list_researches()), 1)

    def test_revision_optimistic_lock_and_paths_rejected(self):
        research = self.store.get_research(self.example['research_id'])
        task = research['revisions'][0]['task']
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Reviewed evidence')
        self.assertEqual(revision['number'], 2)
        with self.assertRaises(ServiceError) as raised:
            self.store.create_revision(research['id'], self.example['revision_id'], task)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.store.get_research(research['id'])['revisions'][0]['task'], task)
        invalid = {**task, 'paper_pdf': '/etc/passwd'}
        response = self.client.post(f"/api/researches/{research['id']}/revisions", json={
            'base_revision_id': revision['id'], 'task': invalid, 'note': ''})
        self.assertEqual(response.status_code, 422)

    def test_submission_is_idempotent_and_conflicts(self):
        run = self.submit()
        self.assertEqual(self.submit()['id'], run['id'])
        response = self.client.post('/api/runs', json={
            'revision_id': self.example['revision_id'], 'mode': 'fixed', 'idempotency_key': 'run-key'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_claim_race_has_one_owner_and_queued_cancel(self):
        run = self.submit()
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(self.store.claim, ['worker-a', 'worker-b']))
        self.assertEqual(sum(c is not None for c in claims), 1)
        queued = self.submit('second-run')
        self.assertEqual(self.store.cancel_run(queued['id'])['status'], 'cancelled')
        self.assertIsNone(self.store.claim('worker-c'))

    def test_cancel_wins_over_late_finish(self):
        run = self.submit()
        claim = self.store.claim('worker')
        self.assertEqual(self.store.cancel_run(run['id'])['status'], 'cancelling')
        finished = self.store.finish(run['id'], 'worker', claim['attempt_id'], 'completed')
        self.assertEqual(finished['status'], 'cancelled')
        self.assertEqual(self.store.list_artifacts(run['id']), [])
        with self.assertRaises(ServiceError):
            self.store.retry_run(run['id'])

    def test_stale_retry_preserves_attempts_and_fences_old_owner(self):
        run = self.submit()
        first = self.store.claim('worker-a')
        sentinel = Path(first['task_path']).parent / 'diagnostic.txt'
        sentinel.write_text('first attempt kept')
        self.assertEqual(self.store.recover_stale(0), [run['id']])
        self.store.retry_run(run['id'])
        second = self.store.claim('worker-b')
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        with self.assertRaises(ServiceError) as raised:
            self.store.finish(run['id'], 'worker-a', first['attempt_id'], 'failed')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(sentinel.read_text(), 'first attempt kept')
        self.assertEqual(len(self.store.get_run(run['id'])['attempts']), 2)

    def test_materialization_failure_is_attempt_bounded(self):
        run = self.submit()
        paper = self.store._fetch('papers', self.store.get_research(self.example['research_id'])['paper_id'])
        Path(paper['pdf_path']).write_bytes(b'tampered')
        for number in range(1, 4):
            self.assertIsNone(self.store.claim('worker'))
            current = self.store.get_run(run['id'])
            self.assertEqual(current['status'], 'failed')
            self.assertEqual(current['attempt_count'], number)
            if number < 3:
                self.store.retry_run(run['id'])
        with self.assertRaises(ServiceError):
            self.store.retry_run(run['id'])

    def test_strict_json_nonfinite_duplicate_and_size(self):
        for body in ('{"revision_id":"a","revision_id":"b"}', '{"x":NaN}', '{"x":1e999}'):
            response = self.client.post('/api/runs', content=body, headers={'content-type': 'application/json'})
            self.assertEqual(response.status_code, 422)
            self.assertIsInstance(response.json()['detail'], str)
        response = self.client.post('/api/runs', content=b' ' * (1024 * 1024 + 1), headers={'content-type': 'application/json'})
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/api/runs', json={'revision_id': self.example['revision_id'], 'mode': 'agent', 'idempotency_key': 'x', 'extra': 1})
        self.assertEqual(response.status_code, 422)

    def test_cross_origin_json_and_multipart_are_blocked(self):
        for path in ('/api/examples/alpha101', '/api/runs'):
            response = self.client.post(path, json={}, headers={'origin': 'https://attacker.example'})
            self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/papers', files={'file': ('test.pdf', b'%PDF-no', 'application/pdf')},
                                    data={'title': 'test'}, headers={'origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/api/examples/alpha101', headers={'origin': 'http://testserver'}).status_code, 200)
        self.assertEqual(self.client.get('/api/health', headers={'host': 'attacker.example'}).status_code, 400)

    def test_invalid_pdf_and_unknown_artifact(self):
        response = self.client.post('/api/papers', files={'file': ('bad.pdf', b'not a pdf', 'application/pdf')}, data={'title': 'Bad'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/runs/missing/artifacts/missing').status_code, 404)

    def test_complete_feedback_cycle_and_artifact_integrity(self):
        run, claim = self.execute()
        self.assertEqual(run['status'], 'completed')
        self.assertTrue(run['verification']['verified'])
        self.assertNotIn('daily', run['state']['candidates'][0]['result'])
        self.assertNotIn(str(self.root.resolve()), self.client.get('/api/runs/' + run['id']).text)
        events = self.store.events(run['id'])
        engine_events = [e for e in events if e['kind'] == 'engine_event']
        self.assertGreater(len(engine_events), 3)
        self.assertEqual(len(engine_events), len({e['payload']['engine_sequence'] for e in engine_events}))
        self.assertEqual(self.store.events(run['id'], events[-1]['sequence']), [])
        review = self.client.post(f"/api/runs/{run['id']}/reviews", json={
            'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'implementation',
            'note': 'Reviewed exact formula and validation configuration.'})
        self.assertEqual(review.status_code, 201, review.text)
        self.assertEqual(review.json()['attempt_id'], claim['attempt_id'])
        self.assertEqual(len(review.json()['result_digest']), 64)
        self.assertEqual(self.store.list_cases(), [])
        case = self.client.post('/api/regression-cases', json={'review_id': review.json()['id'], 'expected_status': 'evaluated', 'note': 'Approve status regression only.'})
        self.assertEqual(case.status_code, 201, case.text)
        check = self.client.post('/api/regression-checks', json={'run_id': run['id'], 'case_ids': [case.json()['id']]})
        self.assertEqual(check.status_code, 201, check.text)
        self.assertTrue(check.json()['passed'])
        self.assertEqual(len(self.store.list_checks()), 1)
        second = self.store.create_review(run['id'], 'alpha006', 'needs_changes', 'hypothesis', 'Separate economic interpretation requires evidence.')
        self.assertEqual(len(self.store.get_run(run['id'])['reviews']), 2)
        self.assertNotEqual(second['id'], review.json()['id'])
        artifacts = self.store.list_artifacts(run['id'])
        self.assertTrue(artifacts)
        self.assertFalse(any(a['name'].startswith('tools/') for a in artifacts))
        report = next(a for a in artifacts if a['name'] == 'report.md')
        response = self.client.get(f"/api/runs/{run['id']}/artifacts/{report['id']}")
        self.assertEqual(response.status_code, 200)
        path, _ = self.store.artifact_path(run['id'], report['id'])
        path.write_text('tampered report')
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/artifacts/{report['id']}").status_code, 409)
        self.assertFalse(self.client.get(f"/api/runs/{run['id']}").json()['verification']['verified'])
        self.assertEqual(self.client.post(f"/api/runs/{run['id']}/reviews", json={
            'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'other', 'note': 'Must fail'}).status_code, 409)

    def test_invalid_evidence_stays_failed_and_review_has_exact_state(self):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][0]['task'])
        task['evidence'][0]['quote'] = 'This evidence is deliberately not in the document.'
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Negative citation test')
        run, _ = self.execute(revision_id=revision['id'])
        self.assertEqual(run['status'], 'failed')
        self.assertTrue(run['verification']['verified'])
        self.assertTrue(all(c['status'] == 'blocked' for c in run['state']['candidates']))

    def test_rejected_status_can_be_explicit_regression_expectation(self):
        run, _ = self.execute()
        review = self.store.create_review(run['id'], 'alpha006', 'rejected', 'other', 'Expected future rejection for negative test')
        case = self.client.post('/api/regression-cases', json={'review_id': review['id'], 'expected_status': 'rejected', 'note': 'Explicit negative expectation'})
        self.assertEqual(case.status_code, 201)
        check = self.store.run_regression_check(run['id'], [case.json()['id']])
        self.assertFalse(check['passed'])
        self.assertEqual(check['results'][0]['actual_status'], 'evaluated')

    def test_finish_rejects_consistent_output_for_wrong_task_mode_or_dataset(self):
        for index, alteration in enumerate(('task', 'mode', 'dataset')):
            with self.subTest(alteration=alteration):
                run = self.submit(f'spoof-{index}', mode='normalized_fixed')
                claim = self.store.claim('worker')
                task_path = Path(claim['task_path'])
                if alteration == 'task':
                    task = read_json(task_path)
                    task['candidates'][1].update(expression='open', origin='user_modification', changes=['Forged task after submission'])
                    atomic_json(task_path, task)
                elif alteration == 'dataset':
                    metadata_path = task_path.parent / 'metadata.json'
                    metadata = read_json(metadata_path)
                    metadata['version'] = 'unregistered-version'
                    atomic_json(metadata_path, metadata)
                mode = 'agent' if alteration == 'mode' else 'normalized_fixed'
                run_task(task_path, claim['output_dir'], mode=mode)
                result = self.store.finish(run['id'], 'worker', claim['attempt_id'], 'completed')
                self.assertEqual(result['status'], 'failed')
                self.assertIsNone(result['verification'])
                self.assertIsNone(result['state'])
                self.assertEqual(self.store.list_artifacts(run['id']), [])
                self.assertIn('does not match', result['error'])

    def test_materialization_retry_cannot_reuse_previous_verification(self):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][0]['task'])
        task['evidence'][0]['quote'] = 'Deliberately absent quote'
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task)
        run, first = self.execute(revision_id=revision['id'])
        self.assertTrue(run['verification']['verified'])
        self.assertEqual(run['status'], 'failed')
        self.store.retry_run(run['id'])
        self.assertIsNone(self.store.get_run(run['id'])['verification'])
        paper = self.store._fetch('papers', research['paper_id'])
        Path(paper['pdf_path']).write_bytes(b'tampered')
        self.assertIsNone(self.store.claim('worker'))
        latest = self.store.get_run(run['id'])
        self.assertEqual(latest['attempt_count'], 2)
        self.assertIsNone(latest['state'])
        self.assertIsNone(latest['verification'])
        self.assertTrue((Path(first['output_dir']) / 'state.json').exists())
        self.assertEqual(len(latest['attempts']), 2)

    def test_live_events_are_persisted_once_before_finish(self):
        run = self.submit()
        claim = self.store.claim('worker')
        output = Path(claim['output_dir'])
        # Worker writes atomic state checkpoints; heartbeat imports only new events.
        snapshot = {'task': {}, 'status': 'running', 'candidates': [],
                    'events': [{'sequence': 1, 'kind': 'tool_started', 'tool': 'preflight'}]}
        atomic_json(output / 'state.json', snapshot)
        self.store.heartbeat('worker', run['id'])
        first = self.client.get(f"/api/runs/{run['id']}/events").json()
        self.assertEqual(first[-1]['payload']['engine_sequence'], 1)
        self.store.heartbeat('worker', run['id'])
        self.assertEqual(self.store.events(run['id']), first)
        snapshot['events'].append({'sequence': 2, 'kind': 'evidence_and_data_verified'})
        atomic_json(output / 'state.json', snapshot)
        self.store.heartbeat('worker', run['id'])
        latest = self.client.get(f"/api/runs/{run['id']}/events?after={first[-1]['sequence']}").json()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]['payload']['engine_sequence'], 2)
        self.assertEqual(self.store.get_run(run['id'])['state']['status'], 'running')

    def test_artifact_parent_symlink_is_rejected(self):
        run, claim = self.execute()
        artifact = next(a for a in self.store.list_artifacts(run['id']) if a['name'] == 'report.md')
        exports = Path(claim['output_dir']).parent / 'exports'
        original = exports.with_name('original-exports')
        exports.rename(original)
        exports.symlink_to(original, target_is_directory=True)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/artifacts/{artifact['id']}").status_code, 409)
        self.assertFalse(self.store.get_run(run['id'])['verification']['verified'])

    def test_premature_review_is_blocked(self):
        run = self.submit()
        response = self.client.post(f"/api/runs/{run['id']}/reviews", json={
            'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'other', 'note': 'Premature'})
        self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
