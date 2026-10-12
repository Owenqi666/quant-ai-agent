"""Read-only candidate suitability hints; never rewrite the submitted hypothesis."""
import ast

from ..evaluation import FIELDS
from ..expressions import ExpressionError, repair_expression, validate_expression


def inspect_task(task, metadata):
    fields = set(FIELDS) | {'returns'}
    hypotheses = {h['id']: h for h in task['hypotheses']}
    dates = metadata['calendar_dates']
    split = task['evaluation']['splits']['validation']
    start, last = dates.index(split['start']), dates.index(split['end']) - 2
    rows = []
    def lookback(node):
        if isinstance(node, ast.Name):
            return 1 if node.id == 'returns' else 0
        if isinstance(node, ast.Call):
            name = node.func.id
            if name in {'delay', 'ts_delta', 'ts_corr', 'ts_mean', 'ts_std', 'ts_min', 'ts_max', 'ts_sum'}:
                prior = max(lookback(arg) for arg in node.args[:-1])
                window = node.args[-1].value
                return prior + (window if name in {'delay', 'ts_delta'} else window - 1)
        return max((lookback(child) for child in ast.iter_child_nodes(node)), default=0)
    for candidate in task['candidates']:
        row = {'candidate_id': candidate['id'], 'status': 'ready', 'code': 'static_preflight_passed',
               'expression_changed': False, 'warmup_rows': None, 'missing_fields': []}
        missing = sorted(set(hypotheses[candidate['hypothesis_id']]['required_fields']) - fields)
        if missing:
            row.update(status='blocked', code='required_field_missing', missing_fields=missing)
        else:
            expression = candidate['expression']
            try:
                try:
                    expression = validate_expression(expression, fields)['canonical_expression']
                except ExpressionError as exc:
                    suggested = repair_expression(expression, exc)
                    if not suggested:
                        raise
                    expression = validate_expression(suggested, fields)['canonical_expression']
                    row.update(status='warning', code='alias_requires_normalization', suggested_expression=suggested)
                warmup = lookback(ast.parse(expression, mode='eval').body)
                row['warmup_rows'] = warmup
                if warmup > last:
                    row.update(status='blocked', code='no_validation_rows_after_warmup')
                elif warmup > start:
                    row.update(status='warning', code='partial_validation_warmup')
            except ExpressionError as exc:
                row.update(status='blocked', code=exc.code, message=str(exc))
        rows.append(row)
    return {'dataset_version': metadata['version'], 'fields': sorted(fields), 'candidates': rows,
            'scope': 'Static suitability only; aliases are suggestions, submitted inputs are unchanged. Blocked candidates remain in the task for explicit engine rejection and audit. No numerical or economic validity claim.'}
