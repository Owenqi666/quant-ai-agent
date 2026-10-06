from copy import deepcopy
import unittest

from paper_alpha.feedback_metrics import aggregate_feedback


class FeedbackMetricsTests(unittest.TestCase):
    def setUp(self):
        self.outputs = [dict(run_id='old', attempt_id='a1', candidate_id='c', result_digest='d1', eligible=True, revision_id='rev1', state_digest='state1'),
                        dict(run_id='new', attempt_id='a2', candidate_id='c', result_digest='d2', eligible=True, revision_id='rev2', state_digest='state2')]
        self.reviews = [{**{k: v for k, v in self.outputs[0].items() if k != 'eligible'},
                         'id': 'review', 'source': 'human', 'created_at': '2026-10-01T00:00:00+00:00'}]
        self.issues = [{'id': 'issue', 'review_id': 'review', 'created_at': '2026-10-01T00:00:00+00:00', 'latest_event_id': 'event'}]
        self.events = [{'id': 'event', 'issue_id': 'issue', 'created_at': '2026-10-01T00:01:00+00:00',
                        'state': 'resolved', 'disposition': 'implementation_fix', 'revision_id': 'rev2',
                        'target_run_id': 'new', 'target_attempt_id': 'a2', 'target_result_digest': 'state2',
                        'check_id': 'check', 'source': 'human', 'note': 'Explicit reviewed repair'}]
        self.cases = [{'id': 'case', 'review_id': 'review', 'approved': True, 'contract': {'frozen': True}}]
        self.checks = [{'id': 'check', 'run_id': 'new', 'attempt_id': 'a2', 'revision_id': 'rev2', 'result_digest': 'state2', 'outcome': 'passed', 'passed': True,
                        'results': [{'case_id': 'case', 'candidate_id': 'c', 'outcome': 'passed', 'compatible': True}]}]

    def calculate(self):
        return aggregate_feedback(self.outputs, self.reviews, self.issues, self.events, self.cases, self.checks)

    def test_duplicate_reviews_do_not_inflate_output_coverage(self):
        self.reviews.append({**self.reviews[0], 'id': 'second'})
        result = self.calculate()
        self.assertEqual(result['human_review_coverage']['numerator'], 1)
        self.assertEqual(result['human_review_coverage']['denominator'], 2)
        self.assertEqual(result['review_rows_by_source']['human'], 2)
        self.assertEqual(result['issue_closure']['rate'], 1)
        self.assertEqual(result['same_condition_fix_verification']['rate'], 1)
        self.assertEqual(result['closure_elapsed']['records'][0]['seconds'], 60)

    def test_automation_and_legacy_notes_are_not_human_identity(self):
        self.reviews[0].update(source='automation', note='This is a human review')
        result = self.calculate()
        self.assertEqual(result['human_review_coverage']['numerator'], 0)
        self.assertEqual(result['review_coverage_by_source']['automation']['numerator'], 1)
        self.reviews[0].pop('source')
        result = self.calculate()
        self.assertEqual(result['human_review_coverage']['numerator'], 0)
        self.assertEqual(result['review_coverage_by_source']['legacy_unknown']['numerator'], 1)

    def test_retry_counts_attempts_without_inventing_independent_runs(self):
        self.outputs.append({**self.outputs[0], 'attempt_id': 'retry', 'result_digest': 'retry-digest'})
        result = self.calculate()
        self.assertEqual(result['independent_run_count'], 2)
        self.assertEqual(result['attempt_count'], 3)
        self.assertEqual(result['human_review_coverage']['denominator'], 3)

    def test_incompatible_or_unrelated_green_check_cannot_close_implementation_fix(self):
        for field, value in [('compatible', False), ('case_id', 'unrelated'), ('candidate_id', 'other')]:
            with self.subTest(field=field):
                original = deepcopy(self.checks)
                self.checks[0]['results'][0][field] = value
                result = self.calculate()
                self.assertEqual(result['same_condition_fix_verification']['numerator'], 0)
                self.assertEqual(result['incomplete_resolved_issue_ids'], ['issue'])
                self.checks = original
        self.checks[0]['attempt_id'] = 'unverified-attempt'
        self.assertEqual(self.calculate()['issue_closure']['numerator'], 0)

    def test_data_change_can_be_explicitly_closed_but_is_not_same_condition_fix(self):
        self.events[0]['disposition'] = 'data_change'
        result = self.calculate()
        self.assertEqual(result['issue_closure']['numerator'], 1)
        self.assertIsNone(result['same_condition_fix_verification']['rate'])
        self.assertFalse(result['issues'][0]['same_condition_fix_verified'])

    def test_frozen_target_missing_or_later_retry_cannot_replace_closure_evidence(self):
        self.events[0]['disposition'] = 'data_change'
        self.outputs[1].update(attempt_id='later-attempt', result_digest='later-candidate', state_digest='later-state')
        self.assertEqual(self.calculate()['issue_closure']['numerator'], 0)
        self.outputs.append(dict(run_id='new', attempt_id='a2', candidate_id='c', result_digest='d2', eligible=True,
                                 revision_id='rev2', state_digest='state2'))
        self.assertEqual(self.calculate()['issue_closure']['numerator'], 1)
        self.events[0].pop('target_result_digest')
        self.assertEqual(self.calculate()['issue_closure']['numerator'], 0)

    def test_external_verified_target_supports_closure_without_polluting_cohort(self):
        self.events[0]['disposition'] = 'data_change'
        self.outputs[1].update(eligible=False, linkage_verified=True)
        result = self.calculate()
        self.assertEqual(result['issue_closure']['numerator'], 1)
        self.assertEqual(result['human_review_coverage']['denominator'], 1)
        self.assertEqual(result['run_count'], 1)
        self.assertEqual(result['attempt_count'], 1)
        self.assertEqual(result['supporting_output_count'], 1)
        self.outputs[1]['linkage_verified'] = False
        self.assertEqual(self.calculate()['issue_closure']['numerator'], 0)

    def test_unrelated_failure_in_mixed_check_does_not_block_original_issue_fix(self):
        self.checks[0].update(outcome='failed', passed=False)
        self.checks[0]['results'].append({'case_id': 'unrelated-case', 'candidate_id': 'another-candidate',
                                          'outcome': 'failed', 'compatible': True})
        result = self.calculate()
        self.assertEqual(result['same_condition_fix_verification']['numerator'], 1)
        self.assertEqual(result['issue_closure']['numerator'], 1)

    def test_all_matching_original_review_cases_must_pass(self):
        self.cases.append({**self.cases[0], 'id': 'second-original-case'})
        self.checks[0]['results'].append({'case_id': 'second-original-case', 'candidate_id': 'c',
                                          'outcome': 'failed', 'compatible': True})
        self.assertEqual(self.calculate()['same_condition_fix_verification']['numerator'], 0)

    def test_append_identity_overrides_wall_clock_or_uuid_sorting(self):
        self.events.append({**self.events[0], 'id': 'z-old', 'state': 'open'})
        self.assertEqual(self.calculate()['issue_closure']['numerator'], 1)

    def test_empty_cohort_is_not_zero_accuracy(self):
        result = aggregate_feedback([], [], [], [])
        self.assertIsNone(result['human_review_coverage']['rate'])
        self.assertIsNone(result['issue_closure']['rate'])

    def test_repeat_is_deterministic_and_does_not_modify_inputs(self):
        before = deepcopy([self.outputs, self.reviews, self.issues, self.events, self.cases, self.checks])
        result = self.calculate()
        self.assertEqual(result, self.calculate())
        self.assertEqual(before, [self.outputs, self.reviews, self.issues, self.events, self.cases, self.checks])
        self.outputs.reverse()
        self.assertEqual(result, self.calculate())

    def test_conflicting_duplicate_identity_fails_instead_of_inflating_counts(self):
        self.outputs.append({**self.outputs[0], 'eligible': False})
        with self.assertRaises(ValueError):
            self.calculate()


if __name__ == '__main__':
    unittest.main()
