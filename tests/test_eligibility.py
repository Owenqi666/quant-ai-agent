from copy import deepcopy
import unittest

from paper_alpha.eligibility import demo_scan, evaluate, validate_scan, validate_plan, render_report
from paper_alpha.storage import digest


class EligibilityTests(unittest.TestCase):
    def test_independent_task_requirements(self):
        scan = demo_scan()
        for task, mv, expected in [('momentum', False, 60), ('momentum_dgw', False, 20),
                                    ('momentum', True, 20), ('momentum_dgw', True, 10)]:
            scan['plan'].update(task=task, requires_market_cap=mv)
            scan['plan_digest'] = digest(scan['plan'])
            result = evaluate(scan)
            self.assertEqual(result['months'][0]['selected_ready'], expected)
            self.assertEqual(result['summary']['status'], 'screen_passed' if expected >= 30 else 'screen_blocked')
            self.assertIs(result['execution_ready'], False)
            self.assertEqual(result['summary']['months'], 2)

    def test_all_months_required_and_report_bound(self):
        scan = demo_scan()
        scan['months'][1]['patterns'][0] += 30
        scan['months'][1]['patterns'][1] = 0
        scan['months'][1]['momentum_missing'] += 30
        scan['plan']['minimum_assets'] = 31
        scan['plan_digest'] = digest(scan['plan'])
        result = evaluate(scan)
        self.assertEqual(result['summary']['months_meeting_threshold'], 1)
        self.assertEqual(result['summary']['status'], 'screen_blocked')
        report = render_report(result)
        self.assertIn('qualifying months: 1/2', report)
        self.assertIn(digest(scan), report)
        self.assertIn('aggregate consistency only', report)

    def test_reject_inconsistent_or_undeclared_counts(self):
        for mutation in (
                lambda s: s['months'][0]['patterns'].__setitem__(0, -1),
                lambda s: s['months'][0]['patterns'].__setitem__(0, True),
                lambda s: s['months'][0]['patterns'].__setitem__(1, 30.0),
                lambda s: s['months'][0].update(momentum_missing=0),
                lambda s: s['months'][0].update(label_ready=10),
                lambda s: s.update(months=s['months'][::-1]),
                lambda s: s.update(schema_version=True),
                lambda s: s['plan'].update(schema_version=1.0),
                lambda s: s['source'].update(assets=5),
                lambda s: s['plan'].update(minimum_assets=1)):
            scan = demo_scan()
            mutation(scan)
            with self.assertRaises(ValueError):
                validate_scan(scan)

    def test_calendar_reserved_boundary_and_table8_origin(self):
        for update in ({'development_start': '1989-01'}, {'development_end': '2007-01'},
                       {'reserved_from': '1993-04'}, {'reserved_from': '2021-01'},
                       {'development_start': '1993-00'}, {'rationale': ' '},
                       {'threshold_origin': 'table8_initial_upper_bound'}, {'requires_market_cap': 1},
                       {'minimum_assets': True}):
            plan = deepcopy(demo_scan()['plan'])
            plan.update(update)
            with self.assertRaises(ValueError):
                validate_plan(plan)
        plan = demo_scan()['plan']
        plan.update(task='momentum_dgw', requires_market_cap=True, minimum_assets=450,
                    threshold_origin='table8_initial_upper_bound')
        self.assertEqual(validate_plan(plan), plan)


if __name__ == '__main__':
    unittest.main()
