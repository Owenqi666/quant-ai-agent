"""Compatibility, negative cases and review lineage through the real store."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from paper_alpha.server.db import transaction
from paper_alpha.server.service import ServiceError
from tests import test_server


class RegressionServiceTests(unittest.TestCase):
    setUp = test_server.ServerCase.setUp
    tearDown = test_server.ServerCase.tearDown
    execute = test_server.ServerCase.execute

    def approve(self, run, candidate='alpha006', status='evaluated'):
        review = self.store.create_review(run['id'], candidate, 'accepted', 'implementation',
                                         'Automated fixture review, not economic judgment')
        return self.store.approve_case(review['id'], status, 'Explicit test expectation')

    def test_alias_repair_passes_but_changed_window_does_not_compare(self):
        first, _ = self.execute(mode='fixed')
        case = self.approve(first)
        self.assertEqual(case['version'], 2)
        before = self.store.run_regression_check(first['id'], [case['id']])
        self.assertEqual(before['outcome'], 'failed')
        self.assertEqual(len(before['results'][0]['checks']), 1)
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][0]['task'])
        task['candidates'][0]['expression'] = '-1 * ts_corr(open, volume, 10)'
        task['budget']['max_tool_calls'] += 1
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Spelling only')
        second, _ = self.execute('canonical', mode='fixed', revision_id=revision['id'])
        after = self.store.run_regression_check(second['id'], [case['id']])
        self.assertEqual(after['outcome'], 'passed', after)
        self.assertEqual({c['name'] for c in after['results'][0]['checks']},
                         {'candidate_status', 'evidence_provenance', 'factor_values', 'daily_numerics', 'aggregate_metrics'})
        task['candidates'][0].update(expression='-1 * ts_corr(open, volume, 5)',
                                     origin='user_modification', changes=['Explicitly shortened lookback'])
        revision = self.store.create_revision(research['id'], revision['id'], task, 'New hypothesis variant')
        third, _ = self.execute('window', revision_id=revision['id'])
        check = self.store.run_regression_check(third['id'], [case['id']])
        self.assertEqual(check['outcome'], 'not_comparable')
        self.assertFalse(check['results'][0]['compatible'])
        self.assertIn('candidate.expression', check['results'][0]['differences'])

    def test_legacy_cases_do_not_acquire_new_approvals_implicitly(self):
        run, _ = self.execute()
        case = self.approve(run)
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE regression_cases SET version=1,contract=NULL,contract_digest=NULL WHERE id=?', (case['id'],))
        before = self.store.run_regression_check(run['id'], [case['id']])
        self.assertEqual(before['outcome'], 'not_comparable')
        self.assertIn('legacy_case_requires_reapproval', before['results'][0]['differences'])
        renewed = self.store.approve_case(case['review_id'], 'evaluated', 'Explicitly approve frozen scientific contract')
        self.assertNotEqual(renewed['id'], case['id'])
        self.assertIsNone(next(c for c in self.store.list_cases() if c['id'] == case['id'])['contract'])
        self.assertTrue(self.store.run_regression_check(run['id'], [renewed['id']])['passed'])

    def test_old_review_remains_bound_to_its_original_attempt_after_retry(self):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][0]['task'])
        task['evidence'][0]['quote'] = 'Deliberately absent quote for a negative regression fixture.'
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Negative citation')
        run, claim = self.execute(revision_id=revision['id'])
        self.assertEqual(run['status'], 'failed')
        review = self.store.create_review(run['id'], 'alpha006', 'rejected', 'evidence', 'Wrong quote should block execution')
        self.store.retry_run(run['id'])
        self.assertIsNone(self.store.get_run(run['id'])['state'])
        case = self.store.approve_case(review['id'], 'blocked', 'Keep original attempt evidence')
        self.assertEqual(case['review_id'], review['id'])
        self.assertEqual(case['contract']['evidence'][0]['quote'], task['evidence'][0]['quote'])
        (Path(claim['output_dir']) / 'state.json').write_text('{}')
        with self.assertRaises(ServiceError):
            self.store.approve_case(review['id'], 'blocked', 'Must not approve tampered historical attempt')

    def test_historical_contract_uses_recorded_semantics(self):
        run, _ = self.execute()
        case = self.approve(run)
        # Current imports cannot relabel an old experiment's engine semantics.
        with patch('paper_alpha.server.regression.OPERATOR_SEMANTICS_VERSION', 'future-unrelated-engine'):
            another = self.approve(run)
        self.assertEqual(case['contract']['operator_semantics'], another['contract']['operator_semantics'])
        self.assertEqual(case['contract_digest'], another['contract_digest'])

    def test_evaluated_unsupported_formula_is_not_a_false_green(self):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][0]['task'])
        task['candidates'][0].update(expression='-1 * ts_corr(open, volume, 5)',
                                     origin='user_modification', changes=['Oracle negative coverage fixture'])
        revision = self.store.create_revision(research['id'], self.example['revision_id'], task, 'Unsupported oracle formula')
        run, _ = self.execute(revision_id=revision['id'])
        case = self.approve(run)
        check = self.store.run_regression_check(run['id'], [case['id']])
        self.assertEqual(check['results'][0]['actual_status'], 'evaluated')
        self.assertTrue(check['results'][0]['compatible'])
        self.assertEqual(check['outcome'], 'not_comparable')
        self.assertFalse(check['passed'])

    def test_reference_computation_allows_writes_but_changed_inputs_block_publication(self):
        from paper_alpha.server import service
        run, _ = self.execute()
        case = self.approve(run)
        original = service.verify_candidate
        def compute_with_queue_write(*args):
            self.store.submit_run(self.example['revision_id'], 'fixed', 'concurrent-queue')
            return original(*args)
        with patch.object(service, 'verify_candidate', side_effect=compute_with_queue_write):
            check = self.store.run_regression_check(run['id'], [case['id']])
        self.assertTrue(check['passed'])
        self.assertLess(check['timing']['write_transaction_before_publish_seconds'], check['timing']['total_before_publish_seconds'])
        count = len(self.store.list_checks())
        def change_approval(*args):
            result = original(*args)
            with transaction(self.store.db_path) as connection:
                connection.execute('UPDATE regression_cases SET note=? WHERE id=?', ('changed concurrently', case['id']))
            return result
        with patch.object(service, 'verify_candidate', side_effect=change_approval):
            with self.assertRaisesRegex(ServiceError, 'changed during verification'):
                self.store.run_regression_check(run['id'], [case['id']])
        self.assertEqual(len(self.store.list_checks()), count)

    def test_artifact_change_during_recompute_cannot_publish_a_check(self):
        from paper_alpha.server import service
        run, job = self.execute()
        case = self.approve(run)
        original = service.verify_candidate
        def alter_result(*args):
            result = original(*args)
            (Path(job['output_dir']) / 'report.md').write_text('Altered after computation')
            return result
        with patch.object(service, 'verify_candidate', side_effect=alter_result):
            with self.assertRaises(ServiceError):
                self.store.run_regression_check(run['id'], [case['id']])
        self.assertEqual(self.store.list_checks(), [])


if __name__ == '__main__':
    unittest.main()
