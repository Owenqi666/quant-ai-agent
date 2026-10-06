"""Immutable monthly inputs, independently checked results, and derived reports."""
from __future__ import annotations

import argparse
from importlib.metadata import version
from pathlib import Path, PurePosixPath
import shutil
import stat

from . import monthly_evaluation as core, monthly_reference
from .evidence import sha256
from .research_protocol import config_digest as rule_digest, SEMANTICS_VERSION as RULE_SEMANTICS
from .storage import atomic_json, digest, json_text, read_json
from .workflow import code_files, environment

MAX_BYTES = 512 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
PROTOCOL_BODY = {'title', 'note', 'parent_id', 'config', 'config_digest', 'changes',
                 'semantics_version', 'sources', 'unresolved'}


def _input(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'protocol', 'config', 'bundle'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Invalid monthly workflow input fields')
    protocol = value['protocol']
    if not isinstance(protocol, dict) or set(protocol) != PROTOCOL_BODY | {'id', 'digest', 'created_at'}:
        raise ValueError('Invalid frozen protocol fields')
    body = {key: protocol[key] for key in PROTOCOL_BODY}
    if (protocol['digest'] != digest(body) or protocol['id'] != 'protocol_' + protocol['digest']
            or protocol['config_digest'] != rule_digest(protocol['config']) or protocol['semantics_version'] != RULE_SEMANTICS):
        raise ValueError('Frozen research protocol digest mismatch')
    from .server.research_protocol_schema import ProtocolRecord
    ProtocolRecord.model_validate(protocol)
    config = core.validate_config(value['config'])
    if json_text(config) != json_text(value['config']):
        raise ValueError('Monthly configuration is not canonical')
    return value


def _text(value):
    return str(value).replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')


def _number(value):
    return 'unavailable' if value is None else format(value, '.17g')


def _reference_valid(request, result, reference):
    if reference['issues']:
        return False
    if request['protocol']['config']['mode'] == 'paper':
        return result['status'] == 'blocked' and reference['supported'] is False and reference['passed'] is None
    return reference['supported'] is True and reference['passed'] is True


def render_report(result):
    """Only format engine values. No financial statistic is computed here."""
    lines = ['# Monthly research experiment', '',
             'Controlled-fixture project comparison. Not real-market performance or original-paper reproduction.', '',
             f"Semantics: {_text(result['semantics_version'])}",
             f"Result: {_text(result['status'])}",
             f"Months: {_text(result['config']['start_month'])} to {_text(result['config']['end_month'])}",
             f"Data: {_text(result['source_id'])}; input digest: {result['input_digest']}", '',
             'Both strategies use the same formation sample and gross exposure 1. MOM-ID is the normalized low-ID/high-ID momentum contrast.',
             'Cost, turnover, net return and NAV are target-weight proxies; they exclude holdings drift, borrow, financing and slippage.', '',
             '| Strategy | Net months | Gross months | Turnover months | Mean gross | Mean net proxy | Annualized Sharpe proxy | Complete terminal NAV proxy | Max drawdown proxy |',
             '|---|---|---|---|---|---|---|---|---|']
    for item in result['summary']:
        lines.append('| ' + ' | '.join([item['strategy_id'], f"{item['months_evaluated']}/{item['months_total']}",
            str(item['gross_months']), str(item['turnover_months']), _number(item['mean_gross_return']),
            _number(item['mean_net_return_proxy']), _number(item['sharpe_annualized_proxy']),
            _number(item['terminal_nav_proxy']), _number(item['max_drawdown_proxy'])]) + ' |')
    lines.extend(['', '| Month | Strategy | Gross | Traded weight | Cost proxy | Net proxy | NAV proxy | Reasons |',
                  '|---|---|---|---|---|---|---|---|'])
    for month in result['months']:
        for item in month['strategies']:
            lines.append('| ' + ' | '.join([month['month'], item['id'], _number(item['gross_return']),
                _number(item['traded_weight']), _number(item['estimated_cost']), _number(item['net_return_proxy']),
                _number(item['nav_proxy']), _text('; '.join(item['reasons']))]) + ' |')
    lines.extend(['', '## Formation exclusions', ''])
    for month in result['months']:
        for item in month['exclusions']:
            lines.append(f"\nExcluded at formation {month['month']}: {_text(item['asset'])}: {_text('; '.join(item['reasons']))}")
    lines.extend(['', '## Boundaries', ''])
    lines.extend('- ' + _text(item) for item in result['warnings'])
    lines.extend(['', 'Exact signals, groups, unchanged weights and label coverage are retained in result.json.', ''])
    return '\n'.join(lines)


def _load(path, limit=MAX_INPUT_BYTES):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('Monthly artifact must be a bounded independent regular file')
    return read_json(path)


def run(input_path, output_dir):
    request = _input(_load(input_path))
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'input.json', request)
    runtime = environment()
    runtime['packages'].update({name: version(name) for name in ('pydantic', 'fastapi')})
    atomic_json(out / 'environment.json', runtime)
    sources = code_files()
    sources['requirements-web-lock.txt'] = Path(__file__).resolve().parents[1] / 'requirements-web-lock.txt'
    source_hashes = {name: sha256(path) for name, path in sources.items()}
    for name, path in sources.items():
        target = out / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha256(target) != source_hashes[name] or sha256(path) != source_hashes[name]:
            raise ValueError('Computation source changed during snapshotting')
    result = core.evaluate(request['protocol']['config'], request['config'], request['bundle'])
    reference = monthly_reference.check(request['protocol']['config'], request['config'], request['bundle'], result)
    from .server.monthly_schema import MonthlyResult, MonthlyReference
    MonthlyResult.model_validate(result)
    MonthlyReference.model_validate(reference)
    atomic_json(out / 'result.json', result)
    atomic_json(out / 'reference.json', reference)
    if not _reference_valid(request, result, reference):
        raise ValueError('Independent monthly reference rejected the result')
    (out / 'report.md').write_text(render_report(result), encoding='utf-8')
    inventory = {str(path.relative_to(out)): sha256(path) for path in sorted(out.rglob('*')) if path.is_file()}
    manifest = {'schema_version': 1, 'semantics_version': core.SEMANTICS_VERSION,
                'input_digest': digest(request), 'result_digest': digest(result), 'files': inventory}
    atomic_json(out / 'manifest.json', manifest)
    return verify(out, expected_input_digest=digest(request))


def verify(output_dir, expected_input_digest=None):
    out = Path(output_dir).absolute()
    if out.resolve() != out or not out.is_dir():
        raise ValueError('Monthly output directory is missing or unsafe')
    manifest = _load(out / 'manifest.json', 1024 * 1024)
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'semantics_version', 'input_digest', 'result_digest', 'files'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or manifest['semantics_version'] != core.SEMANTICS_VERSION):
        raise ValueError('Unsupported monthly output manifest')
    files = manifest['files']
    if not isinstance(files, dict) or not 5 <= len(files) <= 4096:
        raise ValueError('Invalid monthly artifact inventory')
    required = {'input.json', 'result.json', 'reference.json', 'report.md', 'environment.json'}
    required.update('source/paper_alpha/' + name + '.py' for name in
                    ('monthly_evaluation', 'monthly_reference', 'monthly_workflow', 'research_protocol', 'storage'))
    required.add('source/requirements-web-lock.txt')
    if not required <= set(files):
        raise ValueError('Monthly artifact inventory is incomplete')
    actual, total = set(), 0
    for path in out.rglob('*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe monthly artifact')
        total += info.st_size
        actual.add(str(path.relative_to(out)))
        if len(actual) > 4097 or total > MAX_BYTES:
            raise ValueError('Monthly artifact inventory exceeds limits')
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('Monthly artifact inventory changed')
    for name, expected in files.items():
        if (not isinstance(name, str) or PurePosixPath(name).is_absolute() or '\\' in name
                or any(part in {'', '.', '..'} for part in name.split('/')) or sha256(out / name) != expected):
            raise ValueError('Monthly artifact digest mismatch')
    request = _input(_load(out / 'input.json'))
    result, reference = _load(out / 'result.json'), _load(out / 'reference.json')
    from .server.monthly_schema import MonthlyResult, MonthlyReference
    MonthlyResult.model_validate(result)
    MonthlyReference.model_validate(reference)
    if (digest(request) != manifest['input_digest'] or digest(result) != manifest['result_digest']
            or (expected_input_digest is not None and digest(request) != expected_input_digest)
            or result['rules'] != request['protocol']['config'] or result['config'] != request['config']
            or result['input_digest'] != digest(request['bundle'])
            or result['config_digest'] != digest({'rules': result['rules'], 'config': result['config']})):
        raise ValueError('Monthly result is not bound to the frozen request')
    checked = monthly_reference.check(result['rules'], result['config'], request['bundle'], result)
    if checked != reference or not _reference_valid(request, result, checked):
        raise ValueError('Monthly independent reference verification failed')
    if (out / 'report.md').read_text(encoding='utf-8') != render_report(result):
        raise ValueError('Monthly report differs from its computed values')
    return {'verified': True, 'input_digest': manifest['input_digest'], 'result_digest': manifest['result_digest'],
            'reference_passed': checked['passed']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    execute = commands.add_parser('run')
    execute.add_argument('input', type=Path)
    execute.add_argument('--out', type=Path, required=True)
    check = commands.add_parser('verify')
    check.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        print(run(args.input, args.out) if args.command == 'run' else verify(args.output))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Monthly workflow failed: {type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
