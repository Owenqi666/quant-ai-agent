"""Controlled declarations only: no market data, genuine human label or execution."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha import research_readiness as module
from paper_alpha.research_readiness import canonical, check_readiness, freeze_digest, sha256
from scripts.check_research_readiness import main

ROOT = Path(__file__).resolve().parents[1]
AS_OF = '2026-10-01T12:00:00+00:00'


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.input = self.base / 'inputs'
        self.input.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def artifact(self, name, content=None):
        content = content if content is not None else ('Controlled non-market fixture: ' + name).encode()
        (self.input / name).write_bytes(content)
        return {'path': name, 'sha256': sha256(content)}

    def data(self):
        snapshot = lambda identity, role: {'id': identity, 'role': role, 'artifact': self.artifact(identity + '.bin')}
        return {'schema_version': 1, 'kind': 'real_data', 'id': 'controlled-contract',
            'data_kind': 'controlled_fixture', 'declaration_source': 'automation',
            'source': {'provider': 'Automated unit-test fixture', 'dataset': 'Non-market bytes',
                       'version': 'fixture-1', 'reference': 'No external source; generated inside this test'},
            'authorization': {'status': 'declared_authorized', 'basis': 'Locally generated test bytes only',
                              'permitted_use': 'Unit tests only, no real market use', 'evidence': self.artifact('permission.txt')},
            'snapshots': [snapshot('sample', 'market_data'), snapshot('calendar', 'calendar'), snapshot('members', 'universe')],
            'field_mapping': [{'target': 'close', 'source': 'fixture_close', 'unit': 'fixture units',
                               'availability_time_rule': 'Declared after the hypothetical daily close',
                               'revision_policy': 'No revisions in this controlled contract', 'snapshot_id': 'sample'}],
            'semantics': {'timezone': 'UTC', 'timestamp_key': 'fixture_date', 'asset_key': 'fixture_asset',
                          'calendar_snapshot_id': 'calendar', 'calendar_rule': 'Controlled sequence, not an exchange calendar',
                          'adjustment_rule': 'Unadjusted controlled values', 'adjustment_as_of_rule': 'No corporate actions in fixture',
                          'universe_snapshot_id': 'members', 'historical_universe_rule': 'Controlled membership declared per date',
                          'missing_value_rule': 'Reject missing fixture entries', 'suspension_rule': 'Retain suspended fixture rows',
                          'delisting_rule': 'Retain historical removed fixture assets'}}

    def heldout(self):
        manifest = {'schema_version': 1, 'kind': 'heldout_study', 'id': 'unpublished-controlled-study',
            'material_kind': 'controlled_fixture',
            'paper': {'id': 'fixture-only-paper', 'title': 'A controlled non-paper',
                      'source_reference': 'Generated test material, no actual publication', 'artifact': self.artifact('paper.bin')},
            'task': {'id': 'new-controlled-task', 'artifact': self.artifact('task.json', b'{"fixture_only":true}')},
            'manual_expected': {'source': 'human', 'declarant': 'Simulated source declaration in automated unit test; not an actual person or review',
                                'label_status': 'completed', 'artifact': self.artifact('expected.json', b'{"fixture_only":true,"actual_human_review":false}')},
            'additional_inputs': [],
            'development_exposure': {'source': 'human', 'declarant': 'Simulated test declaration only',
                                     'paper_used': False, 'task_used': False, 'expected_used': False},
            'freeze': {'status': 'frozen', 'frozen_at': '2026-09-30T09:00:00Z', 'input_digest': '',
                       'execution_state': 'not_started', 'execution_started_at': None}}
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        return manifest

    def inspect(self, manifest, *, as_of=AS_OF):
        (self.input / 'manifest.json').write_bytes(canonical(manifest))
        return check_readiness(self.input, 'manifest.json', as_of=as_of)

    def codes(self, report):
        return {reason['code'] for reason in report['reasons']}

    def test_controlled_data_contract_complete_never_enables_execution(self):
        manifest = self.data()
        # Snapshot contents intentionally are not even CSV: readiness hashes bytes,
        # whereas actual data parsing/semantics belong to a future adapter review.
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'contract_complete', result)
        self.assertEqual(result['material_scope'], 'controlled_fixture')
        self.assertEqual(len(result['files']), 4)
        self.assertEqual(result['execution'], {'enabled': False, 'synthetic_only_lock': True, 'final_test_unlocked': False})
        self.assertTrue(all(value is False for value in result['claims'].values()))
        self.assertEqual(result, self.inspect(manifest))
        stripped = {key: value for key, value in result.items() if key != 'report_sha256'}
        self.assertEqual(result['report_sha256'], sha256(canonical(stripped)))

    def test_real_market_declaration_is_at_most_adapter_review_not_validation(self):
        manifest = self.data()
        # Exercise the declaration branch with controlled bytes; never real data.
        manifest.update(data_kind='real_market', declaration_source='human')
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'ready_for_adapter_review')
        self.assertFalse(result['claims']['market_values_validated'])
        self.assertFalse(result['claims']['authorization_verified'])
        self.assertFalse(result['execution']['enabled'])

    def test_blank_templates_are_blocked_without_invented_sources_or_labels(self):
        for name in ('readiness_real_data_template.json', 'readiness_heldout_template.json'):
            with self.subTest(name=name):
                template = json.loads((ROOT / 'evaluation_suites/v09' / name).read_text())
                report = self.inspect(template)
                self.assertEqual(report['status'], 'blocked')
                self.assertTrue(report['reasons'])
                self.assertEqual(report['files'], [])

    def test_missing_authorization_availability_and_rules_block(self):
        original = self.data()
        mutations = [lambda m: m['authorization'].update(status='unknown'),
                     lambda m: m['authorization'].update(basis='TBD'),
                     lambda m: m['field_mapping'][0].update(availability_time_rule=''),
                     lambda m: m['semantics'].update(adjustment_as_of_rule=''),
                     lambda m: m['semantics'].update(historical_universe_rule=''),
                     lambda m: m['semantics'].update(delisting_rule=''),
                     lambda m: m['semantics'].update(missing_value_rule=''),
                     lambda m: m['semantics'].update(calendar_snapshot_id='sample'),
                     lambda m: m['field_mapping'][0].update(snapshot_id='calendar'),
                     lambda m: m['snapshots'].pop(0)]
        for mutate in mutations:
            copy = deepcopy(original)
            mutate(copy)
            self.assertEqual(self.inspect(copy)['status'], 'blocked')

    def test_snapshot_digest_change_is_retained_as_failure(self):
        manifest = self.data()
        (self.input / 'sample.bin').write_bytes(b'changed bytes')
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('artifact_digest_mismatch', self.codes(result))
        self.assertEqual(next(x for x in result['files'] if x['path'] == 'sample.bin')['sha256'], sha256(b'changed bytes'))

    def test_relative_paths_are_strict_and_no_outside_bytes_are_read(self):
        manifest = self.data()
        secret = self.base / 'private.txt'
        secret.write_bytes(b'private outside bytes')
        for path in ('../private.txt', str(secret), 'a/../private.txt', './sample.bin', 'a//sample.bin', 'C:\\private.txt'):
            with self.subTest(path=path):
                altered = deepcopy(manifest)
                altered['snapshots'][0]['artifact'] = {'path': path, 'sha256': sha256(secret.read_bytes())}
                result = self.inspect(altered)
                self.assertEqual(result['status'], 'blocked')
                self.assertIn('unsafe_path', self.codes(result))
                self.assertNotIn(sha256(secret.read_bytes()), [file['sha256'] for file in result['files']])

    def test_symlink_parent_leaf_root_and_hardlinks_are_rejected(self):
        manifest = self.data()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'source').write_bytes(b'outside')
        (self.input / 'parent').symlink_to(outside, target_is_directory=True)
        (self.input / 'leaf').symlink_to(outside / 'source')
        os.link(outside / 'source', self.input / 'hardlink')
        for path in ('parent/source', 'leaf', 'hardlink'):
            with self.subTest(path=path):
                altered = deepcopy(manifest)
                altered['snapshots'][0]['artifact'] = {'path': path, 'sha256': sha256(b'outside')}
                self.assertEqual(self.inspect(altered)['status'], 'blocked')
        root_alias = self.base / 'root-alias'
        root_alias.symlink_to(self.input, target_is_directory=True)
        self.assertEqual(check_readiness(root_alias, 'manifest.json')['status'], 'blocked')

    def test_fifo_rejected_without_waiting_for_a_writer(self):
        manifest = self.data()
        os.mkfifo(self.input / 'fifo')
        manifest['snapshots'][0]['artifact'] = {'path': 'fifo', 'sha256': '0' * 64}
        self.inspect(manifest)
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/check_research_readiness.py'),
            '--root', str(self.input), '--manifest', 'manifest.json', '--out', str(self.base / 'fifo-report')],
            capture_output=True, text=True, timeout=3)
        self.assertEqual(process.returncode, 2)
        report = json.loads((self.base / 'fifo-report/report.json').read_text())
        self.assertIn('not_independent_regular_file', self.codes(report))

    def test_oversize_and_total_read_budgets_fail_without_data_parsing(self):
        manifest = self.data()
        path = self.input / 'sample.bin'
        with path.open('wb') as stream:
            stream.truncate(module.MAX_FILE_BYTES + 1)
        self.assertIn('input_size_limit', self.codes(self.inspect(manifest)))
        manifest = self.data()
        with patch.object(module, 'MAX_TOTAL_BYTES', len(canonical(manifest)) + 10):
            self.assertEqual(self.inspect(manifest)['status'], 'blocked')

    def test_same_file_mutated_during_hashing_is_rejected(self):
        manifest = self.data()
        original_read = os.read
        def tamper(fd, size):
            chunk = original_read(fd, size)
            if chunk.startswith(b'Controlled non-market fixture: sample.bin'):
                (self.input / 'sample.bin').write_bytes(b'changed concurrently')
            return chunk
        with patch.object(module.os, 'read', side_effect=tamper):
            result = self.inspect(manifest)
        self.assertIn('input_changed_during_read', self.codes(result))

    def test_heldout_controlled_contract_requires_explicit_freeze_reference(self):
        manifest = self.heldout()
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'contract_complete', result)
        self.assertFalse(result['claims']['scientific_independence_approved'])
        self.assertFalse(result['claims']['human_identity_verified'])
        self.assertEqual(result, self.inspect(manifest))
        self.assertEqual(self.inspect(manifest, as_of=None)['status'], 'blocked')
        self.assertIn('freeze_in_future', self.codes(self.inspect(manifest, as_of='2026-01-01T00:00:00Z')))

    def test_heldout_pending_labels_exposure_and_started_execution_block(self):
        original = self.heldout()
        mutations = [lambda m: m['manual_expected'].update(source='automation'),
                     lambda m: m['manual_expected'].update(label_status='pending'),
                     lambda m: m['development_exposure'].update(paper_used=True),
                     lambda m: m['development_exposure'].update(task_used=None),
                     lambda m: m['freeze'].update(execution_state='started'),
                     lambda m: m['freeze'].update(execution_started_at=AS_OF)]
        for mutate in mutations:
            altered = deepcopy(original)
            mutate(altered)
            altered['freeze']['input_digest'] = freeze_digest(altered)
            self.assertEqual(self.inspect(altered)['status'], 'blocked')

    def test_freeze_binds_changed_task_or_declaration_and_detects_file_drift(self):
        manifest = self.heldout()
        manifest['task']['id'] = 'changed-after-freeze'
        self.assertIn('freeze_digest_mismatch', self.codes(self.inspect(manifest)))
        manifest = self.heldout()
        (self.input / 'expected.json').write_bytes(b'changed expectation')
        self.assertIn('artifact_digest_mismatch', self.codes(self.inspect(manifest)))

    def test_renamed_alpha101_paper_still_overlaps_by_bytes_or_normalized_identity(self):
        manifest = self.heldout()
        manifest['paper']['artifact'] = self.artifact('renamed-paper.pdf', (ROOT / 'examples/alpha101/paper.pdf').read_bytes())
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        self.assertIn('development_input_digest_overlap', self.codes(self.inspect(manifest)))
        for identity in ('arxiv:1601.00991v99', 'https://arxiv.org/pdf/1601.00991v3.pdf'):
            altered = self.heldout()
            altered['paper']['id'] = identity
            altered['freeze']['input_digest'] = freeze_digest(altered)
            self.assertIn('development_paper_overlap', self.codes(self.inspect(altered)))

    def test_development_task_ids_and_renamed_input_bytes_are_not_holdouts(self):
        manifest = self.heldout()
        manifest['task']['id'] = 'user_selected_alpha101'
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        self.assertIn('development_input_id_overlap', self.codes(self.inspect(manifest)))
        manifest = self.heldout()
        manifest['additional_inputs'] = [{'id': 'renamed-development-input', 'role': 'heldout_material',
            'artifact': self.artifact('renamed-task.json', (ROOT / 'evaluation_suites/v05/task.json').read_bytes())}]
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        self.assertIn('development_input_digest_overlap', self.codes(self.inspect(manifest)))

    def test_shared_development_market_context_is_recorded_without_holdout_claim(self):
        manifest = self.heldout()
        manifest['additional_inputs'] = [{'id': 'reused-synthetic-market', 'role': 'shared_context',
            'artifact': self.artifact('shared-market.csv', (ROOT / 'examples/alpha101/market.csv').read_bytes())}]
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'contract_complete', result)
        self.assertEqual(result['shared_development_context'], [{
            'location': 'additional_inputs[0]', 'id': 'reused-synthetic-market', 'path': 'shared-market.csv',
            'sha256': manifest['additional_inputs'][0]['artifact']['sha256'], 'matched_by': ['sha256']}])
        self.assertFalse(result['claims']['market_holdout_validated'])
        self.assertFalse(result['claims']['data_leakage_absence_verified'])
        self.assertFalse(result['claims']['scientific_independence_approved'])
        self.assertFalse(result['execution']['enabled'])
        self.assertEqual(result['files'][-1]['purpose'], 'shared_context')
        # Exactly the same context bytes cannot become held-out material by relabelling.
        manifest['additional_inputs'][0]['role'] = 'heldout_material'
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('development_input_digest_overlap', self.codes(result))

    def test_additional_input_role_required_and_core_material_cannot_be_shared(self):
        manifest = self.heldout()
        manifest['additional_inputs'] = [{'id': 'user_selected_alpha101', 'role': 'shared_context',
                                         'artifact': self.artifact('context.bin')}]
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'contract_complete', result)
        self.assertEqual(result['shared_development_context'][0]['matched_by'], ['id'])
        for role in (None, 'unknown', 'heldout_material'):
            altered = deepcopy(manifest)
            if role is None:
                del altered['additional_inputs'][0]['role']
            else:
                altered['additional_inputs'][0]['role'] = role
            altered['freeze']['input_digest'] = freeze_digest(altered)
            self.assertEqual(self.inspect(altered)['status'], 'blocked')
        manifest = self.heldout()
        manifest['paper']['role'] = 'shared_context'
        manifest['paper']['artifact'] = self.artifact('old-paper.pdf', (ROOT / 'examples/alpha101/paper.pdf').read_bytes())
        manifest['freeze']['input_digest'] = freeze_digest(manifest)
        result = self.inspect(manifest)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('unknown_fields', self.codes(result))
        self.assertIn('development_input_digest_overlap', self.codes(result))

    def test_strict_json_unknown_fields_and_bool_version_are_blocked(self):
        for raw in (b'{"kind":"real_data","kind":"heldout_study"}', b'{"x":NaN}', b'{"x":1e999}', b'[]'):
            (self.input / 'manifest.json').write_bytes(raw)
            self.assertEqual(check_readiness(self.input, 'manifest.json')['status'], 'blocked')
        for mutate in (lambda m: m.update(schema_version=True), lambda m: m.update(execute=True),
                       lambda m: m['authorization'].update(extra='no silent field acceptance')):
            manifest = self.data(); mutate(manifest)
            self.assertEqual(self.inspect(manifest)['status'], 'blocked')

    def test_cli_preserves_failure_reports_and_refuses_overwrite_or_input_destination(self):
        self.inspect(self.data())
        before = {path.name: path.read_bytes() for path in self.input.iterdir()}
        args = ['--root', str(self.input), '--manifest', 'manifest.json', '--out', str(self.base / 'report')]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)
            original = (self.base / 'report/report.json').read_bytes()
            self.assertEqual(main(args), 2)
            self.assertEqual(main([*args[:-1], str(self.input / 'invalid-output')]), 2)
        self.assertEqual((self.base / 'report/report.json').read_bytes(), original)
        self.assertEqual({path.name: path.read_bytes() for path in self.input.iterdir()}, before)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--root', str(self.input), '--manifest', 'missing.json', '--out', str(self.base / 'failed')]), 2)
        self.assertEqual(json.loads((self.base / 'failed/report.json').read_text())['status'], 'blocked')

    def test_freeze_digest_command_does_not_modify_or_approve_the_manifest(self):
        manifest = self.heldout()
        self.inspect(manifest)
        before = (self.input / 'manifest.json').read_bytes()
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(main(['--root', str(self.input), '--manifest', 'manifest.json', '--freeze-digest']), 0)
        self.assertEqual(captured.getvalue().strip(), freeze_digest(manifest))
        self.assertEqual((self.input / 'manifest.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
