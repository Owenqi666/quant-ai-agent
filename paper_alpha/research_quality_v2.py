"""Frozen paper-component checks with complete task and mode denominators.

Semantic comparisons measure agreement with automation-created declarations.
No model is called and no empirical/scientific score is assigned. Host bindings
prove readback only; genuine model receipts remain a separate Q2 gate.
"""
from __future__ import annotations
from copy import deepcopy
from collections import Counter
import hashlib
import math
import re
from .research_quality_v2_binding import (canonical, digest, read_bounded_json,
    HostRunBinding, verify_report_metrics, QualityBindingError)
from .research_quality_v2_reference import evaluate_reference, REFERENCE_IDS

PROTOCOL_ID = 'research-quality-v2'
MODES = ('fixed', 'deterministic_repair', 'model_assisted')
PAPERS = ('paper-52-week-high', 'paper-betting-against-beta', 'paper-time-series-momentum')
CATEGORIES = ('citation_span', 'paper_vs_project_attribution', 'signal_direction',
    'required_data', 'window_units_and_values', 'missing_policy', 'operator_expression',
    'execution_or_justified_abstention', 'independent_numeric_reference',
    'reproducibility', 'report_result_binding')
GATES = ('source_ready', 'reference_ready', 'baseline_ready', 'check_ids_frozen', 'exposure_frozen')
STATUSES = ('completed', 'failed', 'abstained', 'cancelled', 'budget_stopped', 'not_run')
IDENTITIES = ('source', 'task', 'config', 'catalog', 'exposure')


class ResearchQualityError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ResearchQualityError(message)


def _fields(value, keys, name):
    _require(type(value) is dict and set(value) == set(keys), 'Unknown/missing ' + name + ' fields')


def _sha(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value), 'Invalid SHA256')


def _bounded(value):
    _require(len(canonical(value).encode('utf8')) <= 2 * 1024 * 1024, 'Protocol/observation byte limit exceeded')


def _view(protocol, task):
    paper = next(p for p in protocol['papers'] if p['id'] == task['paper_id'])
    return {'task_id': task['id'], 'prompt': task['prompt'],
            'source': deepcopy(paper['source']), 'catalog': deepcopy(protocol['catalog']),
            'limits': {'blind_test': False, 'scientific_truth': False}}


def validate_protocol(protocol):
    _bounded(protocol)
    _fields(protocol, ('schema_version', 'protocol_id', 'version', 'scope', 'modes', 'repetitions',
        'papers', 'tasks', 'catalog', 'reference_policy', 'baseline_policy', 'numeric_tolerance'), 'protocol')
    _require(type(protocol['schema_version']) is int and protocol['schema_version'] == 2 and
        protocol['protocol_id'] == PROTOCOL_ID and protocol['version'] == '1', 'Unsupported protocol version')
    _require(protocol['scope'] == 'engineering_checker_with_paper_components' and
        protocol['modes'] == list(MODES) and type(protocol['repetitions']) is int and protocol['repetitions'] == 1,
        'Frozen scope, modes or repetition budget differs')
    _require(canonical(protocol['reference_policy']) == canonical({'reviewer': 'automation', 'human_reviewed': False,
        'scientific_truth': False, 'references_frozen_before_outputs': True, 'post_output_relabel': False}),
        'Automation references cannot be relabeled human/scientific truth')
    _require(protocol['numeric_tolerance'] == {'absolute': 1e-12, 'relative': 1e-10}, 'Frozen numeric tolerance differs')
    _fields(protocol['baseline_policy'], ('same_task_data_config_catalog', 'fixed_reference_access',
        'deterministic_repair_reference_access', 'model_reference_access', 'fairness_claim'), 'baseline')
    _require(canonical(protocol['baseline_policy']) == canonical({'same_task_data_config_catalog': True,
        'fixed_reference_access': True, 'deterministic_repair_reference_access': True,
        'model_reference_access': False, 'fairness_claim': 'declared_reference_exposure_difference_no_blind_comparison'}),
        'Baseline knowledge/exposure policy differs')
    _fields(protocol['catalog'], ('fields', 'operators', 'sha256'), 'catalog')
    for key in ('fields', 'operators'):
        xs = protocol['catalog'][key]
        _require(type(xs) is list and len(xs) <= 128 and all(type(x) is str and re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]{0,63}', x) for x in xs) and len(xs) == len(set(xs)), 'Invalid capability names')
    _sha(protocol['catalog']['sha256'])
    _require(protocol['catalog']['sha256'] == digest({k: protocol['catalog'][k] for k in ('fields', 'operators')}), 'Catalog SHA differs')
    _require(type(protocol['papers']) is list and [p.get('id') for p in protocol['papers'] if type(p) is dict] == list(PAPERS), 'Three frozen paper slots must remain present')
    for paper in protocol['papers']:
        _fields(paper, ('id', 'title', 'source', 'gates', 'reference_sha256', 'gaps'), 'paper')
        _require(type(paper['title']) is str and type(paper['gaps']) is list and all(type(g) is str for g in paper['gaps']), 'Invalid paper title/gaps')
        _fields(paper['gates'], GATES, 'paper gates')
        _require(all(type(v) is bool for v in paper['gates'].values()), 'Paper gates require explicit booleans')
        if paper['reference_sha256'] is not None:
            _sha(paper['reference_sha256'])
        source = paper['source']
        if source is not None:
            _fields(source, ('sha256', 'pages'), 'source')
            _sha(source['sha256'])
            _require(type(source['pages']) is list and 1 <= len(source['pages']) <= 16, 'Selected-page inventory differs')
            seen = set()
            for page in source['pages']:
                _fields(page, ('physical_page', 'text', 'text_sha256'), 'source page')
                n = page['physical_page']
                _require(type(n) is int and 1 <= n <= 500 and n not in seen, 'Physical page inventory differs')
                seen.add(n)
                _require(type(page['text']) is str and 0 < len(page['text']) <= 16384, 'Invalid selected source text')
                _require(hashlib.sha256(page['text'].encode('utf8')).hexdigest() == page['text_sha256'], 'Selected source text SHA differs')
        _require(not paper['gates']['source_ready'] or source is not None, 'Ready source must exist')
        _require(not paper['gates']['reference_ready'] or paper['reference_sha256'] is not None, 'Ready reference must be frozen')
    tasks = protocol['tasks']
    expected_ids = [p + ':hypothesis-' + str(n) for p in PAPERS for n in (1, 2)]
    _require(type(tasks) is list and [t.get('id') for t in tasks if type(t) is dict] == expected_ids, 'Six frozen research task slots must remain present')
    for task in tasks:
        _fields(task, ('id', 'paper_id', 'prompt', 'task_sha256', 'exposure_sha256', 'expected', 'numeric_reference'), 'task')
        _require(task['paper_id'] == task['id'].split(':')[0], 'Task/paper identity differs')
        _require(type(task['prompt']) is str and 0 < len(task['prompt']) <= 16384, 'Task prompt differs')
        _require(task['task_sha256'] == digest({'prompt': task['prompt']}), 'Task SHA differs')
        _require(task['exposure_sha256'] == digest(_view(protocol, task)), 'Frozen provider exposure differs')
        _fields(task['expected'], CATEGORIES, 'frozen checks')
        # A null expectation is explicit unresolved evidence, never an omitted check.
        nr = task['numeric_reference']
        if nr is not None:
            _fields(nr, ('reference_id', 'input'), 'numeric reference')
            _require(nr['reference_id'] in REFERENCE_IDS, 'Unknown numeric component')
            evaluate_reference(nr['reference_id'], nr['input'])
    return deepcopy(protocol)


def load_protocol(path):
    return validate_protocol(read_bounded_json(path)[0])


def provider_view(protocol, task_id):
    protocol = validate_protocol(protocol)
    rows = [t for t in protocol['tasks'] if t['id'] == task_id]
    _require(len(rows) == 1, 'Foreign task id')
    return _view(protocol, rows[0])


def _numeric(actual, expected, tolerance):
    if type(actual) is not dict or set(actual) != set(expected):
        return False
    for key, value in expected.items():
        got = actual[key]
        if value is None:
            if got is not None:
                return False
        elif type(got) not in (int, float) or not math.isfinite(got) or not math.isclose(got, value, rel_tol=tolerance['relative'], abs_tol=tolerance['absolute']):
            return False
    return True


def _check(category, submitted, task, paper, protocol, binding, scope):
    expected = task['expected'][category]
    if expected is None or not paper['gates']['reference_ready']:
        return 'unknown'
    if submitted is None:
        return 'unknown'
    if category == 'citation_span':
        if type(submitted) is not dict or set(submitted) != {'source_sha256', 'physical_page', 'start_char', 'end_char', 'quote'} or paper['source'] is None:
            return 'failed'
        page = next((p for p in paper['source']['pages'] if p['physical_page'] == submitted['physical_page']), None)
        a, b = submitted['start_char'], submitted['end_char']
        ok = page is not None and submitted['source_sha256'] == paper['source']['sha256'] and type(a) is int and type(b) is int and 0 <= a < b <= len(page['text']) and page['text'][a:b] == submitted['quote']
        return 'passed' if ok and canonical(submitted) == canonical(expected) else 'failed'
    if category == 'independent_numeric_reference':
        nr = task['numeric_reference']
        if nr is None:
            return 'unknown'
        oracle = evaluate_reference(nr['reference_id'], nr['input'])['values']
        if scope == 'engineering_fixture':
            actual = submitted
        elif binding is not None:
            data = binding.to_dict()['results'].get('factor_values')
            if data is None or data['values']['reference_id'] != nr['reference_id'] or data['values']['input_digest'] != digest(nr['input']):
                return 'unknown'
            actual = data['values']['values']
        else:
            return 'unknown'
        return 'passed' if _numeric(actual, oracle, protocol['numeric_tolerance']) else 'failed'
    if category == 'report_result_binding':
        if binding is None:
            return 'unknown'
        result = verify_report_metrics(binding, submitted)
        return 'passed' if result['denominator'] > 0 and result['passed'] == result['denominator'] else 'failed' if any(r['state'] == 'failed' for r in result['rows']) else 'unknown'
    if category == 'reproducibility':
        if binding is None:
            return 'unknown'
        return 'passed' if submitted == binding.to_dict()['manifest_sha256'] else 'failed'
    if category == 'execution_or_justified_abstention':
        # Abstention checks its frozen reason; successful execution needs host bytes.
        if type(expected) is dict and expected.get('outcome') == 'abstained':
            return 'passed' if canonical(submitted) == canonical(expected) else 'failed'
        if binding is None:
            return 'unknown'
        return 'passed' if binding.to_dict()['manifest']['status'] == 'completed' and canonical(submitted) == canonical(expected) else 'failed'
    # These are automation declaration agreements, not causal/scientific truth.
    return 'passed' if canonical(submitted) == canonical(expected) else 'failed'


def evaluate(protocol, observations, *, host_bindings=None):
    protocol = validate_protocol(protocol)
    _bounded(observations)
    _fields(observations, ('schema_version', 'protocol_sha256', 'scope', 'runs'), 'observations')
    _require(type(observations['schema_version']) is int and observations['schema_version'] == 2 and observations['protocol_sha256'] == digest(protocol), 'Observation/protocol identity differs')
    _require(observations['scope'] in ('engineering_fixture', 'actual_observation'), 'Observation scope differs')
    _require(type(observations['runs']) is list and len(observations['runs']) <= 18, 'Observation run bounds differ')
    bindings = {} if host_bindings is None else host_bindings
    _require(type(bindings) is dict and all(type(v) is HostRunBinding for v in bindings.values()), 'Bindings must come from external host inputs')
    tasks = {t['id']: t for t in protocol['tasks']}; papers = {p['id']: p for p in protocol['papers']}
    indexed = {}
    for run in observations['runs']:
        _fields(run, ('task_id', 'mode', 'repeat', 'status', 'checks', 'identities'), 'run')
        _require(type(run['task_id']) is str and type(run['mode']) is str and type(run['status']) is str and run['task_id'] in tasks and run['mode'] in MODES and type(run['repeat']) is int and run['repeat'] == 1 and run['status'] in STATUSES, 'Foreign run or status')
        key = (run['task_id'], run['mode'], run['repeat'])
        _require(key not in indexed, 'Duplicate run slot')
        _fields(run['checks'], CATEGORIES, 'submitted check inventory')
        _fields(run['identities'], IDENTITIES, 'run identities')
        for val in run['identities'].values():
            _sha(val)
        task, paper = tasks[run['task_id']], papers[tasks[run['task_id']]['paper_id']]
        _require(run['identities']['task'] == task['task_sha256'] and run['identities']['catalog'] == protocol['catalog']['sha256'] and run['identities']['exposure'] == task['exposure_sha256'] and
            run['identities']['source'] == (paper['source']['sha256'] if paper['source'] is not None else '0' * 64), 'Source/task/catalog/exposure binding differs')
        indexed[key] = run
    _require(not set(bindings) - set(indexed), 'Foreign host binding slot')
    results = []
    for tid, task in tasks.items():
        paper = papers[task['paper_id']]
        for mode in MODES:
            key = (tid, mode, 1); run = indexed.get(key); binding = bindings.get(key)
            if binding is not None:
                manifest = binding.to_dict()['manifest']
                _require(manifest['task_id'] == tid and manifest['mode'] == mode and manifest['identities'] == run['identities'] and manifest['status'] == run['status'], 'Host/run identity differs')
            rows = []
            for category in CATEGORIES:
                state = 'unknown'
                if run is not None and run['status'] not in ('not_run', 'cancelled', 'budget_stopped', 'failed'):
                    # Real model receipt authentication is not provided by byte readback.
                    admitted = observations['scope'] == 'engineering_fixture' or (binding is not None and mode != 'model_assisted' and all(paper['gates'].values()))
                    if admitted:
                        state = _check(category, run['checks'][category], task, paper, protocol, binding, observations['scope'])
                rows.append({'id': tid + '::' + category, 'category': category, 'state': state})
            results.append({'task_id': tid, 'mode': mode, 'status': run['status'] if run else 'not_run', 'checks': rows,
                'paper_qualification': 'ready' if all(paper['gates'].values()) else 'blocked_per_paper'})
    states = Counter(c['state'] for r in results for c in r['checks'])
    actual = observations['scope'] == 'actual_observation'
    return {'protocol_id': PROTOCOL_ID, 'protocol_sha256': digest(protocol), 'scope': observations['scope'],
        'planned_papers': 3, 'planned_research_tasks': 6, 'planned_runs': 18, 'declared_checks': 198,
        'automation_reference_agreement': {'passed': states['passed'], 'failed': states['failed'],
            'unknown': states['unknown'], 'denominator': 198, 'fixture_only': not actual},
        'runs': results, 'paper_gates': {p['id']: deepcopy(p['gates']) for p in protocol['papers']},
        'model_quality_evaluated': False, 'actual_model_receipts': 'not_bound', 'scientific_score': None,
        'original_paper_empirical_reproduction': False, 'model_calls': 0, 'final_numeric_reads': 0}


# The float baseline is separate from the Fraction oracle. It is an educational
# component baseline, not a backtest or an implementation of the full papers.
def fixed_component_baseline(reference_id, payload):
    from datetime import date
    from calendar import monthrange
    _require(type(payload) is dict and payload.get('scope') == 'controlled_formula_component', 'Controlled component baseline required')
    data = {k: v for k, v in payload.items() if k != 'scope'}
    def num(v):
        _require(type(v) in (int, float, str) and type(v) is not bool, 'Invalid baseline scalar')
        try:
            result = float(v)
        except (ValueError, OverflowError):
            raise ResearchQualityError('Invalid baseline scalar') from None
        _require(math.isfinite(result), 'Nonfinite baseline scalar')
        return result
    if reference_id == '52week_ratio':
        _fields(data, ('as_of', 'declared_calendar', 'prices'), 'baseline price input')
        end = date.fromisoformat(data['as_of'])
        _require(end.day == monthrange(end.year, end.month)[1], 'Baseline formation date must be month-end')
        start = date(end.year - (end.month != 12), end.month % 12 + 1, 1)
        ds = data['declared_calendar']
        _require(type(ds) is list and 0 < len(ds) <= 4096, 'Invalid baseline calendar')
        dates = [date.fromisoformat(d) for d in ds]
        _require(dates == sorted(set(dates)) and all(start <= d <= end for d in dates), 'Invalid baseline calendar range')
        _require(type(data['prices']) is list and len(data['prices']) == len(ds), 'Incomplete baseline price grid')
        values = []
        for row, d in zip(data['prices'], ds):
            _fields(row, ('date', 'value'), 'baseline price')
            _require(row['date'] == d, 'Baseline price calendar differs')
            value = num(row['value']); _require(value > 0, 'Baseline prices must be positive')
            values.append(value)
        result = {'ratio': values[-1] / max(values)}
    elif reference_id == 'equal_weight_spread':
        _fields(data, ('winner_returns', 'loser_returns'), 'baseline spread')
        def mean(xs):
            _require(type(xs) is list and 0 < len(xs) <= 4096, 'Invalid baseline return grid')
            return math.fsum(num(v) for v in xs) / len(xs)
        result = {'spread': mean(data['winner_returns']) - mean(data['loser_returns'])}
    elif reference_id == 'bab_beta_shrink':
        _fields(data, ('sigma_security', 'sigma_market', 'rho'), 'baseline beta')
        si, sm, rho = (num(data[k]) for k in ('sigma_security', 'sigma_market', 'rho'))
        _require(si >= 0 and sm > 0 and -1 <= rho <= 1, 'Invalid baseline beta domains')
        beta = rho * si / sm
        result = {'beta_ts': beta, 'beta_shrunk': .6 * beta + .4}
    elif reference_id == 'bab_excess_spread':
        _fields(data, ('low_beta', 'high_beta', 'low_return', 'high_return', 'risk_free'), 'baseline BAB')
        lo, hi, lr, hr, rf = (num(data[k]) for k in ('low_beta', 'high_beta', 'low_return', 'high_return', 'risk_free'))
        _require(0 < lo < hi, 'Invalid baseline BAB betas')
        result = {'bab_excess': (lr - rf) / lo - (hr - rf) / hi}
    elif reference_id == 'tsmom_sign':
        _fields(data, ('monthly_excess_returns',), 'baseline TSM sign')
        xs = data['monthly_excess_returns']
        _require(type(xs) is list and len(xs) == 12, 'Baseline TSM requires twelve months')
        rs = [num(r) for r in xs]; _require(all(r >= -1 for r in rs), 'Invalid baseline returns')
        value = math.prod(1 + r for r in rs) - 1
        result = {'past_excess': value, 'signal': None if value == 0 else 1 if value > 0 else -1}
    elif reference_id == 'tsmom_position_return':
        _fields(data, ('past_excess', 'annualized_volatility', 'next_excess_return'), 'baseline TSM position')
        past, vol, following = (num(data[k]) for k in ('past_excess', 'annualized_volatility', 'next_excess_return'))
        _require(vol > 0, 'Positive baseline formation volatility required')
        position = None if past == 0 else (1 if past > 0 else -1) * .4 / vol
        result = {'position': position, 'strategy_return': None if position is None else position * following}
    else:
        raise ResearchQualityError('Unknown component baseline')
    _require(all(v is None or type(v) in (int, float) and math.isfinite(v) for v in result.values()), 'Baseline result overflow')
    return {'scope': 'controlled_formula_component', 'values': result, 'original_paper_reproduced': False}


def normalize_component_submission(values):
    """A declared deterministic numeric serialization repair; no semantic repair."""
    _require(type(values) is dict and len(values) <= 16, 'Invalid component submission')
    result = {}
    for key, value in values.items():
        _require(type(key) is str, 'Invalid component key')
        if value is None:
            result[key] = None
        elif type(value) in (int, float):
            _require(math.isfinite(value), 'Nonfinite component value')
            result[key] = value
        elif type(value) is str and len(value) <= 80:
            try:
                number = float(value.strip())
            except (ValueError, OverflowError):
                raise ResearchQualityError('Invalid numeric serialization') from None
            _require(math.isfinite(number), 'Nonfinite numeric serialization')
            result[key] = number
        else:
            raise ResearchQualityError('Unsupported numeric serialization')
    return result
