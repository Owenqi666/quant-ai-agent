"""Original public input, fixture-only formal workflow, independent arithmetic.

No model/credential/network provider is used. Failure and tamper cases operate
only in an ephemeral test workspace and restore original bytes afterward.
"""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from paper_alpha.server.api import create_app
from scripts.demo_public_agent import run, ASSETS
from scripts.check_public_agent_demo import check, load, sha, reference


class PublicAgentDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name).resolve() / 'agent'
        cls.summary = run(cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_formal_fixture_rejection_and_worker_completion_preserve_unknowns(self):
        summary = self.summary
        self.assertEqual((summary['fixture_dispatches'], summary['real_model_dispatches'], summary['worker_runs']), (3, 0, 1))
        invalid = load(self.output / 'proposals/invalid/status.json')
        self.assertEqual(invalid['status'], 'failed')
        self.assertEqual(invalid['error']['code'], 'REQUEST_MISMATCH')
        self.assertIsNone(invalid['accepted_response'])
        self.assertTrue(invalid['attempts'][0]['receipt']['fixture'])
        raw = load(self.output / 'proposals/extract/raw-response.json')
        imported = load(self.output / 'material/import.json')
        host = load(self.output / 'material/component-binding-version.json')
        engineering = load(self.output / 'material/engineering-version.json')
        self.assertNotIn('grounding_version', raw['material'])
        self.assertNotIn('support_bindings', imported['draft'])
        self.assertTrue(host['host_authored_component_bindings']); self.assertFalse(host['is_model_output'])
        self.assertEqual(host['version']['material']['hypotheses'], imported['draft']['hypotheses'])
        self.assertEqual(host['version']['material']['evidence'], imported['draft']['evidence'])
        self.assertEqual(host['version']['actor'], 'automation')
        self.assertEqual(engineering['version']['version'], 2)
        self.assertIsNone(engineering['version']['material']['hypotheses'][0]['economic_mechanism'])
        self.assertEqual(engineering['version']['material']['hypotheses'][0]['mechanism_attribution'], 'unresolved')
        self.assertFalse(engineering['human_approval'])
        experiment = load(self.output / 'experiment.json')
        self.assertEqual(experiment['job']['state'], 'completed')
        self.assertEqual([s['action'] for s in experiment['job']['steps']], ['validate', 'commit', 'submit', 'observe', 'complete'])
        self.assertEqual(experiment['reservation']['status'], 'finished')
        self.assertEqual(experiment['reservation']['outcome']['status'], 'completed')

    def test_reference_arithmetic_alignment_tie_skip_and_all_metrics(self):
        result = check(self.output)
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertEqual(result['reference_factor_cells'], 216)
        self.assertEqual(result['reference_metrics']['eligible_days'], 18)
        self.assertEqual(result['reference_metrics']['evaluated_days'], 17)
        self.assertEqual(result['reference_metrics']['skipped_days'], 1)
        self.assertEqual(result['reference_metrics']['purged_days'], 2)
        expected = reference(self.output / 'input/market.csv', load(self.output / 'input/metadata.json'), load(self.output / 'input/evaluation.json'))
        first = next(d for d in expected['daily'] if d['status'] == 'evaluated')
        self.assertGreater(first['entry_date'], first['signal_date']); self.assertGreater(first['exit_date'], first['entry_date'])
        self.assertAlmostEqual(sum(a['weight'] for a in first['assets']), 0)
        self.assertAlmostEqual(sum(abs(a['weight']) for a in first['assets']), 1)
        self.assertEqual(len([d for d in expected['daily'] if d['reason'] == 'constant_factor']), 1)
        self.assertIsNone(result['human_review']['scores'])

    def test_changed_report_number_is_detected_independently(self):
        path = self.output / 'report.json'; original = path.read_bytes()
        try:
            report = load(path)
            report['payload']['runs'][0]['candidates'][0]['result']['metrics']['mean_gross_return'] += .01
            path.write_text(json.dumps(report))
            result = check(self.output)
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(next(c for c in result['checks'] if c['name'] == 'tool_database_and_report_numbers')['outcome'], 'failed')
        finally:
            path.write_bytes(original)

    def test_factor_byte_tamper_is_not_hidden_by_saved_metrics(self):
        report = load(self.output / 'report.json'); prun = report['payload']['runs'][0]
        name = next(a['name'] for a in prun['artifacts'] if a['name'].endswith('/factor.csv'))
        attempt = prun['candidates'][0]['metric_source']['attempt_id']
        path = self.output / 'workspace/runs' / self.summary['run_id'] / 'attempts' / attempt / 'exports' / name
        original = path.read_bytes()
        try:
            lines = original.decode().splitlines(); fields = lines[1].split(','); fields[1] = str(float(fields[1]) + .25)
            lines[1] = ','.join(fields); path.write_text('\n'.join(lines) + '\n')
            result = check(self.output)
            self.assertEqual(result['outcome'], 'failed')
            self.assertEqual(next(c for c in result['checks'] if c['name'] == 'independent_close_minus_open_factor')['outcome'], 'failed')
            self.assertEqual(next(c for c in result['checks'] if c['name'] == 'artifact_bytes_and_report_receipts')['outcome'], 'failed')
        finally:
            path.write_bytes(original)

    def test_normal_app_restores_same_workspace_without_an_account(self):
        with TestClient(create_app(self.output / 'workspace')) as client:
            study = client.get('/api/researches/' + self.summary['research_id'])
            self.assertEqual(study.status_code, 200, study.text)
            self.assertEqual(len(study.json()['revisions']), 2)
            self.assertIsNone(study.json()['revisions'][-1]['task']['hypotheses'][0]['economic_mechanism'])
            run = client.get('/api/runs/' + self.summary['run_id'])
            self.assertEqual(run.status_code, 200, run.text)
            self.assertTrue(run.json()['verification']['verified']); self.assertEqual(run.json()['reviews'], [])
            report = client.get('/api/research-reports/' + self.summary['report_id'])
            self.assertEqual(report.status_code, 200, report.text)
            self.assertEqual(report.json()['payload']['selected_run_ids'], [self.summary['run_id']])

    def test_existing_output_and_original_public_assets_are_preserved(self):
        before = sha(self.output / 'demo-summary.json')
        with self.assertRaisesRegex(ValueError, 'new or empty'):
            run(self.output)
        self.assertEqual(sha(self.output / 'demo-summary.json'), before)
        context = load(self.output / 'proposals/extract/context.json')
        self.assertNotIn('evidence', context); self.assertNotIn('hypotheses', context)
        self.assertNotIn('development', context); self.assertNotIn('grounding_policy', context)
        self.assertEqual(context['paper_selection']['selected_pages'], [1, 2])
        self.assertEqual(context['paper_selection']['paper_sha256'], sha(ASSETS / 'teaching_note.pdf'))
        for source in (self.output / 'proposals').rglob('*.json'):
            self.assertNotIn('paper_alpha101', source.read_text())
            self.assertNotIn('Alpha#12', source.read_text())


if __name__ == '__main__':
    unittest.main()
