from copy import deepcopy
import unittest

from paper_alpha.feedback_metrics import aggregate_feedback


class AssessmentMetricsTests(unittest.TestCase):
    def setUp(self):
        self.output = dict(run_id='r', attempt_id='a', candidate_id='c', result_digest='d', eligible=True)
        self.review = {**self.output, 'id': 'first', 'source': 'human', 'created_at': '2026-10-01T02:00:00Z',
                       'assessment': {'reviewer': 'reviewer', 'expected_attempt_id': 'a', 'expected_result_digest': 'd',
                                      'dimensions': {key: {'outcome': 'passed', 'reason': 'Explicit fixture label'} for key in
                                                     ('evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution', 'field_semantics', 'implementation_alignment')},
                                      'active_intervals': [{'started_at': '2026-10-01T00:00:00Z', 'ended_at': '2026-10-01T00:01:00Z'}]}}

    def calculate(self, reviews):
        return aggregate_feedback([self.output], reviews, [], [])['structured_assessments']

    def test_repeated_reviews_and_overlapping_time_are_not_double_counted(self):
        second = deepcopy(self.review)
        second['id'] = 'second'
        second['assessment']['active_intervals'] = [{'started_at': '2026-10-01T00:00:30Z', 'ended_at': '2026-10-01T00:01:30Z'}]
        result = self.calculate([self.review, second])
        self.assertEqual(result['human_output_coverage']['numerator'], 1)
        self.assertEqual(result['human_active_time']['observed_person_seconds'], 90)
        self.assertEqual(len(result['records']), 2)

    def test_automation_and_legacy_do_not_become_human_judgments(self):
        self.review['source'] = 'automation'
        result = self.calculate([self.review])
        self.assertEqual(result['human_output_coverage']['numerator'], 0)
        self.assertIsNone(result['human_active_time']['observed_person_seconds'])
        self.assertEqual(result['records'][0]['semantic_status'], 'not_human')
        self.review['assessment'] = None
        self.assertEqual(self.calculate([self.review])['records'], [])

    def test_unknown_is_not_passed_and_no_time_is_not_zero(self):
        self.review['assessment']['dimensions']['field_semantics']['outcome'] = 'not_assessed'
        self.review['assessment']['active_intervals'] = []
        self.review['assessment_summary'] = {'semantic_status': 'passed', 'total_active_seconds': 999}
        result = self.calculate([self.review])
        self.assertEqual(result['records'][0]['semantic_status'], 'incomplete')
        self.assertIsNone(result['human_active_time']['observed_person_seconds'])
        self.assertIsNone(result['records'][0]['recorded_active_seconds'])

    def test_invalid_target_or_impossible_intervals_fail_closed(self):
        for mutation in (
            lambda a: a.update(expected_attempt_id='different'),
            lambda a: a['active_intervals'][0].update(ended_at='2026-10-01T03:00:00Z'),
            lambda a: a['active_intervals'].append(a['active_intervals'][0].copy()),
        ):
            review = deepcopy(self.review)
            mutation(review['assessment'])
            with self.assertRaises(ValueError):
                self.calculate([review])

    def test_other_reviewers_contribute_person_time_and_outside_cohort_is_excluded(self):
        second = deepcopy(self.review)
        second['id'] = 'second'
        second['assessment']['reviewer'] = 'another'
        outside = deepcopy(self.review)
        outside.update(id='outside', result_digest='unverified')
        result = self.calculate([self.review, second, outside])
        self.assertEqual(result['human_active_time']['observed_person_seconds'], 120)
        self.assertEqual(len(result['records']), 2)


if __name__ == '__main__':
    unittest.main()
