"""Fixture-only Provider evaluation preparation and deterministic aggregation.

No provider, queue, experiment, subprocess, authentication or dispatch is invoked.
Fixture checks are declared projections used to test aggregation; they never
constitute a business execution or a model-quality measurement.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import platform
import stat

import pydantic

from .provider_evaluation_schema import (EvaluationManifest, EvaluationPlan, EvaluationProtocol,
                                        FixtureObservation, FixtureRecord)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / 'evaluation_suites/provider_dev_v1'
SOURCE_NAMES = ('paper_alpha/provider_evaluation_schema.py', 'paper_alpha/provider_evaluation.py',
                'scripts/run_provider_evaluation.py')
MAX_JSON_BYTES = 2 * 1024 * 1024


class ProviderEvaluationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ProviderEvaluationError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _relative(name):
    _require(isinstance(name, str) and bool(name), 'Invalid relative file reference')
    path = Path(name)
    _require(not path.is_absolute() and '..' not in path.parts
             and path.as_posix() == name and '\\' not in name, 'Invalid relative file reference')
    return path


def _read(path, limit=MAX_JSON_BYTES):
    path = Path(path).absolute()
    _require(not any(parent.is_symlink() for parent in (path, *path.parents)), 'Symlinked file reference')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit, 'Missing, nonregular or oversized file')
            raw = stream.read(limit + 1)
            _require(0 < len(raw) <= limit, 'File exceeds its bound')
            return raw
    except OSError as exc:
        raise ProviderEvaluationError('Cannot read required bounded file') from exc


def _identity(raw):
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ProviderEvaluationError('Duplicate JSON key')
            value[key] = item
        return value

    def nonfinite(_):
        raise ProviderEvaluationError('Nonfinite JSON value')

    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=nonfinite)
        pending, nodes = [(result, 0)], 0
        while pending:
            value, depth = pending.pop(); nodes += 1
            _require(depth <= 32 and nodes <= 20000, 'JSON complexity exceeds its bound')
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        canonical(result)  # Also rejects finite-looking exponent overflow.
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProviderEvaluationError('Invalid bounded JSON: ' + str(exc)[:160]) from exc


def _model(model, value):
    try:
        normalized = model.model_validate(value).model_dump()
        _require(canonical(normalized) == canonical(value), 'Contract is not canonical or contains coerced values')
        return normalized
    except (ValueError, TypeError) as exc:
        raise ProviderEvaluationError('Invalid closed ' + model.__name__ + ' contract') from exc


def _write(path, value):
    raw = (canonical(value) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _output(path):
    path = Path(path).absolute()
    _require('..' not in path.parts and not any(p.is_symlink() for p in (path, *path.parents)), 'Unlinked new output directory required')
    _require(not path.is_relative_to(ROOT / 'var'), 'Formal workspace cannot be an evaluation output')
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except OSError as exc:
        raise ProviderEvaluationError('Output must be a new directory') from exc
    return path


def _implementation():
    return {name: _identity(_read(ROOT / name)) for name in SOURCE_NAMES}


def _environment():
    return {'python': platform.python_version(), 'pydantic': pydantic.__version__}


def _record_ids(protocol, manifest):
    return [f"{case['id']}/fixture/{repeat}" for case in manifest['cases'] if case['partition'] == 'development'
            for repeat in range(1, protocol['repetitions'] + 1)]


def build_catalog(protocol, manifest):
    """Derive all denominators before records exist; pending domains stay visible."""
    cases = [case for case in manifest['cases'] if case['partition'] == 'development']
    reps = protocol['repetitions']
    return {'schema_version': 1, 'kind': 'provider_development_case_catalog',
        'measurement_scope': 'aggregation_fixture', 'repetitions': reps,
        'declared_record_count': len(cases) * reps,
        'fixture_ready_record_count': sum(c['availability'] == 'fixture_ready' for c in cases) * reps,
        'blocked_record_count': sum(c['availability'] == 'blocked' for c in cases) * reps,
        'positive_record_count': sum(c['role'] == 'positive' for c in cases) * reps,
        'negative_record_count': sum(c['role'] == 'negative' for c in cases) * reps,
        'numerical_required_record_count': sum(c['numerical_required'] for c in cases) * reps,
        'citation_required_record_count': sum(c['citation_required'] for c in cases) * reps,
        'planned_real_provider_dispatches': 0, 'planned_actual_business_runs': 0,
        'unique_paper_lineages': sorted({c['paper_lineage'] for c in cases if c['paper_lineage']}),
        'positive_formula_families': sorted({c['formula_family'] for c in cases if c['role'] == 'positive' and c['formula_family']}),
        'reserved_case_ids': [c['id'] for c in manifest['cases'] if c['partition'] == 'reserved'],
        'blocked_cases': [{'id': c['id'], 'requires': c['requires'], 'reason': c['blocked_reason']} for c in cases if c['availability'] == 'blocked'],
        'labels': 'automation_engineering_not_human_references'}


def _validate_suite(protocol, manifest, catalog):
    protocol = _model(EvaluationProtocol, protocol)
    manifest = _model(EvaluationManifest, manifest)
    ids = [case['id'] for case in manifest['cases']]
    _require(len(ids) == len(set(ids)), 'Duplicate case ID')
    _require(protocol['future_business_modes'] == ['normalized_fixed', 'provider_one_shot', 'provider_bounded_repair'], 'Business mode declaration differs')
    _require(catalog == build_catalog(protocol, manifest), 'Catalog differs from declared case denominators')
    _require(len(_record_ids(protocol, manifest)) <= protocol['budget']['max_fixture_records'], 'Fixture plan exceeds its record budget')
    _require(any(c['availability'] == 'fixture_ready' for c in manifest['cases']), 'No fixture-ready development case')
    return protocol, manifest


def load_suite(suite_dir=DEFAULT_SUITE):
    """Validate exact suite bytes and reference identities; execute nothing."""
    directory = Path(suite_dir).absolute()
    snapshots = {name: _read(directory / name) for name in ('protocol.json', 'manifest.json', 'case_catalog.json')}
    protocol, manifest = _validate_suite(*(_json(snapshots[name]) for name in ('protocol.json', 'manifest.json', 'case_catalog.json')))
    for case in manifest['cases']:
        if case['fixture'] is None:
            continue
        reference = case['fixture']
        _require(reference['path'].startswith('fixtures/'), 'Fixture must belong to the suite fixtures directory')
        raw = _read(directory / _relative(reference['path']), protocol['budget']['max_record_bytes'])
        _require(_identity(raw) == reference['identity'], 'Fixture identity differs')
        fixture = _model(FixtureObservation, _json(raw))
        _require(fixture['case_id'] == case['id'], 'Fixture belongs to another case')
        snapshots[reference['path']] = raw
    for key, reference in manifest['inputs'].items():
        raw = _read(ROOT / _relative(reference['path']), 64 * 1024 * 1024)
        _require(_identity(raw) == reference['identity'], 'Frozen input reference differs')
        snapshots[f'inputs/{key}.bin'] = raw
    _require(sum(map(len, snapshots.values())) <= protocol['budget']['max_total_artifact_bytes'], 'Suite snapshots exceed their total bound')
    return {'protocol': protocol, 'manifest': manifest, 'catalog': _json(snapshots['case_catalog.json']), 'snapshots': snapshots}


def create_plan(suite_dir, output_dir):
    suite = load_suite(suite_dir)
    body = {'schema_version': 1, 'kind': 'provider_evaluation_plan',
        'protocol_digest': digest(suite['protocol']), 'measurement_scope': 'aggregation_fixture',
        'queue_exercised': False, 'real_execution_supported': False,
        'planned_real_provider_dispatches': 0, 'planned_actual_business_runs': 0,
        'snapshots': {name: _identity(raw) for name, raw in suite['snapshots'].items()},
        'implementation': _implementation(), 'environment': _environment(),
        'record_ids': _record_ids(suite['protocol'], suite['manifest'])}
    fingerprint = digest(body)
    plan = _model(EvaluationPlan, {**body, 'id': 'provider_plan_' + fingerprint, 'digest': fingerprint})
    out = _output(output_dir)
    for name, raw in suite['snapshots'].items():
        path = out / _relative(name); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(raw)
    _write(out / 'plan.json', plan)
    return plan


def _load_plan(directory, *, current_implementation=False):
    directory = Path(directory).absolute()
    plan = _model(EvaluationPlan, _json(_read(directory / 'plan.json')))
    body = {key: value for key, value in plan.items() if key not in {'id', 'digest'}}
    _require(plan['digest'] == digest(body) and plan['id'] == 'provider_plan_' + plan['digest'], 'Plan content identity differs')
    _require(set(plan['implementation']) == set(SOURCE_NAMES), 'Implementation closure differs')
    for name, identity in plan['snapshots'].items():
        _require(_identity(_read(directory / _relative(name), identity['bytes'])) == identity, 'Plan snapshot identity differs')
    protocol, manifest = _validate_suite(*(_json(_read(directory / name)) for name in ('protocol.json', 'manifest.json', 'case_catalog.json')))
    expected = ({'protocol.json', 'manifest.json', 'case_catalog.json'}
        | {c['fixture']['path'] for c in manifest['cases'] if c['fixture'] is not None}
        | {f'inputs/{key}.bin' for key in manifest['inputs']})
    _require(set(plan['snapshots']) == expected, 'Snapshot inventory differs')
    _require(plan['protocol_digest'] == digest(protocol) and plan['record_ids'] == _record_ids(protocol, manifest), 'Plan protocol or record IDs differ')
    _require(sum(item['bytes'] for item in plan['snapshots'].values()) <= protocol['budget']['max_total_artifact_bytes'], 'Plan snapshots exceed their total bound')
    for key, reference in manifest['inputs'].items():
        _relative(reference['path'])
        _require(plan['snapshots'][f'inputs/{key}.bin'] == reference['identity'], 'Input reference differs from saved bytes')
    for case in manifest['cases']:
        if case['fixture'] is not None:
            _require(plan['snapshots'][case['fixture']['path']] == case['fixture']['identity'], 'Fixture reference differs from snapshot')
            fixture = _model(FixtureObservation, _json(_read(directory / case['fixture']['path'])))
            _require(fixture['case_id'] == case['id'], 'Fixture belongs to another case')
    if current_implementation:
        _require(plan['implementation'] == _implementation() and plan['environment'] == _environment(), 'Implementation or environment changed; create a new plan')
    return plan, protocol, manifest


def _ratio(passed, eligible):
    eligible = sorted(set(eligible)); passed = sorted(set(passed) & set(eligible))
    return {'numerator': len(passed), 'denominator': len(eligible),
            'rate': len(passed) / len(eligible) if eligible else None,
            'passed_ids': passed, 'eligible_ids': eligible}


def _inventory(directory):
    names = set()
    for path in directory.rglob('*'):
        _require(not path.is_symlink(), 'Artifact inventory contains a symlink')
        if not path.is_dir():
            names.add(path.relative_to(directory).as_posix())
    return names


def aggregate_records(protocol, manifest, records):
    """Pure aggregation; simulated declarations remain distinct from actual work."""
    protocol = _model(EvaluationProtocol, protocol); manifest = _model(EvaluationManifest, manifest)
    expected = {key: case for case in manifest['cases'] if case['partition'] == 'development'
                for repeat in range(1, protocol['repetitions'] + 1) for key in [f"{case['id']}/fixture/{repeat}"]}
    normalized = [_model(FixtureRecord, record) for record in records]
    indexed = {record['id']: record for record in normalized}
    _require(len(indexed) == len(normalized) and set(indexed) <= set(expected), 'Duplicate or unexpected record')
    for key, record in indexed.items():
        case = expected[key]
        _require(record['case_id'] == case['id'] and key == f"{case['id']}/fixture/{record['repetition']}", 'Record case/repetition identity differs')
        _require(case['availability'] != 'blocked' or record['outcome'] == 'not_run', 'Unimplemented case cannot declare completion')
    allowed = [key for key, r in indexed.items() if r['outcome'] in expected[key]['allowed_outcomes'] and expected[key]['availability'] == 'fixture_ready']
    ready = [key for key, case in expected.items() if case['availability'] == 'fixture_ready']
    positive = [key for key, case in expected.items() if case['role'] == 'positive']
    negative = [key for key, case in expected.items() if case['role'] == 'negative']
    numerical = [key for key, case in expected.items() if case['numerical_required']]
    citations = [key for key, case in expected.items() if case['citation_required']]
    dispatched = [key for key, r in indexed.items() if r['simulated_provider_invocations'] > 0]
    comparable = [key for key in numerical if key in indexed and indexed[key]['simulated_numerical'] in {'passed', 'failed'}]
    projections = [key for key in indexed if indexed[key]['simulated_projection_digest'] is not None]
    repeated, repeat_passed = [], []
    for case in manifest['cases']:
        first = indexed.get(f"{case['id']}/fixture/1")
        for repeat in range(2, protocol['repetitions'] + 1):
            key = f"{case['id']}/fixture/{repeat}"; other = indexed.get(key)
            if first and other and first['simulated_projection_digest'] and other['simulated_projection_digest']:
                repeated.append(key)
                if first['simulated_projection_digest'] == other['simulated_projection_digest']:
                    repeat_passed.append(key)
    return {'measurement_scope': 'aggregation_fixture', 'queue_exercised': False,
        'model_execution': 'model_not_run', 'actual_provider_dispatches': 0,
        'actual_business_runs': 0, 'backend_model_calls': 0, 'cost': None, 'usage': None,
        'human_quality': None, 'human_time_saved': None, 'model_quality': None,
        'model_reproducibility': _ratio([], []), 'business_compute_reproducibility': _ratio([], []),
        'fixture_declared_handling': _ratio(allowed, expected),
        'fixture_ready_handling': _ratio(allowed, ready),
        'fixture_positive_completion': _ratio([key for key in positive if key in allowed and indexed[key]['outcome'] == 'completed'], positive),
        'fixture_negative_handling': _ratio(allowed, negative),
        'fixture_schema_acceptance': _ratio([key for key in dispatched if indexed[key]['simulated_schema_valid'] is True], dispatched),
        'fixture_citation_verification': _ratio([key for key in citations if key in indexed and indexed[key]['simulated_citation_valid'] is True], citations),
        'fixture_numerical_coverage': _ratio(comparable, numerical),
        'fixture_numerical_agreement': _ratio([key for key in comparable if indexed[key]['simulated_numerical'] == 'passed'], comparable),
        'fixture_repeat_consistency': _ratio(repeat_passed, repeated),
        'fixture_projection_records': projections,
        'observed_record_count': len(indexed), 'declared_record_count': len(expected),
        'missing_record_ids': sorted(set(expected) - set(indexed)),
        'not_run_record_ids': sorted(key for key, r in indexed.items() if r['outcome'] == 'not_run'),
        'fixture_outcomes': dict(sorted(Counter(r['outcome'] for r in indexed.values()).items())),
        'simulated_provider_invocations': sum(r['simulated_provider_invocations'] for r in normalized),
        'simulated_experiments': sum(r['simulated_experiments'] for r in normalized)}


def _not_run(case):
    return {'schema_version': 1, 'case_id': case['id'], 'measurement_scope': 'aggregation_fixture',
        'outcome': 'not_run', 'reason_code': 'BUSINESS_CONTRACT_NOT_IMPLEMENTED',
        'simulated_provider_invocations': 0, 'simulated_experiments': 0,
        'simulated_response': 'not_requested', 'simulated_schema_valid': None,
        'simulated_citation_valid': None, 'simulated_numerical': 'not_run',
        'simulated_baseline': 'not_run', 'simulated_projection_digest': None,
        'actual_provider_dispatches': 0, 'actual_business_runs': 0, 'backend_model_calls': 0,
        'usage': None, 'cost': None, 'human_reference': 'pending', 'semantic_fidelity': 'unverified'}


def _records(directory, protocol, manifest):
    records = []
    for case in manifest['cases']:
        if case['partition'] != 'development':
            continue
        base = (_json(_read(Path(directory) / case['fixture']['path'])) if case['availability'] == 'fixture_ready' else _not_run(case))
        for repeat in range(1, protocol['repetitions'] + 1):
            records.append(_model(FixtureRecord, {**base, 'id': f"{case['id']}/fixture/{repeat}", 'repetition': repeat}))
    return records


def report_markdown(summary):
    metrics = summary['metrics']
    lines = ['# Provider development: aggregation fixture', '',
        'Measurement scope: aggregation_fixture. No actual dispatch, business run, model call or human review occurred.', '',
        'These are frozen simulated declarations testing aggregation, not queue execution, Alpha results or model-quality scores.', '',
        '| Fixture metric | Numerator | Denominator | Rate |', '|---|---:|---:|---:|']
    for key, value in metrics.items():
        if key.startswith('fixture_') and isinstance(value, dict) and 'denominator' in value:
            lines.append(f"| {key} | {value['numerator']} | {value['denominator']} | {value['rate']} |")
    lines += ['', f"Actual provider dispatches: {metrics['actual_provider_dispatches']}; actual business runs: {metrics['actual_business_runs']}.",
        'Cost/usage/model quality/human quality/human time: null. Actual reproducibility: not measured.', '',
        '## Not-run development records', '', *[f'- {key}' for key in metrics['not_run_record_ids']], '',
        '## Reserved cases (not executed)', '', *[f"- {item['id']}: {item['reason']}" for item in summary['reserved_cases']], '']
    return '\n'.join(lines)


def run_fixture(plan_dir, output_dir):
    plan, protocol, manifest = _load_plan(plan_dir, current_implementation=True)
    records = _records(plan_dir, protocol, manifest)
    metrics = aggregate_records(protocol, manifest, records)
    out = _output(output_dir)
    for name, identity in plan['snapshots'].items():
        path = out / _relative(name); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(_read(Path(plan_dir) / name, identity['bytes']))
    _write(out / 'plan.json', plan)
    _write(out / 'records.json', records)
    _require(plan['implementation'] == _implementation(), 'Implementation changed during aggregation')
    summary = {'schema_version': 1, 'kind': 'provider_evaluation_fixture_summary', 'plan_id': plan['id'],
        'plan_digest': plan['digest'], 'measurement_scope': 'aggregation_fixture',
        'records_digest': digest(records), 'metrics': metrics,
        'aggregation_fixture_complete': not metrics['missing_record_ids'],
        'business_evaluation_complete': False, 'real_execution_supported': False,
        'reserved_cases': [{'id': c['id'], 'status': 'not_run', 'reason': c['blocked_reason']} for c in manifest['cases'] if c['partition'] == 'reserved']}
    _write(out / 'summary.json', summary)
    (out / 'report.md').write_text(report_markdown(summary), encoding='utf-8')
    inventory = {name: _identity(_read(out / name, protocol['budget']['max_total_artifact_bytes'])) for name in (*plan['snapshots'], 'plan.json', 'records.json', 'summary.json', 'report.md')}
    _write(out / 'artifact-manifest.json', {'schema_version': 1, 'kind': 'aggregation_fixture_artifacts', 'files': inventory})
    return summary


def verify_artifact(output_dir):
    """Read/recompute a plan or fixture report; never dispatch or rerun a task."""
    out = Path(output_dir).absolute()
    plan, protocol, manifest = _load_plan(out)
    if not (out / 'artifact-manifest.json').exists():
        expected_files = set(plan['snapshots']) | {'plan.json'}
        _require(_inventory(out) == expected_files, 'Plan file inventory differs')
        return {'verified': True, 'kind': 'plan', 'plan_id': plan['id'], 'measurement_scope': 'aggregation_fixture',
            'actual_provider_dispatches': 0, 'actual_business_runs': 0,
            'implementation_matches_current': plan['implementation'] == _implementation(), 'business_evaluation_complete': False}
    artifact = _json(_read(out / 'artifact-manifest.json'))
    _require(set(artifact) == {'schema_version', 'kind', 'files'} and artifact['schema_version'] == 1 and artifact['kind'] == 'aggregation_fixture_artifacts', 'Artifact manifest contract differs')
    expected_files = set(plan['snapshots']) | {'plan.json', 'records.json', 'summary.json', 'report.md'}
    _require(set(artifact['files']) == expected_files, 'Artifact manifest inventory differs')
    actual_files = _inventory(out)
    _require(actual_files == expected_files | {'artifact-manifest.json'}, 'Artifact file inventory differs')
    for name, identity in artifact['files'].items():
        _require(_identity(_read(out / _relative(name), protocol['budget']['max_total_artifact_bytes'])) == identity, 'Recorded artifact identity differs')
    records = _json(_read(out / 'records.json'))
    expected_records = _records(out, protocol, manifest)
    _require(records == expected_records, 'Fixture records differ from frozen declarations')
    summary = _json(_read(out / 'summary.json'))
    metrics = aggregate_records(protocol, manifest, records)
    expected_summary = {'schema_version': 1, 'kind': 'provider_evaluation_fixture_summary', 'plan_id': plan['id'],
        'plan_digest': plan['digest'], 'measurement_scope': 'aggregation_fixture', 'records_digest': digest(records), 'metrics': metrics,
        'aggregation_fixture_complete': not metrics['missing_record_ids'], 'business_evaluation_complete': False,
        'real_execution_supported': False, 'reserved_cases': [{'id': c['id'], 'status': 'not_run', 'reason': c['blocked_reason']} for c in manifest['cases'] if c['partition'] == 'reserved']}
    _require(summary == expected_summary, 'Summary differs from recomputed fixture denominators')
    _require(_read(out / 'report.md').decode('utf-8') == report_markdown(expected_summary), 'Report differs from actual summary')
    return {'verified': True, 'kind': 'fixture', 'plan_id': plan['id'], 'measurement_scope': 'aggregation_fixture',
        'checked_record_count': len(records), 'actual_provider_dispatches': 0, 'actual_business_runs': 0,
        'implementation_matches_current': plan['implementation'] == _implementation(), 'business_evaluation_complete': False}
