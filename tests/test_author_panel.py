"""Synthetic contract cases, not claims of extraction from the real author MAT."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

from paper_alpha import author_panel as core, author_reference as reference, author_workflow as workflow
from paper_alpha.author_archive_contract import ADAPTER_VERSION, source_metadata
from paper_alpha.storage import atomic_json, digest, read_json
from paper_alpha.evidence import sha256


def panel_fixture(count=2, filename='IntnlData.mat'):
    months = ['2019-' + f'{i:02d}' for i in range(1, 13)] + ['2020-01']
    return {'schema_version': 1, 'kind': 'author_perturbed_monthly_panel', 'adapter_version': ADAPTER_VERSION,
            'source': source_metadata(filename), 'selection': {'target_month': '2020-01', 'row_offset': 0, 'row_count': count},
            'months': months, 'source_observation_dates': [m + '-28' for m in months] if filename == 'USData.mat' else [None] * 13,
            'rows': [{'source_row': i + 1, 'asset': f'{filename}:row:{i+1}', 'country': 'AE' if filename == 'IntnlData.mat' else None,
                      'returns': [.1] * 11 + [9., .25], 'return_states': ['value'] * 13,
                      'dgw': .2, 'dgw_state': 'value', 'market_cap': 100., 'market_cap_state': 'value'} for i in range(count)]}


class AuthorPanelTests(unittest.TestCase):
    def checked(self, p):
        result = core.evaluate(p)
        self.assertTrue(reference.check(p, result)['passed'])
        return result

    def test_exact_window_excludes_skip_and_future(self):
        p = panel_fixture()
        r = self.checked(p)
        self.assertAlmostEqual(r['rows'][0]['momentum'], float(Fraction(11, 10) ** 11 - 1), places=12)
        self.assertEqual(r['windows']['momentum_months'], p['months'][:11])
        self.assertEqual(r['summary']['formation_ready'], 2)
        p['rows'][0]['returns'][-2:] = [-.999, None]
        p['rows'][0]['return_states'][-1] = 'nan'
        changed = self.checked(p)
        self.assertEqual(changed['rows'][0]['momentum'], r['rows'][0]['momentum'])
        self.assertTrue(changed['rows'][0]['formation_ready'])
        self.assertFalse(changed['rows'][0]['label_ready'])
        self.assertEqual(changed['summary']['formation_ready'], 2)

    def test_missing_history_raw_zero_cap_invalid_return_and_unclipped_label(self):
        p = panel_fixture(3)
        p['rows'][0]['returns'][4] = None; p['rows'][0]['return_states'][4] = 'nan'
        p['rows'][1]['returns'][2] = -1.01
        p['rows'][1]['market_cap'] = 0.
        p['rows'][2]['returns'][-1] = 24.
        r = self.checked(p)
        self.assertIsNone(r['rows'][0]['momentum'])
        self.assertEqual(r['rows'][0]['missing_momentum_months'], ['2019-05'])
        self.assertIn('momentum_invalid_return:2019-03', r['rows'][1]['reasons'])
        self.assertIn('market_cap_not_positive', r['rows'][1]['reasons'])
        self.assertEqual(r['rows'][1]['market_cap'], 0)
        self.assertEqual(r['rows'][2]['label'], 24)
        self.assertTrue(r['rows'][2]['label_ready'])

    def test_nonfinite_states_and_zero_readiness_are_valid(self):
        p = panel_fixture(1)
        p['rows'][0].update(dgw=None, dgw_state='posinf')
        r = self.checked(p)
        self.assertEqual(r['summary']['formation_ready'], 0)
        self.assertIn('dgw_missing:posinf', r['rows'][0]['reasons'])
        self.assertIn('formation available: 0', core.render_report(r))

    def test_zero_factor_extremes_and_overflow(self):
        p = panel_fixture(3)
        p['rows'][0]['returns'][:11] = [1e308]*10 + [-1.]
        p['rows'][1]['returns'][:11] = [1e308]*11
        p['rows'][2]['returns'][:11] = [-.9999999999999999]*10 + [1e150]
        r = self.checked(p)
        self.assertEqual(r['rows'][0]['momentum'], -1.)
        self.assertIsNone(r['rows'][1]['momentum'])
        self.assertIn('momentum_not_representable', r['rows'][1]['reasons'])

    def test_closed_shape_calendar_identity_state_and_type_guards(self):
        mutations = [lambda p: p.update(schema_version=True), lambda p: p.update(extra=1),
            lambda p: p['selection'].update(row_offset=True), lambda p: p['source'].update(sha256='a'*64),
            lambda p: p['months'].__setitem__(0, '2018-12'), lambda p: p['rows'][0].update(source_row=2),
            lambda p: p['rows'][0]['returns'].__setitem__(1, True), lambda p: p['rows'][0]['returns'].__setitem__(1, float('nan')),
            lambda p: p['rows'][0].update(dgw=None), lambda p: p['rows'][0].update(country='<script>'),
            lambda p: p['source_observation_dates'].__setitem__(0, '2019-01-31')]
        for mutation in mutations:
            p = panel_fixture(); mutation(p)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError)):
                core.validate_panel(p)
        us = panel_fixture(filename='USData.mat')
        self.checked(us)  # Actual observation date need not be calendar month end.
        us['source_observation_dates'][1] = '2019-02-30'
        with self.assertRaises(ValueError): core.validate_panel(us)

    def test_independent_reference_catches_numerical_eligibility_and_report_claims(self):
        p = panel_fixture()
        for mutation in (lambda r: r['rows'][0].update(momentum=0.), lambda r: r['rows'][0].update(formation_ready=False),
                         lambda r: r['summary'].update(formation_ready=1), lambda r: r['windows'].update(skip_month='2019-11'),
                         lambda r: r.update(returns_included_in_formation=0), lambda r: r.update(limitations=[])):
            r = core.evaluate(p); mutation(r)
            self.assertFalse(reference.check(p, r)['passed'])

    def test_frozen_workflow_and_rehashed_false_result_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve() / 'run'
            verified = workflow.run(panel_fixture(), out)
            self.assertTrue(verified['verified'])
            self.assertFalse(verified['raw_source_reverified'])
            self.assertFalse(verified['raw_file_verified_at_creation'])
            with self.assertRaises(FileExistsError): workflow.run(panel_fixture(), out)
            r = read_json(out / 'result.json'); r['rows'][0]['momentum'] = 0.
            atomic_json(out / 'result.json', r)
            (out / 'report.md').write_text(core.render_report(r))
            m = read_json(out / 'manifest.json'); m['result_digest'] = digest(r)
            for name in ('result.json','report.md'): m['files'][name] = sha256(out / name)
            atomic_json(out / 'manifest.json', m)
            with self.assertRaisesRegex(ValueError, 'reference'): workflow.verify(out)

    def test_extra_files_symlinks_and_report_tampering_rejected(self):
        for kind in ('extra', 'symlink', 'report'):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp).resolve() / 'run'; workflow.run(panel_fixture(), out)
                if kind == 'extra': (out / 'rogue').write_text('unregistered')
                elif kind == 'symlink': (out / 'link').symlink_to('input.json')
                else:
                    (out / 'report.md').write_text('made up statistic')
                    m = read_json(out / 'manifest.json'); m['files']['report.md'] = sha256(out / 'report.md'); atomic_json(out / 'manifest.json', m)
                with self.assertRaises(ValueError): workflow.verify(out)

    def test_reference_boolean_number_alias_cannot_pass_rehashed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve() / 'run'; workflow.run(panel_fixture(), out)
            ref = read_json(out / 'reference.json'); ref.update(schema_version=True, passed=1)
            atomic_json(out / 'reference.json', ref)
            m = read_json(out / 'manifest.json'); m['files']['reference.json'] = sha256(out / 'reference.json'); atomic_json(out / 'manifest.json', m)
            with self.assertRaises(ValueError): workflow.verify(out)

    def test_frozen_source_imports_and_recomputes_without_checkout(self):
        import os, subprocess, sys
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp).resolve() / 'run'; workflow.run(panel_fixture(), out)
            env = {k: v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
            code = "import json; from pathlib import Path; from paper_alpha.author_panel import evaluate; from paper_alpha import author_workflow; from paper_alpha.storage import read_json; assert Path(author_workflow.__file__).resolve().is_relative_to(Path.cwd()); assert evaluate(read_json('../input.json')) == read_json('../result.json')"
            subprocess.run([sys.executable, '-c', code], cwd=out / 'source', env=env, check=True, capture_output=True, timeout=30)
