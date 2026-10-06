"""Retry safety covers actual business rows, transactions and immutable targets."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import SCHEMA_VERSION, transaction
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.server import mutations
from paper_alpha.workflow import run_task


class MutationIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'workspace'
        self.app = create_app(self.home)
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.example = self.store.seed_example()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def execute(self, *, failed=False, run_id=None):
        if run_id is None:
            revision_id = self.example['revision_id']
            if failed:
                task = deepcopy(self.store.get_research(self.example['research_id'])['revisions'][0]['task'])
                task['evidence'][0]['quote'] = 'Intentionally absent quotation for replay fixture.'
                revision_id = self.store.create_revision(self.example['research_id'], revision_id, task, 'Failed fixture')['id']
            run_id = self.store.submit_run(revision_id, 'normalized_fixed', 'fixture-run')['id']
        claim = self.store.claim('receipt-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        self.store.finish(run_id, 'receipt-worker', claim['attempt_id'], state['status'])
        return self.store.get_run(run_id)

    def review_body(self, run, *, structured=True):
        body = {'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'implementation',
                'note': 'Automated retry fixture; not human approval.', 'source': 'automation',
                'idempotency_key': 'review-key'}
        if structured:
            target = next(item for item in run['review_targets'] if item['candidate_id'] == 'alpha006')
            body['assessment'] = {
                'reviewer': 'Automated fixture', 'expected_attempt_id': target['attempt_id'],
                'expected_result_digest': target['result_digest'],
                'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'No human judgment recorded.'}
                               for name in DIMENSIONS}, 'active_intervals': []}
        return body

    def case_body(self, review):
        return {'review_id': review['id'], 'expected_status': 'evaluated',
                'note': 'Automated retry expectation.', 'idempotency_key': 'case-key'}

    def target_pair(self, run):
        target = next(item for item in run['review_targets'] if item['candidate_id'] == 'alpha006')
        return {'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest']}

    def counts(self, store=None):
        store = store or self.store
        return {table: store._read(f'SELECT COUNT(*) AS n FROM {table}')[0]['n']
                for table in ('reviews', 'regression_cases', 'mutation_receipts', 'events')}

    def test_http_replays_return_identical_payload_without_extra_events(self):
        run = self.execute()
        url = f"/api/runs/{run['id']}/reviews"
        body = self.review_body(run)
        first = self.client.post(url, json=body)
        self.assertEqual(first.status_code, 201, first.text)
        before = self.counts()
        second = self.client.post(url, json=dict(reversed(list(body.items()))))
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.counts(), before)
        approval = self.case_body(first.json())
        first_case = self.client.post('/api/regression-cases', json=approval)
        self.assertEqual(first_case.status_code, 201, first_case.text)
        before = self.counts()
        repeated_case = self.client.post('/api/regression-cases', json=approval)
        self.assertEqual(repeated_case.status_code, 201, repeated_case.text)
        self.assertEqual(repeated_case.json(), first_case.json())
        self.assertEqual(self.counts(), before)

    def test_concurrent_same_key_has_one_business_record_and_receipt(self):
        run = self.execute()
        body = self.review_body(run)
        barrier = threading.Barrier(4)
        def review(_):
            barrier.wait(timeout=10)
            return self.store.create_review(run['id'], **body)
        with ThreadPoolExecutor(max_workers=4) as executor:
            reviews = list(executor.map(review, range(4)))
        self.assertTrue(all(item == reviews[0] for item in reviews))
        self.assertEqual(self.counts()['reviews'], 1)
        self.assertEqual(len([e for e in self.store.events(run['id']) if e['kind'] == 'review_recorded']), 1)
        approval = self.case_body(reviews[0])
        barrier = threading.Barrier(4)
        def approve(_):
            barrier.wait(timeout=10)
            return self.store.approve_case(**approval)
        with ThreadPoolExecutor(max_workers=4) as executor:
            cases = list(executor.map(approve, range(4)))
        self.assertTrue(all(item == cases[0] for item in cases))
        self.assertEqual(self.counts()['regression_cases'], 1)
        self.assertEqual(self.counts()['mutation_receipts'], 2)

    def test_conflict_compares_target_source_and_nested_content(self):
        run = self.execute()
        body = self.review_body(run)
        review = self.store.create_review(run['id'], **body)
        before = self.counts()
        for change in ({'note': 'Different note'}, {'candidate_id': 'unknown'}, {'source': 'human'}):
            with self.subTest(change=change), self.assertRaises(ServiceError) as raised:
                self.store.create_review(run['id'], **{**body, **change})
            self.assertEqual(raised.exception.status, 409)
        changed = deepcopy(body)
        changed['assessment']['dimensions']['hypothesis_fidelity']['reason'] = 'Different reason'
        with self.assertRaises(ServiceError) as raised:
            self.store.create_review(run['id'], **changed)
        self.assertEqual(raised.exception.status, 409)
        response = self.client.post('/api/runs/another-run/reviews', json=body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.counts(), before)
        approval = self.case_body(review)
        self.store.approve_case(**approval)
        before = self.counts()
        for change in ({'review_id': 'another-review'}, {'expected_status': 'failed'}, {'note': 'Different'}):
            response = self.client.post('/api/regression-cases', json={**approval, **change})
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.counts(), before)

    def test_new_keys_allow_intentional_repeated_operations_and_namespace_is_separate(self):
        run = self.execute()
        body = self.review_body(run)
        first = self.store.create_review(run['id'], **body)
        second = self.store.create_review(run['id'], **{**body, 'idempotency_key': 'new-review-key'})
        self.assertNotEqual(first['id'], second['id'])
        approval = {**self.case_body(first), 'idempotency_key': 'review-key'}
        first_case = self.store.approve_case(**approval)
        second_case = self.store.approve_case(**{**approval, 'idempotency_key': 'new-case-key'})
        self.assertNotEqual(first_case['id'], second_case['id'])
        self.assertEqual(self.counts()['mutation_receipts'], 4)

    def test_legacy_omitted_null_and_default_filled_requests_remain_compatible(self):
        run = self.execute()
        body = self.review_body(run, structured=False)
        del body['source']
        first = self.client.post(f"/api/runs/{run['id']}/reviews", json=body)
        second = self.client.post(f"/api/runs/{run['id']}/reviews", json={**body, 'source': 'legacy_unknown', 'assessment': None,
                                                                            'expected_attempt_id': None, 'expected_result_digest': None})
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.json(), first.json())
        legacy = {k: v for k, v in body.items() if k != 'idempotency_key'}
        old1 = self.store.create_review(run['id'], **legacy)
        old2 = self.store.create_review(run['id'], **legacy, idempotency_key=None)
        self.assertNotEqual(old1['id'], old2['id'])
        approval = self.case_body(old1)
        del approval['idempotency_key']
        case1 = self.store.approve_case(**approval)
        case2 = self.store.approve_case(**approval, idempotency_key=None)
        self.assertNotEqual(case1['id'], case2['id'])
        self.assertEqual(self.counts()['mutation_receipts'], 1)

    def test_invalid_keys_are_rejected_by_http_and_direct_service(self):
        body = {'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'other', 'note': 'Fixture'}
        for key in ('', ' \t\n', 'x' * 129, 1, True, {}, []):
            with self.subTest(key=key):
                review = self.client.post('/api/runs/missing/reviews', json={**body, 'idempotency_key': key})
                case = self.client.post('/api/regression-cases', json={
                    'review_id': 'missing', 'expected_status': 'evaluated', 'note': 'Fixture', 'idempotency_key': key})
                self.assertEqual(review.status_code, 422, review.text)
                self.assertEqual(case.status_code, 422, case.text)
                with self.assertRaises(ServiceError) as raised:
                    self.store.create_review('missing', **body, idempotency_key=key)
                self.assertEqual(raised.exception.status, 422)
                with self.assertRaises(ServiceError) as raised:
                    self.store.approve_case('missing', 'evaluated', 'Fixture', idempotency_key=key)
                self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.counts()['mutation_receipts'], 0)

    def test_receipt_failure_rolls_back_business_row_and_review_event(self):
        run = self.execute()
        body = self.review_body(run)
        record = mutations.record
        def fail_after_receipt(*args):
            record(*args)
            raise RuntimeError('Injected failure after receipt insertion')
        before = self.counts()
        with patch.object(mutations, 'record', side_effect=fail_after_receipt):
            with self.assertRaisesRegex(RuntimeError, 'Injected'):
                self.store.create_review(run['id'], **body)
        self.assertEqual(self.counts(), before)
        review = self.store.create_review(run['id'], **body)
        approval = self.case_body(review)
        before = self.counts()
        with patch.object(mutations, 'record', side_effect=fail_after_receipt):
            with self.assertRaisesRegex(RuntimeError, 'Injected'):
                self.store.approve_case(**approval)
        self.assertEqual(self.counts(), before)
        self.store.approve_case(**approval)
        self.assertEqual(self.counts()['regression_cases'], 1)

    def test_failed_first_target_check_does_not_reserve_key(self):
        run = self.execute()
        body = self.review_body(run)
        stale = deepcopy(body)
        stale['assessment']['expected_attempt_id'] = 'stale-attempt'
        with self.assertRaises(ServiceError) as raised:
            self.store.create_review(run['id'], **stale)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.counts()['mutation_receipts'], 0)
        self.store.create_review(run['id'], **body)
        self.assertEqual(self.counts()['mutation_receipts'], 1)

    def test_committed_replays_survive_artifact_tampering_but_new_writes_are_blocked(self):
        run = self.execute()
        body = self.review_body(run)
        review = self.store.create_review(run['id'], **body)
        approval = self.case_body(review)
        case = self.store.approve_case(**approval)
        artifact = next(a for a in self.store.list_artifacts(run['id']) if a['name'] == 'report.md')
        path, _ = self.store.artifact_path(run['id'], artifact['id'])
        path.write_text('Tampered artifact fixture', encoding='utf-8')
        before = self.counts()
        self.assertEqual(self.store.create_review(run['id'], **body), review)
        self.assertEqual(self.store.approve_case(**approval), case)
        for call in (lambda: self.store.create_review(run['id'], **{**body, 'idempotency_key': 'new'}),
                     lambda: self.store.approve_case(**{**approval, 'idempotency_key': 'new'})):
            with self.assertRaises(ServiceError) as raised:
                call()
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.counts(), before)

    def test_replay_after_run_retry_keeps_original_attempt(self):
        run = self.execute(failed=True)
        self.assertEqual(run['status'], 'failed')
        body = self.review_body(run)
        review = self.store.create_review(run['id'], **body)
        approval = {**self.case_body(review), 'expected_status': 'blocked'}
        case = self.store.approve_case(**approval)
        self.store.retry_run(run['id'])
        queued = self.store.get_run(run['id'])
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(self.store.create_review(run['id'], **body), review)
        self.assertEqual(self.store.approve_case(**approval), case)
        retried = self.execute(run_id=run['id'])
        current_target = next(item for item in retried['review_targets'] if item['candidate_id'] == 'alpha006')
        self.assertNotEqual(current_target['attempt_id'], review['attempt_id'])
        before = self.counts()
        self.assertEqual(self.store.create_review(run['id'], **body), review)
        self.assertEqual(self.store.approve_case(**approval), case)
        with self.assertRaises(ServiceError) as raised:
            self.store.create_review(run['id'], **{**body, 'idempotency_key': 'new-key'})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.counts(), before)

    def test_backup_restore_keeps_receipts_and_legacy_records(self):
        run = self.execute()
        body = self.review_body(run)
        review = self.store.create_review(run['id'], **body)
        approval = self.case_body(review)
        case = self.store.approve_case(**approval)
        legacy_body = {k: v for k, v in body.items() if k != 'idempotency_key'}
        legacy = self.store.create_review(run['id'], **legacy_body)
        before = self.counts()
        manifest = create_backup(self.home, self.base / 'backup')
        self.assertEqual(manifest['database_schema'], str(SCHEMA_VERSION))
        restore_backup(self.base / 'backup', self.base / 'restored')
        self.home.rename(self.base / 'original-offline')
        restored = Store(self.base / 'restored')
        self.assertEqual(restored.create_review(run['id'], **body), review)
        self.assertEqual(restored.approve_case(**approval), case)
        self.assertEqual(self.counts(restored), before)
        self.assertIn(legacy, restored.get_run(run['id'])['reviews'])
        self.assertTrue(restored.get_run(run['id'])['verification']['verified'])

    def test_damaged_receipt_is_rejected_without_duplicate_or_false_success(self):
        run = self.execute()
        body = self.review_body(run)
        self.store.create_review(run['id'], **body)
        before = self.counts()
        with transaction(self.store.db_path) as connection:
            connection.execute("UPDATE mutation_receipts SET response='{}' WHERE operation='review.create'")
        response = self.client.post(f"/api/runs/{run['id']}/reviews", json=body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('receipt is invalid', response.json()['detail'])
        self.assertEqual(self.counts(), before)

    def test_generic_review_target_pair_rejects_stale_values_before_first_commit(self):
        run = self.execute()
        body = {**self.review_body(run, structured=False), **self.target_pair(run)}
        before = self.counts()
        for change in ({'expected_attempt_id': 'old-attempt'}, {'expected_result_digest': 'f' * 64}):
            response = self.client.post(f"/api/runs/{run['id']}/reviews", json={**body, **change})
            self.assertEqual(response.status_code, 412, response.text)
        self.assertEqual(self.counts(), before)
        response = self.client.post(f"/api/runs/{run['id']}/reviews", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()['assessment'])
        self.assertEqual(response.json()['attempt_id'], body['expected_attempt_id'])
        self.assertEqual(response.json()['result_digest'], body['expected_result_digest'])
        before = self.counts()
        # A valid-shaped changed target conflicts with the receipt before any
        # current-target lookup, even if the rest of the request is identical.
        changed = {**body, 'expected_attempt_id': 'another-attempt'}
        conflict = self.client.post(f"/api/runs/{run['id']}/reviews", json=changed)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn('different request', conflict.json()['detail'])
        self.assertEqual(self.counts(), before)

    def test_review_target_fields_are_a_strict_pair_for_api_and_direct_call(self):
        body = {'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'other', 'note': 'Automated fixture',
                'idempotency_key': 'bad-target'}
        invalid = [
            {'expected_attempt_id': 'attempt'}, {'expected_result_digest': 'a' * 64},
            {'expected_attempt_id': None, 'expected_result_digest': 'a' * 64},
            {'expected_attempt_id': 'attempt', 'expected_result_digest': None},
            {'expected_attempt_id': ' ', 'expected_result_digest': 'a' * 64},
            {'expected_attempt_id': 'a' * 65, 'expected_result_digest': 'a' * 64},
            {'expected_attempt_id': True, 'expected_result_digest': 'a' * 64},
            {'expected_attempt_id': 'attempt', 'expected_result_digest': 'g' * 64},
            {'expected_attempt_id': 'attempt', 'expected_result_digest': 'a' * 63},
        ]
        for pair in invalid:
            with self.subTest(pair=pair):
                response = self.client.post('/api/runs/missing/reviews', json={**body, **pair})
                self.assertEqual(response.status_code, 422, response.text)
                with self.assertRaises(ServiceError) as raised:
                    self.store.create_review('missing', **body, **pair)
                self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.counts()['mutation_receipts'], 0)

    def test_structured_and_top_level_targets_must_agree(self):
        run = self.execute()
        body = {**self.review_body(run), **self.target_pair(run)}
        before = self.counts()
        for change in ({'expected_attempt_id': 'inconsistent'}, {'expected_result_digest': 'f' * 64}):
            response = self.client.post(f"/api/runs/{run['id']}/reviews", json={**body, **change})
            self.assertEqual(response.status_code, 422, response.text)
            with self.assertRaises(ServiceError) as raised:
                self.store.create_review(run['id'], **{**body, **change})
            self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.counts(), before)
        response = self.client.post(f"/api/runs/{run['id']}/reviews", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.client.post(f"/api/runs/{run['id']}/reviews", json=body).json(), response.json())

    def test_generic_old_request_replay_after_retry_cannot_rebind_or_reserve_stale_write(self):
        run = self.execute(failed=True)
        body = {**self.review_body(run, structured=False), **self.target_pair(run)}
        review = self.store.create_review(run['id'], **body)
        self.store.retry_run(run['id'])
        self.assertEqual(self.store.create_review(run['id'], **body), review)
        retried = self.execute(run_id=run['id'])
        current = {**body, **self.target_pair(retried)}
        self.assertNotEqual(current['expected_attempt_id'], body['expected_attempt_id'])
        before = self.counts()
        self.assertEqual(self.store.create_review(run['id'], **body), review)
        # This is the first write for a stale, still-unsaved generic draft.
        with self.assertRaises(ServiceError) as raised:
            self.store.create_review(run['id'], **{**body, 'idempotency_key': 'fresh-stale'})
        self.assertEqual(raised.exception.status, 412)
        with self.assertRaises(ServiceError) as raised:
            self.store.create_review(run['id'], **current)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.counts(), before)
        fresh = self.store.create_review(run['id'], **{**current, 'idempotency_key': 'fresh-stale'})
        self.assertEqual(fresh['attempt_id'], current['expected_attempt_id'])
        self.assertNotEqual(fresh['id'], review['id'])


if __name__ == '__main__':
    unittest.main()
