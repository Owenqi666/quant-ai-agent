"""Public synthetic preservation, window, and complete-definition regressions.

Every case supplies explicit source text, without private historical responses,
model calls, or claims of human research-quality review.
"""
import ast
from copy import deepcopy
import unittest

from paper_alpha.server.operator_definition_grounding import (
    assess_operator_definitions, plan_operator_definition_repair,
)
from paper_alpha.server.paper_material_spans import build_span_catalog
from paper_alpha.server.paper_segments import segment_document, validate_material_draft
from paper_alpha.paper_research_preflight import assess as execution_preflight
from paper_alpha.expressions import validate_expression
from paper_alpha.storage import digest
from tests.test_paper_segments import document


FORMULA = '(sign(delta(volume, 1)) * (-1 * delta(close, 1)))'
SIGN = 'abs(x), log(x), sign(x) = standard definitions'
DELTA = 'delta(x, d) = today’s value of x minus the value of x d days ago'
RANK = 'rank(x) = cross-sectional rank'
TS_RANK = 'ts_rank(x, d) = time-series rank in the past d days'
CORR = 'correlation(x, y, d) = time-serial correlation of x and y for the past d days'
COVARIANCE = 'covariance(x, y, d) = time-serial covariance of x and y for the past d days'


def synthetic(formula=FORMULA, definitions=(SIGN, DELTA), *, cited_definition_pages=None,
              selected_pages=None, calculation=None, source_formula=None,
              chunk_chars=4000, max_span_chars=600):
    """Each test provides its source statements explicitly, never a provider call."""
    texts = ['Alpha#12: ' + formula if source_formula is None else source_formula, *definitions]
    pages = list(range(1, len(texts) + 1)) if selected_pages is None else selected_pages
    selection = segment_document(document(texts), pages, chunk_chars=chunk_chars)
    evidence = []
    for index, chunk in enumerate(selection['chunks']):
        start = len(chunk['text']) - len(chunk['text'].lstrip())
        end = len(chunk['text'].rstrip())
        evidence.append({'id': 'e' + str(chunk['page']) +
            (f'_chunk{index}' if sum(x['page'] == chunk['page'] for x in selection['chunks']) > 1 else ''),
            'page': chunk['page'], 'chunk_id': chunk['id'], 'quote': chunk['text'][start:end],
            'normalized_start': chunk['start'] + start, 'normalized_end': chunk['start'] + end,
            'printed_page': None})
    formula_ids = [e['id'] for e in evidence if e['page'] == 1]
    cited = ([e['id'] for e in evidence] if cited_definition_pages is None else
             [*formula_ids, *(e['id'] for e in evidence if e['page'] in cited_definition_pages)])
    material = {'title': 'Explicit definition-grounding test fixture', 'evidence': evidence,
        'hypotheses': [{'id': 'h', 'claim': 'Alpha#12 is the displayed source formula.',
            'claim_attribution': 'paper_original', 'evidence_ids': [e['id'] for e in evidence],
            'economic_mechanism': None, 'mechanism_attribution': 'unresolved',
            'signal_direction': 'Mathematical sign only; predictive validity is unknown.',
            'required_data': [{'name': name, 'semantics': 'Explicit fixture variable ' + name,
                               'evidence_ids': list(formula_ids)} for name in ['close', 'volume']],
            'calculation': formula if calculation is None else calculation,
            'conditions': [], 'unresolved_rules': []}],
        'unresolved_rules': [], 'limitations': ['Fixture only, not scientific interpretation.'],
        'grounding_version': 1, 'paper_statistics': [],
        'support_bindings': [{'hypothesis_id': 'h', 'component': name, 'evidence_ids': list(formula_ids),
            'status': 'direct', 'explanation': 'Explicit fixture source assertion.'}
            for name in ['claim', 'data:close', 'data:volume']] + [
            {'hypothesis_id': 'h', 'component': 'calculation', 'evidence_ids': list(cited),
             'status': 'direct', 'explanation': 'Fixture declares calculation source support.'},
            {'hypothesis_id': 'h', 'component': 'signal_direction', 'evidence_ids': list(cited),
             'status': 'inference', 'explanation': 'Mathematical fixture inference only.'},
            {'hypothesis_id': 'h', 'component': 'economic_mechanism', 'evidence_ids': [],
             'status': 'unresolved', 'explanation': 'Economic mechanism is unknown.'}]}
    validate_material_draft(selection, material)
    return selection, material, build_span_catalog(selection, max_span_chars=max_span_chars)


def missing(result):
    return {name for h in result['hypotheses'] for name in h['missing_operators']}


def operators(result):
    return [item for h in result['hypotheses'] for item in h['operators']]


class PublicOperatorDefinitionTests(unittest.TestCase):
    def test_exact_source_binding_repair_preserves_claim_formula_unknowns_and_is_idempotent(self):
        selection, material, catalog = synthetic(cited_definition_pages=[2])
        material['evidence'] = [e for e in material['evidence'] if e['id'] != 'e3']
        material['hypotheses'][0]['evidence_ids'].remove('e3')
        material['hypotheses'][0]['unresolved_rules'] = ['[scientific] Economic mechanism is not supplied.']
        material['unresolved_rules'] = ['[scientific] Predictive validity is unknown.']
        before = deepcopy(material)
        repair = plan_operator_definition_repair(selection, material, catalog)
        self.assertTrue(repair['changed']); self.assertEqual(repair['before']['status'], 'repairable')
        self.assertEqual(repair['repair']['actor'], 'automation')
        self.assertIs(repair['repair']['human_approval'], False)
        self.assertEqual(repair['repair']['semantic_fidelity'], 'unverified')
        self.assertEqual(repair['after']['status'], 'grounded'); fixed = repair['material']
        self.assertEqual(material, before)
        for key in ['claim', 'claim_attribution', 'calculation', 'economic_mechanism', 'mechanism_attribution',
                    'signal_direction', 'required_data', 'conditions', 'unresolved_rules']:
            self.assertEqual(fixed['hypotheses'][0][key], before['hypotheses'][0][key])
        self.assertEqual(fixed['unresolved_rules'], before['unresolved_rules'])
        added = [e for e in fixed['evidence'] if e['id'] not in {e['id'] for e in before['evidence']}]
        self.assertTrue(added)
        self.assertTrue(any(DELTA in e['quote'] for e in added))
        for e in added:
            span = next(s for s in catalog['spans'] if s['page'] == e['page']
                and s['chunk_id'] == e['chunk_id'] and s['normalized_start'] == e['normalized_start']
                and s['normalized_end'] == e['normalized_end'])
            self.assertEqual(e['quote'], span['quote'])
        again = plan_operator_definition_repair(selection, fixed, catalog)
        self.assertFalse(again['changed']); self.assertEqual(again['material'], fixed)
        validate_material_draft(selection, fixed)

    def test_source_formula_window_mismatch_is_not_repaired_by_generic_definition(self):
        selection, fixed, catalog = synthetic()
        changed = deepcopy(fixed)
        changed['hypotheses'][0]['calculation'] = changed['hypotheses'][0]['calculation'].replace(
            'delta(volume, 1)', 'delta(volume, 2)').replace('delta(close, 1)', 'delta(close, 2)')
        # Both windows are valid engineering DSL, which cannot establish paper fidelity.
        expression = FORMULA.replace('delta(', 'ts_delta(').replace(', 1)', ', 2)')
        self.assertEqual(validate_expression(expression, {'close', 'volume'})['operators'], ['sign', 'ts_delta'])
        result = assess_operator_definitions(selection, changed, catalog)
        self.assertTrue(all(row['covered'] for row in operators(result)))
        self.assertEqual(result['definition_status'], 'grounded')
        self.assertEqual(result['hypotheses'][0]['formula_support']['status'], 'mismatch')
        self.assertEqual(result['hypotheses'][0]['formula_support']['source_label'], 'Alpha#12')
        self.assertEqual(result['status'], 'needs_information')
        self.assertIn('SOURCE_FORMULA_MISMATCH', {blocker['code'] for blocker in result['blockers']})
        repaired = plan_operator_definition_repair(selection, changed, catalog)
        self.assertFalse(repaired['changed']); self.assertEqual(repaired['material'], changed)

    def test_same_page_neighbour_formula_cannot_replace_the_named_alpha_formula(self):
        # The sibling expression uses the same operators and fields with another window.
        neighbour = FORMULA.replace(', 1)', ', 2)')
        selection, material, catalog = synthetic(source_formula=
            'Alpha#11: ' + neighbour + ' Alpha#12: ' + FORMULA + ' Alpha#13: ' + neighbour)
        self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'grounded')
        material['hypotheses'][0]['calculation'] = neighbour
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information')
        self.assertIn('SOURCE_FORMULA_MISMATCH', {b['code'] for b in result['blockers']})

    def test_definitions_without_reliable_named_source_formula_leave_formula_support_unknown(self):
        selection, material, catalog = synthetic(source_formula='Illustrative calls only: ' + FORMULA)
        result = assess_operator_definitions(selection, material, catalog)
        self.assertTrue(all(row['covered'] for row in operators(result)))
        self.assertEqual(result['status'], 'needs_information')
        self.assertIn('SOURCE_FORMULA_UNRESOLVED', {b['code'] for b in result['blockers']})
        self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])

    def test_multiple_formula_and_exact_definition_cites_grounded(self):
        selection, material, catalog = synthetic()
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'grounded'); self.assertFalse(missing(result))
        self.assertTrue(all(row['covered'] for row in operators(result)))

    def test_complete_definition_can_be_covered_by_multiple_exact_cites_but_not_a_gap(self):
        selection, material, catalog = synthetic()
        complete = next(e for e in material['evidence'] if e['id'] == 'e3')
        split = complete['quote'].index('minus')
        pieces = []
        for suffix, start, end in [('a', 0, split - 1), ('b', split, len(complete['quote']))]:
            pieces.append({**complete, 'id': 'e3' + suffix, 'quote': complete['quote'][start:end],
                'normalized_start': complete['normalized_start'] + start,
                'normalized_end': complete['normalized_start'] + end})
        material['evidence'] = [e for e in material['evidence'] if e['id'] != 'e3'] + pieces
        for item in [material['hypotheses'][0], *material['support_bindings']]:
            if 'e3' in item['evidence_ids']:
                item['evidence_ids'] = [i for i in item['evidence_ids'] if i != 'e3'] + ['e3a', 'e3b']
        self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'grounded')
        # Quotes still exactly match, but a non-whitespace character is unsupported.
        material['evidence'][-1]['normalized_start'] += 1
        material['evidence'][-1]['quote'] = material['evidence'][-1]['quote'][1:]
        self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'repairable')

    def test_formal_delta_header_does_not_accept_wrong_meaning_or_parameter_arity(self):
        for declaration in [
            'delta(x, d) = the value of x d days ago minus today’s value of x',
            'delta(x) = today’s value of x minus the value of x d days ago',
            'delta(x, d) = time-series rank in the past d days',
        ]:
            with self.subTest(declaration=declaration):
                selection, material, catalog = synthetic(definitions=(SIGN, declaration))
                result = assess_operator_definitions(selection, material, catalog)
                self.assertEqual(result['status'], 'needs_information')
                self.assertEqual(missing(result), {'delta'})
                self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])

    def test_correct_delta_prefix_with_division_suffix_is_not_complete_definition(self):
        selection, material, catalog = synthetic(definitions=(SIGN, DELTA + ' divided by d'))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information')
        self.assertEqual(missing(result), {'delta'})
        repair = plan_operator_definition_repair(selection, material, catalog)
        self.assertFalse(repair['changed'])
        self.assertEqual(repair['material'], material)

    def test_exact_delta_division_by_old_value_counterexample_is_unresolved(self):
        declaration = DELTA + ' divided by the value of x d days ago'
        selection, material, catalog = synthetic(definitions=(SIGN, declaration))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
        repair = plan_operator_definition_repair(selection, material, catalog)
        self.assertFalse(repair['changed']); self.assertEqual(repair['material'], material)

    def test_adjacent_selected_page_division_is_not_a_complete_definition(self):
        suffix = 'divided by the value of x d days ago'
        for terminal in ['', '.']:
            for tail in [suffix, suffix.upper(), suffix + '; ' + RANK]:
                with self.subTest(terminal=terminal, tail=tail):
                    selection, material, catalog = synthetic('delta(close, 1)', (DELTA + terminal, tail))
                    result = assess_operator_definitions(selection, material, catalog)
                    self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
                    plan = plan_operator_definition_repair(selection, material, catalog)
                    self.assertFalse(plan['changed']); self.assertEqual(plan['material'], material)

    def test_adjacent_selected_page_arithmetic_continuations_remain_unresolved(self):
        for tail in ['multiplied by d', 'MULTIPLIED BY d', 'plus d', 'MINUS d', '/ d', '* d', '+ d', '- d']:
            with self.subTest(tail=tail):
                selection, material, catalog = synthetic('delta(close, 1)', (DELTA + '.', tail))
                result = assess_operator_definitions(selection, material, catalog)
                self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
                self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])
        selection, material, catalog = synthetic('sign(close)', (SIGN, 'multiplied by 2'))
        self.assertEqual(missing(assess_operator_definitions(selection, material, catalog)), {'sign'})

    def test_next_selected_page_independent_definition_or_named_formula_ends_the_declaration(self):
        for following in [RANK, 'Alpha#9002: rank(close)', 'minus(x) = an independently named function']:
            with self.subTest(following=following):
                selection, material, catalog = synthetic('delta(close, 1)', (DELTA, following))
                self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'grounded')

    def test_unselected_page_continuation_is_not_read_or_inferred(self):
        selection, material, catalog = synthetic('delta(close, 1)',
            (DELTA, 'divided by the value of x d days ago'), selected_pages=[1, 2])
        self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'grounded')

    def test_delta_rhs_keeps_arithmetic_and_sentence_suffixes_unresolved(self):
        for suffix in [' multiplied by d', ' / d', ' * 2', ' plus the current value of x',
                       '; divided by d', '. Divided by d', '\nmultiplied by d', '...']:
            with self.subTest(suffix=suffix):
                selection, material, catalog = synthetic(definitions=(SIGN, DELTA + suffix))
                result = assess_operator_definitions(selection, material, catalog)
                self.assertEqual(result['status'], 'needs_information')
                self.assertEqual(missing(result), {'delta'})
                plan = plan_operator_definition_repair(selection, material, catalog)
                self.assertFalse(plan['changed']); self.assertEqual(plan['material'], material)

    def test_full_rhs_matching_is_shared_by_other_definition_families(self):
        cases = [('rank(close)', RANK, 'rank'),
                 ('delay(close, 2)', 'delay(x, d) = value of x d days ago', 'delay'),
                 ('correlation(close, volume, 5)', CORR, 'correlation'),
                 ('covariance(close, volume, 5)', COVARIANCE, 'covariance'),
                 ('ts_rank(close, 5)', TS_RANK, 'ts_rank'),
                 ('ts_min(close, 5)', 'ts_min(x, d) = time-series min over the past d days', 'ts_min'),
                 ('ts_max(close, 5)', 'ts_max(x, d) = time-series max over the past d days', 'ts_max'),
                 ('ts_sum(close, 5)', 'ts_sum(x, d) = time-series sum over the past d days', 'ts_sum'),
                 ('ts_mean(close, 5)', 'ts_mean(x, d) = time-series mean over the past d days', 'ts_mean'),
                 ('stddev(close, 5)', 'stddev(x, d) = moving standard deviation over the past d days', 'stddev'),
                 ('ts_std(close, 5)', 'ts_std(x, d) = time-series standard deviation over the past d days', 'ts_std')]
        for formula, declaration, name in cases:
            with self.subTest(operator=name):
                selection, material, catalog = synthetic(formula, (declaration,))
                self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'grounded')
                for suffix in [' divided by d', ' multiplied by 2']:
                    selection, material, catalog = synthetic(formula, (declaration + suffix,))
                    result = assess_operator_definitions(selection, material, catalog)
                    self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {name})

    def test_grouped_and_single_standard_definitions_reject_modifiers(self):
        for declaration in [SIGN, 'sign(x) = standard definitions']:
            for suffix in [' divided by 2', ' * 2', '; multiplied by 2']:
                with self.subTest(declaration=declaration, suffix=suffix):
                    selection, material, catalog = synthetic(definitions=(declaration + suffix, DELTA))
                    result = assess_operator_definitions(selection, material, catalog)
                    self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'sign'})

    def test_punctuation_and_newlines_before_independent_declarations_are_boundaries(self):
        for delimiter in ['; ', '.\n', ',\n', '\n']:
            with self.subTest(delimiter=delimiter):
                selection, material, catalog = synthetic(definitions=(SIGN + delimiter + DELTA + delimiter + RANK + '.',))
                result = assess_operator_definitions(selection, material, catalog)
                self.assertEqual(result['status'], 'grounded'); self.assertFalse(missing(result))

    def test_dangling_comma_at_selected_page_end_is_not_a_complete_definition(self):
        selection, material, catalog = synthetic(definitions=(SIGN, DELTA + ','))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
        self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])

    def test_quoted_nested_and_chained_headers_are_not_independent_declarations(self):
        for declaration in ['“' + DELTA + ' ' + RANK + '”',
                            '(' + DELTA + ' ' + RANK + ')',
                            'outer(x) = ' + DELTA,
                            DELTA + ' * sign(x) = standard definitions']:
            with self.subTest(declaration=declaration):
                selection, material, catalog = synthetic(definitions=(SIGN, declaration))
                result = assess_operator_definitions(selection, material, catalog)
                self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
                self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])

    def test_standard_operator_annotation_is_a_closed_clause_not_ignored_prose(self):
        annotation = '; same for the operators “+”, “-”, “*”, “/”, “>”, “<”, “==”, “||”, “x ? y : z”'
        selection, material, catalog = synthetic(definitions=(SIGN + annotation + '; ' + DELTA,))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'grounded')
        sign = next(row for row in operators(result) if row['source_name'] == 'sign')
        self.assertIn(annotation, sign['definition']['declaration'])
        for tail in [annotation + ' divided by d', '; same for the operators “magic”']:
            with self.subTest(tail=tail):
                selection, material, catalog = synthetic(definitions=(SIGN + tail + '; ' + DELTA,))
                result = assess_operator_definitions(selection, material, catalog)
                self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'sign'})

    def test_complete_definition_across_catalog_spans_repairs_exact_available_fragments(self):
        declaration = DELTA.replace(' days ago', ' observations ago')
        selection, material, catalog = synthetic(definitions=(SIGN, declaration),
            cited_definition_pages=[2], max_span_chars=64)
        material['evidence'] = [e for e in material['evidence'] if e['id'] != 'e3']
        material['hypotheses'][0]['evidence_ids'].remove('e3')
        original = deepcopy(material)
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'repairable')
        delta = next(row for row in operators(result) if row['source_name'] == 'delta')
        self.assertGreaterEqual(len(delta['definition_span_ids']), 2)
        plan = plan_operator_definition_repair(selection, material, catalog)
        self.assertEqual(plan['after']['status'], 'grounded'); self.assertTrue(plan['changed'])
        self.assertEqual(material, original)
        self.assertEqual(plan['material']['hypotheses'][0]['calculation'], original['hypotheses'][0]['calculation'])
        self.assertEqual(set(plan['repair']['added_span_ids']), set(delta['definition_span_ids']))

    def test_source_chunk_cut_cannot_hide_a_contradictory_suffix(self):
        prefix = DELTA
        self.assertEqual(len(prefix), 64)
        selection, material, catalog = synthetic('delta(close, 1)', (prefix + ' divided by d',), chunk_chars=64)
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['hypotheses'][0]['formula_support']['status'], 'matches')
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
        self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])

    def test_declaration_across_source_chunks_is_explicitly_outside_single_chunk_v1(self):
        selection, material, catalog = synthetic('delta(close, 1)', ('P' * 20 + ' ' + DELTA,), chunk_chars=64)
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
        self.assertFalse(plan_operator_definition_repair(selection, material, catalog)['changed'])

    def test_definition_in_material_but_unbound_requires_binding_repair(self):
        selection, material, catalog = synthetic(cited_definition_pages=[2])
        self.assertEqual(assess_operator_definitions(selection, material, catalog)['status'], 'repairable')
        repaired = plan_operator_definition_repair(selection, material, catalog)
        self.assertEqual(repaired['after']['status'], 'grounded')
        self.assertEqual(len(repaired['material']['evidence']), len(material['evidence']))
        self.assertTrue(repaired['changed'])

    def test_inferred_direction_binding_requires_definition_even_if_calculation_is_covered(self):
        selection, material, catalog = synthetic()
        direction = next(b for b in material['support_bindings'] if b['component'] == 'signal_direction')
        direction['evidence_ids'].remove('e3')
        result = assess_operator_definitions(selection, material, catalog)
        rows = [row for row in operators(result) if row['source_name'] == 'delta']
        self.assertTrue(all(row['covered_components']['calculation'] for row in rows))
        self.assertTrue(all(not row['covered_components']['signal_direction'] for row in rows))
        repaired = plan_operator_definition_repair(selection, material, catalog)
        self.assertEqual(repaired['after']['status'], 'grounded')
        self.assertIn('e3', next(b for b in repaired['material']['support_bindings']
            if b['component'] == 'signal_direction')['evidence_ids'])

    def test_keywords_calls_and_parameter_explanation_are_not_formal_source_definitions(self):
        prose = 'Delta is mentioned; delta(close, 1) and delta(volume, 1) occur. Today’s values and days ago appear in this prose.'
        selection, material, catalog = synthetic(definitions=(SIGN, prose),
            calculation='Alpha#12 is ' + FORMULA + '; delta(x, d) is today’s value minus an old value.')
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
        repair = plan_operator_definition_repair(selection, material, catalog)
        self.assertFalse(repair['changed']); self.assertEqual(repair['material'], material)

    def test_omitted_definition_page_is_not_retrieved_or_invented(self):
        selection, material, catalog = synthetic(selected_pages=[1, 2])
        self.assertEqual(selection['omitted_pages'], [3])
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'delta'})
        repair = plan_operator_definition_repair(selection, material, catalog)
        self.assertFalse(repair['changed']); self.assertEqual(repair['material'], material)

    def test_documented_delta_alias_uses_the_source_delta_definition(self):
        selection, material, catalog = synthetic(formula=FORMULA.replace('delta(', 'ts_delta('))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'grounded')
        self.assertEqual({row['canonical_name'] for row in operators(result)}, {'sign', 'ts_delta'})
        self.assertEqual(material['hypotheses'][0]['calculation'], FORMULA.replace('delta(', 'ts_delta('))

    def test_nested_calls_and_time_series_rank_are_distinct_from_cross_section_rank(self):
        formula = 'correlation(ts_rank(close, 5), rank(volume), 10)'
        selection, material, catalog = synthetic(formula, (RANK, CORR))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'needs_information'); self.assertEqual(missing(result), {'ts_rank'})
        self.assertTrue(all(row['covered'] for row in operators(result) if row['source_name'] in {'rank', 'correlation'}))
        selection, material, catalog = synthetic(formula, (RANK, CORR, TS_RANK))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'grounded')
        self.assertFalse(next(row for row in operators(result) if row['source_name'] == 'ts_rank')['execution_supported'])
        windows = {row['source_name']: ast.parse(row['call'], mode='eval').body.args[-1].value
                   for row in operators(result) if row['source_name'] in {'correlation', 'ts_rank'}}
        self.assertEqual(windows, {'correlation': 10, 'ts_rank': 5})

    def test_covariance_source_definition_and_execution_support_are_separate(self):
        formula = 'rank(covariance(rank(close), rank(volume), 5))'
        selection, material, catalog = synthetic(formula, (RANK, COVARIANCE))
        result = assess_operator_definitions(selection, material, catalog)
        self.assertEqual(result['status'], 'grounded')
        self.assertEqual([row['source_name'] for row in operators(result)].count('rank'), 3)
        self.assertEqual([row['source_name'] for row in operators(result)].count('covariance'), 1)
        self.assertTrue(all(row['execution_supported'] for row in operators(result) if row['source_name'] == 'rank'))
        self.assertFalse(next(row for row in operators(result) if row['source_name'] == 'covariance')['execution_supported'])
        stopped = execution_preflight(material, {'kind': 'daily_synthetic'}, available_fields={'close', 'volume'})
        self.assertEqual(stopped['stop_reason'], 'UNSUPPORTED_OPERATOR')
        unsupported = stopped['automatic_checks']['expressions']['items'][0]['unsupported_operators']
        self.assertEqual([item['operator'] for item in unsupported], ['covariance'])

    def test_stale_or_rehashed_catalog_and_foreign_selection_are_rejected(self):
        selection, material, catalog = synthetic()
        changed = deepcopy(catalog); changed['spans'][-1]['text_digest'] = '0' * 64
        for recompute in [False, True]:
            if recompute: changed['catalog_digest'] = digest({k:v for k,v in changed.items() if k != 'catalog_digest'})
            with self.subTest(recompute=recompute), self.assertRaises(ValueError):
                assess_operator_definitions(selection, material, changed)
        foreign, _, other = synthetic(definitions=(SIGN, DELTA + ' changed source'))
        with self.assertRaises(ValueError):
            assess_operator_definitions(selection, material, other)

    def test_mismatched_quote_coordinates_cannot_claim_direct_definition_support(self):
        selection, material, catalog = synthetic()
        changed = deepcopy(material); changed['evidence'][-1]['quote'] = 'delta(x, d) = a fabricated rule'
        with self.assertRaises(ValueError):
            assess_operator_definitions(selection, changed, catalog)
        changed = deepcopy(material); changed['evidence'][-1]['normalized_start'] += 1
        with self.assertRaises(ValueError):
            plan_operator_definition_repair(selection, changed, catalog)


if __name__ == '__main__':
    unittest.main()
