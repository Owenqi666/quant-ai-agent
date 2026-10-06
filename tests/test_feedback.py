"""Issue decisions remain bound to exact approved outputs and reproducible inputs."""
from copy import deepcopy
import unittest

from tests import test_server
from paper_alpha.feedback_metrics import aggregate_feedback
from paper_alpha.server.feedback import Feedback
from paper_alpha.server.service import ServiceError
from paper_alpha.storage import digest


class FeedbackCase(unittest.TestCase):
    setUp = test_server.ServerCase.setUp
    tearDown = test_server.ServerCase.tearDown
    execute = test_server.ServerCase.execute

    def test_issue_resolution_requires_original_case_and_exact_target(self):
        original, _ = self.execute(mode='fixed')
        review = self.store.create_review(original['id'], 'alpha006', 'needs_changes', 'implementation', 'Fix operator alias', source='automation')
        case = self.store.approve_case(review['id'], 'evaluated', 'Canonical spelling should evaluate')
        feedback = Feedback(self.store)
        issue = feedback.create(review['id'], 'Track correction', 'automation', 'implementation_fix', 'issue')
        self.assertEqual(feedback.create(review['id'], 'Track correction', 'automation', 'implementation_fix', 'issue')['id'], issue['id'])
        with self.assertRaises(ServiceError):
            feedback.create(review['id'], 'Different request', 'automation', 'implementation_fix', 'issue')
        def decide(state, key, **extra):
            return feedback.update(issue['id'], issue['latest_event_id'], state, 'implementation_fix', 'Explicit decision', 'automation', key, **extra)
        with self.assertRaises(ServiceError):
            decide('resolved', 'no-proof')
        task = deepcopy(self.store.get_research(self.example['research_id'])['revisions'][0]['task'])
        task['candidates'][0]['expression'] = '-1 * ts_corr(open, volume, 10)'
        revision = self.store.create_revision(self.example['research_id'], self.example['revision_id'], task, 'Canonical alias')
        target, _ = self.execute('corrected', 'fixed', revision['id'])
        check = self.store.run_regression_check(target['id'], [case['id']])
        self.assertTrue(check['passed'])
        unrelated = self.store.create_review(target['id'], 'alpha101', 'accepted', 'implementation', 'Other formula', 'automation')
        other = self.store.approve_case(unrelated['id'], 'evaluated', 'Other candidate')
        other_check = self.store.run_regression_check(target['id'], [other['id']])
        with self.assertRaises(ServiceError):
            decide('resolved', 'wrong-proof', revision_id=revision['id'], target_run_id=target['id'], check_id=other_check['id'])
        resolved = decide('resolved', 'resolve', revision_id=revision['id'], target_run_id=target['id'], check_id=check['id'])
        self.assertEqual(len(resolved['events']), 2)
        self.assertEqual(decide('resolved', 'resolve', revision_id=revision['id'], target_run_id=target['id'], check_id=check['id']), resolved)
        with self.assertRaises(ServiceError):
            decide('deferred', 'stale')
        summary = feedback.summary(self.example['research_id'])
        self.assertEqual(summary['input_digest'], digest(summary['inputs']))
        self.assertEqual(summary['metrics'], aggregate_feedback(**summary['inputs']))
        self.assertEqual(summary['metrics']['human_review_coverage']['numerator'], 0)
        self.assertEqual(summary['metrics']['same_condition_fix_verification']['numerator'], 1)
        self.assertEqual(summary['metrics']['issue_closure']['denominator'], 1)
        self.assertEqual(len(summary['attempt_timings']), 2)
        self.assertEqual(feedback.page('issues', category='implementation', source='automation')['total_records'], 1)
        self.assertEqual(feedback.page('issues', source='human')['total_records'], 0)
        self.assertEqual(feedback.page('reviews', candidate_id='alpha006')['total_records'], 1)
        self.assertEqual(feedback.page('runs', dataset_id=self.store.example_dataset_id)['total_records'], 2)
        self.assertEqual(self.client.get('/api/feedback-summary/export?format=json').status_code, 200)
        self.assertIn('input_digest', self.client.get('/api/feedback-summary/export?format=csv').text)
        self.assertEqual(self.client.get('/api/feedback-summary/export?format=exe').status_code, 422)

    def test_catalog_pages_filter_and_append_boundary(self):
        for index in range(5):
            self.store.submit_run(self.example['revision_id'], 'fixed', 'page-' + str(index))
        feedback = Feedback(self.store)
        first = feedback.page('runs', limit=2, status='queued')
        self.store.submit_run(self.example['revision_id'], 'fixed', 'late')
        items = first['items'][:]
        current = first
        while current['has_more']:
            current = feedback.page('runs', after=current['next_cursor'], limit=2, through=first['high_watermark'], status='queued')
            items += current['items']
        self.assertEqual(len({r['id'] for r in items}), 5)
        self.assertEqual(feedback.page('runs')['total_records'], 6)
        self.assertEqual(feedback.page('researches', candidate_id='alpha006')['total_records'], 1)
        self.assertEqual(feedback.page('runs', candidate_id='absent')['total_records'], 0)
        self.assertEqual(self.client.get('/api/catalog/runs?limit=0').status_code, 422)
        self.assertEqual(self.client.get('/api/catalog/runs?after=999').status_code, 409)
        self.assertEqual(self.client.post('/api/issues', json={'review_id':'missing','note':'test','idempotency_key':'missing'}).status_code, 404)

    def test_cross_research_decision_keeps_exact_target_after_retry(self):
        original, _ = self.execute(mode='fixed')
        review = self.store.create_review(original['id'], 'alpha006', 'needs_changes', 'hypothesis', 'Explicit research limitation', 'automation')
        feedback = Feedback(self.store)
        issue = feedback.create(review['id'], 'Track limitation', 'automation', 'accept_limitation', 'cross-issue')
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][0]['task'])
        task['evidence'][0]['quote'] = 'Unmatched reference for negative synthetic engineering test'
        target_research = self.store.create_research('Cross-research negative test', research['paper_id'], research['dataset_id'], task)
        target, _ = self.execute('cross-target', 'fixed', target_research['latest_revision_id'])
        self.assertEqual(target['status'], 'failed')
        closed = feedback.update(issue['id'], issue['latest_event_id'], 'resolved', 'accept_limitation',
            'Explicitly acknowledge remaining limitation; no implementation-fix credit', 'automation', 'cross-close',
            revision_id=target_research['latest_revision_id'], target_run_id=target['id'])
        exact_attempt = closed['events'][-1]['target_attempt_id']
        self.assertTrue(closed['events'][-1]['target_result_digest'])
        self.store.retry_run(target['id'])
        self.assertEqual(feedback.get(issue['id'])['events'][-1]['target_attempt_id'], exact_attempt)
        report = feedback.summary(self.example['research_id'])
        self.assertEqual(report['metrics']['issue_closure']['numerator'], 1)
        self.assertEqual(report['metrics']['same_condition_fix_verification']['numerator'], 0)
        self.assertEqual(report['metrics']['human_review_coverage']['denominator'], 3)
        self.assertTrue(any(o.get('linkage_verified') and not o['eligible'] for o in report['inputs']['outputs']))


if __name__ == '__main__':
    unittest.main()
