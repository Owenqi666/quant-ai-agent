"""Read-only material/data gates; neither a scientific score nor execution approval."""
from __future__ import annotations

import ast
from copy import deepcopy
from collections.abc import Mapping

from . import author_panel
from .expressions import (ExpressionError, MAX_EXPRESSION_LENGTH, _ALIASES, _FUNCTION_ARITY,
                          repair_expression, validate_expression)
from .server.paper_materials_schema import MaterialDraft


def _gaps(material):
    gaps = []
    for index, rule in enumerate(material['unresolved_rules']):
        gaps.append({'path': f'unresolved_rules[{index}]', 'value': rule})
    for index, hypothesis in enumerate(material['hypotheses']):
        prefix = f'hypotheses[{index}]'
        for name in ('economic_mechanism', 'signal_direction', 'calculation'):
            if hypothesis[name] is None:
                gaps.append({'path': f'{prefix}.{name}', 'value': None, 'hypothesis_id': hypothesis['id']})
        for name in ('claim_attribution', 'mechanism_attribution'):
            if hypothesis[name] == 'unresolved':
                gaps.append({'path': f'{prefix}.{name}', 'value': 'unresolved', 'hypothesis_id': hypothesis['id']})
        for offset, rule in enumerate(hypothesis['unresolved_rules']):
            gaps.append({'path': f'{prefix}.unresolved_rules[{offset}]', 'value': rule,
                         'hypothesis_id': hypothesis['id']})
    return gaps


def _validate_actual_expression(expression, fields):
    """Only the project's documented alias proposal is permitted; preserve input."""
    try:
        return {'passed': True, 'metadata': validate_expression(expression, fields),
                'validation_expression': expression, 'alias_proposal': None}
    except ExpressionError as error:
        raw_error = {'code': error.code, 'message': str(error)}
        proposed = repair_expression(expression, error)
        if proposed is not None:
            try:
                return {'passed': True, 'metadata': validate_expression(proposed, fields),
                        'validation_expression': proposed, 'alias_proposal': proposed,
                        'original_validation_error': raw_error}
            except ExpressionError as alias_error:
                return {'passed': False, 'error': {'code': alias_error.code, 'message': str(alias_error)},
                        'validation_expression': proposed, 'alias_proposal': proposed,
                        'original_validation_error': raw_error}
        return {'passed': False, 'error': raw_error, 'validation_expression': expression, 'alias_proposal': None}


def _expression_check(hypothesis, fields):
    expression = hypothesis['calculation']
    result = {'hypothesis_id': hypothesis['id'], 'original_calculation': expression,
              'material_changed': False, 'unsupported_operators': []}
    if expression is None:
        return {**result, 'status': 'unknown_calculation'}
    if len(expression) > MAX_EXPRESSION_LENGTH:
        return {**result, 'status': 'not_checked', 'reason': 'expression_length_limit',
                'blocking_reason': 'INVALID_EXPRESSION'}
    try:
        tree = ast.parse(expression, mode='eval')
    except (SyntaxError, ValueError, RecursionError):
        # Narrative method text is not mined for a formula or rewritten into DSL.
        return {**result, 'status': 'not_expression', 'reason': 'calculation_is_not_an_ast_expression'}
    # Python treats the rest of "Alpha#6 ..." as a comment and parses just
    # the name Alpha. Source prose must not become a nonexistent data field.
    if isinstance(tree.body, ast.Name) and expression.strip() != tree.body.id:
        return {**result, 'status': 'not_expression', 'reason': 'calculation_is_source_prose'}
    checked = _validate_actual_expression(expression, fields)
    result.update(status='validated' if checked['passed'] else 'rejected', validation=checked)
    result['operator_probes_use_syntactic_names_only_not_data_capability'] = True
    # Inspect the call's own name against the actual registry. A recursive
    # validation error from an inner call must not be attributed to its parent.
    # Whole-expression validation above still checks fields, grammar and arity.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            segment = ast.get_source_segment(expression, node)
            name = node.func.id
            normalized_name = _ALIASES.get(name, name)
            if normalized_name not in _FUNCTION_ARITY:
                item = {'operator': name,
                        'actual_call': segment, 'code': 'unknown_operator'}
                if item not in result['unsupported_operators']:
                    result['unsupported_operators'].append(item)
    if result['unsupported_operators']:
        result['blocking_reason'] = 'UNSUPPORTED_OPERATOR'
    elif not checked['passed']:
        result['blocking_reason'] = ('FIELD_UNAVAILABLE' if checked['error']['code'] == 'unknown_field'
                                     else 'INVALID_EXPRESSION')
    return result


def assess(material_draft, target, *, monthly_panel=None, available_fields=None, accepted=False):
    """Assess actual material and capabilities without reading expected labels.

    ``source_only`` never checks fields/DSL. The other supported target kinds
    are ``author_mom_missing_history``, ``author_information_discreteness`` and
    ``daily_synthetic``. Ready means only ready for engineering configuration.
    The caller separately owns exact source-citation checks and human review.
    """
    if not isinstance(target, Mapping):
        raise ValueError('target must be a mapping')
    kind = target.get('kind')
    details = {'target_kind': kind, 'material_gaps': [], 'blocking_reasons': [],
               'source_citation_integrity': 'not_assessed_here', 'science_validity': 'unknown',
               'human_approval': False, 'experiment_started': False, 'field_substitutions': [],
               'material_changed': False}
    checks = {'material_contract': {'passed': False}, 'material_gaps': {'passed': False},
              'data': {'status': 'not_checked'}, 'expressions': {'status': 'not_checked', 'items': []}}

    def finish(reason=None, *, status=None):
        reasons = details['blocking_reasons']
        if reason is not None and reason not in reasons:
            reasons.insert(0, reason)
        if details['material_gaps'] and 'MATERIAL_UNRESOLVED' not in reasons:
            reasons.append('MATERIAL_UNRESOLVED')
        primary = reasons[0] if reasons else None
        return {'status': status or ('stopped' if primary else 'ready_for_engineering_configuration'),
                'stop_reason': primary, 'details': details, 'automatic_checks': checks,
                'human_review': {'status': 'pending_human', 'scores': None}}

    raw = material_draft.model_dump() if hasattr(material_draft, 'model_dump') else deepcopy(material_draft)
    try:
        if isinstance(raw, dict) and 'grounding_version' in raw:
            from .server.grounded_material_schema import material_model
            model = material_model(raw, accepted=accepted)
        else:
            if accepted:
                from .server.paper_materials_schema import AcceptedMaterialDraft
                model = AcceptedMaterialDraft
            else:
                model = MaterialDraft
        material = model.model_validate(raw).model_dump()
    except (ValueError, TypeError) as error:
        checks['material_contract']['error'] = str(error)
        return finish('MATERIAL_INVALID')
    checks['material_contract']['passed'] = True
    details['material_gaps'] = _gaps(material)
    checks['material_gaps'] = {'passed': not details['material_gaps'], 'count': len(details['material_gaps'])}
    if kind == 'source_only':
        return finish(status='needs_information' if details['material_gaps'] else 'ready_for_engineering_configuration')

    if kind in {'author_mom_missing_history', 'author_information_discreteness'}:
        try:
            diagnostic = author_panel.evaluate(monthly_panel)
        except (ValueError, TypeError, KeyError) as error:
            checks['data'] = {'status': 'invalid_or_unavailable', 'error': str(error)}
            # An invalid monthly claim cannot create an absent daily path.
            return finish('DAILY_RETURN_PATH_UNAVAILABLE' if kind == 'author_information_discreteness'
                          else 'MONTHLY_PANEL_INVALID')
        if kind == 'author_information_discreteness':
            checks['data'] = {'status': 'blocked', 'data_kind': diagnostic['data_kind'],
                              'frequency': 'calendar_month', 'dgw_kind': 'author_precomputed',
                              'raw_daily_return_path_available': False,
                              'daily_construction_verified': False,
                              'unknown_rules': ['internal_daily_window', 'valid_daily_denominator',
                                                'zero_and_missing_daily_return_policy', 'minimum_valid_days'],
                              'retvol_100_day_threshold_applied': False}
            return finish('DAILY_RETURN_PATH_UNAVAILABLE')
        source_row = target.get('source_row')
        if type(source_row) is not int or source_row < 1:
            return finish('SOURCE_ROW_UNAVAILABLE')
        row = next((item for item in diagnostic['rows'] if item['source_row'] == source_row), None)
        if row is None:
            return finish('SOURCE_ROW_UNAVAILABLE')
        # formation_ready additionally includes DGW/MV. It is deliberately not
        # the gate for a MOM-only signal; labels also never determine formation.
        checks['data'] = {'status': 'passed' if row['momentum'] is not None else 'blocked',
                          'source_row': source_row, 'asset': row['asset'], 'windows': diagnostic['windows'],
                          'momentum': row['momentum'], 'missing_momentum_months': row['missing_momentum_months'],
                          'momentum_reasons': [reason for reason in row['reasons'] if reason.startswith('momentum_')],
                          'target_label_available': row['label_ready'],
                          'dgw_and_market_cap_required_for_mom_signal': False,
                          'target_label_used_for_formation': False}
        return finish('MONTHLY_HISTORY_INCOMPLETE' if row['momentum'] is None or row['missing_momentum_months'] else None)

    if kind == 'daily_synthetic':
        if available_fields is not None and (not isinstance(available_fields, (set, frozenset, list, tuple))
                or any(not isinstance(name, str) or not name for name in available_fields)):
            raise ValueError('available_fields must be an explicit collection of exact field names')
        fields = set(available_fields or ())
        required = {item['name'] for hypothesis in material['hypotheses'] for item in hypothesis['required_data']}
        missing = sorted(required - fields)
        checks['data'] = {'status': 'blocked' if missing or available_fields is None else 'passed', 'required_fields': sorted(required),
                          'available_fields': sorted(fields), 'unavailable_fields': missing,
                          'capability_inventory_provided': available_fields is not None,
                          'comparison': 'exact_name_set_difference', 'substitutions': []}
        expression_checks = [_expression_check(hypothesis, fields) for hypothesis in material['hypotheses']]
        checks['expressions'] = {'status': 'checked_actual_calculation_only', 'items': expression_checks}
        reasons = [item['blocking_reason'] for item in expression_checks if item.get('blocking_reason')]
        if available_fields is None:
            details['blocking_reasons'].append('DATA_CAPABILITY_UNAVAILABLE')
        if missing or 'FIELD_UNAVAILABLE' in reasons:
            details['blocking_reasons'].append('FIELD_UNAVAILABLE')
        for reason in reasons:
            if reason not in details['blocking_reasons']:
                details['blocking_reasons'].append(reason)
        return finish()
    return finish('TARGET_KIND_UNSUPPORTED')
