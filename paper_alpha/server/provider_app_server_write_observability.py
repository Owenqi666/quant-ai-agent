"""Closed historical evidence loader for one synthetic policy intervention.

No process, network, authentication, current target/state observation or business
consumer is owned here. Root supplies the independently frozen identity. Raw
records establish a bounded controlled observation, never a permission errno.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .provider_contract import canonical_json, digest, strict_json
from .provider_schema import ProviderConfig
from .provider_outer_sandbox import assess_outer_boundary, static_descriptor
from .provider_app_server_invocation import (
    render_synthetic_app_server_argv, render_controlled_synthetic_app_server_argv, synthetic_plan, TRANSPORT_VERSION, RPC_POLICY, RPC_POLICY_DIGEST,
    CONTROLLED_TRANSPORT_VERSION, CONTROLLED_RPC_POLICY, CONTROLLED_RPC_POLICY_DIGEST,
)
from .provider_app_server_protocol import AppServerProtocol, AppServerProtocolError
from .provider_app_server_probe import assess as assess_legacy_matrix, _summary_valid
from .provider_read_probe import OUTPUT_SCHEMA, _done_response
from .provider_write_probe import (
    BEFORE_CONTENT, AFTER_CONTENT, BEFORE_SHA256, AFTER_SHA256, PROMPT, EXPECTED_TOOLS,
    _patch_response, CONTROL_POLICY_DIGEST,
)
from .provider_read_boundary import READ_BOUNDARY_POLICY, READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION

REPORT_VERSION = 'native_app_server_write_observability_v1'
SCOPE = 'synthetic_policy_intervention_only_no_business_qualification'
CONTRACT_SHA256 = 'ceca32fbf66432f7a4f989b5b57b4b0b8c0ca5d030b09235771fe21dd1ab14a6'
SOURCE_FILES = tuple('paper_alpha/server/' + name for name in (
    'codex_cli_provider.py', 'provider_supervisor.py', 'provider_supervision_journal.py',
    'provider_app_server_protocol.py', 'provider_app_server_invocation.py',
    'provider_app_server_probe.py', 'provider_app_server_write_intervention_probe.py',
    'provider_app_server_write_observability.py', 'provider_outer_sandbox.py',
    'provider_read_probe.py', 'provider_write_probe.py', 'provider_contract.py',
    'provider_schema.py', 'provider_cli_profile.py', 'provider_read_boundary.py'))
PHASES = ('write_before', 'readonly', 'write_after')
MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_HTTP_BODY = 2 * 1024 * 1024
MAX_HEADER_BYTES = 16384
_HEX = re.compile(r'^[a-f0-9]{64}$')
_IDENTITY = {'transport', 'source_pins', 'config_snapshot', 'config_digest',
    'native_executable_path', 'native_executable_sha256', 'work', 'cli_state',
    'response_path', 'target', 'loopback_port', 'challenge_nonce', 'inner_argv',
    'inner_argv_digest', 'rpc_plan', 'rpc_plan_digest', 'environment',
    'target_identity', 'state_identity'}
_TOP = {'report_version', 'scope', 'identity', 'phases', 'fixture_operations',
        'legacy_matrix_reference', 'raw_manifest', 'assessment', 'resources'}
_SNAPSHOT = {'path', 'dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size_bytes', 'sha256'}
_FILE_ID = _SNAPSHOT - {'size_bytes', 'sha256'}
_STATE_ID = {'path', 'dev', 'ino', 'uid', 'gid', 'mode'}
_ENV = {'HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'CODEX_HOME', 'SYSTEMROOT',
        'SSL_CERT_FILE', 'SSL_CERT_DIR'}
_LEGACY = {'report_version', 'path', 'sha256', 'read_boundary_observed',
           'write_boundary_observed', 'cleanup_confirmed', 'qualified'}
_OBSERVATION = {'active_policy_digest', 'allow_apply_patch', 'allowed_root', 'argv',
    'argv_digest', 'authorization_used', 'blockers', 'case', 'challenge_nonce',
    'config_digest', 'config_snapshot', 'control_policy_digest', 'enforcement',
    'events', 'executable_sha256', 'exit_code', 'failure', 'image_payloads_in_requests',
    'invocation_started', 'matrix_layout', 'matrix_layout_digest',
    'measurement_policy_reference', 'outer_boundary', 'policy', 'policy_digest',
    'policy_version', 'process_stopped', 'protocol_completed', 'readonly_policy_digest',
    'real_model_invocations', 'report_version', 'request_count', 'request_shapes',
    'rpc_plan', 'rpc_plan_digest', 'rpc_policy_digest', 'rpc_summary', 'schema_digest',
    'stderr_sha256', 'stdout_sha256', 'synthetic_target', 'synthetic_tool_outputs',
    'target_after_sha256', 'target_before_sha256', 'target_exists', 'target_exists_before',
    'target_read_error', 'target_sha256', 'target_size_bytes', 'tool_call_sent',
    'tool_names', 'transport', 'transport_error', 'phase', 'environment_snapshot'}
_PHASE_FILES = ('observation.json', 'stdout.jsonl', 'stderr.log', 'target.before.bin',
    'target.after.bin', 'request.1.http', 'request.2.http', 'response.1.sse', 'response.2.sse',
    'supervision/spec.json', 'supervision/stdin.bin', 'supervision/ready.json',
    'supervision/launched.json', 'supervision/stop.json', 'supervision/stdout.bin',
    'supervision/stderr.bin', 'supervision/rpc-result.json', 'supervision/response.raw.json',
    'supervision/supervisor.stderr.log')
_STOP = {'protocol_version', 'nonce', 'supervisor_pid', 'cli_pid', 'invocation_started',
    'started_monotonic', 'finished_monotonic', 'deadline_monotonic', 'exit_code',
    'reason', 'leader_reaped', 'group_absent', 'process_stopped', 'stdout_bytes',
    'stderr_bytes', 'stdout_sha256', 'stderr_sha256', 'response_saved', 'response_error',
    'response_sha256', 'transport', 'rpc_completed', 'rpc_result_sha256'}


class WriteObservabilityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code='WRITE_OBSERVABILITY_INVALID_EVIDENCE'):
    if not condition:
        raise WriteObservabilityError(code)


def _closed(value, keys):
    _require(isinstance(value, dict) and set(value) == keys)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9223372036854775807


def _json(data, limit=MAX_JSON_BYTES):
    value = strict_json(data, max_bytes=limit, max_depth=64)
    canonical_json(value).encode('utf-8')  # Reject lone surrogate strings too.
    return value


def _path(value):
    _require(isinstance(value, str) and value and len(value) <= 4096
             and not any(ord(c) < 32 for c in value))
    path = PurePosixPath(value)
    _require(path.is_absolute() and str(path) == value and '..' not in path.parts)
    return path


def _private_bytes(root, relative, limit):
    """Open each fixed path component without following symlinks; no discovery."""
    parts = PurePosixPath(relative).parts
    _require(parts and not PurePosixPath(relative).is_absolute()
             and str(PurePosixPath(relative)) == relative
             and '..' not in parts and '.' not in parts)
    root = Path(root).absolute()
    _require('..' not in root.parts)
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and before.st_size <= limit)
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            linked = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size',
                      'st_mtime_ns', 'st_ctime_ns')
            _require(len(data) <= limit and len(data) == before.st_size
                     and all(getattr(before, k) == getattr(after, k) == getattr(linked, k) for k in fields))
            return data
    finally:
        os.close(directory)


def _snapshot(value, identity, content):
    _closed(value, _SNAPSHOT)
    _require({k: value[k] for k in _FILE_ID} == identity
             and all(_integer(value[k]) for k in _SNAPSHOT - {'path', 'sha256'})
             and stat.S_ISREG(value['mode']) and value['nlink'] == 1
             and value['size_bytes'] == len(content) and value['sha256'] == _sha(content))


def _identity(value, expected, *, controlled_context=False):
    _closed(value, _IDENTITY)
    _require(value == expected, 'WRITE_OBSERVABILITY_IDENTITY_DRIFT')
    from .provider_app_server_invocation import CONTROLLED_TRANSPORT_VERSION, render_controlled_synthetic_app_server_argv
    _require(value['transport'] == (CONTROLLED_TRANSPORT_VERSION if controlled_context else TRANSPORT_VERSION))
    for key in ('native_executable_sha256', 'config_digest', 'rpc_plan_digest', 'inner_argv_digest'):
        _require(isinstance(value[key], str) and _HEX.fullmatch(value[key]))
    pins = value['source_pins']
    _require(isinstance(pins, dict) and set(pins) == set(SOURCE_FILES)
             and all(isinstance(k, str) and 0 < len(k) <= 4096 and isinstance(v, str)
                     and _HEX.fullmatch(v) for k, v in pins.items()))
    for key in ('native_executable_path', 'work', 'cli_state', 'response_path', 'target'):
        _path(value[key])
    work = PurePosixPath(value['work'])
    _require(value['target'] == str(work / 'target.txt')
             and value['response_path'] == str(work / 'response.raw.json')
             and value['cli_state'] == str(work.parent / 'cli-state'))
    config = ProviderConfig.model_validate(value['config_snapshot'])
    _require(config.model_dump(mode='json') == value['config_snapshot']
             and digest(value['config_snapshot']) == value['config_digest']
             and config.executable == value['native_executable_path'])
    inner = (render_controlled_synthetic_app_server_argv if controlled_context else render_synthetic_app_server_argv)(config, port=value['loopback_port'],
                                            nonce=value['challenge_nonce'], write=True)
    rpc = synthetic_plan(config, Path(value['work']), PROMPT, OUTPUT_SCHEMA, write=True)
    _require(value['inner_argv'] == inner and digest(inner) == value['inner_argv_digest']
             and value['rpc_plan'] == rpc and digest(rpc) == value['rpc_plan_digest'])
    environment = value['environment']
    _require(isinstance(environment, dict) and not set(environment) - _ENV
             and all(isinstance(v, str) and '\0' not in v and len(v) <= 8192 for v in environment.values())
             and environment.get('HOME') == environment.get('CODEX_HOME') == value['cli_state'])
    target, state = value['target_identity'], value['state_identity']
    _closed(target, _FILE_ID)
    _closed(state, _STATE_ID)
    _require(target['path'] == value['target'] and state['path'] == value['cli_state']
             and all(_integer(target[k]) for k in _FILE_ID - {'path'})
             and all(_integer(state[k]) for k in _STATE_ID - {'path'})
             and target['ino'] > 0 and state['ino'] > 0
             and target['uid'] == state['uid'] == os.getuid()
             and stat.S_ISREG(target['mode']) and target['nlink'] == 1
             and stat.S_ISDIR(state['mode']) and stat.S_IMODE(state['mode']) == 0o700)


def _http(data, identity):
    _require(isinstance(data, bytes) and len(data) <= MAX_HTTP_BODY + MAX_HEADER_BYTES)
    header, separator, body = data.partition(b'\r\n\r\n')
    _require(separator and len(header) + 4 <= MAX_HEADER_BYTES and len(body) <= MAX_HTTP_BODY)
    lines = header.decode('ascii').split('\r\n')
    route = '/write-challenge-' + identity['challenge_nonce'] + '/v1/responses'
    _require(lines[0] == 'POST ' + route + ' HTTP/1.1')
    headers = {}
    for line in lines[1:]:
        key, separator, value = line.partition(':')
        key = key.strip().lower()
        _require(separator and re.fullmatch('[a-z0-9-]+', key) and key not in headers)
        headers[key] = value.strip()
    _require(not {'authorization', 'proxy-authorization', 'transfer-encoding'} & set(headers)
             and headers.get('content-encoding', 'identity') == 'identity'
             and re.fullmatch('[0-9]+', headers.get('content-length', ''))
             and int(headers['content-length']) == len(body))
    value = _json(body, MAX_HTTP_BODY)
    _require(isinstance(value, dict) and value.get('model') == 'gpt-5.5'
             and isinstance(value.get('reasoning'), dict) and value['reasoning'].get('effort') == 'low'
             and value.get('stream') is True and value.get('store') is False)
    declarations = value.get('tools')
    _require(isinstance(declarations, list) and len(declarations) == len(EXPECTED_TOOLS))
    names = []
    for tool in declarations:
        _require(isinstance(tool, dict) and isinstance(tool.get('name'), str)
                 and 'namespace' not in tool and tool.get('type') == (
                     'custom' if tool.get('name') == 'apply_patch' else 'function'))
        names.append(tool['name'])
    _require(len(set(names)) == len(names) and set(names) == EXPECTED_TOOLS)
    items = value.get('input')
    _require(isinstance(items, list))
    outputs = [item for item in items if isinstance(item, dict)
               and item.get('type') == 'custom_tool_call_output']
    for output in outputs:
        _closed(output, {'type', 'call_id', 'output'})
    pending = [(node, True) for node in items]
    while pending:
        node, top_level = pending.pop()
        if isinstance(node, dict):
            kind = node.get('type')
            _require(kind not in ('additional_tools', 'function_call_output', 'input_image'))
            _require(kind != 'custom_tool_call_output' or top_level)
            pending.extend((child, False) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, False) for child in node)
    return value, outputs


def _phase_evidence(phase, raw, identity, expected_outer, *, expected_controller=None):
    label, observation = phase['phase'], phase['observation']
    _closed(observation, _OBSERVATION)
    _require(observation == _json(raw['observation.json']) and observation['phase'] == label)
    _require(observation['report_version'] == REPORT_VERSION
             and observation['environment_snapshot'] == identity['environment']
             and observation['enforcement'] == {'outer': 'mandatory_macos_whole_process',
                 'inner': RPC_POLICY['sandbox'], 'named_inner_profile_executed': False}
             and observation['measurement_policy_reference'] == 'legacy_original_matrix_predicates_only'
             and observation['policy'] == READ_BOUNDARY_POLICY
             and observation['policy_version'] == READ_BOUNDARY_POLICY_VERSION
             and observation['policy_digest'] == READ_BOUNDARY_POLICY_DIGEST)
    boundary, argv = observation['outer_boundary'], observation['argv']
    _require(assess_outer_boundary(boundary, argv, expected_identity=expected_outer)['valid'],
             'WRITE_OBSERVABILITY_OUTER_DRIFT')
    allowed = label != 'readonly'
    _require(boundary['inner_argv'] == identity['inner_argv']
             and boundary['work'] == identity['work'] and boundary['cli_state'] == identity['cli_state']
             and boundary['response_path'] == identity['response_path']
             and boundary['network_mode'] == 'synthetic_loopback'
             and boundary['loopback_port'] == identity['loopback_port']
             and boundary['auth_exact_path'] is None
             and boundary['synthetic_write_target'] == (identity['target'] if allowed else None))
    for key, expected in (('transport', identity['transport']), ('config_snapshot', identity['config_snapshot']),
        ('config_digest', identity['config_digest']), ('executable_sha256', identity['native_executable_sha256']),
        ('allowed_root', identity['work']), ('challenge_nonce', identity['challenge_nonce']),
        ('rpc_plan', identity['rpc_plan']), ('rpc_plan_digest', identity['rpc_plan_digest']),
        ('rpc_policy_digest', RPC_POLICY_DIGEST if expected_controller is None else CONTROLLED_RPC_POLICY_DIGEST), ('synthetic_target', identity['target']),
        ('case', 'allow_write_control' if allowed else 'readonly_inside'),
        ('argv_digest', digest(argv)), ('schema_digest', digest(OUTPUT_SCHEMA)),
        ('active_policy_digest', CONTROL_POLICY_DIGEST if allowed else READ_BOUNDARY_POLICY_DIGEST),
        ('control_policy_digest', CONTROL_POLICY_DIGEST), ('readonly_policy_digest', READ_BOUNDARY_POLICY_DIGEST)):
        _require(observation[key] == expected)
    _require(observation['allow_apply_patch'] is True and observation['authorization_used'] is False
             and observation['real_model_invocations'] == 0 and type(observation['real_model_invocations']) is int
             and observation['blockers'] == [] and observation['failure'] is None
             and observation['transport_error'] is None and observation['tool_call_sent'] is True
             and observation['request_count'] == 2 and type(observation['request_count']) is int
             and observation['tool_names'] == sorted(EXPECTED_TOOLS)
             and type(observation['image_payloads_in_requests']) is int
             and observation['image_payloads_in_requests'] == 0)
    spec = _json(raw['supervision/spec.json'])
    _closed(spec, {'protocol_version', 'nonce', 'argv', 'cwd', 'deadline_monotonic', 'output_limit',
                   'response_path', 'response_limit', 'pass_fds', 'transport'})
    _require(spec['protocol_version'] == 2 and type(spec['protocol_version']) is int
             and spec['transport'] == 'app_server' and spec['argv'] == argv
             and spec['cwd'] == identity['work'] and spec['response_path'] == identity['response_path']
             and canonical_json(spec['pass_fds']) == canonical_json([] if expected_controller is None else expected_controller['pass_fds']) and spec['output_limit'] == 1048576
             and spec['response_limit'] == 65536
             and isinstance(spec['nonce'], str) and re.fullmatch('[a-f0-9]{32}', spec['nonce']))
    if expected_controller is not None:
        _require(type(spec['deadline_monotonic']) in (int, float) and math.isfinite(spec['deadline_monotonic'])
            and spec['deadline_monotonic'] <= expected_controller['deadline_monotonic'], 'CONTROLLED_WRITE_DEADLINE_DRIFT')
    _require(raw['supervision/stdin.bin'] == canonical_json(identity['rpc_plan']).encode('utf-8'))
    ready, launched, stop = (_json(raw['supervision/' + name + '.json']) for name in ('ready', 'launched', 'stop'))
    _closed(ready, {'protocol_version', 'nonce', 'supervisor_pid', 'ready'})
    _closed(launched, {'protocol_version', 'nonce', 'cli_pid', 'supervisor_pid'})
    _closed(stop, _STOP)
    _require(ready['ready'] is True and _integer(ready['supervisor_pid'], 1)
             and _integer(launched['cli_pid'], 1)
             and all(record['protocol_version'] == 2 and type(record['protocol_version']) is int
                     and record['nonce'] == spec['nonce'] and _integer(record['supervisor_pid'], 1)
                     and record['supervisor_pid'] == ready['supervisor_pid']
                     for record in (ready, launched, stop))
             and _integer(stop['cli_pid'], 1)
             and launched['cli_pid'] == stop['cli_pid']
             and stop['transport'] == 'app_server'
             and all(stop[key] is True for key in ('invocation_started', 'leader_reaped', 'group_absent',
                                                   'process_stopped', 'response_saved', 'rpc_completed'))
             and type(stop['exit_code']) is int and stop['exit_code'] == 0
             and stop['reason'] is None and stop['response_error'] is None)
    times = [stop[k] for k in ('started_monotonic', 'finished_monotonic', 'deadline_monotonic')]
    _require(all(type(v) in (int, float) and math.isfinite(v) for v in times)
             and times[0] <= times[1] <= times[2] and 0 < times[2] - times[0] <= 12
             and stop['deadline_monotonic'] == spec['deadline_monotonic'])
    _require(raw['stdout.jsonl'] == raw['supervision/stdout.bin']
             and raw['stderr.log'] == raw['supervision/stderr.bin']
             and len(raw['stdout.jsonl']) + len(raw['stderr.log']) <= spec['output_limit']
             and raw['supervision/supervisor.stderr.log'] == b'')
    for name in ('stdout', 'stderr'):
        content = raw['supervision/' + name + '.bin']
        _require(type(stop[name + '_bytes']) is int and len(content) == stop[name + '_bytes']
                 and _sha(content) == stop[name + '_sha256'] == observation[name + '_sha256'])
    _require(_sha(raw['supervision/rpc-result.json']) == stop['rpc_result_sha256'])
    protocol = AppServerProtocol(identity['rpc_plan'])
    protocol.start()
    frames = raw['stdout.jsonl'].splitlines(keepends=True)
    for line in frames:
        protocol.feed(line)
    protocol.finish_eof()
    summary = _json(raw['supervision/rpc-result.json'])
    _require(isinstance(summary, dict) and 'supervision' in summary)
    core = {k: v for k, v in summary.items() if k != 'supervision'}
    _require(core == protocol.summary() and summary == observation['rpc_summary'])
    diag = summary['supervision']
    _closed(diag, {'phase', 'term_sent', 'kill_sent', 'error_errno', 'error_count', 'remaining_streams'})
    _require(_summary_valid(summary))
    response = raw['supervision/response.raw.json']
    response_value = _json(response, 65536)
    _require(response == protocol.response and response_value == {'done': True}
             and response_value['done'] is True
             and _sha(response) == stop['response_sha256'])
    _require(observation['protocol_completed'] is True and observation['process_stopped'] is True
             and observation['invocation_started'] is True and type(observation['exit_code']) is int
             and observation['exit_code'] == 0)
    request1, outputs1 = _http(raw['request.1.http'], identity)
    request2, outputs2 = _http(raw['request.2.http'], identity)
    _require(outputs1 == [] and len(outputs2) == 1 and outputs2[0].get('call_id') == 'call_synthetic'
             and isinstance(outputs2[0].get('output'), str) and outputs2[0]['output'])
    _require(observation['synthetic_tool_outputs'] == [outputs2[0]['output']])
    _require(raw['response.1.sse'] == _patch_response(Path(identity['target']))
             and raw['response.2.sse'] == _done_response())
    patch_diff = '@@ -1 +1 @@\n-PAPER_ALPHA_SYNTHETIC_BEFORE\n+PAPER_ALPHA_SYNTHETIC_AFTER\n'
    file_events = []
    for index, line in enumerate(frames):
        frame = _json(line)
        params = frame.get('params', {})
        item = params.get('item') if isinstance(params, dict) else None
        if isinstance(item, dict) and item.get('type') == 'fileChange':
            _require(frame.get('method') in ('item/started', 'item/completed')
                     and params.get('threadId') == core['thread_id'] and params.get('turnId') == core['turn_id']
                     and item.get('id') == 'call_synthetic'
                     and item.get('changes') == [{'path': identity['target'],
                         'kind': {'type': 'update', 'move_path': None}, 'diff': patch_diff}])
            file_events.append((index, frame['method'], item['status']))
    _require(len(file_events) == 2 and file_events[0][1:] == ('item/started', 'inProgress')
             and file_events[1][1] == 'item/completed' and file_events[0][0] < file_events[1][0])
    before, after = raw['target.before.bin'], raw['target.after.bin']
    _snapshot(phase['target_before'], identity['target_identity'], before)
    _snapshot(phase['target_after'], identity['target_identity'], after)
    _require(observation['target_before_sha256'] == _sha(before)
             and observation['target_after_sha256'] == observation['target_sha256'] == _sha(after)
             and observation['target_exists_before'] is True and observation['target_exists'] is True
             and observation['target_read_error'] is None
             and type(observation['target_size_bytes']) is int and observation['target_size_bytes'] == len(before))
    return {'phase': label, 'file_status': file_events[1][2], 'before_sha256': _sha(before),
            'after_sha256': _sha(after), 'policy_text': boundary['policy_text'], 'nonce': spec['nonce'],
            'started': times[0], 'finished': times[1], 'response_sha256': _sha(response),
            'response_size_bytes': len(response)}


class _ValidatedReport(dict):
    def __init__(self, report, evidence):
        super().__init__(copy.deepcopy(report))
        self._evidence = copy.deepcopy(evidence)
        self._seal = digest({'report': self, 'evidence': self._evidence})


def _legacy_gate(matrix, reference, identity, *, controlled_context=False):
    _closed(matrix, {'assessment', 'authorization_used', 'cases', 'external_target_cleanup',
                     'matrix_layout', 'real_model_invocations', 'report_version', 'rpc_policy',
                     'scope', 'transport'})
    _require(matrix['report_version'] == reference['report_version']
             and matrix['transport'] == (CONTROLLED_TRANSPORT_VERSION if controlled_context else TRANSPORT_VERSION)
             and matrix['rpc_policy'] == (CONTROLLED_RPC_POLICY if controlled_context else RPC_POLICY)
             and matrix['scope'] == 'synthetic_transport_compatibility_only_no_business_qualification'
             and matrix['authorization_used'] is False
             and type(matrix['real_model_invocations']) is int and matrix['real_model_invocations'] == 0)
    cases = matrix['cases']
    names = {'inside', 'outside', 'unrelated_tmp', 'declared_tmpdir', 'desktop',
             'allow_write_control', 'readonly_inside', 'readonly_outside'}
    _require(isinstance(cases, list) and len(cases) == 8
             and all(isinstance(case, dict) for case in cases)
             and {case.get('case') for case in cases} == names)
    assessment = assess_legacy_matrix(cases, matrix['matrix_layout'], matrix['external_target_cleanup'], **({'controlled_context': True} if controlled_context else {}))
    _require(matrix['assessment'] == assessment
             and assessment['read']['read_boundary_observed'] is reference['read_boundary_observed']
             and assessment['write']['write_boundary_observed'] is reference['write_boundary_observed']
             and assessment['cleanup_confirmed'] is reference['cleanup_confirmed']
             and assessment['qualified'] is reference['qualified']
             and assessment['read']['app_server_identity_valid'] is True
             and assessment['write']['app_server_identity_valid'] is True
             and assessment['write']['positive_control_executed'] is True
             and assessment['write']['readonly_outside_blocked'] is True
             and assessment['write']['readonly_inside_blocked'] is False)
    for case in cases:
        _require(case.get('config_snapshot') == identity['config_snapshot']
                 and case.get('config_digest') == identity['config_digest']
                 and case.get('executable_sha256') == identity['native_executable_sha256']
                 and case.get('authorization_used') is False and case.get('real_model_invocations') == 0
                 and case.get('process_stopped') is True and case.get('protocol_completed') is True
                 and case.get('invocation_started') is True and type(case.get('exit_code')) is int
                 and case['exit_code'] == 0 and case.get('transport_error') is None
                 and case.get('failure') is None and case.get('blockers') == [])
    readonly = next(case for case in cases if case['case'] == 'readonly_inside')
    _require(readonly.get('target_before_sha256') == readonly.get('target_after_sha256') == BEFORE_SHA256
             and readonly.get('target_read_error') is None and readonly.get('tool_call_sent') is True
             and readonly.get('synthetic_tool_outputs'))


def _load_write_observability_report(report_path, *, expected_identity, expected_controller=None):
    """Validate fixed raw files against externally frozen pins, without live calls."""
    try:
        _closed(expected_identity, {'identity', 'outer_identity', 'legacy_matrix_reference'})
        path = Path(report_path)
        _require(path.name == 'report.json')
        report = _json(_private_bytes(path.parent, path.name, MAX_JSON_BYTES))
        _closed(report, _TOP)
        _require(report['report_version'] == REPORT_VERSION and report['scope'] == SCOPE)
        _identity(report['identity'], expected_identity['identity'], controlled_context=expected_controller is not None)
        identity = report['identity']
        _closed(expected_identity['outer_identity'], {'helper_sha256', 'launcher_sha256',
            'native_executable_path', 'native_executable_sha256'})
        _require(expected_identity['outer_identity']['native_executable_path'] == identity['native_executable_path']
                 and expected_identity['outer_identity']['native_executable_sha256'] == identity['native_executable_sha256'])
        legacy = report['legacy_matrix_reference']
        _closed(legacy, _LEGACY)
        _require(legacy == expected_identity['legacy_matrix_reference']
                 and legacy['report_version'] == 'native_app_server_boundary_v1'
                 and isinstance(legacy['sha256'], str) and _HEX.fullmatch(legacy['sha256'])
                 and legacy['read_boundary_observed'] is True and legacy['cleanup_confirmed'] is True
                 and legacy['write_boundary_observed'] is False and legacy['qualified'] is False)
        resources = report['resources']
        _closed(resources, {'native_attempts', 'real_model_invocations', 'auth_operations', 'business_grants'})
        _require(all(type(v) is int for v in resources.values()) and 0 <= resources['native_attempts'] <= 3
                 and all(resources[k] == 0 for k in resources if k != 'native_attempts'))
        phases = report['phases']
        _require(isinstance(phases, list) and 0 <= len(phases) <= 3
                 and resources['native_attempts'] == len(phases))
        for index, phase in enumerate(phases):
            _closed(phase, {'phase', 'observation', 'target_before', 'target_after'})
            _require(phase['phase'] == PHASES[index])
        manifest = report['raw_manifest']
        _require(isinstance(manifest, list) and len(manifest) <= 60)
        allowed = {'fixture-operations.json', 'source-freeze.json', 'legacy-matrix-report.json'} | {
            phase['phase'] + '/' + name for phase in phases for name in _PHASE_FILES}
        raw, total = {}, 0
        for entry in manifest:
            _closed(entry, {'path', 'sha256', 'size_bytes'})
            name = entry['path']
            _require(isinstance(name, str) and name in allowed and name not in raw
                     and _integer(entry['size_bytes']) and entry['size_bytes'] <= MAX_HTTP_BODY + MAX_HEADER_BYTES
                     and isinstance(entry['sha256'], str) and _HEX.fullmatch(entry['sha256']))
            data = _private_bytes(path.parent, name, MAX_HTTP_BODY + MAX_HEADER_BYTES)
            total += len(data)
            _require(total <= MAX_RAW_BYTES and len(data) == entry['size_bytes'] and _sha(data) == entry['sha256'])
            raw[name] = data
        _require(set(raw) == allowed)
        freeze = _json(raw['source-freeze.json'])
        _closed(freeze, {'schema_version', 'batch_id', 'source_pins', 'native_executable_path',
            'native_executable_sha256', 'outer_helper_sha256', 'launcher_sha256',
            'outer_template_sha256', 'contract_sha256'})
        outer = expected_identity['outer_identity']
        _require(type(freeze['schema_version']) is int and freeze['schema_version'] == 1
                 and isinstance(freeze['batch_id'], str)
                 and re.fullmatch('[A-Za-z0-9_-]{1,128}', freeze['batch_id'])
                 and freeze['source_pins'] == identity['source_pins']
                 and freeze['native_executable_path'] == identity['native_executable_path']
                 and freeze['native_executable_sha256'] == identity['native_executable_sha256']
                 and freeze['outer_helper_sha256'] == outer['helper_sha256']
                 and freeze['outer_helper_sha256'] == identity['source_pins']['paper_alpha/server/provider_outer_sandbox.py']
                 and freeze['launcher_sha256'] == outer['launcher_sha256']
                 and freeze['outer_template_sha256'] == static_descriptor()['template_sha256']
                 and freeze['contract_sha256'] == CONTRACT_SHA256)
        _require(_sha(raw['legacy-matrix-report.json']) == legacy['sha256'])
        matrix = _json(raw['legacy-matrix-report.json'])
        _legacy_gate(matrix, legacy, identity, controlled_context=expected_controller is not None)
        _require(_json(raw['fixture-operations.json']) == report['fixture_operations'])
        evidence = []
        for phase in phases:
            subset = {name: raw[phase['phase'] + '/' + name] for name in _PHASE_FILES}
            evidence.append(_phase_evidence(phase, subset, identity, expected_identity['outer_identity'], expected_controller=expected_controller))
        if len(phases) == 3:
            _require(len({p['nonce'] for p in evidence}) == 3
                     and evidence[0]['finished'] <= evidence[1]['started']
                     and evidence[1]['finished'] <= evidence[2]['started'])
            _fixture_chain(report, evidence)
        else:
            _require(report['fixture_operations'] == [])
        validated = _ValidatedReport(report, evidence)
        result = assess_write_observability(validated)
        _require(report['assessment'] is None or report['assessment'] == result)
        return validated
    except WriteObservabilityError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, AppServerProtocolError) as exc:
        raise WriteObservabilityError('WRITE_OBSERVABILITY_INVALID_EVIDENCE') from exc


def _fixture_chain(report, evidence):
    phases = report['phases']
    operations = report['fixture_operations']
    _require(isinstance(operations, list) and len(operations) == 3)
    reset, remove1, remove2 = operations
    for operation in operations:
        _closed(operation, {'operation', 'after_phase', 'before_phase', 'target', 'before', 'after'})
    _require(reset == {'operation': 'reset_after_write_before', 'after_phase': 'write_before',
        'before_phase': 'readonly', 'target': report['identity']['target'],
        'before': phases[0]['target_after'], 'after': phases[1]['target_before']}
        and reset['before']['sha256'] == AFTER_SHA256 and reset['after']['sha256'] == BEFORE_SHA256
        and phases[1]['target_after'] == phases[2]['target_before'])
    for removal, previous, following, item in (
        (remove1, 'write_before', 'readonly', evidence[0]),
        (remove2, 'readonly', 'write_after', evidence[1])):
        _require(removal == {'operation': 'remove_response_before_' + following,
            'after_phase': previous, 'before_phase': following,
            'target': report['identity']['response_path'],
            'before': {'exists': True, 'sha256': item['response_sha256'], 'size_bytes': item['response_size_bytes']},
            'after': {'exists': False, 'sha256': None, 'size_bytes': 0}})


def assess_write_observability(validated_report):
    """A sealed loader result supplies measured facts; arbitrary dicts do not."""
    result = {'report_version': REPORT_VERSION, 'scope': SCOPE, 'claim_strength': 'controlled_observation',
        'evidence_valid': False, 'same_identity': False, 'exact_policy_intervention': False,
        'positive_before_executed': False, 'readonly_executed_unchanged': False,
        'positive_after_executed': False, 'write_effect_prevention_observed': False,
        'permission_cause_status': 'not_exposed', 'permission_errno': None,
        'legacy_explicit_refusal_passed': False, 'business_qualification': False, 'blockers': []}
    if not isinstance(validated_report, _ValidatedReport):
        result['blockers'] = ['WRITE_OBSERVABILITY_RAW_LOAD_REQUIRED']
        return result
    try:
        _require(validated_report._seal == digest({'report': validated_report,
                                                  'evidence': validated_report._evidence}))
        evidence = validated_report._evidence
        _require(len(evidence) == 3)
        result['evidence_valid'] = result['same_identity'] = True
        target = validated_report['identity']['target']
        rule = '(allow file-write* (literal ' + canonical_json(target) + '))\n'
        first, middle, last = evidence
        result['exact_policy_intervention'] = (
            first['policy_text'] == last['policy_text'] and first['policy_text'].count(rule) == 1
            and first['policy_text'].replace(rule, '', 1) == middle['policy_text'])
        result['positive_before_executed'] = (first['file_status'] == 'completed'
            and first['before_sha256'] == BEFORE_SHA256 and first['after_sha256'] == AFTER_SHA256)
        result['readonly_executed_unchanged'] = (middle['file_status'] == 'failed'
            and middle['before_sha256'] == middle['after_sha256'] == BEFORE_SHA256)
        result['positive_after_executed'] = (last['file_status'] == 'completed'
            and last['before_sha256'] == BEFORE_SHA256 and last['after_sha256'] == AFTER_SHA256)
        properties = ('evidence_valid', 'same_identity', 'exact_policy_intervention',
                      'positive_before_executed', 'readonly_executed_unchanged', 'positive_after_executed')
        result['write_effect_prevention_observed'] = all(result[k] for k in properties)
        result['blockers'] = [k.upper() for k in properties if not result[k]]
    except (WriteObservabilityError, ValueError, TypeError, KeyError, AttributeError):
        result['blockers'] = ['WRITE_OBSERVABILITY_INCOMPLETE_OR_MODIFIED_EVIDENCE']
    return result


# A distinct loader contract is required for the original non-empty leases.
# The v1 entry point still rejects non-empty pass_fds, and its source archive
# and historical raw report format remain unchanged. No report data is rewritten.
CONTROLLED_WRITE_LOADER_VERSION = 'native_app_server_controlled_write_loader_v2'


def load_write_observability_report(report_path, *, expected_identity):
    return _load_write_observability_report(report_path, expected_identity=expected_identity)


class _ControlledValidatedReport:
    __slots__ = ('_validated', '_controller', '_seal')

    def __init__(self, validated, controller):
        from copy import deepcopy
        self._validated = validated
        self._controller = deepcopy(controller)
        self._seal = digest({'loader_version': CONTROLLED_WRITE_LOADER_VERSION,
            'controller': self._controller, 'report': validated, 'evidence': validated._evidence})


def _controlled_identity(controller):
    _closed(controller, {'deadline_monotonic', 'pass_fds', 'fd_identity'})
    _require(type(controller['deadline_monotonic']) in (int, float)
        and math.isfinite(controller['deadline_monotonic'])
        and type(controller['pass_fds']) is list and len(controller['pass_fds']) <= 64
        and all(type(fd) is int and fd >= 3 for fd in controller['pass_fds'])
        and len(set(controller['pass_fds'])) == len(controller['pass_fds'])
        and type(controller['fd_identity']) is list
        and len(controller['fd_identity']) == len(controller['pass_fds']), 'CONTROLLED_WRITE_CONTROLLER_INVALID')
    for fd, item in zip(controller['pass_fds'], controller['fd_identity']):
        _closed(item, {'fd', 'dev', 'ino', 'uid', 'gid', 'mode', 'nlink'})
        _require(all(type(v) is int and v >= 0 for v in item.values())
            and item['fd'] == fd and item['ino'] > 0, 'CONTROLLED_WRITE_FD_IDENTITY_INVALID')


def load_controlled_write_observability_report(report_path, *, expected_identity, expected_controller):
    """Versioned host-controller loader; no live FD stat or policy weakening."""
    _controlled_identity(expected_controller)
    value = _load_write_observability_report(report_path, expected_identity=expected_identity,
        expected_controller=expected_controller)
    return _ControlledValidatedReport(value, expected_controller)


def assess_controlled_write_observability(value):
    _require(type(value) is _ControlledValidatedReport, 'CONTROLLED_WRITE_RAW_LOAD_REQUIRED')
    _controlled_identity(value._controller)
    _require(value._seal == digest({'loader_version': CONTROLLED_WRITE_LOADER_VERSION,
        'controller': value._controller, 'report': value._validated, 'evidence': value._validated._evidence}),
        'CONTROLLED_WRITE_SEAL_DRIFT')
    result = assess_write_observability(value._validated)
    _require(result['evidence_valid'], 'CONTROLLED_WRITE_EVIDENCE_INVALID')
    return {**result, 'loader_version': CONTROLLED_WRITE_LOADER_VERSION,
        'controller_digest': digest(value._controller)}
