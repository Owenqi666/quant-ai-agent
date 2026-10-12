"""Engineering adoption preserves source unknowns and the original strict gate."""
from copy import deepcopy
import unittest

from paper_alpha.server.material_context import (ENGINEERING_CONDITIONS, build_material_projection,
    default_material_method_policy, engineering_disposition_condition,
    formula_engineering_method_policy, unresolved_inventory)
from paper_alpha.server.paper_segments import validate_material_draft, segment_document
from tests.test_paper_segments import document, material


def grounded_fixture(selection):
    value = material(selection, quote=selection['chunks'][0]['text'])
    hypothesis = value['hypotheses'][0]
    hypothesis.update(economic_mechanism=None, mechanism_attribution='unresolved',
                      calculation='close', unresolved_rules=['[scientific] Economic mechanism is unstated.'])
    bindings = [{'hypothesis_id': 'h1', 'component': name, 'evidence_ids': ['e1'],
                 'status': 'direct', 'explanation': 'Synthetic schema fixture, not semantic evidence.'}
                for name in ('claim', 'signal_direction', 'calculation', 'data:close')]
    bindings.append({'hypothesis_id': 'h1', 'component': 'economic_mechanism', 'evidence_ids': [],
                     'status': 'unresolved', 'explanation': 'The source supplies no mechanism.'})
    return {**value, 'grounding_version': 1, 'support_bindings': bindings, 'paper_statistics': []}


class FormulaEngineeringMaterialTests(unittest.TestCase):
    def setUp(self):
        # Original synthetic numbered source: unrelated to any paper's formula.
        # Complete citation support lets these cases reach their intended
        # scientific-unknown and engineering-disposition gates.
        self.selection = segment_document(document([
            'Alpha#9001: close; close is the daily session close observation. '
            'Higher values represent the named signal. '
            'This synthetic source supplies no economic mechanism or predictive-validity claim.'
        ]), [1])
        self.original = grounded_fixture(self.selection)
        self.original['hypotheses'][0]['claim'] = 'Alpha#9001 uses daily close as its displayed formula.'
        self.configured = deepcopy(self.original)
        self.dispositions = [{'path': path, 'original': rule, 'disposition': 'scientific_unresolved',
                              'rationale': 'Preserved as a scientific unknown; this task verifies execution only.'}
                             for path, rule in unresolved_inventory(self.original).items()]
        self.configured['hypotheses'][0]['conditions'].extend([
            *ENGINEERING_CONDITIONS, engineering_disposition_condition(self.dispositions)])
        self.version = {'selection': self.selection, 'material': self.configured, 'actor': 'automation',
                        'semantic_fidelity': 'unverified', 'material_id': 'material_fixture',
                        'id': 'material_version_fixture', 'digest': 'a' * 64, 'draft_digest': 'b' * 64}
        self.policy = formula_engineering_method_policy(self.configured, self.dispositions)

    def test_default_gate_still_stops_and_engineering_preserves_null_and_rules(self):
        with self.assertRaisesRegex(ValueError, 'Resolve required material rules'):
            build_material_projection(self.version, default_material_method_policy())
        projection = build_material_projection(self.version, self.policy)
        self.assertIsNone(projection['hypotheses'][0]['economic_mechanism'])
        self.assertEqual(projection['hypotheses'][0]['mechanism_attribution'], 'unresolved')
        self.assertEqual(projection['source_unresolved_rules'], unresolved_inventory(self.original))
        self.assertEqual(projection['origin']['adoption_actor'], 'automation')
        self.assertFalse(projection['human_approval'])
        self.assertEqual(self.original['hypotheses'][0]['claim_attribution'],
                         self.configured['hypotheses'][0]['claim_attribution'])

    def test_execution_gap_cannot_be_classified_as_scientific_unknown(self):
        self.version['material']['hypotheses'][0]['unresolved_rules'] = ['[engineering] Sampling frequency is unknown.']
        dispositions = [{'path': path, 'original': rule, 'disposition': 'scientific_unresolved', 'rationale': 'Incorrect classification'}
                        for path, rule in unresolved_inventory(self.version['material']).items()]
        self.version['material']['hypotheses'][0]['conditions'][-1] = engineering_disposition_condition(dispositions)
        with self.assertRaisesRegex(ValueError, 'Execution gaps'):
            build_material_projection(self.version, self.policy)

    def test_disposition_must_match_exact_original_and_blocked_stays_blocked(self):
        for patch in [{'original': 'Another rule'}, {'disposition': 'blocked'}]:
            version = deepcopy(self.version); changed = deepcopy(self.dispositions); changed[0].update(patch)
            version['material']['hypotheses'][0]['conditions'][-1] = engineering_disposition_condition(changed)
            with self.subTest(patch=patch), self.assertRaisesRegex(ValueError, 'still blocks'):
                build_material_projection(version, self.policy)

    def test_bindings_are_separate_from_exact_quote_validation(self):
        checked = validate_material_draft(self.selection, self.original)
        self.assertEqual(checked['citation_integrity'], 'verified')
        self.assertEqual(checked['semantic_fidelity'], 'unverified')
        wrong = deepcopy(self.original)
        wrong['support_bindings'] = [item for item in wrong['support_bindings'] if item['component'] != 'data:close']
        with self.assertRaisesRegex(ValueError, 'support binding'):
            validate_material_draft(self.selection, wrong)

    def test_automation_conditions_do_not_resolve_missing_calculation(self):
        wrong = deepcopy(self.version)
        wrong['material']['hypotheses'][0]['calculation'] = None
        binding = next(item for item in wrong['material']['support_bindings'] if item['component'] == 'calculation')
        binding.update(status='unresolved', evidence_ids=[])
        with self.assertRaisesRegex(ValueError, 'source calculation'):
            build_material_projection(wrong, self.policy)


if __name__ == '__main__':
    unittest.main()
