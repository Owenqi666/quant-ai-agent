"""Manifest integrity and denominator/orchestration tests; mocked runs are not research evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.evaluation_suite import MODES, load_manifest, run_suite, summarize, verify_suite
from paper_alpha.evidence import sha256
from paper_alpha.server.regression import oracle_support
from paper_alpha.storage import atomic_json, read_json


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'evaluation_suites/v04/manifest.json'


class EvaluationSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = read_json(MANIFEST)
        for entry in self.manifest['tasks']:
            if entry['status'] == 'ready':
                entry['task'] = str((MANIFEST.parent / entry['task']).resolve())
        self.path = self.root / 'manifest.json'
        self.write()

    def write(self):
        atomic_json(self.path, self.manifest)

    def fake_run(self, task_path, out, mode):
        raw = read_json(task_path)
        out.mkdir(parents=True)
        task_id = task_path.parent.name
        statuses = {'alpha006': 'rejected' if mode == 'fixed' else 'evaluated',
                    'alpha101': 'evaluated', 'alpha005': 'blocked', 'canonical_corr': 'evaluated',
                    'unsafe_expr': 'rejected', 'negative_delay': 'rejected', 'changed_original': 'rejected',
                    'zero_signal': 'not_evaluable', 'alpha012': 'rejected' if mode == 'fixed' else 'evaluated',
                    'alpha033_modified': 'evaluated', 'unsupported_open': 'evaluated'}
        run_status = 'completed'
        if task_id == 'wrong_quote':
            statuses['alpha101'], run_status = 'blocked', 'failed'
        if task_id == 'budget_stop':
            statuses['alpha101'], run_status = 'budget_stopped', 'budget_exhausted'
        candidates = []
        for candidate in raw['candidates']:
            item = {**candidate, 'status': statuses[candidate['id']]}
            if item['status'] in {'evaluated', 'not_evaluable'}:
                item['result'] = {'fixture_test_marker': True, 'metrics': {'number': .125}, 'daily': [1, 2, 3]}
            candidates.append(item)
        state = {'status': run_status, 'candidates': candidates, 'tool_calls': 1,
                'preflight': {'evidence': [{**e, 'citation_verified': True} for e in raw['evidence']]} if task_id != 'wrong_quote' else None}
        atomic_json(out / 'state.json', state)
        atomic_json(out / 'manifest.json', {'signature': {'code': {'test': 'stable'}, 'mode': mode,
                    'task_sha256': sha256(task_path), 'inputs': {key: sha256(task_path.parent / raw[key])
                                                              for key in ('paper', 'paper_pdf', 'data', 'data_metadata')}}})
        return state

    def fake_oracle(self, out, cid, task):
        candidate = next(c for c in task['candidates'] if c['id'] == cid)
        checks = [{'name': 'evidence_provenance', 'outcome': 'passed', 'reason_code': 'evidence_verified', 'reason': 'Mock fixture'}]
        if oracle_support(candidate['expression'])['supported']:
            checks += [{'name': name, 'outcome': 'passed', 'reason_code': 'numeric_verified', 'reason': 'Mock fixture'}
                       for name in ('factor_values', 'daily_numerics', 'aggregate_metrics')]
        else:
            checks.append({'name': 'numerical_oracle', 'outcome': 'not_comparable', 'reason_code': 'oracle_unsupported_formula', 'reason': 'Mock unsupported fixture'})
        return {'checks': checks}

    def execute_mock(self, run=None):
        with patch('paper_alpha.evaluation_suite._source_hashes', return_value={'test': 'stable'}), \
             patch('paper_alpha.evaluation_suite.run_task', side_effect=run or self.fake_run), \
             patch('paper_alpha.evaluation_suite.verify_run', return_value={'verified': True}), \
             patch('paper_alpha.evaluation_suite.verify_candidate', side_effect=self.fake_oracle):
            return run_suite(self.path, self.root / 'output')

    def test_checked_in_manifest_is_versioned_and_reserved_material_is_pending(self):
        manifest = load_manifest(MANIFEST)
        self.assertEqual(len([e for e in manifest['tasks'] if e['partition'] == 'development']), 4)
        self.assertTrue(all(e['status'] == 'pending_annotation' for e in manifest['tasks'] if e['partition'] == 'reserved'))
        self.assertTrue(all(e['human_fidelity'] == 'unlabelled' for e in manifest['tasks']))

    def test_pending_material_alone_cannot_produce_a_passing_evaluation(self):
        self.manifest['tasks'] = [self.manifest['tasks'][-1]]
        self.write()
        load_manifest(self.path)
        with self.assertRaisesRegex(ValueError, 'No ready development'):
            run_suite(self.path, self.root / 'output')
        self.assertFalse((self.root / 'output').exists())

    def test_hash_drift_and_missing_expectations_are_rejected_before_execution(self):
        self.manifest['tasks'][0]['input_sha256']['task'] = '0' * 64
        self.write()
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            load_manifest(self.path)
        self.manifest = read_json(MANIFEST)
        self.manifest['tasks'][0]['expected'].pop('agent')
        for entry in self.manifest['tasks']:
            if entry['status'] == 'ready':
                entry['task'] = str((MANIFEST.parent / entry['task']).resolve())
        self.write()
        with self.assertRaisesRegex(ValueError, 'three policies'):
            load_manifest(self.path)

    def test_reserved_family_or_same_paper_cannot_be_relabelled_as_independent(self):
        self.manifest['tasks'][-1]['family_ids'] = ['alpha006']
        self.write()
        with self.assertRaisesRegex(ValueError, 'share a formula'):
            load_manifest(self.path)
        self.manifest['tasks'].pop()
        reserved = deepcopy(self.manifest['tasks'][1])
        reserved.update(id='fake_reserved', partition='reserved', family_ids=['renamed_family'])
        reserved['formula_groups'] = {cid: 'renamed_family' for cid in reserved['formula_groups']}
        self.manifest['tasks'].append(reserved)
        self.write()
        with self.assertRaisesRegex(ValueError, 'same paper'):
            load_manifest(self.path)

    def test_empty_records_still_retain_predeclared_denominators(self):
        result = summarize(load_manifest(self.path), [])
        self.assertEqual(result['planned_run_count'], 24)
        for mode in MODES:
            metrics = result['by_mode'][mode]
            self.assertEqual(metrics['task_handling']['denominator'], 8)
            self.assertEqual(metrics['positive_execution']['denominator'], 12)
            self.assertEqual(metrics['numerical_coverage']['denominator'], 14)
            self.assertEqual(metrics['citation_verification']['denominator'], 16)
            self.assertEqual(metrics['numerical_coverage']['numerator'], 0)
        self.assertIsNone(result['human_fidelity']['rate'])

    def test_mocked_orchestration_retains_gaps_and_repeat_contract(self):
        summary = self.execute_mock()
        self.assertEqual(len(summary['records']), 24)
        self.assertTrue(summary['acceptance_passed'])
        self.assertFalse(summary['fully_covered'])
        self.assertEqual(summary['metrics']['reproducibility']['rate'], 1)
        self.assertEqual(summary['reserved_tasks'][0]['status'], 'not_executed')
        for mode in MODES:
            metrics = summary['metrics']['by_mode'][mode]
            self.assertLess(metrics['numerical_coverage']['numerator'], metrics['numerical_coverage']['denominator'])
            self.assertGreater(metrics['nonpass_reasons']['oracle_unsupported_formula'], 0)
        self.assertEqual(read_json(self.root / 'output/summary.json'), summary)
        self.assertEqual((self.root / 'output/manifest.original.json').read_bytes(), self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'already exists'):
            run_suite(self.path, self.root / 'output')

    def test_suite_verifier_detects_summary_report_and_mode_binding_drift(self):
        summary = self.execute_mock()
        out = self.root / 'output'
        with patch('paper_alpha.evaluation_suite.verify_run', return_value={'verified': True}), \
             patch('paper_alpha.evaluation_suite.verify_candidate', side_effect=self.fake_oracle):
            self.assertTrue(verify_suite(out)['verified'])
            altered = deepcopy(summary)
            altered['metrics']['by_mode']['agent']['numerical_coverage']['numerator'] += 1
            atomic_json(out / 'summary.json', altered)
            with self.assertRaisesRegex(ValueError, 'metrics/denominators'):
                verify_suite(out)
            atomic_json(out / 'summary.json', summary)
            report = (out / 'report.md').read_text()
            (out / 'report.md').write_text(report + 'fabricated claim')
            with self.assertRaisesRegex(ValueError, 'report differs'):
                verify_suite(out)
            (out / 'report.md').write_text(report)
            first_manifest = out / summary['records'][0]['run_dir'] / 'manifest.json'
            manifest = read_json(first_manifest)
            manifest['signature']['task_sha256'] = 'different-input'
            atomic_json(first_manifest, manifest)
            with self.assertRaisesRegex(ValueError, 'same frozen task inputs'):
                verify_suite(out)

    def test_failed_execution_remains_in_every_relevant_denominator(self):
        def failing(task_path, out, mode):
            if task_path.parent.name == 'extra_formulas' and mode == 'agent':
                raise RuntimeError('Deliberate orchestration failure fixture')
            return self.fake_run(task_path, out, mode)
        result = self.execute_mock(failing)
        self.assertFalse(result['acceptance_passed'])
        self.assertEqual(len(result['records']), 24)
        self.assertEqual(result['metrics']['by_mode']['agent']['numerical_coverage']['denominator'], 14)
        self.assertEqual(result['metrics']['by_mode']['agent']['citation_verification']['denominator'], 16)
        self.assertEqual(len([r for r in result['records'] if r['execution_status'] == 'failed']), 2)

    def test_changed_observed_metric_breaks_repeat_without_rewriting_expectations(self):
        def drifting(task_path, out, mode):
            state = self.fake_run(task_path, out, mode)
            if out.name == '2' and mode == 'agent' and task_path.parent.name == 'extra_formulas':
                state['candidates'][0]['result']['metrics']['number'] = 123
            return state
        result = self.execute_mock(drifting)
        self.assertFalse(result['acceptance_passed'])
        self.assertLess(result['metrics']['reproducibility']['numerator'], result['metrics']['reproducibility']['denominator'])

    def test_source_change_retains_unexecuted_plan_and_fails_acceptance(self):
        with patch('paper_alpha.evaluation_suite._source_hashes', side_effect=[{'v': '1'}, {'v': '2'}, {'v': '2'}]), \
             patch('paper_alpha.evaluation_suite.run_task') as run:
            result = run_suite(self.path, self.root / 'output')
        run.assert_not_called()
        self.assertFalse(result['acceptance_passed'])
        self.assertFalse(result['source_unchanged'])
        self.assertEqual(len(result['records']), 24)
        self.assertTrue(all(r['execution_status'] == 'not_run' for r in result['records']))
        self.assertEqual(result['metrics']['by_mode']['agent']['task_handling']['denominator'], 8)


if __name__ == '__main__':
    unittest.main()
