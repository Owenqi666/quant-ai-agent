"""Task-level policy evidence must remain distinct from human semantic labels."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.evidence import sha256
from paper_alpha.storage import atomic_json, digest, read_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v017_policy_evaluator', ROOT / 'scripts' / 'evaluate_v017.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
import demo_v017


class ResearchDecisionSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.out = Path(cls.temp.name) / 'evaluation'
        cls.result = evaluation.run_evaluation(cls.out)

    def test_all_declared_rules_have_saved_observable_evidence(self):
        manifest = demo_v017.load_manifest()
        self.assertTrue(self.result['passed'])
        self.assertEqual(self.result['cases_total'], len(manifest['cases']))
        self.assertEqual(self.result['cases_passed'], self.result['cases_total'])
        self.assertEqual({item['id'] for item in self.result['checks']}, {item['id'] for item in manifest['cases']})
        for check in self.result['checks']:
            record = read_json(self.out / check['record'])
            self.assertTrue(record['passed'])
            self.assertTrue(record['checks'])
            self.assertTrue(record['calls'])
            self.assertTrue(record['sessions'])
        index = read_json(self.out / 'artifact-index.json')
        for name, expected in index['files'].items():
            self.assertEqual(sha256(self.out / name), expected, name)

    def test_report_does_not_upgrade_engineering_checks_to_semantic_truth(self):
        truth = self.result['ground_truth']
        self.assertEqual(truth['semantic_fidelity'], 'pending_human_confirmation')
        self.assertIsNone(truth['human_reviewer'])
        self.assertIsNone(truth['human_confirmed_at'])
        self.assertFalse(self.result['llm_api_called'])
        self.assertIsNone(self.result['human_time_saved'])
        self.assertIsNone(self.result['model_call_cost'])
        self.assertFalse(self.result['input_origin']['raw_source_reverified'])
        self.assertEqual(self.result['input_origin']['positive'], 'controlled_fixture')
        self.assertEqual(self.result['input_origin']['negative'], 'synthetic_aggregate_fixture')

    def test_read_result_contains_the_computed_monthly_payload(self):
        record = read_json(self.out / 'decisions' / 'computed_result_trace.json')
        fixed = read_json(self.out / 'fixed-monthly-result.json')
        recorded = read_json(self.out / 'monthly-detail.json')
        self.assertEqual(fixed, recorded['result'])
        self.assertTrue(recorded['verification']['reference_passed'])
        self.assertIn('exact_result_reference', record['checks'])
        self.assertEqual(read_json(self.out / 'negative-case.json')['context']['state'], 'data_insufficient')

    def test_reexecution_never_overwrites_an_accepted_directory(self):
        before = sha256(self.out / 'result.json')
        with self.assertRaises(FileExistsError):
            evaluation.run_evaluation(self.out)
        self.assertEqual(sha256(self.out / 'result.json'), before)

    def test_a_failure_retains_status_and_never_emits_a_pass_report(self):
        failed = Path(self.temp.name) / 'failed'
        with patch.object(evaluation, 'build_sources', side_effect=ValueError('Injected source preparation failure')):
            with self.assertRaisesRegex(ValueError, 'Injected source preparation'):
                evaluation.run_evaluation(failed)
        status = read_json(failed / 'status.json')
        self.assertEqual(status['status'], 'failed')
        self.assertFalse(status['passed'])
        self.assertTrue((failed / 'manifest.json').exists())
        self.assertFalse((failed / 'result.json').exists())
        self.assertFalse((failed / 'report.md').exists())


class ResearchDecisionManifestTests(unittest.TestCase):
    def test_unconfirmed_semantics_and_policy_cases_cannot_be_silently_relabelled(self):
        manifest = demo_v017.load_manifest()
        variants = []
        confirmed = deepcopy(manifest)
        confirmed['ground_truth']['semantic_fidelity'] = 'human_confirmed'
        variants.append(confirmed)
        dropped = deepcopy(manifest)
        dropped['cases'].pop()
        variants.append(dropped)
        for value in variants:
            with patch.object(demo_v017, 'read_json', return_value=value):
                with self.assertRaisesRegex(ValueError, 'frozen suite requires'):
                    demo_v017.load_manifest()

    def test_frozen_input_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory)
            manifest = demo_v017.load_manifest()
            atomic_json(suite / 'manifest.json', manifest)
            scan = read_json(demo_v017.SUITE / 'blocked_scan.json')
            scan['plan']['rationale'] += ' Changed.'
            scan['plan_digest'] = digest(scan['plan'])
            atomic_json(suite / 'blocked_scan.json', scan)
            with patch.object(demo_v017, 'SUITE', suite):
                with self.assertRaisesRegex(ValueError, 'input digest changed'):
                    demo_v017.load_manifest()


if __name__ == '__main__':
    unittest.main()
