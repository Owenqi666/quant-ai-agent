"""Closed schema3 OHLC tasks; independent of old daily-task-v2 semantics."""
from __future__ import annotations

import ast
from copy import deepcopy

from .contracts import Task
from .expressions import validate_expression
from .real_daily_main_metadata import snapshot
from .real_daily_method import EXPRESSION_101
from .real_daily_web_contract import validate_execution, validate_selection, validate_real_daily_catalog, FIELDS
from .storage import digest, json_text

TASK_KIND = 'real_daily_ohlc_development_v1'
DATA_KIND = 'real_daily_ohlc_development'
CORE_KEYS = frozenset({'evidence', 'hypotheses', 'candidates', 'evaluation', 'budget'})
TASK_KEYS = CORE_KEYS | {'schema_version', 'title', 'execution'}
MAX_MAIN_DEVELOPMENT_CELLS = 10_000
MAX_MAIN_VALIDATION_CELLS = 4_000


class RealDailyMainTaskError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(condition, code):
    if not condition:
        raise RealDailyMainTaskError(code)


def is_main_task(value):
    return (type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] == 3 and
            type(value.get('execution')) is dict and value['execution'].get('kind') == TASK_KIND)


def _formula_tree(expression):
    try:
        return ast.dump(ast.parse(expression, mode='eval'), include_attributes=False)
    except (SyntaxError, RecursionError):
        raise RealDailyMainTaskError('invalid_expression') from None


def _capacity(assets, development, validation):
    # A cell is one date/asset pair, not four price values. This smaller main
    # execution limit leaves bounded result/state JSON headroom before Worker IO.
    need(type(assets) is list and type(development) is list and type(validation) is list,
         'invalid_main_capacity_scope')
    need(len(assets) * len(development) <= MAX_MAIN_DEVELOPMENT_CELLS, 'main_development_grid_limit')
    need(len(assets) * max(0, len(validation) - 2) <= MAX_MAIN_VALIDATION_CELLS, 'main_validation_grid_limit')


def parse_main_task(value, expected_binding, declared_selection, catalog):
    """Structural consistency only; expected binding must come from host DB."""
    value = snapshot(value, maximum=512 * 1024)
    need(type(value) is dict and set(value) == TASK_KEYS and is_main_task(value), 'invalid_main_task_schema')
    need(type(value['title']) is str and 0 < len(value['title'].strip()) <= 200, 'invalid_main_task_title')
    actual = validate_execution(value['execution'], expected_binding=expected_binding)
    selection = validate_selection(declared_selection)
    _capacity(selection['assets'], selection['development_sessions'], selection['partitions']['validation'])
    checked_catalog = validate_real_daily_catalog(catalog, expected_selection=selection)
    need(actual['selection_digest'] == digest(selection) and actual['catalog_digest'] == checked_catalog['catalog_digest'],
         'main_task_binding_drift')
    need(json_text(value['evaluation']) == json_text(selection['config']) and digest(value['evaluation']) == actual['config_digest'],
         'main_task_configuration_drift')
    need(type(value['budget']) is dict and not set(value['budget']) -
         {'max_candidates', 'max_attempts_per_candidate', 'max_tool_calls', 'max_seconds'}, 'invalid_main_budget')
    for key, item in value['budget'].items():
        need(type(item) in (int, float) if key == 'max_seconds' else type(item) is int, 'invalid_main_budget')
    core = {key: deepcopy(value[key]) for key in CORE_KEYS}
    parsed = Task.parse({**core, 'schema_version': 1, 'title': value['title'], 'paper': 'paper.json',
        'paper_pdf': 'paper.pdf', 'data': 'prefix.csv', 'data_metadata': 'prefix_metadata.json'})
    need(len(parsed.candidates) == 1 and parsed.candidates[0].id == 'alpha101-101', 'main_method_candidate_unsupported')
    candidate = parsed.candidates[0]
    hypothesis = next(item for item in parsed.hypotheses if item.id == candidate.hypothesis_id)
    actual_fields = set(FIELDS)
    declared_fields = set(hypothesis.required_fields)
    need(declared_fields <= actual_fields, 'required_field_missing')
    need(len(candidate.expression) <= 2048, 'bounded_expression_required')
    checked = validate_expression(candidate.expression, actual_fields)
    need(set(checked['used_fields']) <= declared_fields, 'undeclared_field')
    need(_formula_tree(checked['canonical_expression']) == _formula_tree(EXPRESSION_101), 'main_method_expression_unsupported')
    # Every hypothesis still has its original citation and attribution; the
    # exact supported formula is a method scope, never an inferred field proxy.
    need(len(parsed.candidates) <= parsed.budget.max_candidates, 'main_candidate_budget_exceeded')
    canonical = {'schema_version': 3, 'title': value['title'],
        **{key: parsed.to_dict()[key] for key in CORE_KEYS}, 'execution': actual}
    snapshot(canonical, maximum=512 * 1024)
    return canonical


def inspect_main_task(task, metadata):
    """Static suitability only; no originals/prefix numbers/model calls."""
    need(is_main_task(task) and type(metadata) is dict and metadata.get('data_kind') == DATA_KIND,
         'registered_ohlc_metadata_required')
    fields = set(metadata.get('columns', [])) - {'date', 'asset'}
    need(fields == set(FIELDS), 'registered_ohlc_fields_drift')
    days = metadata.get('calendar_dates')
    need(type(days) is list, 'invalid_main_capacity_scope')
    bounds = task['evaluation']['splits']['validation']
    validation = [day for day in days if bounds['start'] <= day <= bounds['end']]
    _capacity(metadata.get('assets'), days, validation)
    candidate = task['candidates'][0]
    hypothesis = next(item for item in task['hypotheses'] if item['id'] == candidate['hypothesis_id'])
    missing = sorted(set(hypothesis['required_fields']) - fields)
    return {'dataset_version': metadata['version'], 'fields': list(FIELDS),
        'candidates': [{'candidate_id': candidate['id'], 'status': 'blocked' if missing else 'ready',
            'code': 'required_field_missing' if missing else 'static_preflight_passed', 'expression_changed': False,
            'warmup_rows': 0, 'missing_fields': missing}],
        'scope': 'Static four-field development declaration; no source authentication or research validity.'}
