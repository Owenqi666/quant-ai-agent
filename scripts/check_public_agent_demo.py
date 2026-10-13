"""Independent stdlib arithmetic/readback checks for the original public demo.

No production expression, evaluation, numerical or storage helper is imported.
The expectation is derived from the original CSV and this explicit teaching
protocol, never copied from a Worker result. SQLite is opened read-only.
"""
from __future__ import annotations
import argparse
import ast
from contextlib import closing
import csv
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sqlite3

ABS_TOL, REL_TOL = 1e-12, 1e-9


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ranks(values):
    # Average tie ranks by pair counts: independent of pandas' sorting routine.
    return [Fraction(1) + sum(other < value for other in values) +
            Fraction(sum(other == value for other in values) - 1, 2) for value in values]


def reference(csv_path, metadata, evaluation):
    dates, assets = metadata['calendar_dates'], metadata['universe']
    bounds = evaluation['splits']['validation']; end = dates.index(bounds['end'])
    prefix = dates[:end + 1]; rows = {}
    with Path(csv_path).open(newline='') as stream:
        for row in csv.DictReader(stream):
            if row['date'] <= bounds['end']:
                key = row['date'], row['asset']
                if key in rows:
                    raise ValueError('Duplicate input grid key')
                rows[key] = {name: Fraction(row[name]) for name in ('open', 'close')}
    if set(rows) != {(day, asset) for day in prefix for asset in assets}:
        raise ValueError('Incomplete input prefix')
    factors = {(day, asset): float(rows[day, asset]['close'] - rows[day, asset]['open'])
               for day in prefix for asset in assets}
    daily, returns, ics = [], [], []
    start = dates.index(bounds['start'])
    for i in range(start, end + 1):
        day = dates[i]
        item = {'signal_date': day, 'entry_date': None, 'exit_date': None, 'status': 'purged',
            'reason': 'label_would_cross_split_boundary', 'available_assets': 0, 'missing_assets': [],
            'rank_ic': None, 'rank_ic_state': 'not_evaluated', 'gross_return': None, 'assets': []}
        if i + 2 > end:
            daily.append(item); continue
        item.update(entry_date=dates[i + 1], exit_date=dates[i + 2], available_assets=len(assets))
        signals = [factors[day, a] for a in assets]; sr = ranks(signals)
        center = [r - sum(sr) / len(sr) for r in sr]; exposure = sum(abs(r) for r in center)
        if exposure == 0:
            item.update(status='skipped', reason='constant_factor'); daily.append(item); continue
        weights = [r / exposure for r in center]
        labels = [rows[dates[i + 2], a]['open'] / rows[dates[i + 1], a]['open'] - 1 for a in assets]
        rr = ranks(labels); cr = [r - sum(rr) / len(rr) for r in rr]
        variance = sum(r * r for r in cr)
        ic = float(sum(a * b for a, b in zip(center, cr))) / math.sqrt(float(sum(r * r for r in center) * variance)) if variance else None
        contributions = [w * label for w, label in zip(weights, labels)]
        gross = float(sum(contributions)); returns.append(gross)
        if ic is not None:
            ics.append(ic)
        item.update(status='evaluated', reason=None, rank_ic=ic,
                    rank_ic_state='defined' if ic is not None else 'constant_forward_returns', gross_return=gross)
        item['assets'] = [{'asset': a, 'signal': signals[j], 'weight': float(weights[j]),
            'entry_open': float(rows[dates[i + 1], a]['open']), 'exit_open': float(rows[dates[i + 2], a]['open']),
            'forward_return': float(labels[j]), 'gross_contribution': float(contributions[j])} for j, a in enumerate(assets)]
        daily.append(item)
    eligible = end - start - 1
    metrics = {'mean_rank_ic': math.fsum(ics) / len(ics) if ics else None, 'rank_ic_days': len(ics),
        'mean_gross_return': math.fsum(returns) / len(returns) if returns else None,
        'sum_gross_return': math.fsum(returns) if returns else None, 'evaluated_days': len(returns),
        'eligible_days': eligible, 'skipped_days': eligible - len(returns), 'purged_days': 2,
        'finite_factor_observations': eligible * len(assets), 'possible_factor_observations': eligible * len(assets), 'factor_coverage': 1.0}
    return {'dates': prefix, 'assets': assets, 'factors': factors, 'daily': daily, 'metrics': metrics}


def compare(actual, expected, path='$'):
    errors = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or not set(expected) <= set(actual):
            return [path + ': missing keys']
        for key, value in expected.items():
            errors.extend(compare(actual[key], value, path + '.' + key))
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [path + ': wrong length']
        for i, value in enumerate(expected):
            errors.extend(compare(actual[i], value, path + '[' + str(i) + ']'))
    elif type(expected) is float:
        if type(actual) not in (float, int) or not math.isfinite(actual) or not math.isclose(actual, expected, abs_tol=ABS_TOL, rel_tol=REL_TOL):
            errors.append(path + ': numerical mismatch')
    elif type(actual) is not type(expected) or actual != expected:
        errors.append(path + ': exact mismatch')
    return errors


def check(output_dir):
    output = Path(output_dir).resolve(); summary = load(output / 'demo-summary.json')
    workspace = output / 'workspace'; report = load(output / 'report.json'); payload = report['payload']
    checks = []
    def record(name, errors):
        checks.append({'name': name, 'outcome': 'failed' if errors else 'passed', 'errors': errors[:20]})
    db = workspace / 'workbench.sqlite3'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row; connection.execute('PRAGMA query_only=ON'); connection.execute('BEGIN')
        row = connection.execute('SELECT * FROM runs WHERE id=?', (summary['run_id'],)).fetchone()
        if row is None or row['status'] != 'completed' or not load_json(row['verification']).get('verified'):
            raise ValueError('No verified completed run')
        state = load_json(row['state']); attempt = row['attempt_id']
        inventory = {r['name']: dict(r) for r in connection.execute('SELECT * FROM artifacts WHERE run_id=? AND attempt_id=?', (row['id'], attempt))}
        review_count = connection.execute('SELECT COUNT(*) FROM reviews WHERE run_id=?', (row['id'],)).fetchone()[0]
    prun = payload['runs'][0]; candidate = state['candidates'][0]; pc = prun['candidates'][0]
    record('run_and_report_identity', compare(prun['summary']['run']['id'], summary['run_id']) +
           compare(payload['selected_run_ids'], [summary['run_id']]) + compare(candidate['id'], pc['id']) +
           compare(load(output / 'report-export.json'), report))
    report_inventory = {a['name']: a for a in prun['artifacts']}; integrity_errors = []; paths = {}
    for name, entry in inventory.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Artifact name escapes the run')
        path = workspace / 'runs' / row['id'] / 'attempts' / attempt / 'exports' / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace):
            raise ValueError('Unsafe or missing artifact')
        paths[name] = path
        if sha(path) != entry['sha256'] or path.stat().st_size != entry['size'] or report_inventory.get(name, {}).get('sha256') != entry['sha256']:
            integrity_errors.append(name + ': SHA/size/report receipt mismatch')
    record('artifact_bytes_and_report_receipts', integrity_errors)
    download_errors = []
    for receipt in load(output / 'worker/receipts.json'):
        copy_path = output / receipt['public_copy']
        if sha(copy_path) != receipt['sha256'] or sha(copy_path) != sha(paths[receipt['name']]):
            download_errors.append(receipt['name'] + ': original download SHA mismatch')
    record('selected_original_worker_downloads', download_errors)
    result_name = pc['metric_source']['artifact_name']; result = load(paths[result_name])
    factor_names = [n for n in pc['artifacts'] if n.endswith('/factor.csv')]
    if len(factor_names) != 1:
        raise ValueError('Expected one corresponding actual factor artifact')
    factor_path = paths[factor_names[0]]
    metadata = load(output / 'input/metadata.json'); evaluation = load(output / 'input/evaluation.json')
    expected = reference(output / 'input/market.csv', metadata, evaluation)
    record('original_synthetic_inputs', compare(sha(paths['inputs/market.csv']), sha(output / 'input/market.csv')) +
           compare(load(paths['inputs/metadata.json']), metadata) + compare(load(paths['inputs/task.json'])['evaluation'], evaluation))
    expression = candidate['attempts'][-1]['expression']
    record('original_arithmetic_formula', compare(ast.dump(ast.parse(expression, mode='eval')), ast.dump(ast.parse('close - open', mode='eval'))) + compare(result['expression'], expression))
    factor_errors = []; seen = set()
    with factor_path.open(newline='') as stream:
        reader = csv.reader(stream); header = next(reader)
        if header[1:] != expected['assets']:
            factor_errors.append('Asset order differs')
        for line in reader:
            day = line[0]
            for a, cell in zip(header[1:], line[1:]):
                key = day, a; seen.add(key)
                if key not in expected['factors']:
                    factor_errors.append('Unexpected factor grid cell')
                else:
                    factor_errors.extend(compare(float(cell), expected['factors'][key], 'factor.' + day + '.' + a))
    if seen != set(expected['factors']):
        factor_errors.append('Factor grid does not equal development prefix')
    record('independent_close_minus_open_factor', factor_errors)
    record('independent_timing_positions_returns_ic_and_skips', compare(result['daily'], expected['daily']))
    record('all_11_validation_metrics', compare(result['metrics'], expected['metrics']))
    record('tool_database_and_report_numbers', compare(candidate['result'], result) + compare(pc['result']['metrics'], expected['metrics']) +
           compare(pc['metric_source']['artifact_sha256'], sha(paths[result_name])))
    engine_report = paths['report.md'].read_text(); marker = '```json\n'
    numeric_blocks = [part.split('\n```', 1)[0] for part in engine_report.split(marker)[1:]]
    record('engine_report_numeric_block', [] if any(load_json(b) == result['metrics'] for b in numeric_blocks) else ['Engine report metrics missing or changed'])
    readable = (output / 'report.md').read_text().replace('\\', '')
    shown = {}
    for line in readable.splitlines():
        cells = [cell.strip() for cell in line.split('|')]
        if len(cells) == 5 and cells[3].startswith('/payload/runs/0/candidates/0/result/metrics/'):
            shown[cells[3].rsplit('/', 1)[1]] = json.loads(cells[2])
    visible = {k: v for k, v in pc['result']['metrics'].items() if k not in ('finite_factor_observations', 'possible_factor_observations')}
    record('research_report_readable_numbers', compare(shown, visible) +
           ([] if all(k in readable for k in ('finite_factor_observations', 'possible_factor_observations')) else ['Unshown count metrics have no complete-index reference']))
    material = load(output / 'material/import.json'); engineering = load(output / 'material/engineering-version.json')
    hypothesis = engineering['version']['material']['hypotheses'][0]
    record('scientific_unknown_and_human_pending', compare(hypothesis['economic_mechanism'], None) + compare(hypothesis['mechanism_attribution'], 'unresolved') +
        compare(engineering['human_approval'], False) + compare(engineering['version']['actor'], 'automation') + compare(review_count, 0) +
        compare(pc['human_review']['status'], 'pending_human') + compare(hypothesis['calculation'], material['draft']['hypotheses'][0]['calculation']))
    invalid = load(output / 'proposals/invalid/status.json')
    record('formal_parser_rejected_invalid_fixture', compare(invalid['status'], 'failed') + compare(invalid['error']['code'], 'REQUEST_MISMATCH') +
        compare(invalid['accepted_response'], None) + compare(summary['fixture_dispatches'], 3) + compare(summary['real_model_dispatches'], 0) + compare(summary['worker_runs'], 1))
    return {'schema_version': 1, 'source': 'independent_stdlib_automation', 'outcome': 'passed' if all(c['outcome'] == 'passed' for c in checks) else 'failed',
        'run_id': summary['run_id'], 'report_id': summary['report_id'], 'checks': checks,
        'reference_metrics': expected['metrics'], 'reference_factor_cells': len(expected['factors']),
        'tolerance': {'absolute': ABS_TOL, 'relative': REL_TOL}, 'human_approval': False,
        'human_review': {'status': 'pending_human', 'scores': None}, 'scientific_validity': 'unknown',
        'limitations': ['Checks one original arithmetic teaching fixture on synthetic data; not model quality or investment evidence.',
                        'Reference values are never provided to the proposal transport.']}


def load_json(value):
    return json.loads(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('.demo/agent'))
    args = parser.parse_args(argv)
    result = check(args.output_dir)
    (args.output_dir / 'verification.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'outcome': result['outcome'], 'checks': len(result['checks']), 'reference_metrics': result['reference_metrics']}, indent=2))
    return 0 if result['outcome'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
