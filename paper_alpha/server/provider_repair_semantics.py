"""One bounded implementation repair; research assertions never change.

An integral-valued decimal float in an operator's window position may become
the identical integer literal. Documented aliases may also canonicalize. This
does not change the engine's ordinary parser or authorize another window value.
"""
import ast
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math

from ..expressions import (_parse, _ALIASES, _FUNCTION_ARITY, _WINDOW_FUNCTIONS,
                           MAX_WINDOW, validate_expression)
from .research_jobs_schema import ResearchDraft


def _normalized(expression, *, convert_windows):
    text = expression.strip()
    tree = _parse(text)
    changed = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = _ALIASES.get(node.func.id, node.func.id)
        node.func.id = name
        if (not convert_windows or name not in _WINDOW_FUNCTIONS
                or node.keywords or len(node.args) != _FUNCTION_ARITY[name]):
            continue
        window = node.args[-1]
        if isinstance(window, ast.Constant) and type(window.value) is float:
            minimum = 0 if name == 'delay' else 1
            if not math.isfinite(window.value) or not window.value.is_integer() or not minimum <= window.value <= MAX_WINDOW:
                raise ValueError('Window value is not a supported integral literal')
            # Do not accept 1.00000000000000001 just because binary64 rounds it
            # to 1.0. The original written decimal must also be exactly integral.
            token = ast.get_source_segment(text, window)
            if token is None or Decimal(token.replace('_', '')) != Decimal(int(window.value)):
                raise ValueError('Written window literal is not exactly integral')
            window.value = int(window.value)
            changed += 1
    return tree, changed


def repair_candidate_expression(expression):
    """Return the one safe repair, or None; alias-only work is already handled."""
    try:
        tree, changed = _normalized(expression, convert_windows=True)
        if not changed:
            return None
        result = ast.unparse(tree.body)
        operators = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - operators
        validate_expression(result, names)
        return result
    except (ValueError, TypeError, SyntaxError, InvalidOperation, OverflowError):
        return None


def validate_repair_output(parent_draft, new_draft, *, candidate_id):
    """Return semantic-change diagnostics; ordinary domain validation must follow.

    Exact no-op output is semantically unchanged but remains invalid to the
    expression tool. Keeping that original diagnostic permits a bounded retry.
    """
    def rejected(message):
        return [{'code': 'REPAIR_SEMANTICS_CHANGED', 'message': message}]

    try:
        parent = ResearchDraft.model_validate(parent_draft).model_dump()
        repaired = ResearchDraft.model_validate(new_draft).model_dump()
        old_items = [item for item in parent['candidates'] if item['id'] == candidate_id]
        new_items = [item for item in repaired['candidates'] if item['id'] == candidate_id]
        if len(old_items) != 1 or len(new_items) != 1:
            return rejected('Repair must preserve exactly the declared candidate identity')
        old, new = old_items[0], new_items[0]
        frozen = deepcopy(repaired)
        next(item for item in frozen['candidates'] if item['id'] == candidate_id)['expression'] = old['expression']
        if frozen != parent:
            return rejected('Evidence, hypotheses, candidate metadata and other candidates must remain identical')
        if repaired == parent:
            return []
        expected = repair_candidate_expression(old['expression'])
        if expected is None:
            return rejected('Target has no supported integral-window representation repair')
        actual_tree, _ = _normalized(new['expression'], convert_windows=False)
        expected_tree = _parse(expected)
        if ast.dump(actual_tree, include_attributes=False) != ast.dump(expected_tree, include_attributes=False):
            return rejected('Fields, operator structure, signs and numeric values may not change')
        names = {node.id for node in ast.walk(expected_tree) if isinstance(node, ast.Name)}
        validate_expression(ast.unparse(actual_tree.body), names)
        return []
    except (ValueError, TypeError, KeyError, SyntaxError, InvalidOperation, OverflowError):
        return rejected('Repair is not a valid bounded equivalent implementation')


def validate_versioned_repair_output(parent_draft, new_draft, *, candidate_id,
                                    available_fields, policy_id, policy_digest):
    """One policy pass for a charged versioned branch; old v1 helper is unchanged."""
    from .research_correction_policy import correct_draft
    from ..storage import json_text
    rejected = [{'code': 'CORRECTION_SEMANTICS_CHANGED',
                 'message': 'Output must preserve all assertions and the unique equivalent expression.'}]
    expected = correct_draft(parent_draft, candidate_id=candidate_id,
        available_fields=available_fields, policy_id=policy_id, digest=policy_digest)
    if expected is None:
        return {'diagnostics': rejected, 'plan': None}
    try:
        draft = ResearchDraft.model_validate(new_draft).model_dump()
        if json_text(draft) != json_text(new_draft):
            return {'diagnostics': rejected, 'plan': expected['plan']}
        candidates = [c for c in draft['candidates'] if c['id'] == candidate_id]
        if len(candidates) != 1:
            return {'diagnostics': rejected, 'plan': expected['plan']}
        tree, _ = _normalized(candidates[0]['expression'], convert_windows=False)
        if ast.dump(tree, include_attributes=False) != ast.dump(_parse(expected['plan']['accepted_expression']), include_attributes=False):
            return {'diagnostics': rejected, 'plan': expected['plan']}
        candidates[0]['expression'] = expected['plan']['accepted_expression']
        return {'diagnostics': [] if json_text(draft) == json_text(expected['draft']) else rejected,
                'plan': expected['plan']}
    except (ValueError, TypeError, KeyError, SyntaxError, InvalidOperation, OverflowError):
        return {'diagnostics': rejected, 'plan': expected['plan']}
