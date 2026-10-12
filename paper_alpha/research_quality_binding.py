"""Read-only engineering bindings; opaque routing is not a blind scientific test."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

from . import research_quality as legacy
from .daily_adapter import precheck_development
from .daily_contract import parse_contract
from .server.provider_catalog import catalog_from_daily_contract, validate_catalog
from .storage import digest, json_text

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
PLAN_VERSION = 'quality-opaque-plan-v1'
BINDING_VERSION = 'host-daily-quality-binding-v1'
RUN_VERSION = 'daily-development-run-v1'
DAILY_FILES = frozenset({name + '.json' for name in (
    'attempt', 'preflight', 'inputs', 'contract', 'config', 'catalog', 'candidate',
    'code', 'factor', 'evaluation', 'report')} | {'factor.csv', 'report.md', 'development_input.csv'})


class QualityBindingError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _need(ok, message, code='QUALITY_BINDING_INVALID'):
    if not ok:
        raise QualityBindingError(code, message)


def _fields(value, names, label):
    _need(type(value) is dict and set(value) == set(names), 'Unknown/missing ' + label + ' fields')


def _sha(value):
    import re
    _need(type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None, 'Invalid SHA256')
    return value


def _finite_number(value):
    if type(value) not in (int, float): return False
    try: return math.isfinite(value)
    except OverflowError: return False


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _regular(path, limit):
    path = Path(path).absolute()
    _need(path.resolve() == path and not path.is_symlink(), 'Linked/noncanonical input', 'UNSAFE_INPUT')
    before = path.stat()
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit,
          'Input must be a bounded, single-link regular file', 'UNSAFE_INPUT')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    stream = os.fdopen(fd, 'rb')
    try:
        _need(_signature(os.fstat(stream.fileno())) == _signature(before), 'Input changed before read', 'INPUT_CHANGED')
        return path, stream, before
    except BaseException:
        stream.close()
        raise


def _finish(path, stream, before):
    _need(not path.is_symlink() and path.resolve() == path and
          _signature(os.fstat(stream.fileno())) == _signature(before) == _signature(path.stat()),
          'Input changed during read', 'INPUT_CHANGED')


def _read(path, limit=MAX_JSON_BYTES):
    path, stream, before = _regular(path, limit)
    with stream:
        raw = stream.read(limit + 1)
        _need(len(raw) == before.st_size and len(raw) <= limit, 'Input size changed', 'INPUT_CHANGED')
        _finish(path, stream, before)
    return raw


def _hash(path, limit=MAX_FILE_BYTES):
    path, stream, before = _regular(path, limit)
    h = hashlib.sha256(); total = 0
    with stream:
        for block in iter(lambda: stream.read(65536), b''):
            total += len(block)
            _need(total <= limit, 'Input exceeded byte limit', 'INPUT_CHANGED')
            h.update(block)
        _need(total == before.st_size, 'Input size changed', 'INPUT_CHANGED')
        _finish(path, stream, before)
    return h.hexdigest(), total


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('Duplicate JSON key')
            value[key] = item
        return value
    def reject(value): raise ValueError('Nonfinite JSON constant')
    def finite(value):
        result = float(value)
        if not math.isfinite(result): raise ValueError('Nonfinite JSON number')
        return result
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=pairs, parse_constant=reject, parse_float=finite)
        count = 0
        def walk(item, depth=0):
            nonlocal count
            count += 1
            _need(count <= 500000 and depth <= 64, 'JSON complexity limit')
            if isinstance(item, dict):
                for child in item.values(): walk(child, depth + 1)
            elif isinstance(item, list):
                for child in item: walk(child, depth + 1)
        walk(value)
        json_text(value)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise QualityBindingError('INVALID_JSON', 'Invalid/duplicate/nonfinite bounded JSON') from exc


def read_bounded_json(path):
    """Strict single-snapshot JSON for host-owned requests/receipts."""
    return _json(_read(path))


def _protocol(manifest_path):
    raw = _read(manifest_path); manifest = _json(raw)
    _fields(manifest, {'schema_version', 'protocol_id', 'fixture_only', 'protocol_file', 'protocol_sha256',
        'source_file', 'source_sha256', 'fixture_observations_file', 'fixture_observations_sha256',
        'negative_matrix_file', 'negative_matrix_sha256', 'planned_checks', 'planned_runs', 'pending_material_slots'}, 'v1 manifest')
    _need(type(manifest['schema_version']) is int and manifest['schema_version'] == 1 and
          manifest['protocol_id'] == legacy.PROTOCOL_ID and manifest['fixture_only'] is True,
          'Only frozen engineering v1 protocol supported')
    root = Path(manifest_path).absolute().parent; protocol = None
    for key in ('protocol', 'source', 'fixture_observations', 'negative_matrix'):
        filename = manifest[key + '_file']
        _need(type(filename) is str and Path(filename).name == filename and filename not in {'.', '..'}, 'Unsafe fixture filename')
        content = _read(root / filename)
        _need(hashlib.sha256(content).hexdigest() == _sha(manifest[key + '_sha256']), 'Frozen v1 file SHA differs', 'PROTOCOL_DRIFT')
        if key == 'protocol': protocol = legacy.validate_protocol(_json(content))
    _need(manifest['planned_runs'] == len(protocol['cases']) * len(legacy.MODES) * protocol['repetitions'] and
          manifest['planned_checks'] == sum(len(c['checks']) for c in protocol['cases']) * len(legacy.MODES) * protocol['repetitions'],
          'Frozen fixture denominator differs')
    return protocol, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class FrozenOpaquePlan:
    _json: str
    def to_dict(self): return _json(self._json.encode())


@dataclass(frozen=True)
class HostDailyBinding:
    _json: str
    def to_dict(self): return _json(self._json.encode())


def _opaque(prefix): return prefix + uuid.uuid4().hex


def _view(value, protocol, opaque_case):
    _need(opaque_case in value['case_map'], 'Foreign opaque case', 'FOREIGN_ID')
    case = next(c for c in protocol['cases'] if c['id'] == value['case_map'][opaque_case])
    source = next(s for s in protocol['sources'] if s['id'] == case['source_id'])
    source_id = next(k for k, v in value['source_map'].items() if v == source['id'])
    checks = {c['id']: c for c in case['checks']}
    return {'schema_version': 1, 'view_version': 'quality-opaque-exposure-v1', 'case_id': opaque_case,
            'task_prompt': case['task']['prompt'],
            'source': {'id': source_id, **{k: source[k] for k in ('sha256', 'media_type', 'physical_page', 'text')}},
            'catalog': deepcopy(value['catalog']),
            'fixture_checker_capabilities': {k: deepcopy(protocol['capabilities'][k]) for k in ('fields', 'operators')},
            'checks': [{'id': key, 'kind': checks[host]['kind']} for key, host in sorted(value['check_map'][opaque_case].items())],
            'exposure_limits': {'routing_condition_names_removed': True, 'task_source_residual_leakage': True,
                'source_may_contain_formula_answers': True, 'blind_test': False,
                'scope': 'Original engineering material preserved; only routing identifiers are opaque.'}}


def freeze_opaque_exposure(protocol_manifest_path, catalog_path):
    """Host generates a private mapping; provider never chooses or edits it."""
    protocol, manifest_sha = _protocol(protocol_manifest_path)
    catalog_raw = _read(catalog_path); catalog = validate_catalog(_json(catalog_raw))
    value = {'schema_version': 1, 'plan_version': PLAN_VERSION,
        'protocol_manifest_path': str(Path(protocol_manifest_path).absolute()), 'protocol_manifest_sha256': manifest_sha,
        'protocol_digest': digest(protocol), 'adapter_code_sha256': _hash(__file__)[0], 'catalog_path': str(Path(catalog_path).absolute()),
        'catalog_file_sha256': hashlib.sha256(catalog_raw).hexdigest(), 'catalog': catalog,
        'case_map': {_opaque('qc_'): c['id'] for c in protocol['cases']},
        'source_map': {_opaque('qs_'): s['id'] for s in protocol['sources']}}
    by_case = {c['id']: c for c in protocol['cases']}
    value['check_map'] = {key: {_opaque('qk_'): c['id'] for c in by_case[host]['checks']} for key, host in value['case_map'].items()}
    value['mapping_digest'] = digest({k: value[k] for k in ('case_map', 'source_map', 'check_map')})
    value['exposure_digests'] = {key: digest(_view(value, protocol, key)) for key in value['case_map']}
    value['plan_digest'] = digest(value)
    plan = FrozenOpaquePlan(json_text(value)); _validated_plan(plan)
    return plan


def _validated_plan(plan):
    _need(type(plan) is FrozenOpaquePlan, 'A host-frozen plan object is required')
    value = plan.to_dict()
    _fields(value, {'schema_version', 'plan_version', 'protocol_manifest_path', 'protocol_manifest_sha256',
        'protocol_digest', 'adapter_code_sha256', 'catalog_path', 'catalog_file_sha256', 'catalog', 'case_map', 'source_map',
        'check_map', 'mapping_digest', 'exposure_digests', 'plan_digest'}, 'opaque plan')
    _need(type(value['schema_version']) is int and value['schema_version'] == 1 and value['plan_version'] == PLAN_VERSION, 'Unsupported opaque plan')
    _need(value['plan_digest'] == digest({k: v for k, v in value.items() if k != 'plan_digest'}), 'Plan digest differs')
    _need(value['adapter_code_sha256'] == _hash(__file__)[0], 'Opaque adapter code changed', 'CODE_DRIFT')
    protocol, actual = _protocol(value['protocol_manifest_path'])
    _need(actual == value['protocol_manifest_sha256'] and digest(protocol) == value['protocol_digest'], 'Plan protocol drift', 'PROTOCOL_DRIFT')
    raw = _read(value['catalog_path']); current = validate_catalog(_json(raw))
    _need(hashlib.sha256(raw).hexdigest() == value['catalog_file_sha256'] and current == value['catalog'], 'Plan catalog drift', 'CATALOG_DRIFT')
    import re
    for key, prefix, expected in [('case_map', 'qc_', {c['id'] for c in protocol['cases']}),
                                 ('source_map', 'qs_', {s['id'] for s in protocol['sources']})]:
        mapping = value[key]
        _need(type(mapping) is dict and len(mapping) == len(expected) and set(mapping.values()) == expected and
              all(re.fullmatch(prefix + '[a-f0-9]{32}', k) for k in mapping), 'Invalid host opaque mapping')
    _need(type(value['check_map']) is dict and set(value['check_map']) == set(value['case_map']), 'Check mapping cases differ')
    for opaque_case, host in value['case_map'].items():
        case = next(c for c in protocol['cases'] if c['id'] == host); mapping = value['check_map'][opaque_case]
        _need(type(mapping) is dict and len(mapping) == len(case['checks']) and set(mapping.values()) == {c['id'] for c in case['checks']} and
              all(re.fullmatch('qk_[a-f0-9]{32}', k) for k in mapping), 'Invalid host check mapping')
    _need(value['mapping_digest'] == digest({k: value[k] for k in ('case_map', 'source_map', 'check_map')}), 'Mapping digest differs')
    _need(value['exposure_digests'] == {key: digest(_view(value, protocol, key)) for key in value['case_map']}, 'Exposure digest differs')
    return value, protocol


def restore_opaque_plan(path, *, expected_file_sha256):
    """Expected file SHA must come from the host receipt, never the generator."""
    raw = _read(path); _need(hashlib.sha256(raw).hexdigest() == _sha(expected_file_sha256), 'Private plan anchor differs')
    plan = FrozenOpaquePlan(json_text(_json(raw))); _validated_plan(plan)
    return plan


def provider_view(plan, opaque_case_id):
    value, protocol = _validated_plan(plan)
    return _view(value, protocol, opaque_case_id)


def evaluate_opaque_fixture(plan, submissions):
    """Host-private mechanical report; no D values substitute frozen v1 labels."""
    value, protocol = _validated_plan(plan)
    _fields(submissions, {'schema_version', 'plan_digest', 'runs'}, 'opaque submissions')
    _need(type(submissions['schema_version']) is int and submissions['schema_version'] == 1 and
          submissions['plan_digest'] == value['plan_digest'], 'Submission plan differs')
    _need(type(submissions['runs']) is list and len(submissions['runs']) <= legacy.MAX_CASES * 9, 'Run list limit')
    runs = []
    for run in submissions['runs']:
        _fields(run, {'case_id', 'mode', 'repeat', 'status', 'checks'}, 'opaque run')
        opaque_case = run['case_id']; _need(opaque_case in value['case_map'], 'Foreign opaque case', 'FOREIGN_ID')
        host_case = value['case_map'][opaque_case]; case = next(c for c in protocol['cases'] if c['id'] == host_case)
        _need(run['mode'] in legacy.MODES and type(run['repeat']) is int and 1 <= run['repeat'] <= protocol['repetitions'], 'Foreign run slot')
        _need(type(run['status']) is str and run['status'] in legacy.STATUSES, 'Unknown run status')
        _need(run['mode'] != 'model_assisted' or run['status'] == 'not_run', 'Model execution remains unqualified')
        _need(type(run['checks']) is list and len(run['checks']) <= 100, 'Check list limit')
        checks = []
        for check in run['checks']:
            _fields(check, {'id', 'submission'}, 'opaque check')
            _need(check['id'] in value['check_map'][opaque_case], 'Foreign opaque check', 'FOREIGN_ID')
            host_check = value['check_map'][opaque_case][check['id']]
            frozen = next(c for c in case['checks'] if c['id'] == host_check)
            submitted = deepcopy(check['submission'])
            if frozen['kind'] in {'citation', 'attribution'}:
                _need(type(submitted) is dict and submitted.get('source_id') in value['source_map'], 'Foreign opaque source', 'FOREIGN_ID')
                submitted['source_id'] = value['source_map'][submitted['source_id']]
            elif frozen['kind'] == 'report_metric':
                # This layer has no actual frozen v1 metric receipt. Keep unknown.
                _fields(submitted, {'value'}, 'unavailable fixture metric submission')
                submitted = {k: frozen['target'][k] for k in ('result_id', 'metric_key')} | submitted
            checks.append({'id': host_check, 'submission': submitted})
        runs.append({'id': f"{host_case}/{run['mode']}/{run['repeat']}", 'case_id': host_case,
            'mode': run['mode'], 'repeat': run['repeat'], 'status': run['status'], 'checks': checks,
            'identities': deepcopy(case['identities']), 'usage': {key: 0 if key in {'provider_attempts', 'backend_model_calls'} else None for key in legacy.USAGE_FIELDS},
            'tool_wall_seconds': None})
    report = legacy.evaluate(protocol, {'schema_version': 1, 'protocol_id': legacy.PROTOCOL_ID,
        'protocol_sha256': digest(protocol), 'runs': runs, 'tool_results': []})
    return {'schema_version': 1, 'report_version': 'opaque-engineering-quality-v1',
        'scope': 'frozen_v1_mechanical_checker_with_opaque_routing_only', 'plan_digest': value['plan_digest'],
        'catalog_digest': value['catalog']['catalog_digest'], 'exposure_digests': value['exposure_digests'],
        'mechanical_report_host_private': report, 'scientific_score': None, 'blind_test': False,
        'project_model_dispatches': 0, 'actual_daily_tool_binding': 'separate_API_not_substituted_into_v1_expectations'}


def _bind_daily_development(out_dir, *, expected_manifest_sha256, allow_engineering_input_hash=False):
    """Read an externally pinned, host-produced D engineering attempt; no compute."""
    _need(allow_engineering_input_hash is True, 'Explicit engineering byte-hash/read permission required', 'INPUT_PERMISSION_REQUIRED')
    root = Path(out_dir).absolute()
    _need(root.is_dir() and root.resolve() == root and not root.is_symlink(), 'Unsafe run directory', 'UNSAFE_INPUT')
    manifest_raw = _read(root / 'manifest.json')
    _need(hashlib.sha256(manifest_raw).hexdigest() == _sha(expected_manifest_sha256), 'Host manifest anchor differs', 'MANIFEST_DRIFT')
    manifest = _json(manifest_raw)
    _fields(manifest, {'schema_version', 'run_version', 'status', 'scope', 'identity', 'files', 'test_freshness',
        'real_source_ready', 'real_execution_readiness', 'registered', 'legacy_guard_connected', 'model_calls',
        'final_numeric_rows_parsed', 'manifest_digest', 'attempt_id'}, 'daily manifest')
    _need(type(manifest['schema_version']) is int and manifest['schema_version'] == 1 and manifest['run_version'] == RUN_VERSION and
          manifest['status'] == 'completed' and manifest['scope'] == 'engineering_fixture_development_validation', 'Uncompleted/unqualified D run')
    _need(manifest['manifest_digest'] == digest({k: v for k, v in manifest.items() if k != 'manifest_digest'}), 'Manifest content digest differs')
    _need(manifest['real_source_ready'] is False and manifest['registered'] is False and manifest['legacy_guard_connected'] is False and
          manifest['real_execution_readiness'] == 'pending' and manifest['test_freshness'] == 'unknown' and
          type(manifest['model_calls']) is int and manifest['model_calls'] == 0 and
          type(manifest['final_numeric_rows_parsed']) is int and manifest['final_numeric_rows_parsed'] == 0, 'Run scope differs')
    _need(type(manifest['files']) is dict and set(manifest['files']) == DAILY_FILES, 'Unexpected/missing D result files')
    _need({p.name for p in root.iterdir()} == DAILY_FILES | {'manifest.json'}, 'Unlisted run files/directories')
    objects = {}; file_receipts = {}; total = 0
    for name, expected in manifest['files'].items():
        if name.endswith('.json'):
            raw = _read(root / name); actual = hashlib.sha256(raw).hexdigest(); size = len(raw)
            objects[name[:-5]] = _json(raw)
        else: actual, size = _hash(root / name)
        _need(actual == _sha(expected), 'Manifest artifact SHA differs: ' + name, 'ARTIFACT_DRIFT')
        total += size; _need(total <= MAX_TOTAL_BYTES, 'Run byte budget exceeded')
        file_receipts[name] = {'sha256': actual, 'bytes': size}
    attempt = objects['attempt']; _need(attempt['status'] == 'completed' and attempt['attempt_id'] == manifest['attempt_id'] and attempt['identity'] == manifest['identity'], 'Attempt identity differs')
    uuid.UUID(manifest['attempt_id'])
    inputs = objects['inputs']; _fields(inputs, {'schema_version', 'paths', 'input_digests', 'read_scope', 'note'}, 'inputs')
    _fields(inputs['paths'], {'contract', 'csv', 'source_receipt'}, 'input paths')
    _need(all(type(p) is str and Path(p).is_absolute() for p in inputs['paths'].values()), 'Input paths must be host absolute paths')
    original_contract = _json(_read(inputs['paths']['contract']))
    contract = parse_contract(original_contract)
    _need(original_contract == objects['contract'] and original_contract['origin'] == 'engineering_fixture', 'Only actual engineering fixture inputs allowed')
    fresh = precheck_development(inputs['paths']['contract'], inputs['paths']['csv'], inputs['paths']['source_receipt'],
                                allow_whole_byte_hash=True, required_fields=('open', 'high', 'low', 'close')).report
    _need(fresh == objects['preflight'] and fresh['inputs'] == inputs['input_digests'] and fresh['read_scope'] == inputs['read_scope'], 'Actual source/preflight identity differs', 'INPUT_DRIFT')
    _need(objects['config'] == original_contract['evaluation'], 'Actual config differs', 'CONFIG_DRIFT')
    catalog = validate_catalog(objects['catalog'])
    _need(catalog['family'] == 'declared_daily' and catalog == catalog_from_daily_contract(contract), 'Actual complete catalog differs', 'CATALOG_DRIFT')
    from .daily_research import CODE_FILES, research_code_identity
    code = objects['code']
    _fields(code, {'run_version', 'operator_semantics_version', 'files', 'python', 'numpy', 'pandas', 'digest'}, 'code')
    _need(type(code['files']) is dict and set(code['files']) == set(CODE_FILES), 'Code inventory differs', 'CODE_DRIFT')
    project = Path(__file__).resolve().parents[1]
    for name, expected in code['files'].items():
        _need(_hash(project / name, MAX_JSON_BYTES)[0] == _sha(expected), 'Actual code bytes differ: ' + name, 'CODE_DRIFT')
    _need(code == research_code_identity(), 'Actual code/environment identity differs', 'CODE_DRIFT')
    expected_identity = {**fresh['identity'], **fresh['inputs'], 'origin': 'engineering_fixture',
        'adapter_code_digest': fresh['identity']['code_digest'], 'catalog_digest': catalog['catalog_digest'],
        'code_digest': code['digest'], 'config_digest': digest(objects['config']),
        'candidate_digest': digest(objects['candidate']), 'factor_digest': digest(objects['factor']),
        'evaluation_digest': digest(objects['evaluation']), 'evaluation_file_sha256': file_receipts['evaluation.json']['sha256']}
    _need(manifest['identity'] == expected_identity, 'Actual input/config/code/tool identities differ', 'IDENTITY_DRIFT')
    result = objects['evaluation']; report = objects['report']
    _fields(report, {'schema_version', 'report_version', 'scope', 'candidate_id', 'source_reference',
        'expression', 'result_digest', 'reported_metrics', 'reported_daily', 'identity', 'test_freshness',
        'real_source_ready', 'real_execution_readiness', 'limitations'}, 'D report')
    _need(type(report['schema_version']) is int and report['schema_version'] == 1 and
          report['report_version'] == RUN_VERSION and report['scope'] == 'engineering_fixture_development_validation' and
          report['test_freshness'] == 'unknown' and report['real_source_ready'] is False and
          report['real_execution_readiness'] == 'pending', 'Report scope differs')
    _need(result['evaluation_scope'] == 'development_prefix_validation_only' and result['test_freshness'] == 'unknown' and
          type(result['final_numeric_rows_used']) is int and result['final_numeric_rows_used'] == 0 and
          result['source_authentication'] == 'not_performed' and result['config'] == objects['config'], 'Evaluation scope/config differs')
    _need(report['identity'] == expected_identity and report['result_digest'] == digest(result) and
          report['reported_metrics'] == result['metrics'] and report['reported_daily'] == result['daily'], 'Report differs from actual evaluator result', 'REPORT_DRIFT')
    _need(type(result['metrics']) is dict, 'Actual metrics must be an object')
    metrics = {}
    for key, value in result['metrics'].items():
        _need(type(key) is str and len(key) <= 128, 'Invalid metric key')
        if value is None or type(value) in (int, float):
            if value is not None: _need(_finite_number(value), 'Invalid host metric')
            metrics[key] = value
    _need(bool(metrics), 'No scalar/null host metric slots')
    value = {'schema_version': 1, 'binding_version': BINDING_VERSION, 'scope': 'host_observed_engineering_daily_validation',
        'adapter_code_sha256': _hash(__file__)[0], 'run_dir': str(root), 'run_id': manifest['attempt_id'], 'manifest_file_sha256': expected_manifest_sha256,
        'manifest_digest': manifest['manifest_digest'], 'identity': expected_identity, 'files': file_receipts,
        'input_paths_host_private': deepcopy(inputs['paths']), 'catalog': catalog, 'actual_tool_digest': digest(result),
        'evaluation_file_sha256': file_receipts['evaluation.json']['sha256'], 'actual_metrics': metrics,
        'read_scope': {'raw_engineering_bytes_hashed': fresh['read_scope']['whole_bytes_hashed'],
            'development_numeric_rows_checked': fresh['read_scope']['development_rows_parsed'],
            'original_final_numeric_rows_parsed': 0, 'evaluation_scope': result['evaluation_scope'],
            'full_dataset_validated': False},
        'scientific_score': None, 'source_authentication': 'not_performed', 'point_in_time': 'not_certified',
        'project_model_dispatches': 0, 'native_integration': 'pending',
        'provenance_limit': 'Externally pinned host-produced local engineering output; byte consistency is not independent scientific truth.'}
    value['binding_digest'] = digest(value)
    # Recheck artifacts after source/code reads without reparsing final market rows.
    _need(_read(root / 'manifest.json') == manifest_raw, 'Manifest changed during binding', 'MANIFEST_DRIFT')
    for name, r in file_receipts.items(): _need(_hash(root / name)[0] == r['sha256'], 'Artifact changed during binding', 'ARTIFACT_DRIFT')
    return HostDailyBinding(json_text(value))


def bind_daily_development(out_dir, *, expected_manifest_sha256, allow_engineering_input_hash=False):
    """Bounded host readback; malformed/missing receipts never become a score."""
    try:
        return _bind_daily_development(out_dir, expected_manifest_sha256=expected_manifest_sha256,
                                       allow_engineering_input_hash=allow_engineering_input_hash)
    except QualityBindingError:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise QualityBindingError('HOST_READBACK_INVALID', 'Host inputs/receipts could not be safely verified') from exc


def restore_daily_binding(path, *, expected_file_sha256):
    raw = _read(path); _need(hashlib.sha256(raw).hexdigest() == _sha(expected_file_sha256), 'Host binding file anchor differs')
    value = _json(raw)
    fresh = bind_daily_development(value['run_dir'], expected_manifest_sha256=value['manifest_file_sha256'], allow_engineering_input_hash=True)
    _need(fresh.to_dict() == value, 'Stored host binding differs from actual readback')
    return fresh


def verify_report_metrics(binding, submission):
    _need(type(binding) is HostDailyBinding, 'A host-bound D result object is required')
    value = binding.to_dict()
    fresh = bind_daily_development(value['run_dir'], expected_manifest_sha256=value['manifest_file_sha256'], allow_engineering_input_hash=True)
    _need(fresh.to_dict() == value, 'Binding drifted or was forged', 'IDENTITY_DRIFT')
    _fields(submission, {'schema_version', 'run_id', 'binding_digest', 'actual_tool_digest', 'metrics'}, 'metric submission')
    _need(type(submission['schema_version']) is int and submission['schema_version'] == 1 and
          submission['run_id'] == value['run_id'] and submission['binding_digest'] == value['binding_digest'] and
          submission['actual_tool_digest'] == value['actual_tool_digest'], 'Foreign/missing tool run binding', 'FOREIGN_RUN')
    _need(type(submission['metrics']) is dict and set(submission['metrics']) <= set(value['actual_metrics']), 'Unknown metric key')
    rows = []
    for key, actual in value['actual_metrics'].items():
        submitted = submission['metrics'].get(key)
        if key in submission['metrics']:
            _need(submitted is None or type(submitted) in (int, float), 'Metric cannot be boolean/non-numeric')
            if submitted is not None: _need(_finite_number(submitted), 'Metric must be finite')
        outcome = 'unknown' if key not in submission['metrics'] or actual is None else 'passed' if submitted == actual else 'failed'
        rows.append({'metric_key': key, 'outcome': outcome, 'actual_tool_value': actual, 'submitted_value': submitted})
    return {'schema_version': 1, 'report_version': 'host-bound-daily-metric-check-v1',
        'scope': 'engineering_report_to_actual_D_tool_value_only', 'run_id': value['run_id'],
        'binding_digest': value['binding_digest'], 'actual_tool_digest': value['actual_tool_digest'],
        'catalog_digest': value['catalog']['catalog_digest'], 'checks': rows,
        'passed': sum(r['outcome'] == 'passed' for r in rows), 'denominator': len(rows),
        'unknown': sum(r['outcome'] == 'unknown' for r in rows), 'failed': sum(r['outcome'] == 'failed' for r in rows),
        'scientific_score': None, 'project_model_dispatches': 0, 'independent_reference': 'pending',
        'native_integration': 'pending'}
