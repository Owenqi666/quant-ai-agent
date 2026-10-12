"""Controller-bound synthetic evidence; never business authorization.

No process manager is defined here. All challenges use the existing bounded
supervisor. The host retains a per-attempt expected identity; saved reports and
readiness booleans cannot substitute for it. Native measurement is loopback,
not a claim of equivalence with the separately pinned business RPC/endpoint.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import stat
import time
import uuid

from .codex_cli_provider import _atomic_bytes, _bounded_private_file, _read_stop, _safe_environment
from .provider_app_server_business_identity import app_server_identity, _validated_identity
from .provider_app_server_invocation import BUSINESS_RPC_POLICY_DIGEST, CONTROLLED_RPC_POLICY_DIGEST
from .provider_app_server_protocol import AppServerProtocol
from .provider_app_server_profile import validate_app_server_profile
from .provider_app_server_probe import assess as legacy_assess
from .provider_app_server_write_observability import (load_controlled_write_observability_report, assess_controlled_write_observability, CONTROLLED_WRITE_LOADER_VERSION)
from .provider_contract import canonical_json, digest, strict_json
from .provider_outer_sandbox import static_descriptor, assess_outer_boundary
from .provider_read_probe import READ_BOUNDARY_CASES
from .provider_schema import ProviderCallControl, ProviderConfig
from .provider_write_probe import CASES

VERSION = 'native_app_server_fresh_control_v2'
SCOPE = 'controller_bound_synthetic_observation_only_no_business_qualification'
ROOT = Path(__file__).resolve().parents[2]
MAX_NATIVE_ATTEMPTS = 11
_IDENTITY_KEYS = {'version', 'scope', 'nonce', 'artifact_dir', 'business_static_identity',
    'config_snapshot', 'config_digest', 'controller', 'environment', 'context_policy_version',
    'business_rpc_policy_digest', 'synthetic_rpc_policy_digest', 'controlled_write_loader_version', 'identity_digest'}
_REPORT_KEYS = {'report_version', 'scope', 'identity', 'legacy_report', 'write_report',
    'raw_manifest', 'resources', 'assessment', 'failure', 'business_activation_eligible'}


class _FreshExpectedIdentity(dict):
    def __init__(self, value, control):
        super().__init__(value)
        self.control_reference = control
        self.cancel_reference = control.cancel_event


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _private_dir(path):
    path = Path(path)
    _require(path.is_absolute() and str(path) == str(path.resolve(strict=True))
        and not any(p.is_symlink() for p in (path, *path.parents)), 'FRESH_ARTIFACT_PATH_INVALID')
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700, 'FRESH_ARTIFACT_DIRECTORY_UNSAFE')
    return path


def _controller(control):
    _require(isinstance(control, ProviderCallControl)
        and type(control.deadline_monotonic) in (int, float)
        and math.isfinite(control.deadline_monotonic)
        and isinstance(control.cancel_event, asyncio.Event)
        and type(control.pass_fds) is tuple
        and len(set(control.pass_fds)) == len(control.pass_fds), 'FRESH_CONTROLLER_INVALID')
    descriptors = []
    for fd in control.pass_fds:
        _require(type(fd) is int and fd >= 3, 'FRESH_CONTROLLER_FD_INVALID')
        info = os.fstat(fd)
        descriptors.append({'fd': fd, 'dev': info.st_dev, 'ino': info.st_ino,
            'uid': info.st_uid, 'gid': info.st_gid, 'mode': info.st_mode, 'nlink': info.st_nlink})
    return {'deadline_monotonic': control.deadline_monotonic, 'pass_fds': list(control.pass_fds),
        'fd_identity': descriptors}


def prepare_fresh_identity(config, control):
    """Read current code-owned static pins; no version, auth or Native call."""
    from .provider_cli_state import APP_SERVER_CONTEXT_POLICY_VERSION
    _require(isinstance(config, ProviderConfig) and Path(config.executable).is_absolute() and str(Path(config.executable).resolve(strict=True)) == config.executable, 'FRESH_CONFIG_INVALID')
    snapshot = config.model_dump(mode='json')
    # bool/int coercion is rejected by the fixed business renderer/profile.
    static_identity, profile = app_server_identity(config)
    _require(profile['static_checks']['passed'], 'FRESH_STATIC_PROFILE_FAILED')
    parent = _private_dir(control.artifact_dir)
    controller = _controller(control)
    _require(not control.cancel_event.is_set() and time.monotonic() < control.deadline_monotonic
        <= time.monotonic() + 600, 'FRESH_CONTROL_EXPIRED')
    value = {'version': VERSION, 'scope': SCOPE, 'nonce': uuid.uuid4().hex,
        'artifact_dir': str(parent), 'business_static_identity': static_identity,
        'config_snapshot': snapshot, 'config_digest': digest(snapshot), 'controller': controller,
        'environment': _safe_environment(), 'context_policy_version': APP_SERVER_CONTEXT_POLICY_VERSION,
        'business_rpc_policy_digest': BUSINESS_RPC_POLICY_DIGEST,
        'synthetic_rpc_policy_digest': CONTROLLED_RPC_POLICY_DIGEST,
        'controlled_write_loader_version': CONTROLLED_WRITE_LOADER_VERSION}
    value['identity_digest'] = digest(value)
    return _FreshExpectedIdentity(value, control)


def _validate_expected(value):
    from .provider_cli_state import APP_SERVER_CONTEXT_POLICY_VERSION
    _require(isinstance(value, dict) and set(value) == _IDENTITY_KEYS and value['version'] == VERSION
        and value['scope'] == SCOPE and type(value['nonce']) is str and len(value['nonce']) == 32
        and all(c in '0123456789abcdef' for c in value['nonce'])
        and value['identity_digest'] == digest({k: v for k, v in value.items() if k != 'identity_digest'})
        and value['context_policy_version'] == APP_SERVER_CONTEXT_POLICY_VERSION
        and value['business_rpc_policy_digest'] == BUSINESS_RPC_POLICY_DIGEST
        and value['synthetic_rpc_policy_digest'] == CONTROLLED_RPC_POLICY_DIGEST
        and value['controlled_write_loader_version'] == CONTROLLED_WRITE_LOADER_VERSION,
        'FRESH_EXPECTED_IDENTITY_INVALID')
    static = value['business_static_identity']
    _require(static['identity_digest'] == digest({k: v for k, v in static.items() if k != 'identity_digest'}),
        'FRESH_STATIC_IDENTITY_INVALID')
    profile = _validated_identity(static)
    _require(profile['static_checks']['passed'] and profile['config_snapshot'] == value['config_snapshot']
        and profile['config_digest'] == value['config_digest'] == digest(value['config_snapshot']),
        'FRESH_CONFIG_BINDING_INVALID')
    return profile


def _dispatch_guard(config, control, expected):
    _validate_expected(expected)
    _require(type(expected) is _FreshExpectedIdentity and expected.control_reference is control
        and expected.cancel_reference is control.cancel_event, 'FRESH_CONTROL_REFERENCE_DRIFT')
    _require(canonical_json(config.model_dump(mode='json')) == canonical_json(expected['config_snapshot'])
        and canonical_json(_controller(control)) == canonical_json(expected['controller'])
        and str(_private_dir(control.artifact_dir)) == expected['artifact_dir']
        and _safe_environment() == expected['environment'], 'FRESH_CONTROLLER_OR_CONFIG_DRIFT')
    current, _ = app_server_identity(config)
    _require(canonical_json(current) == canonical_json(expected['business_static_identity']),
        'FRESH_CURRENT_SOURCE_DRIFT')
    _require(not control.cancel_event.is_set() and time.monotonic() < control.deadline_monotonic,
        'FRESH_CONTROL_EXPIRED')


def _read(root, name, limit=2 * 1024 * 1024):
    path = Path(name)
    _require(not path.is_absolute() and '..' not in path.parts and str(path) == name,
        'FRESH_RAW_PATH_INVALID')
    actual = root / path
    _require(not any(p.is_symlink() for p in (actual, *actual.parents)), 'FRESH_RAW_LINK_INVALID')
    return _bounded_private_file(actual, limit)


def _json(root, name, limit=2 * 1024 * 1024):
    return strict_json(_read(root, name, limit), max_bytes=limit)


def _allowed_raw_paths():
    from .provider_app_server_write_intervention_probe import PHASE_FILES, SUPERVISION_FILES, PHASES
    allowed = {'expected-identity.json', 'source-freeze.json', 'legacy/report.json', 'legacy/continuation.json',
        'write/report.json', 'write/expected-identity.json', 'write/source-freeze.json',
        'write/legacy-matrix-report.json', 'write/fixture-operations.json'}
    case_files = ('controller.json', 'environment.json', 'target.before.bin', 'target.after.bin',
        'target.before.identity.json', 'target.after.identity.json', 'controller.before.json', 'stdout.jsonl', 'stderr.log', 'observation.json',
        'request.1.http', 'request.2.http', 'response.1.sse', 'response.2.sse')
    for case in READ_BOUNDARY_CASES + CASES:
        allowed.update('legacy/' + case + '/' + name for name in case_files)
        allowed.update('legacy/' + case + '/supervision/' + name for name in SUPERVISION_FILES)
    for phase in PHASES:
        allowed.update('write/' + phase + '/' + name for name in (*PHASE_FILES, 'controller.json', 'controller.before.json', 'environment.json'))
        allowed.update('write/' + phase + '/supervision/' + name for name in SUPERVISION_FILES)
    return allowed


def _manifest(root):
    entries = []
    # Fixed classified paths only: never traverse/read work, cli-state, an
    # unknown credential/config/database file, or the full checkout.
    for name in sorted(_allowed_raw_paths()):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        raw = _read(root, name)
        entries.append({'path': name, 'sha256': _sha(raw), 'size_bytes': len(raw)})
    return entries


def _validate_controller_raw(root, name, expected, observation):
    record = _json(root, name)
    before_record = _json(root, str(Path(name).parent / 'controller.before.json'))
    _require(canonical_json(before_record) == canonical_json({**record, 'cancelled_after': False}), 'FRESH_CONTROLLER_PREFIX_DRIFT')
    # Controller extras are driver-owned; these required facts are matched
    # against host identity and the original supervisor spec below.
    ctrl = expected['controller']
    _require(type(record) is dict and set(record) == {'version', 'config_digest', 'deadline_monotonic', 'case_deadline_monotonic', 'pass_fds', 'fd_metadata', 'context_policy_version', 'case', 'cancelled_after'} and type(record['version']) is int and record['version'] == 1, 'FRESH_CONTROLLER_RECORD_INVALID')
    _require(record['config_digest'] == expected['config_digest']
        and record['deadline_monotonic'] == ctrl['deadline_monotonic']
        and type(record['deadline_monotonic']) is type(ctrl['deadline_monotonic'])
        and canonical_json(record['pass_fds']) == canonical_json(ctrl['pass_fds'])
        and canonical_json(record['fd_metadata']) == canonical_json(ctrl['fd_identity'])
        and record['context_policy_version'] == expected['context_policy_version']
        and record['case'] == observation.get('phase', observation['case']), 'FRESH_CASE_CONTROLLER_DRIFT')
    environment = _json(root, str(Path(name).parent / 'environment.json'))
    state = observation['outer_boundary']['cli_state']
    _require(environment == {**expected['environment'], 'HOME': state, 'CODEX_HOME': state}, 'FRESH_ENVIRONMENT_DRIFT')
    case_deadline = record['case_deadline_monotonic']
    _require(type(case_deadline) in (int, float) and math.isfinite(case_deadline)
        and case_deadline <= ctrl['deadline_monotonic'], 'FRESH_CASE_DEADLINE_INVALID')
    directory = root / Path(name).parent / 'supervision'
    spec = _json(directory, 'spec.json')
    _require(type(spec) is dict and set(spec) == {'protocol_version', 'nonce', 'argv', 'cwd', 'deadline_monotonic', 'output_limit', 'response_path', 'response_limit', 'pass_fds', 'transport'}
        and type(spec['protocol_version']) is int and spec['protocol_version'] == 2 and spec['transport'] == 'app_server'
        and type(spec['response_limit']) is int and spec['response_limit'] == 65536
        and type(spec['output_limit']) is int and spec['output_limit'] == 1048576
        and spec['response_path'] == observation['outer_boundary']['response_path'], 'FRESH_SPEC_INVALID')
    ready = _json(directory, 'ready.json'); launched = _json(directory, 'launched.json')
    _require(set(ready) == {'protocol_version', 'nonce', 'supervisor_pid', 'ready'} and ready['ready'] is True
        and set(launched) == {'protocol_version', 'nonce', 'supervisor_pid', 'cli_pid'}
        and all(type(record['protocol_version']) is int and record['protocol_version'] == 2 and record['nonce'] == spec['nonce']
            and type(record['supervisor_pid']) is int and record['supervisor_pid'] > 0 and record['supervisor_pid'] == ready['supervisor_pid']
            for record in (ready, launched)) and type(launched['cli_pid']) is int and launched['cli_pid'] > 0, 'FRESH_LAUNCH_BINDING_INVALID')
    stop = _json(directory, 'stop.json')
    _require(spec['argv'] == observation['argv'] and spec['cwd'] == observation['allowed_root']
        and canonical_json(spec['pass_fds']) == canonical_json(ctrl['pass_fds']) and spec['deadline_monotonic'] == case_deadline,
        'FRESH_SPEC_CONTROLLER_MISMATCH')
    checked, stdout, stderr = _read_stop(directory, spec['nonce'], ready['supervisor_pid'],
        case_deadline, spec['output_limit'], protocol_version=2)
    _require(type(checked['supervisor_pid']) is int and checked['cli_pid'] == launched['cli_pid'] and checked['process_stopped'] is True and checked['invocation_started'] is True
        and checked['exit_code'] == 0 and checked['reason'] is None and checked['rpc_completed'] is True
        and checked['response_saved'] is True
        and checked['started_monotonic'] <= checked['finished_monotonic'] <= checked['deadline_monotonic'] <= checked['started_monotonic'] + 12
        and _sha(stdout) == observation['stdout_sha256'] and _sha(stderr) == observation['stderr_sha256'],
        'FRESH_STOP_INCOMPLETE')
    rpc = _json(directory, 'rpc-result.json', 65536)
    _require(canonical_json(rpc) == canonical_json(observation['rpc_summary']), 'FRESH_RPC_SUMMARY_DRIFT')
    _require(_read(directory, 'stdin.bin', 131072) == canonical_json(observation['rpc_plan']).encode(), 'FRESH_RPC_PLAN_DRIFT')
    protocol = AppServerProtocol(observation['rpc_plan'])
    protocol.start()
    for line in stdout.splitlines(keepends=True):
        protocol.feed(line)
    protocol.finish_eof()
    _require(protocol.summary() == {k: v for k, v in rpc.items() if k != 'supervision'}
        and strict_json(protocol.response, max_bytes=65536) == {'done': True}, 'FRESH_RPC_REPLAY_FAILED')
    _require(record.get('cancelled_after') is False, 'FRESH_CANCELLED_CASE')


def _validate_legacy_http(root, observation):
    from .codex_cli_provider import _Capture
    from .provider_app_server_write_intervention_probe import (_phase_effect,
        OUTSIDE_PREFLIGHT_RAW_FILES, outside_preflight_refusal_valid)
    from .provider_read_probe import _Stub, _done_response
    from .provider_write_probe import _WriteStub
    write = observation['case'] in CASES
    route = ('/write-challenge-' if write else '/challenge-') + observation['challenge_nonce'] + '/v1/responses'
    target = Path(observation['synthetic_target'])
    stub = _WriteStub(route, target) if write else _Stub(route, target, allow_apply_patch=True)
    prefix = 'legacy/' + observation['case'] + '/'
    if observation['case'] == 'readonly_outside':
        _require(outside_preflight_refusal_valid(observation,
            {name: _read(root, prefix+name) for name in OUTSIDE_PREFLIGHT_RAW_FILES}),
            'FRESH_OUTSIDE_PREFLIGHT_INVALID')
    elif write:
        _require(_phase_effect(_Capture(stdout=_read(root, prefix+'stdout.jsonl'), app_server_summary=observation['rpc_summary']), target,
            'write_before' if observation['case'] == 'allow_write_control' else 'readonly'), 'FRESH_LEGACY_MATCHED_PATCH_INVALID')
    for i in (1, 2):
        data = _read(root, prefix + f'request.{i}.http')
        header, body = data.split(b'\r\n\r\n', 1)
        lines = header.decode('ascii').split('\r\n')
        _require(lines[0] == 'POST ' + route + ' HTTP/1.1', 'FRESH_HTTP_ROUTE_INVALID')
        headers = {}
        for line in lines[1:]:
            key, value = line.split(':', 1); key = key.strip().lower()
            _require(key not in headers, 'FRESH_HTTP_DUPLICATE_HEADER')
            headers[key] = value.strip()
        _require('authorization' not in headers and 'proxy-authorization' not in headers
            and 'transfer-encoding' not in headers and headers.get('content-encoding', 'identity') == 'identity'
            and headers.get('host') == '127.0.0.1:' + str(observation['outer_boundary']['loopback_port'])
            and headers.get('content-length') == str(len(body)), 'FRESH_HTTP_HEADERS_INVALID')
        stub.count += 1
        stub.observe(strict_json(body, max_bytes=2 * 1024 * 1024))
        _require(not stub.blockers and not stub.cancel.is_set(), 'FRESH_HTTP_TOOL_BOUNDARY_INVALID')
    _require(stub.count == observation['request_count'] == 2 and sorted(stub.tools) == observation['tool_names']
        and stub.image_payloads == observation['image_payloads_in_requests']
        and canonical_json(stub.tool_outputs) == canonical_json(observation['synthetic_tool_outputs'])
        and canonical_json(stub.request_shapes) == canonical_json(observation['request_shapes'])
        and _read(root, prefix+'response.1.sse') == stub.tool_response()
        and _read(root, prefix+'response.2.sse') == _done_response(), 'FRESH_HTTP_OBSERVATION_DRIFT')


def _validate_legacy_records(root, legacy_reference, expected_identity):
    """Re-read all eight records before authorizing the next synthetic phase."""
    _require(type(legacy_reference) is dict and set(legacy_reference) == {'path', 'sha256'}
        and legacy_reference['path'] == 'legacy/report.json', 'FRESH_LEGACY_REFERENCE_INVALID')
    legacy = _json(root, legacy_reference['path'])
    _require(legacy_reference['sha256'] == _sha(_read(root, legacy_reference['path']))
        and legacy['scope'] == 'synthetic_transport_compatibility_only_no_business_qualification'
        and legacy['authorization_used'] is False and type(legacy['real_model_invocations']) is int
        and legacy['real_model_invocations'] == 0, 'FRESH_LEGACY_REFERENCE_INVALID')
    names = list(READ_BOUNDARY_CASES + CASES)
    _require([item['case'] for item in legacy['cases']] == names, 'FRESH_MATRIX_INCOMPLETE')
    rebuilt = legacy_assess(legacy['cases'], legacy['matrix_layout'], legacy['external_target_cleanup'], controlled_context=True)
    _require(canonical_json(rebuilt) == canonical_json(legacy['assessment']), 'FRESH_LEGACY_ASSESSMENT_DRIFT')
    for item in legacy['cases']:
        _require(canonical_json(item['config_snapshot']) == canonical_json(expected_identity['config_snapshot'])
            and item['config_digest'] == expected_identity['config_digest']
            and item['executable_sha256'] == expected_identity['business_static_identity']['profile']['executable']['sha256'],
            'FRESH_CASE_CONFIG_DRIFT')
        _validate_controller_raw(root, 'legacy/' + item['case'] + '/controller.json', expected_identity, item)
        before = _read(root, 'legacy/' + item['case'] + '/target.before.bin', 65536)
        after = _read(root, 'legacy/' + item['case'] + '/target.after.bin', 65536)
        _require(_sha(before) == item['target_before_sha256'] and _sha(after) == item['target_sha256'], 'FRESH_TARGET_SNAPSHOT_DRIFT')
        before_id = _json(root, 'legacy/' + item['case'] + '/target.before.identity.json')
        after_id = _json(root, 'legacy/' + item['case'] + '/target.after.identity.json')
        keys = {'path', 'dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size_bytes', 'sha256'}
        _require(set(before_id) == set(after_id) == keys and before_id['path'] == after_id['path'] == item['synthetic_target']
            and all(type(before_id[k]) is int and type(after_id[k]) is int and before_id[k] == after_id[k] for k in ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink'))
            and before_id['sha256'] == _sha(before) and after_id['sha256'] == _sha(after)
            and type(before_id['size_bytes']) is int and type(after_id['size_bytes']) is int
            and before_id['size_bytes'] == len(before) and after_id['size_bytes'] == len(after), 'FRESH_TARGET_IDENTITY_DRIFT')
        host_outer = expected_identity['business_static_identity']['outer_identity']
        host_native = expected_identity['business_static_identity']['profile']['executable']
        _require(assess_outer_boundary(item['outer_boundary'], item['argv'], expected_identity={
            'helper_sha256': host_outer['helper_sha256'], 'launcher_sha256': host_outer['launcher_sha256'],
            'native_executable_path': host_native['resolved'], 'native_executable_sha256': host_native['sha256']})['valid'], 'FRESH_CASE_OUTER_DRIFT')
        _validate_legacy_http(root, item)
    from .provider_app_server_probe import controlled_continuation_valid, _controlled_case_ok
    for index, item in enumerate(legacy['cases']):
        _require(_controlled_case_ok(item, legacy['cases'][:index+1], root/'legacy', legacy['matrix_layout']),
            'FRESH_CASE_CONTINUATION_FAILED')
    continuation = _json(root, 'legacy/continuation.json')
    _require(controlled_continuation_valid(continuation, legacy['cases'],
        config_digest=expected_identity['config_digest'],
        observation_bytes=[_read(root, 'legacy/'+item['case']+'/observation.json') for item in legacy['cases']],
        controller_bytes=[_read(root, 'legacy/'+item['case']+'/controller.json') for item in legacy['cases']]),
        'FRESH_CONTINUATION_INVALID')
    _require(continuation['complete'] is True and continuation['failed_case'] is None
        and continuation['failure_reason'] is None and type(continuation['native_attempts']) is int
        and continuation['native_attempts'] == 8
        and all(item['passed'] is True for item in continuation['cases']), 'FRESH_MATRIX_STOPPED')
    return rebuilt


def _verify_fresh_report(root, report, *, expected_identity):
    _validate_expected(expected_identity)
    _require(type(report) is dict and set(report) == _REPORT_KEYS
        and report['report_version'] == VERSION and report['scope'] == SCOPE
        and canonical_json(report['identity']) == canonical_json(expected_identity)
        and report['business_activation_eligible'] is False, 'FRESH_REPORT_IDENTITY_INVALID')
    manifest = report['raw_manifest']
    _require(type(manifest) is list and len(manifest) <= 300, 'FRESH_MANIFEST_INVALID')
    paths = set()
    total = 0
    for entry in manifest:
        _require(type(entry) is dict and set(entry) == {'path', 'sha256', 'size_bytes'}
            and entry['path'] not in paths and entry['path'] in _allowed_raw_paths() and type(entry['size_bytes']) is int,
            'FRESH_MANIFEST_ENTRY_INVALID')
        raw = _read(root, entry['path']); total += len(raw)
        _require(len(raw) == entry['size_bytes'] and _sha(raw) == entry['sha256']
            and total <= 32 * 1024 * 1024, 'FRESH_RAW_HASH_MISMATCH')
        paths.add(entry['path'])
    _require(paths == _allowed_raw_paths() and manifest == _manifest(root), 'FRESH_MANIFEST_INCOMPLETE')
    rebuilt = _validate_legacy_records(root, report['legacy_report'], expected_identity)
    expected_write = _json(root, 'write/expected-identity.json')
    # The fixed B0 v1 fifteen-source contract is still interpreted unchanged.
    # The new wrapper binds that exact subset to its complete current profile.
    full_pins = expected_identity['business_static_identity']['profile']['source_digests']
    _require(all(full_pins.get(k) == v for k, v in expected_write['identity']['source_pins'].items())
        and canonical_json(expected_write['identity']['config_snapshot']) == canonical_json(expected_identity['config_snapshot']),
        'FRESH_WRITE_SOURCE_OR_CONFIG_DRIFT')
    _require(type(report['write_report']) is dict and set(report['write_report']) == {'path', 'sha256'}
        and report['write_report']['path'] == 'write/report.json', 'FRESH_WRITE_REFERENCE_INVALID')
    write_path = root / report['write_report']['path']
    _require(report['write_report']['sha256'] == _sha(_read(root, report['write_report']['path'])),
        'FRESH_WRITE_REFERENCE_INVALID')
    validated = load_controlled_write_observability_report(write_path, expected_identity=expected_write, expected_controller=expected_identity['controller'])
    write = assess_controlled_write_observability(validated)
    raw_write = _json(root, report['write_report']['path'])
    _require(raw_write['legacy_matrix_reference']['sha256'] == report['legacy_report']['sha256'],
        'FRESH_WRITE_LEGACY_DRIFT')
    for phase in raw_write['phases']:
        _validate_controller_raw(root, 'write/' + phase['phase'] + '/controller.json', expected_identity, phase['observation'])
    assessment = {'fresh_matrix_observed': bool(rebuilt['read']['read_boundary_observed']
        and rebuilt['write']['positive_control_executed'] and rebuilt['write']['readonly_outside_blocked']
        and rebuilt['write']['same_identity'] and rebuilt['write']['app_server_identity_valid']
        and rebuilt['cleanup_confirmed'] and write['write_effect_prevention_observed']),
        'legacy_qualified': rebuilt['qualified'], 'legacy_write_boundary_observed': rebuilt['write']['write_boundary_observed'],
        'write_effect_prevention_observed': write['write_effect_prevention_observed'],
        'permission_cause_status': write['permission_cause_status'], 'business_qualification': False}
    _require(report['failure'] is None and all(type(v) is int for v in report['resources'].values()) and report['resources'] == {'native_attempts': 11,
        'real_model_invocations': 0, 'auth_operations': 0, 'business_grants': 0}, 'FRESH_RESOURCE_OR_FAILURE_INVALID')
    _require(report['assessment'] is None or canonical_json(report['assessment']) == canonical_json(assessment), 'FRESH_ASSESSMENT_DRIFT')
    return {'assessment': assessment, 'identity_digest': expected_identity['identity_digest'],
        'raw_file_count': len(paths)}


def load_fresh_report(path, *, expected_identity):
    """Historical raw readback against a host-retained identity, not authority."""
    path = Path(path)
    result = _verify_fresh_report(path.parent, _json(path.parent, path.name), expected_identity=expected_identity)
    return {**result, 'report_sha256': _sha(_read(path.parent, path.name))}


async def run_fresh_boundary(config, control, *, expected_identity, seconds=12):
    """One complete current matrix. No retries; all outputs remain on failure."""
    from .provider_app_server_probe import run_controlled_matrix
    from .provider_app_server_write_intervention_probe import run as run_write, SOURCE_FILES, CONTRACT_SHA256
    _require(type(seconds) in (int, float) and 0 < seconds <= 12, 'FRESH_CASE_LIMIT_INVALID')
    _dispatch_guard(config, control, expected_identity)
    root = Path(expected_identity['artifact_dir']) / ('fresh-' + expected_identity['nonce'])
    root.mkdir(mode=0o700)
    _atomic_bytes(root / 'expected-identity.json', (canonical_json(expected_identity) + '\n').encode())
    report = {'report_version': VERSION, 'scope': SCOPE, 'identity': deepcopy(dict(expected_identity)),
        'legacy_report': None, 'write_report': None, 'raw_manifest': [], 'resources': {
            'native_attempts': 0, 'real_model_invocations': 0, 'auth_operations': 0, 'business_grants': 0},
        'assessment': None, 'failure': None, 'business_activation_eligible': False}
    guard = lambda: _dispatch_guard(config, control, expected_identity)
    try:
        legacy = await run_controlled_matrix(config, root / 'legacy', control=control, seconds=seconds, dispatch_guard=guard)
        legacy_raw = _read(root, 'legacy/report.json')
        report['legacy_report'] = {'path': 'legacy/report.json', 'sha256': _sha(legacy_raw)}
        report['resources']['native_attempts'] = len(legacy['cases'])
        _require(len(legacy['cases']) == 8, 'FRESH_MATRIX_STOPPED')
        _validate_legacy_records(root, report['legacy_report'], expected_identity)
        guard()
        profile = expected_identity['business_static_identity']['profile']
        outer = expected_identity['business_static_identity']['outer_identity']
        freeze = {'schema_version': 1, 'batch_id': 'P033',
            'source_pins': {k: profile['source_digests'][k] for k in SOURCE_FILES},
            'native_executable_path': profile['executable']['resolved'],
            'native_executable_sha256': profile['executable']['sha256'],
            'outer_helper_sha256': outer['helper_sha256'], 'launcher_sha256': outer['launcher_sha256'],
            'outer_template_sha256': static_descriptor()['template_sha256'], 'contract_sha256': CONTRACT_SHA256}
        _atomic_bytes(root / 'source-freeze.json', (canonical_json(freeze) + '\n').encode())
        guard()
        write = await run_write(config.executable, root / 'write', source_freeze=root / 'source-freeze.json',
            legacy_matrix_report=root / 'legacy/report.json', control=control, seconds=seconds,
            config=config, dispatch_guard=guard)
        report['write_report'] = {'path': 'write/report.json', 'sha256': _sha(_read(root, 'write/report.json'))}
        report['resources']['native_attempts'] += write['resources']['native_attempts']
        guard()
        report['raw_manifest'] = _manifest(root)
        readback = _verify_fresh_report(root, report, expected_identity=expected_identity)
        report['assessment'] = readback['assessment']
        _require(report['assessment']['fresh_matrix_observed'], 'FRESH_OBSERVATION_NOT_COMPLETE')
    except asyncio.CancelledError:
        report['failure'] = {'type': 'CancelledError', 'code': 'FRESH_CANCELLED'}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report['failure'] = {'type': type(exc).__name__, 'code': str(exc)[:160] if isinstance(exc, ValueError) else 'FRESH_EXECUTION_FAILED'}
    finally:
        report['raw_manifest'] = _manifest(root)
        _atomic_bytes(root / 'report.json', (canonical_json(report) + '\n').encode())
    return {'report_path': str(root / 'report.json'), 'report': report}
