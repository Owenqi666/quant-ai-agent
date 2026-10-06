"""Offline declared human annotations remain separate from software truth."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest

from paper_alpha.evidence import sha256
from paper_alpha.storage import atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v018_semantic_confirmation', ROOT / 'scripts' / 'confirm_v018_semantics.py')
confirmation = importlib.util.module_from_spec(spec); sys.modules[spec.name] = confirmation; spec.loader.exec_module(confirmation)


class HumanMaterialDeclarationTests(unittest.TestCase):
    def request(self):
        return {'schema_version': 1, 'material_sha256': sha256(confirmation.MATERIAL), 'declaration': 'human_annotation',
                'annotations': [{'case_id': 'alpha101_formula', 'reviewer': 'Synthetic validation test identity; not real user judgment',
                                 'confirmed_at': '2020-01-01T00:00:00+00:00',
                                 'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Synthetic parser test only.'} for name in confirmation.DIMENSIONS}}]}

    def test_valid_declaration_binds_exact_material_but_cannot_approve_result(self):
        result = confirmation.validate_declarations(self.request())
        self.assertFalse(result['identity_verified'])
        self.assertFalse(result['software_verified_semantic_truth'])
        annotation = result['annotations'][0]
        self.assertEqual(annotation['declared_semantic_status'], 'incomplete')
        self.assertFalse(annotation['original_result_approval'])
        self.assertEqual(len(annotation['material_case_digest']), 64)

    def test_wrong_hash_foreign_case_duplicate_and_forged_scope_fail(self):
        variants = []
        value = self.request(); value['material_sha256'] = '0' * 64; variants.append(value)
        value = self.request(); value['annotations'][0]['case_id'] = 'foreign'; variants.append(value)
        value = self.request(); value['annotations'] *= 2; variants.append(value)
        value = self.request(); value['identity_verified'] = True; variants.append(value)
        for value in variants:
            with self.assertRaises(ValueError):
                confirmation.validate_declarations(value)

    def test_reviewer_aware_time_and_all_dimensions_are_required(self):
        variants = []
        value = self.request(); value['annotations'][0]['reviewer'] = ' '; variants.append(value)
        value = self.request(); value['annotations'][0]['confirmed_at'] = '2020-01-01T00:00:00'; variants.append(value)
        value = self.request(); value['annotations'][0]['confirmed_at'] = '2999-01-01T00:00:00+00:00'; variants.append(value)
        value = self.request(); value['annotations'][0]['dimensions'].pop('field_semantics'); variants.append(value)
        value = self.request(); value['annotations'][0]['dimensions']['field_semantics']['reason'] = ' '; variants.append(value)
        for value in variants:
            with self.assertRaises(ValueError):
                confirmation.validate_declarations(value)

    def test_file_entry_preserves_original_template_and_never_overwrites_output(self):
        before = sha256(confirmation.MATERIAL)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); source = root / 'declared.json'; atomic_json(source, self.request())
            out = root / 'confirmation'; result = confirmation.confirm(source, out)
            self.assertEqual(result['annotations'], 1)
            self.assertEqual(read_json(out / 'status.json')['scope'], 'declaration_validation')
            self.assertFalse(read_json(out / 'result.json')['software_verified_semantic_truth'])
            with self.assertRaises(FileExistsError):
                confirmation.confirm(source, out)
        self.assertEqual(sha256(confirmation.MATERIAL), before)

    def test_unfilled_template_rejected_and_failure_evidence_saved(self):
        template = read_json(confirmation.MATERIAL.with_name('human_review_template.json'))['standalone_material_annotation']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); source = root / 'pending.json'; atomic_json(source, template)
            with self.assertRaises(ValueError):
                confirmation.confirm(source, root / 'failed')
            status = read_json(root / 'failed' / 'status.json')
            self.assertFalse(status['passed']); self.assertEqual(status['status'], 'failed')
            self.assertFalse((root / 'failed' / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
