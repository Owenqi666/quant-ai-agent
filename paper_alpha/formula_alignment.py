"""Bounded deterministic formula comparisons, without eval or research judgments.

The original workflow admission rule stays independent. This stricter review
projection requires one unique supported formula across every cited reference.
Source decimal tokens, rather than binary64 AST values, define numeric identity.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from .expressions import (_parse, _ALIASES, _WINDOW_FUNCTIONS, validate_expression,
                          ExpressionError)
from .formula_provenance import (_LABEL, _complete_formula, claim_alpha_number,
                                 MAX_QUOTE_CHARS, MAX_LABELS_PER_QUOTE)
from .storage import digest

POLICY_VERSION = 'bounded-formula-structure-v1-decimal-literals'
DEFAULT_FIELDS = {'open', 'high', 'low', 'close', 'volume', 'returns'}
UNASSESSED = ['economic_mechanism', 'natural_language_direction', 'field_units',
              'calendar_and_missing_policy', 'algebraic_equivalence', 'overall_hypothesis_fidelity']


def _number(node, text):
    token = ast.get_source_segment(text, node)
    if token is None:
        raise ValueError('Numeric source token is unavailable')
    if type(node.value) is int:
        return ('integer', str(node.value))
    number = Decimal(token.replace('_', ''))
    if not number.is_finite():
        raise ValueError('Numeric source token is not finite')
    sign, digits, exponent = number.as_tuple()
    if not any(digits):
        return ('decimal', '0')
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop(); exponent += 1
    # No normalize() under the default 28-digit context: it would round source.
    return ('decimal', str(Decimal((sign, tuple(digits), exponent))))


def _parsed(expression, fields, *, structural=False):
    text = expression.strip() if isinstance(expression, str) else expression
    tree = _parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            node.func.id = _ALIASES.get(node.func.id, node.func.id)
    # Validation uses the executable DSL, including strict integer windows.
    # Original numeric source spans survive normalization and define signature.
    if structural:
        function_names = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        profile_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - function_names
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) for name in profile_names):
            raise ValueError('Source fields must be bounded identifiers')
        fields = set(fields) | profile_names
    validation = validate_expression(ast.unparse(tree.body), fields)
    constants, windows, operators = [], [], []
    def signature(node):
        if isinstance(node, ast.Constant):
            value = _number(node, text); constants.append(':'.join(value)); return value
        if isinstance(node, ast.Name):
            return ('field', node.id)
        if isinstance(node, ast.Call):
            name = node.func.id; operators.append(name)
            if name in _WINDOW_FUNCTIONS:
                windows.append({'operator': name, 'rows': node.args[-1].value})
            return ('call', name, tuple(signature(arg) for arg in node.args))
        if isinstance(node, ast.UnaryOp):
            return ('unary', type(node.op).__name__, signature(node.operand))
        if isinstance(node, ast.BinOp):
            return ('binary', type(node.op).__name__, signature(node.left), signature(node.right))
        if isinstance(node, ast.Compare):
            return ('compare', type(node.ops[0]).__name__, signature(node.left), signature(node.comparators[0]))
        raise ValueError('Unsupported formula node')
    value = signature(tree.body)
    features = {'fields': validation['used_fields'], 'operators': sorted(operators),
                'windows': sorted(windows, key=lambda x: (x['operator'], x['rows'])),
                'constants': sorted(constants), 'signature_digest': digest(value)}
    return value, features


def _try_parse(expression, fields, *, structural=False):
    try:
        value, features = _parsed(expression, fields, structural=structural)
        return value, features, None
    except (ValueError, TypeError, SyntaxError, RecursionError, InvalidOperation, OverflowError) as exc:
        return None, None, getattr(exc, 'code', 'UNSUPPORTED_FORMULA')


def _reference(hypothesis, evidence, fields):
    empty = {'status': 'unavailable', 'reason_code': 'REFERENCE_NOT_FOUND', 'mode': 'none',
             'alpha_number': None, 'expression': None, 'citations': []}
    try:
        target = claim_alpha_number(hypothesis['claim'])
    except (ValueError, TypeError):
        return {**empty, 'status': 'ambiguous', 'reason_code': 'REFERENCE_TARGET_AMBIGUOUS'}, None, None
    empty['alpha_number'] = target
    if (not isinstance(evidence, list) or len(evidence) > 100
            or any(not isinstance(item, dict) for item in evidence)):
        return {**empty, 'reason_code': 'REFERENCE_ITEMS_INVALID'}, None, None
    ids = hypothesis.get('evidence_ids')
    if (not isinstance(ids, list) or len(ids) > 100 or len(set(ids)) != len(ids)):
        return {**empty, 'reason_code': 'REFERENCE_IDS_INVALID'}, None, None
    selected = []
    for identity in ids:
        matches = [item for item in evidence if item.get('id') == identity]
        if len(matches) != 1:
            return {**empty, 'status': 'ambiguous', 'reason_code': 'REFERENCE_ID_AMBIGUOUS'}, None, None
        selected.append(matches[0])
    formulas, unsupported, labelled, any_labels = [], [], False, False
    for item in selected:
        quote = item.get('quote')
        if not isinstance(quote, str) or len(quote) > MAX_QUOTE_CHARS or type(item.get('page')) is not int:
            return {**empty, 'status': 'unsupported', 'reason_code': 'REFERENCE_ITEMS_INVALID'}, None, None
        labels = list(_LABEL.finditer(quote))
        if len(labels) > MAX_LABELS_PER_QUOTE:
            return {**empty, 'status': 'unsupported', 'reason_code': 'REFERENCE_TOO_COMPLEX'}, None, None
        if labels:
            any_labels = True
            for index, label in enumerate(labels):
                if target is None or int(label.group(1)) != target:
                    continue
                labelled = True
                end = labels[index + 1].start() if index + 1 < len(labels) else len(quote)
                try:
                    expression = _complete_formula(quote, label.end(), end)
                except ValueError:
                    return {**empty, 'status': 'unsupported', 'reason_code': 'REFERENCE_LABEL_INVALID'}, None, None
                signature, features, error = _try_parse(expression, fields, structural=True)
                if error:
                    unsupported.append((expression, item))
                else:
                    formulas.append((expression, signature, features, item))
        else:
            # Entire quote only; no embedded substring / matching descendant.
            signature, features, error = _try_parse(quote, fields, structural=True)
            if error is None:
                formulas.append((quote, signature, features, item))
            else:
                try:
                    _parse(quote.strip())
                except (ValueError, TypeError, SyntaxError):
                    pass
                else:
                    unsupported.append((quote, item))
    if any_labels and not labelled:
        return {**empty, 'reason_code': 'REFERENCE_LABEL_TARGET_MISSING'}, None, None
    if unsupported:
        expression, item = unsupported[0]
        return {**empty, 'status': 'unsupported', 'reason_code': 'REFERENCE_FORMULA_UNSUPPORTED',
                'mode': 'labelled_alpha' if labelled else 'standalone',
                'expression': expression if len(unsupported) == 1 and not formulas else None,
                'citations': [{'evidence_id': item['id'], 'page': item['page']}]}, None, None
    if not formulas:
        return empty, None, None
    if len({digest(item[1]) for item in formulas}) != 1:
        return {**empty, 'status': 'ambiguous', 'reason_code': 'REFERENCE_FORMULAS_CONFLICT'}, None, None
    first = formulas[0]
    citations = sorted({(item[3]['id'], item[3]['page']) for item in formulas})
    return {'status': 'identified', 'reason_code': 'REFERENCE_UNIQUE_SUPPORTED',
            'mode': 'labelled_alpha' if labelled else 'standalone', 'alpha_number': target,
            'expression': first[0], 'citations': [{'evidence_id': identity, 'page': page} for identity, page in citations]}, first[1], first[2]


def _window_bindings(signature):
    result = []
    def walk(value):
        if not isinstance(value, tuple):
            return
        if value and value[0] == 'call' and value[1] in _WINDOW_FUNCTIONS:
            result.append(digest((value[1], value[2][:-1], value[2][-1])))
        for child in value:
            if isinstance(child, tuple):
                walk(child)
    walk(signature)
    return sorted(result)


def _comparison(left, right, missing):
    return {'status': 'unknown', 'reason_code': missing} if left is None or right is None else {
        'status': 'exact' if left == right else 'different',
        'reason_code': 'STRUCTURE_EXACT' if left == right else 'STRUCTURE_DIFFERENT'}


def _check(dimension, status, code, message):
    return {'dimension': dimension, 'status': status, 'code': code, 'message': message}


def analyze_alignment(*, hypothesis, evidence, candidate, computed_result=None, available_fields=None):
    """Pure review facts. No computation, model, mutation or inferred approval.

    Inputs belong to one verified immutable Task/result in the server. Offline
    callers can supply fixtures, but cannot thereby claim a verified run.
    """
    if not isinstance(hypothesis, dict) or not isinstance(candidate, dict):
        raise ValueError('Hypothesis and candidate must be objects')
    if candidate.get('hypothesis_id') != hypothesis.get('id'):
        raise ValueError('Candidate hypothesis identity differs')
    required_input = hypothesis.get('required_fields', [])
    available = sorted(DEFAULT_FIELDS) if available_fields is None else available_fields
    for names in (required_input, available):
        if (not isinstance(names, (list, set, tuple)) or len(names) > 100
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) for name in names)):
            raise ValueError('Field declarations must be bounded identifiers')
    if candidate.get('origin') not in {'paper_original', 'user_modification', 'model_conjecture'}:
        raise ValueError('Candidate origin is unsupported')
    if (not isinstance(candidate.get('changes'), list) or len(candidate['changes']) > 100
            or any(not isinstance(change, str) or not change or len(change) > 4000 for change in candidate['changes'])):
        raise ValueError('Candidate changes must be bounded declarations')
    available = set(available)
    fields = DEFAULT_FIELDS | available | set(required_input)
    reference, ref_sig, ref_features = _reference(hypothesis, evidence, fields)
    submitted = candidate['expression']
    declared_sig, declared_features, error = _try_parse(submitted, fields, structural=True)
    _, _, availability_error = _try_parse(submitted, available)
    executed = computed_result.get('expression') if isinstance(computed_result, dict) else None
    executed_sig, _, executed_error = _try_parse(executed, fields, structural=True) if executed is not None else (None, None, 'EXECUTION_EXPRESSION_UNAVAILABLE')
    rtd = _comparison(ref_sig, declared_sig, reference['reason_code'] if ref_sig is None else 'DECLARED_FORMULA_UNSUPPORTED')
    dte = _comparison(declared_sig, executed_sig, executed_error or 'DECLARED_FORMULA_UNSUPPORTED')
    both = ref_features is not None and declared_features is not None
    used = declared_features['fields'] if declared_features else []
    required = sorted(set(hypothesis.get('required_fields', [])))
    undeclared = sorted(set(used) - set(required)); unused = sorted(set(required) - set(used))
    differences = {'reference': ref_features, 'declared': declared_features,
        'added_fields': sorted(set(used) - set(ref_features['fields'])) if both else [],
        'removed_fields': sorted(set(ref_features['fields']) - set(used)) if both else [],
        'added_operators': sorted((Counter(declared_features['operators']) - Counter(ref_features['operators'])).elements()) if both else [],
        'removed_operators': sorted((Counter(ref_features['operators']) - Counter(declared_features['operators'])).elements()) if both else [],
        'window_changed': _window_bindings(ref_sig) != _window_bindings(declared_sig) if both else None,
        'constants_changed': ref_features['constants'] != declared_features['constants'] if both else None,
        'structure_changed': ref_sig != declared_sig if both else None, 'outer_transform': None}
    if both and declared_sig == ('call', 'rank', (ref_sig,)):
        differences['outer_transform'] = 'rank'
    elif both and declared_sig == ('unary', 'USub', ref_sig):
        differences['outer_transform'] = 'negation'
    origin, changes = candidate['origin'], candidate.get('changes', [])
    attribution_failed = ((origin == 'paper_original' and (bool(changes) or rtd['status'] == 'different'))
        or (origin in {'user_modification', 'model_conjecture'} and not changes))
    attribution_status = 'failed' if attribution_failed else ('unknown' if origin == 'paper_original' and rtd['status'] == 'unknown' else 'passed')
    checks = [_check('expression_legality', 'failed' if availability_error else 'passed', availability_error or 'DSL_VALID', 'Safe local DSL and actual available fields only; no economic judgment. Missing available fields do not erase a supported structural profile.'),
        _check('formula_reference', 'passed' if ref_sig is not None else 'unknown', reference['reason_code'], 'Only uniquely supported cited formula syntax is compared.'),
        _check('field_declarations', 'unknown' if error else 'failed' if undeclared else 'warning' if unused else 'passed',
               'FIELD_USAGE_UNKNOWN' if error else 'UNDECLARED_FIELDS' if undeclared else 'DECLARED_UNUSED_FIELDS' if unused else 'FIELDS_DECLARED',
               'Used fields must be declared; unused declarations are hints, not proof of a research omission.'),
        _check('execution_identity', 'passed' if dte['status'] == 'exact' else 'failed' if dte['status'] == 'different' else 'unknown', dte['reason_code'], 'Declared-to-executed comparison is independent of paper-reference comparison.'),
        _check('attribution_declarations', attribution_status, 'ATTRIBUTION_INCONSISTENT' if attribution_failed else 'ATTRIBUTION_UNASSESSED' if attribution_status == 'unknown' else 'ATTRIBUTION_DECLARED', 'Origin/change declarations only; no automatic claim or human approval.')]
    result = computed_result if isinstance(computed_result, dict) else {}
    facts = result.get('execution') if isinstance(result.get('execution'), dict) else {}
    metadata = result.get('market_metadata') if isinstance(result.get('market_metadata'), dict) else {}
    diagnostics = result.get('factor_diagnostics') if isinstance(result.get('factor_diagnostics'), dict) else {}
    execution = {'status': 'available' if executed is not None else 'unavailable',
        'operator_semantics_version': diagnostics.get('operator_semantics_version'), 'split': result.get('split'),
        **{key: facts.get(key) for key in ('signal', 'entry', 'exit', 'label', 'weights', 'rank_ic')},
        **{key: metadata.get(key) for key in ('data_kind', 'price_units', 'volume_units')}}
    return {'policy_version': POLICY_VERSION, 'reference': reference,
        'candidate': {'hypothesis_id': candidate['hypothesis_id'], 'origin': origin,
                      'submitted_expression': submitted, 'executed_expression': executed, 'declared_changes': changes},
        'reference_to_declared': rtd, 'declared_to_executed': dte, 'features': differences,
        'field_declarations': {'used': used, 'required': required, 'undeclared': undeclared, 'declared_unused': unused},
        'execution': execution, 'checks': checks, 'unassessed_semantics': UNASSESSED.copy(),
        'semantic_fidelity': 'unverified', 'semantic_quality_score': None,
        'human_review_written': False, 'llm_api_called': False}
