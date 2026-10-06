"""Frozen eligibility artifacts, including intentionally unauthenticated imports."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha import eligibility, eligibility_workflow as workflow
from paper_alpha.evidence import sha256
from paper_alpha.storage import atomic_json, digest, read_json
from tests.test_eligibility_archive import make_fixture, pinned_fixture, plan_fixture


class EligibilityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.path = make_fixture(self.directory)
        self.out = self.directory / 'run'
        self.plan = plan_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def run_fixture(self):
        with pinned_fixture(self.path):
            return workflow.run(self.plan, self.out, source_path=self.path)

    def rehash(self):
        manifest = read_json(self.out / 'manifest.json')
        for name in manifest['files']:
            manifest['files'][name] = sha256(self.out / name)
        manifest['input_digest'] = digest(read_json(self.out / 'input.json'))
        manifest['result_digest'] = digest(read_json(self.out / 'result.json'))
        manifest['plan_digest'] = digest(read_json(self.out / 'plan.json'))
        atomic_json(self.out / 'manifest.json', manifest)

    def test_plan_exists_before_scan_failure_preserved_and_never_overwritten(self):
        def fail(path, plan):
            self.assertEqual(read_json(self.out / 'plan.json'), plan)
            raise ValueError('Explicit fixture read failure')
        with pinned_fixture(self.path), patch.object(workflow, 'scan', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'fixture read failure'):
                workflow.run(self.plan, self.out, source_path=self.path)
        self.assertEqual(read_json(self.out / 'error.json')['status'], 'failed')
        self.assertFalse((self.out / 'manifest.json').exists())
        with pinned_fixture(self.path), self.assertRaises(FileExistsError):
            workflow.run(self.plan, self.out, source_path=self.path)

    def test_full_raw_recomputation_distinct_from_manifest_only_verification(self):
        initial = self.run_fixture()
        self.assertTrue(initial['creation_verification_claim'])
        self.assertFalse(initial['raw_source_reverified'])
        self.assertEqual(initial['verification_scope'], 'aggregate_consistency_only')
        with pinned_fixture(self.path):
            result = workflow.verify(self.out, source_path=self.path)
        self.assertTrue(result['raw_source_reverified'])
        self.assertEqual(result['verification_scope'], 'raw_source_recomputed')
        self.assertEqual(result['summary']['status'], 'screen_passed')
        with pinned_fixture(self.path), self.assertRaises(FileExistsError):
            workflow.run(self.plan, self.out, source_path=self.path)

    def test_insufficient_assets_is_a_completed_frozen_screen_not_an_error(self):
        self.plan['minimum_assets'] = 30
        result = self.run_fixture()
        self.assertTrue(result['verified'])
        self.assertEqual(result['summary']['status'], 'screen_blocked')
        self.assertTrue((self.out / 'manifest.json').is_file())
        self.assertFalse((self.out / 'error.json').exists())

    def test_rehashed_decision_report_and_receipt_tampering_is_rejected(self):
        for target in ('result', 'report', 'receipt', 'boolean_manifest'):
            with self.subTest(target=target):
                self.out = self.directory / target
                self.run_fixture()
                if target == 'result':
                    result = read_json(self.out / 'result.json'); result['summary']['status'] = 'screen_blocked'
                    atomic_json(self.out / 'result.json', result)
                elif target == 'report':
                    (self.out / 'report.md').write_text('Invented return performance', encoding='utf-8')
                elif target == 'receipt':
                    receipt = read_json(self.out / 'source_check.json'); receipt['creation_verification_claim'] = 1
                    atomic_json(self.out / 'source_check.json', receipt)
                self.rehash()
                if target == 'boolean_manifest':
                    value = read_json(self.out / 'manifest.json'); value['schema_version'] = True
                    atomic_json(self.out / 'manifest.json', value)
                with pinned_fixture(self.path), self.assertRaises(ValueError):
                    workflow.verify(self.out)

    def test_internally_consistent_false_aggregate_needs_raw_source_to_detect(self):
        self.run_fixture()
        # An attacker can forge aggregate JSON and all local hashes. They are not
        # a signature of the raw MAT. Without source, our scope must say so.
        value = read_json(self.out / 'input.json')
        value['months'][0]['patterns'][7] -= 1
        value['months'][0]['patterns'][6] += 1
        value['months'][0]['momentum_missing'] += 1
        with pinned_fixture(self.path):
            result = eligibility.evaluate(value)
        atomic_json(self.out / 'input.json', value)
        atomic_json(self.out / 'result.json', result)
        (self.out / 'report.md').write_text(eligibility.render_report(result), encoding='utf-8')
        receipt = read_json(self.out / 'source_check.json'); receipt['input_digest'] = digest(value)
        atomic_json(self.out / 'source_check.json', receipt)
        self.rehash()
        with pinned_fixture(self.path):
            checked = workflow.verify(self.out)
            self.assertFalse(checked['raw_source_reverified'])
            self.assertEqual(checked['verification_scope'], 'aggregate_consistency_only')
            with self.assertRaisesRegex(ValueError, 'source recomputation'):
                workflow.verify(self.out, source_path=self.path)

    def test_extra_symlink_hardlink_and_plan_change_are_rejected(self):
        for kind in ('extra', 'symlink', 'hardlink', 'plan'):
            self.out = self.directory / kind
            self.run_fixture()
            if kind == 'extra':
                (self.out / 'extra.txt').write_text('extra')
            elif kind == 'symlink':
                (self.out / 'unsafe').symlink_to('plan.json')
            elif kind == 'hardlink':
                os.link(self.out / 'plan.json', self.directory / 'duplicate-plan')
            else:
                changed = deepcopy(self.plan); changed['minimum_assets'] = 2
                atomic_json(self.out / 'plan.json', changed)
                self.rehash()
            with pinned_fixture(self.path), self.assertRaises(ValueError):
                workflow.verify(self.out)

    def test_cli_creates_same_frozen_result_and_snapshot_is_importable(self):
        plan_path = self.directory / 'plan.json'
        atomic_json(plan_path, self.plan)
        with pinned_fixture(self.path), patch('builtins.print') as printed:
            self.assertEqual(workflow.main(['run', '--plan', str(plan_path), '--source', str(self.path), '--out', str(self.out)]), 0)
            result = json.loads(printed.call_args.args[0])
            self.assertFalse(result['raw_source_reverified'])
            self.assertEqual(workflow.main(['verify', str(self.out), '--source', str(self.path)]), 0)
            self.assertTrue(json.loads(printed.call_args.args[0])['raw_source_reverified'])
        env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
        code = ('from pathlib import Path; from paper_alpha import eligibility_workflow; '
                'assert Path(eligibility_workflow.__file__).resolve().is_relative_to(Path.cwd())')
        subprocess.run([sys.executable, '-c', code], cwd=self.out / 'source', env=env,
                       capture_output=True, check=True, timeout=30)

    def test_removed_required_source_is_rejected_even_with_rehashed_manifest(self):
        self.run_fixture()
        original = read_json(self.out / 'manifest.json')
        for name in workflow.REQUIRED_SOURCE_FILES:
            with self.subTest(name=name):
                relative = 'source/' + name
                path = self.out / relative
                contents = path.read_bytes()
                path.unlink()
                manifest = deepcopy(original)
                del manifest['files'][relative]
                atomic_json(self.out / 'manifest.json', manifest)
                try:
                    with pinned_fixture(self.path), self.assertRaisesRegex(ValueError, 'inventory is incomplete'):
                        workflow.verify(self.out)
                finally:
                    path.write_bytes(contents)
                    atomic_json(self.out / 'manifest.json', original)

    def test_minimum_required_source_closure_can_execute_a_new_frozen_scan(self):
        self.run_fixture()
        minimal = self.directory / 'minimal-source'
        for name in workflow.REQUIRED_SOURCE_FILES:
            target = minimal / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.out / 'source' / name, target)
        # Only the explicitly enumerated dependency closure is on sys.path.
        # Test-local source metadata admits the synthetic HDF, never production.
        code = '''
import sys
from pathlib import Path
from paper_alpha import eligibility_workflow
from paper_alpha.author_archive_contract import SOURCES
from paper_alpha.storage import read_json
assert Path(eligibility_workflow.__file__).resolve().is_relative_to(Path.cwd())
scan = read_json(Path(sys.argv[1]) / 'input.json')
SOURCES[scan['source']['filename']] = scan['source']
result = eligibility_workflow.run(scan['plan'], sys.argv[3], source_path=sys.argv[2])
assert result['verified'] and not result['raw_source_reverified']
assert eligibility_workflow.verify(sys.argv[3], source_path=sys.argv[2])['raw_source_reverified']
assert read_json(Path(sys.argv[3]) / 'result.json') == read_json(Path(sys.argv[1]) / 'result.json')
'''
        env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
        subprocess.run([sys.executable, '-c', code, str(self.out), str(self.path), str(self.directory / 'minimum-run')],
                       cwd=minimal, env=env, capture_output=True, check=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
