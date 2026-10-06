"""Research judgment boundaries, exact result binding and explicit active time."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from paper_alpha.server.api import create_app
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.server.research_assessments import DIMENSIONS, assessment_summary, validate_assessment
from paper_alpha.server.service import Store
from paper_alpha.storage import digest, read_json
from paper_alpha.workflow import run_task


def assessment(target=None):
    return {'reviewer': 'Fixture reviewer (automated test)',
            'expected_attempt_id': (target or {}).get('attempt_id', 'fixture-attempt'),
            'expected_result_digest': (target or {}).get('result_digest', 'a' * 64),
            'dimensions': {name: {'outcome': 'passed', 'reason': 'Explicit fixture judgment only.'} for name in DIMENSIONS},
            'active_intervals': []}


class AssessmentValidationTests(unittest.TestCase):
    def test_required_dimensions_blank_unknown_and_false_coercions(self):
        invalid = []
        for key, value in [('reviewer', ' \t'), ('expected_attempt_id', ' '), ('expected_result_digest', '0' * 63),
                           ('expected_result_digest', 'G' * 64), ('active_intervals', None)]:
            item = assessment(); item[key] = value; invalid.append(item)
        for name in DIMENSIONS:
            item = assessment(); del item['dimensions'][name]; invalid.append(item)
        for name, value in [('reason', ' \n '), ('outcome', True), ('outcome', 'accepted'), ('surprise', 'x')]:
            item = assessment(); item['dimensions']['evidence_accuracy'][name] = value; invalid.append(item)
        item = assessment(); item['dimensions']['extra'] = {}; invalid.append(item)
        item = assessment(); item['extra'] = True; invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises((ValidationError, ValueError)):
                validate_assessment(item)

    def test_timezones_order_overlap_future_and_budget(self):
        item = assessment()
        item['active_intervals'] = [
            {'started_at': '2024-01-01T11:00:00+01:00', 'ended_at': '2024-01-01T11:00:20+01:00'},
            {'started_at': '2024-01-01T09:59:50Z', 'ended_at': '2024-01-01T10:00:00Z'}]
        result = validate_assessment(item)
        summary = assessment_summary(result, 'human')
        self.assertEqual(summary['total_active_seconds'], 30.0)
        self.assertTrue(summary['timing_recorded'])
        self.assertEqual(result['active_intervals'], item['active_intervals'])
        future = datetime.now(timezone.utc) + timedelta(days=1)
        invalid = [
            [{'started_at': '2024-01-01T10:00:00', 'ended_at': '2024-01-01T11:00:00'}],
            [{'started_at': '2024-01-01T10:00:00Z', 'ended_at': '2024-01-01T10:00:00Z'}],
            [{'started_at': '2024-01-01T11:00:00Z', 'ended_at': '2024-01-01T10:00:00Z'}],
            [{'started_at': '2024-01-01T00:00:00Z', 'ended_at': '2024-01-02T00:00:01Z'}],
            [{'started_at': future.isoformat(), 'ended_at': (future + timedelta(seconds=1)).isoformat()}],
            [item['active_intervals'][0], item['active_intervals'][0]],
            [item['active_intervals'][0]] * 101,
            [{'started_at': '2024-02-30T10:00:00Z', 'ended_at': '2024-03-01T10:00:00Z'}],
            [{'started_at': '2024-01-01T10:00:00Z', 'ended_at': '2024-01-01T11:00:00Z', 'extra': 0}],
        ]
        for intervals in invalid:
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                validate_assessment({**item, 'active_intervals': intervals})
        self.assertFalse(assessment_summary(assessment(), 'human')['timing_recorded'])

    def test_declared_source_and_incomplete_dimensions_never_imply_full_pass(self):
        self.assertEqual(assessment_summary(None, 'human')['semantic_status'], 'not_assessed')
        for source in ('automation', 'imported', 'legacy_unknown'):
            self.assertEqual(assessment_summary(assessment(), source)['semantic_status'], 'not_human')
        for outcome, expected in [('failed', 'failed'), ('not_assessed', 'incomplete'), ('not_applicable', 'incomplete')]:
            item = assessment(); item['dimensions']['field_semantics']['outcome'] = outcome
            self.assertEqual(assessment_summary(item, 'human')['semantic_status'], expected)
        self.assertEqual(assessment_summary(assessment(), 'human')['semantic_status'], 'passed')


class AssessmentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.app = create_app(self.base / 'workspace')
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.example = self.store.seed_example()

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def execute(self, *, failed=False, run_id=None):
        if run_id is None:
            revision_id = self.example['revision_id']
            if failed:
                task = deepcopy(self.store.get_research(self.example['research_id'])['revisions'][0]['task'])
                task['evidence'][0]['quote'] = 'Deliberately absent quotation for retry binding test.'
                revision = self.store.create_revision(self.example['research_id'], revision_id, task, 'Failure fixture')
                revision_id = revision['id']
            run_id = self.store.submit_run(revision_id, 'normalized_fixed', 'fixture-run')['id']
        claim = self.store.claim('assessment-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        self.store.finish(run_id, 'assessment-worker', claim['attempt_id'], state['status'])
        return self.store.get_run(run_id), claim

    def post_review(self, run, value=None, source='human'):
        return self.client.post(f"/api/runs/{run['id']}/reviews", json={
            'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'hypothesis',
            'note': 'Automated API test; not a real human review.', 'source': source, 'assessment': value})

    def test_full_digest_legacy_and_structured_projections(self):
        run, claim = self.execute()
        target = next(t for t in run['review_targets'] if t['candidate_id'] == 'alpha006')
        complete = next(c for c in read_json(Path(claim['output_dir']) / 'state.json')['candidates'] if c['id'] == 'alpha006')
        self.assertEqual(target['result_digest'], digest(complete))
        self.assertNotIn('daily', run['state']['candidates'][0]['result'])
        old = self.post_review(run).json()
        self.assertIsNone(old['assessment'])
        self.assertEqual(old['assessment_summary']['semantic_status'], 'not_assessed')
        body = assessment(target)
        body['active_intervals'] = [{'started_at': '2024-01-01T10:00:00Z', 'ended_at': '2024-01-01T10:02:30Z'}]
        response = self.post_review(run, body)
        self.assertEqual(response.status_code, 201, response.text)
        review = response.json()
        self.assertEqual(review['assessment'], body)
        self.assertEqual(review['assessment_summary']['total_active_seconds'], 150.0)
        self.assertEqual(review['assessment_summary']['semantic_status'], 'passed')
        automated = self.post_review(run, body, source='automation').json()
        self.assertEqual(automated['assessment_summary']['semantic_status'], 'not_human')
        refreshed = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()['reviews'], [old, review, automated])
        catalog = self.client.get('/api/catalog/reviews')
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.json()['items'], [old, review, automated])

    def test_invalid_and_stale_inputs_do_not_append_records(self):
        run, _ = self.execute()
        target = next(t for t in run['review_targets'] if t['candidate_id'] == 'alpha006')
        for key, value, code in [('reviewer', ' ', 422), ('extra', 1, 422),
                                 ('expected_attempt_id', 'old-attempt', 409), ('expected_result_digest', 'f' * 64, 409)]:
            body = assessment(target); body[key] = value
            response = self.post_review(run, body)
            self.assertEqual(response.status_code, code, response.text)
        self.assertEqual(self.store.get_run(run['id'])['reviews'], [])
        artifact = next(a for a in self.store.list_artifacts(run['id']) if a['name'] == 'report.md')
        path, _ = self.store.artifact_path(run['id'], artifact['id'])
        path.write_text('Changed verified content')
        self.assertEqual(self.post_review(run, assessment(target)).status_code, 409)
        self.assertEqual(self.store.get_run(run['id'])['review_targets'], [])

    def test_retry_between_validation_and_transaction_cannot_rebind_review(self):
        run, _ = self.execute(failed=True)
        self.assertEqual(run['status'], 'failed')
        target = next(t for t in run['review_targets'] if t['candidate_id'] == 'alpha006')
        original = self.post_review(run, assessment(target)).json()
        ready, proceed = threading.Event(), threading.Event()
        def pause(value):
            result = validate_assessment(value)
            ready.set()
            if not proceed.wait(30):
                raise RuntimeError('Retry fixture did not release pending assessment')
            return result
        with patch('paper_alpha.server.service.validate_assessment', side_effect=pause):
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(self.post_review, run, assessment(target))
                try:
                    self.assertTrue(ready.wait(10))
                    self.store.retry_run(run['id'])
                    retried, _ = self.execute(run_id=run['id'])
                    self.assertNotEqual(retried['review_targets'][0]['attempt_id'], target['attempt_id'])
                finally:
                    proceed.set()
                response = pending.result(timeout=30)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.get_run(run['id'])['reviews'], [original])

    def test_backup_restore_preserves_assessment_and_target(self):
        run, _ = self.execute()
        target = next(t for t in run['review_targets'] if t['candidate_id'] == 'alpha006')
        review = self.post_review(run, assessment(target), source='automation').json()
        snapshot = create_backup(self.store.root, self.base / 'backup')
        self.assertEqual(str(snapshot['database_schema']), str(SCHEMA_VERSION))
        restore_backup(self.base / 'backup', self.base / 'restored')
        restored = Store(self.base / 'restored').get_run(run['id'])
        self.assertEqual(restored['reviews'], [review])
        self.assertEqual(restored['review_targets'], run['review_targets'])
        self.assertTrue(restored['verification']['verified'])


if __name__ == '__main__':
    unittest.main()
