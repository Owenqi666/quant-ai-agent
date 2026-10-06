"""Frozen provider-free evaluation evidence and unconfirmed semantic material."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.evidence import sha256
from paper_alpha.storage import read_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v018_claim_policy', ROOT / 'scripts' / 'evaluate_v018.py')
evaluation = importlib.util.module_from_spec(spec); spec.loader.exec_module(evaluation)


class ClaimPolicyEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.out = Path(cls.temp.name).resolve() / 'evaluation'
        cls.result = evaluation.run_evaluation(cls.out)

    def test_every_policy_has_retained_real_observations_or_rejections(self):
        self.assertTrue(self.result['passed'])
        self.assertEqual(self.result['cases_passed'], len(evaluation.EXPECTED))
        self.assertEqual({item['id'] for item in self.result['checks']}, evaluation.EXPECTED)
        for item in self.result['checks']:
            record = read_json(self.out / item['record'])
            self.assertTrue(record['passed']); self.assertTrue(record['checks'])
            self.assertTrue(record['observations'] or record['rejections'])
        for name, expected in read_json(self.out / 'artifact-index.json')['files'].items():
            self.assertEqual(sha256(self.out / name), expected, name)

    def test_actual_job_and_baseline_are_not_claimed_as_ai_or_market_evidence(self):
        self.assertTrue(self.result['actual_research_job_executed'])
        self.assertTrue(self.result['same_domain_baseline_equal'])
        job = read_json(self.out / 'job-final.json')
        self.assertEqual(job['state'], 'completed')
        comparison = read_json(self.out / 'baseline-comparison.json')
        self.assertEqual(comparison['actual'], comparison['baseline'])
        self.assertEqual(self.result['input_origin']['positive'], 'synthetic_market_fixture')
        self.assertFalse(self.result['llm_api_called'])
        self.assertIsNone(self.result['human_time_saved']); self.assertIsNone(self.result['model_call_cost'])
        self.assertEqual(self.result['ground_truth']['semantic_fidelity'], 'pending_human_confirmation')

    def test_semantic_material_and_five_dimensions_stay_unconfirmed(self):
        material = read_json(self.out / 'semantic_cases.json')
        self.assertEqual(len(material['cases']), 7)
        self.assertIsNone(material['confirmation']['reviewer'])
        self.assertIsNone(material['confirmation']['confirmed_at'])
        for case in material['cases']:
            self.assertEqual(case['human_confirmation']['status'], 'pending')
            self.assertIsNone(case['human_confirmation']['assessment'])
        template = read_json(self.out / 'human_review_template.json')
        self.assertEqual(set(template['assessment']['dimensions']), set(evaluation.DIMENSIONS))
        self.assertTrue(all(item['outcome'] == 'not_assessed' for item in template['assessment']['dimensions'].values()))
        self.assertIsNone(template['assessment']['reviewer'])

    def test_rerun_preserves_existing_artifact_and_failure_never_gets_pass(self):
        before = sha256(self.out / 'result.json')
        with self.assertRaises(FileExistsError):
            evaluation.run_evaluation(self.out)
        self.assertEqual(sha256(self.out / 'result.json'), before)
        failed = self.out.parent / 'failed'
        with patch.object(evaluation, 'build_sources', side_effect=ValueError('Injected source failure')):
            with self.assertRaisesRegex(ValueError, 'Injected source failure'):
                evaluation.run_evaluation(failed)
        self.assertEqual(read_json(failed / 'status.json')['status'], 'failed')
        self.assertFalse(read_json(failed / 'status.json')['passed'])
        self.assertFalse((failed / 'result.json').exists())


class ClaimPolicyManifestTests(unittest.TestCase):
    def test_human_confirmation_or_dropped_policy_cannot_silently_change_expectations(self):
        real_read = evaluation.read_json; original = evaluation.load_manifest()
        variants = []
        confirmed = deepcopy(original); confirmed['ground_truth']['semantic_fidelity'] = 'human_confirmed'; variants.append(confirmed)
        missing = deepcopy(original); missing['cases'].pop(); variants.append(missing)
        for manifest in variants:
            with patch.object(evaluation, 'read_json', side_effect=lambda path: manifest if Path(path).name == 'manifest.json' else real_read(path)):
                with self.assertRaises(AssertionError):
                    evaluation.load_manifest()


if __name__ == '__main__':
    unittest.main()
