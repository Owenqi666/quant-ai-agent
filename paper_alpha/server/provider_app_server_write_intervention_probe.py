"""Collect one same-file allow/read-only/allow synthetic observation.

The original supervisor is the sole launcher. This driver owns only its local
synthetic fixtures and loopback recorder; it grants no business qualification.
No authentication or model backend is used. Native execution is an explicit
Root-owned acceptance step, separate from this module's fake tests.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .codex_cli_provider import (
    _Capture, _atomic_bytes, _bounded_private_file, _read_stop, _run_bounded,
    _safe_environment,
)
from .provider_contract import canonical_json, digest, strict_json
from .provider_app_server_invocation import (
    RPC_POLICY, RPC_POLICY_DIGEST, TRANSPORT_VERSION,
    render_synthetic_app_server_argv, synthetic_plan,
    CONTROLLED_RPC_POLICY, CONTROLLED_RPC_POLICY_DIGEST, CONTROLLED_TRANSPORT_VERSION,
    render_controlled_synthetic_app_server_argv,
)
from .provider_app_server_probe import (
    _events, _identity_valid, _summary_valid, assess as legacy_assess,
    _controlled_config, _control_identity, _check_control, _controller_record,
)
from .provider_app_server_protocol import AppServerProtocol
from .provider_outer_sandbox import build_outer_invocation, static_descriptor, LAUNCHER
from .provider_read_boundary import READ_BOUNDARY_POLICY, READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION
from .provider_read_probe import _CancellationView, _matrix_layout, OUTPUT_SCHEMA, READ_BOUNDARY_CASES
from .provider_schema import ProviderConfig
from .provider_write_probe import (
    _WriteStub, _target_state, CASES, BEFORE_CONTENT, AFTER_CONTENT,
    BEFORE_SHA256, AFTER_SHA256, CONTROL_POLICY_DIGEST, PROMPT,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT_VERSION = 'native_app_server_write_observability_v1'
SCOPE = 'synthetic_policy_intervention_only_no_business_qualification'
CONTRACT_SHA256 = 'ceca32fbf66432f7a4f989b5b57b4b0b8c0ca5d030b09235771fe21dd1ab14a6'
PHASES = ('write_before', 'readonly', 'write_after')
SOURCE_FILES = tuple('paper_alpha/server/' + name + '.py' for name in (
    'codex_cli_provider', 'provider_supervisor', 'provider_supervision_journal',
    'provider_app_server_protocol', 'provider_app_server_invocation',
    'provider_app_server_probe', 'provider_app_server_write_intervention_probe',
    'provider_app_server_write_observability', 'provider_outer_sandbox',
    'provider_read_probe', 'provider_write_probe', 'provider_contract', 'provider_schema',
    'provider_cli_profile', 'provider_read_boundary',
))
SUPERVISION_FILES = ('spec.json', 'stdin.bin', 'ready.json', 'launched.json', 'stop.json',
    'stdout.bin', 'stderr.bin', 'rpc-result.json', 'response.raw.json', 'supervisor.stderr.log')
PHASE_FILES = ('observation.json', 'stdout.jsonl', 'stderr.log', 'target.before.bin',
    'target.after.bin', 'request.1.http', 'request.2.http', 'response.1.sse', 'response.2.sse')
FREEZE_KEYS = {'schema_version', 'batch_id', 'source_pins', 'native_executable_path',
    'native_executable_sha256', 'outer_helper_sha256', 'launcher_sha256',
    'outer_template_sha256', 'contract_sha256'}
FILE_ID_KEYS = ('path', 'dev', 'ino', 'uid', 'gid', 'mode', 'nlink')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_file(path, limit=8 * 1024 * 1024):
    raw = _bounded_private_file(Path(path), limit)
    return raw, strict_json(raw, max_bytes=limit)


def _snapshot(path, *, limit=65536):
    """Read one owned regular file without following/replacing its identity."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        first = os.fstat(fd)
        if not stat.S_ISREG(first.st_mode) or first.st_nlink != 1 or first.st_uid != os.getuid() or first.st_size > limit:
            raise ValueError('FIXTURE_FILE_IDENTITY_INVALID')
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(16384, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        last, link = os.fstat(fd), path.lstat()
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size')
        if len(data) > limit or any(getattr(first, key) != getattr(last, key) or getattr(first, key) != getattr(link, key) for key in fields):
            raise ValueError('FIXTURE_FILE_CHANGED_DURING_CAPTURE')
        result = dict(zip(FILE_ID_KEYS, (str(path), first.st_dev, first.st_ino,
            first.st_uid, first.st_gid, first.st_mode, first.st_nlink)))
        result.update(size_bytes=len(data), sha256=_sha(bytes(data)))
        return result, bytes(data)
    finally:
        os.close(fd)


def _state_identity(path):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise ValueError('FIXTURE_STATE_IDENTITY_INVALID')
    return dict(zip(('path', 'dev', 'ino', 'uid', 'gid', 'mode'),
        (str(path), info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode)))


def _response_snapshot(path):
    try:
        snapshot, raw = _snapshot(path)
    except FileNotFoundError:
        return {'exists': False, 'sha256': None, 'size_bytes': 0}, None
    return {'exists': True, 'sha256': snapshot['sha256'], 'size_bytes': snapshot['size_bytes']}, raw


def _reset_target(target, expected):
    before, raw = _snapshot(target, limit=1024)
    if before != expected or raw != AFTER_CONTENT:
        raise ValueError('FIXTURE_RESET_PRECONDITION_FAILED')
    fd = os.open(target, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink) != tuple(before[key] for key in FILE_ID_KEYS[1:]):
            raise ValueError('FIXTURE_RESET_IDENTITY_CHANGED')
        os.ftruncate(fd, 0)
        offset = 0
        while offset < len(BEFORE_CONTENT):
            written = os.write(fd, BEFORE_CONTENT[offset:])
            if written <= 0:
                raise OSError('FIXTURE_RESET_WRITE_FAILED')
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    after, raw = _snapshot(target, limit=1024)
    if raw != BEFORE_CONTENT or any(after[key] != before[key] for key in FILE_ID_KEYS):
        raise ValueError('FIXTURE_RESET_IDENTITY_CHANGED')
    return {'operation': 'reset_after_write_before', 'after_phase': 'write_before',
        'before_phase': 'readonly', 'target': str(target), 'before': before, 'after': after}


def _remove_response(response, supervision, after_phase, before_phase):
    before, raw = _response_snapshot(response)
    archived = _bounded_private_file(supervision / 'response.raw.json', 65536)
    stop = _json_file(supervision / 'stop.json', 65536)[1]
    if not before['exists'] or raw != archived or not stop.get('process_stopped') or stop.get('response_sha256') != _sha(raw):
        raise ValueError('RESPONSE_ARCHIVE_OR_STOP_UNCONFIRMED')
    response.unlink()
    after, _ = _response_snapshot(response)
    if after['exists']:
        raise ValueError('RESPONSE_DELETE_FAILED')
    return {'operation': 'remove_response_before_' + before_phase, 'after_phase': after_phase,
        'before_phase': before_phase, 'target': str(response), 'before': before, 'after': after}


def _validate_inputs(executable, source_freeze, legacy_matrix_report, *, config=None):
    controlled = config is not None
    freeze_raw, freeze = _json_file(source_freeze, 1024 * 1024)
    if not isinstance(freeze, dict) or set(freeze) != FREEZE_KEYS or type(freeze['schema_version']) is not int or freeze['schema_version'] != 1:
        raise ValueError('SOURCE_FREEZE_INVALID')
    if not isinstance(freeze['batch_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', freeze['batch_id']):
        raise ValueError('SOURCE_FREEZE_BATCH_INVALID')
    if not isinstance(freeze['source_pins'], dict) or set(freeze['source_pins']) != set(SOURCE_FILES):
        raise ValueError('SOURCE_FREEZE_PATHS_INVALID')
    for path, expected in freeze['source_pins'].items():
        if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected) or _sha(_bounded_private_file(ROOT / path, 2 * 1024 * 1024)) != expected:
            raise ValueError('SOURCE_FREEZE_MISMATCH')
    native = str(Path(executable).resolve(strict=True))
    pins = {'native_executable_path': native,
        'native_executable_sha256': _sha(_bounded_private_file(Path(native), 512 * 1024 * 1024)),
        'outer_helper_sha256': freeze['source_pins']['paper_alpha/server/provider_outer_sandbox.py'],
        'launcher_sha256': _sha(_bounded_private_file(Path(LAUNCHER), 8 * 1024 * 1024)),
        'outer_template_sha256': static_descriptor()['template_sha256'],
        'contract_sha256': _sha(_bounded_private_file(ROOT / 'docs/OBSERVABILITY_CONTRACT.md', 65536))}
    if pins['contract_sha256'] != CONTRACT_SHA256 or any(freeze[key] != value for key, value in pins.items()):
        raise ValueError('SOURCE_FREEZE_RUNTIME_MISMATCH')
    legacy_raw, legacy = _json_file(legacy_matrix_report)
    if not isinstance(legacy, dict) or legacy.get('report_version') != 'native_app_server_boundary_v1' or legacy.get('real_model_invocations') != 0 or legacy.get('authorization_used') is not False:
        raise ValueError('LEGACY_MATRIX_INVALID')
    if controlled and (legacy.get('transport') != CONTROLLED_TRANSPORT_VERSION
            or legacy.get('rpc_policy') != CONTROLLED_RPC_POLICY):
        raise ValueError('LEGACY_MATRIX_CONTROLLED_TRANSPORT_INVALID')
    cases = legacy.get('cases')
    if not isinstance(cases, list) or [item.get('case') for item in cases] != list(READ_BOUNDARY_CASES + CASES):
        raise ValueError('LEGACY_MATRIX_INCOMPLETE')
    rebuilt = legacy_assess(cases, legacy.get('matrix_layout'), legacy.get('external_target_cleanup'),
        controlled_context=controlled)
    if rebuilt != legacy.get('assessment') or not rebuilt['read']['read_boundary_observed'] or not rebuilt['cleanup_confirmed']:
        raise ValueError('LEGACY_MATRIX_GATE_FAILED')
    write = rebuilt['write']
    if not write['positive_control_executed'] or not write['readonly_outside_blocked'] or not write['same_identity'] or not write['app_server_identity_valid']:
        raise ValueError('LEGACY_MATRIX_WRITE_GATE_FAILED')
    if config is None:
        expected_config = ProviderConfig(config_id='synthetic-app-server-boundary', enabled=True,
            executable=native, model='gpt-5.5').model_dump(mode='json')
    else:
        checked = _controlled_config(config)
        if checked.executable != native:
            raise ValueError('LEGACY_ACTUAL_CONFIG_EXECUTABLE_MISMATCH')
        expected_config = checked.model_dump(mode='json')
    for item in cases:
        if (not _identity_valid(item, controlled_context=controlled) or not item.get('invocation_started') or not item.get('process_stopped')
                or type(item.get('exit_code')) is not int or item['exit_code'] != 0
                or item.get('transport_error') or item.get('failure') or item.get('blockers')
                or item.get('config_snapshot') != expected_config
                or item.get('executable_sha256') != freeze['native_executable_sha256']):
            raise ValueError('LEGACY_MATRIX_STOP_OR_IDENTITY_FAILED')
    inside = cases[6]
    if inside.get('target_after_sha256') != BEFORE_SHA256 or not inside.get('tool_call_sent') or not inside.get('synthetic_tool_outputs'):
        raise ValueError('LEGACY_MATRIX_READONLY_EFFECT_INVALID')
    # This exact original raw is a pre-dispatch gate, not a fabricated summary.
    # The complete eight-case archive remains separately owned/reviewed by Root.
    legacy_stdout = _bounded_private_file(Path(legacy_matrix_report).parent / 'readonly_inside/stdout.jsonl', 1024 * 1024)
    if _sha(legacy_stdout) != inside.get('stdout_sha256'):
        raise ValueError('LEGACY_MATRIX_READONLY_RAW_MISMATCH')
    replay = AppServerProtocol(inside['rpc_plan'])
    replay.start()
    for line in legacy_stdout.splitlines(keepends=True):
        replay.feed(line)
    replay.finish_eof()
    if (replay.summary() != {key: value for key, value in inside['rpc_summary'].items() if key != 'supervision'}
            or strict_json(replay.response, max_bytes=65536) != {'done': True}):
        raise ValueError('LEGACY_MATRIX_READONLY_RPC_MISMATCH')
    evidence = _Capture(stdout=legacy_stdout, app_server_summary=inside['rpc_summary'])
    if not _phase_effect(evidence, Path(inside['synthetic_target']), 'readonly'):
        raise ValueError('LEGACY_MATRIX_READONLY_PATCH_INVALID')
    reference = {'report_version': legacy['report_version'], 'path': str(Path(legacy_matrix_report).resolve(strict=True)),
        'sha256': _sha(legacy_raw), 'read_boundary_observed': rebuilt['read']['read_boundary_observed'],
        'write_boundary_observed': write['write_boundary_observed'], 'cleanup_confirmed': rebuilt['cleanup_confirmed'],
        'qualified': rebuilt['qualified']}
    return freeze_raw, freeze, legacy_raw, reference


class _RecordingReader:
    def __init__(self, reader):
        self.reader, self.header, self.body = reader, None, None

    async def readuntil(self, separator):
        value = await self.reader.readuntil(separator)
        self.header = value
        return value

    async def readexactly(self, count):
        value = await self.reader.readexactly(count)
        self.body = value
        return value


class _RecordingWriter:
    def __init__(self, writer):
        self.writer, self.raw = writer, bytearray()

    def write(self, value):
        if len(self.raw) + len(value) > 2 * 1024 * 1024 + 16384:
            raise ValueError('RESPONSE_CAPTURE_LIMIT')
        self.writer.write(value)
        self.raw.extend(value)

    async def drain(self):
        await self.writer.drain()

    def close(self):
        self.writer.close()

    async def wait_closed(self):
        await self.writer.wait_closed()


class _LoopbackRecorder:
    def __init__(self):
        self.stub, self.directory = None, None
        self.tasks, self.errors = set(), set()
        self.connections = 0

    def select(self, stub, directory):
        if self.tasks:
            raise ValueError('HTTP_PHASE_TASKS_ACTIVE')
        self.stub, self.directory, self.connections = stub, directory, 0

    async def handle(self, reader, writer):
        current = asyncio.current_task()
        self.tasks.add(current)
        stub, directory = self.stub, self.directory
        self.connections += 1
        number = self.connections
        wrapped_reader, wrapped_writer = _RecordingReader(reader), _RecordingWriter(writer)
        try:
            if stub is None or number > 2:
                self.errors.add('HTTP_OUT_OF_PHASE' if stub is None else 'REQUEST_LIMIT')
                if stub:
                    stub.blockers.add('REQUEST_LIMIT')
                    stub.cancel.set()
                writer.close()
                await writer.wait_closed()
                return
            await stub.handle(wrapped_reader, wrapped_writer)
        finally:
            try:
                header, body = wrapped_reader.header, wrapped_reader.body
                if header is not None and body is not None:
                    # A rejected auth-bearing header must never become an evidence leak.
                    names = [line.split(b':', 1)[0].strip().lower() for line in header.split(b'\r\n')[1:] if line]
                    if b'authorization' not in names and b'proxy-authorization' not in names:
                        _atomic_bytes(directory / f'request.{number}.http', header + body)
                if wrapped_writer.raw and number <= 2 and directory is not None:
                    pieces = bytes(wrapped_writer.raw).split(b'\r\n\r\n', 1)
                    if len(pieces) == 2:
                        _atomic_bytes(directory / f'response.{number}.sse', pieces[1])
            except (OSError, ValueError):
                self.errors.add('HTTP_CAPTURE_FAILED')
                if stub:
                    stub.blockers.add('HTTP_CAPTURE_FAILED')
                    stub.cancel.set()
            self.tasks.discard(current)

    async def drain(self):
        tasks = list(self.tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                self.errors.add('HTTP_TASK_CLEANUP_REQUIRED')


def _stopped_capture(capture, supervision):
    try:
        _, spec = _json_file(supervision / 'spec.json', 65536)
        _, ready = _json_file(supervision / 'ready.json', 65536)
        _, launched = _json_file(supervision / 'launched.json', 65536)
        stop, stdout, stderr = _read_stop(supervision, spec['nonce'], ready['supervisor_pid'],
            spec['deadline_monotonic'], spec['output_limit'], protocol_version=2)
        _, summary = _json_file(supervision / 'rpc-result.json', 65536)
        response = _bounded_private_file(supervision / 'response.raw.json', 65536)
        return (ready == {'protocol_version': 2, 'nonce': spec['nonce'], 'supervisor_pid': stop['supervisor_pid'], 'ready': True}
            and launched == {'protocol_version': 2, 'nonce': spec['nonce'], 'supervisor_pid': stop['supervisor_pid'], 'cli_pid': stop['cli_pid']}
            and capture.started and capture.stopped and capture.error is None and capture.returncode == 0
            and capture.protocol_completed and _summary_valid(capture.app_server_summary)
            and summary == capture.app_server_summary and strict_json(response, max_bytes=65536) == {'done': True}
            and stop['process_stopped'] and stop['rpc_completed'] and stop['reason'] is None
            and stop['response_saved'] and stdout == capture.stdout and stderr == capture.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _phase_effect(capture, target, phase):
    try:
        events = []
        for line in capture.stdout.splitlines():
            frame = strict_json(line, max_bytes=1024 * 1024)
            if frame.get('method') in ('item/started', 'item/completed') and frame.get('params', {}).get('item', {}).get('type') == 'fileChange':
                events.append((frame['method'], frame['params']))
        if len(events) != 2 or [event[0] for event in events] != ['item/started', 'item/completed']:
            return False
        summary = capture.app_server_summary
        diff = '@@ -1 +1 @@\n-PAPER_ALPHA_SYNTHETIC_BEFORE\n+PAPER_ALPHA_SYNTHETIC_AFTER\n'
        changes = [{'path': str(target), 'kind': {'type': 'update', 'move_path': None}, 'diff': diff}]
        return all(params.get('threadId') == summary['thread_id'] and params.get('turnId') == summary['turn_id']
            and params['item'].get('id') == 'call_synthetic'
            and params['item'].get('status') == ('inProgress' if index == 0 else 'failed' if phase == 'readonly' else 'completed')
            and params['item'].get('changes') == changes for index, (_, params) in enumerate(events))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


OUTSIDE_PREFLIGHT_RAW_FILES = (
    'request.1.http', 'request.2.http', 'response.1.sse', 'response.2.sse',
    'stdout.jsonl', 'stderr.log', 'target.before.bin', 'target.after.bin',
    'target.before.identity.json', 'target.after.identity.json',
    'supervision/spec.json', 'supervision/ready.json', 'supervision/launched.json',
    'supervision/stop.json', 'supervision/rpc-result.json', 'supervision/response.raw.json',
)


def outside_preflight_refusal_valid(observation, raw):
    """Pure verification of one exact patch rejected before fileChange begins.

    Supplied bytes are archived originals. This never reads the live target,
    launches anything, or upgrades the legacy inside-write refusal predicate.
    """
    try:
        if (type(raw) is not dict or set(raw) != set(OUTSIDE_PREFLIGHT_RAW_FILES)
                or any(type(value) is not bytes or len(value) > 2*1024*1024 for value in raw.values())
                or type(observation) is not dict or observation.get('case') != 'readonly_outside'
                or not _identity_valid(observation, controlled_context=True)
                or observation.get('invocation_started') is not True
                or observation.get('process_stopped') is not True
                or type(observation.get('exit_code')) is not int or observation['exit_code'] != 0
                or observation.get('transport_error') is not None or observation.get('failure') is not None
                or observation.get('blockers') != []):
            return False
        target = Path(observation['synthetic_target'])
        if (not target.is_absolute() or str(target) != observation['synthetic_target']
                or '..' in target.parts or target != Path(observation['allowed_root']).parent/'outside-target.txt'
                or raw['target.before.bin'] != BEFORE_CONTENT or raw['target.after.bin'] != BEFORE_CONTENT):
            return False
        before = strict_json(raw['target.before.identity.json'], max_bytes=65536)
        after = strict_json(raw['target.after.identity.json'], max_bytes=65536)
        if (type(before) is not dict or set(before) != set(FILE_ID_KEYS)|{'size_bytes','sha256'}
                or canonical_json(before) != canonical_json(after) or before['path'] != str(target)
                or any(type(before[key]) is not int or before[key] < 0 for key in ('dev','ino','uid','gid','mode','nlink','size_bytes'))
                or not stat.S_ISREG(before['mode']) or before['nlink'] != 1
                or before['size_bytes'] != len(BEFORE_CONTENT) or before['sha256'] != BEFORE_SHA256
                or any(observation.get(key) != BEFORE_SHA256 for key in
                    ('target_before_sha256','target_sha256','target_after_sha256'))
                or observation.get('target_exists_before') is not True or observation.get('target_exists') is not True
                or observation.get('target_read_error') is not None):
            return False
        from .provider_read_probe import _done_response
        stub = _WriteStub('/write-challenge-'+observation['challenge_nonce']+'/v1/responses', target)
        if raw['response.1.sse'] != stub.tool_response() or raw['response.2.sse'] != _done_response():
            return False
        patch = ('*** Begin Patch\n*** Update File: '+str(target)
            +'\n@@\n-PAPER_ALPHA_SYNTHETIC_BEFORE\n+PAPER_ALPHA_SYNTHETIC_AFTER\n*** End Patch')
        expected_output = ('apply_patch verification failed: Failed to read file to update '
            +str(target)+': Operation not permitted (os error 1)')
        for number in (1, 2):
            header, body = raw[f'request.{number}.http'].split(b'\r\n\r\n', 1)
            if len(header) > 16384:
                return False
            lines = header.decode('ascii').split('\r\n')
            if lines[0] != 'POST '+stub.path+' HTTP/1.1':
                return False
            headers = {}
            for line in lines[1:]:
                name, value = line.split(':', 1)
                name = name.strip().lower()
                if name in headers:
                    return False
                headers[name] = value.strip()
            if ('authorization' in headers or 'proxy-authorization' in headers or 'transfer-encoding' in headers
                    or headers.get('content-encoding','identity') != 'identity'
                    or headers.get('host') != '127.0.0.1:'+str(observation['outer_boundary']['loopback_port'])
                    or headers.get('content-length') != str(len(body))):
                return False
            request = strict_json(body, max_bytes=2*1024*1024)
            if (type(request) is not dict or request.get('model') != 'gpt-5.5'
                    or request.get('reasoning') != {'effort':'low'}
                    or request.get('stream') is not True or request.get('store') is not False
                    or type(request.get('input')) is not list):
                return False
            calls = [item for item in request['input'] if type(item) is dict and item.get('type')=='custom_tool_call']
            outputs = [item for item in request['input'] if type(item) is dict and item.get('type') in
                ('custom_tool_call_output','function_call_output')]
            if number == 1 and (calls or outputs):
                return False
            if number == 2 and (calls != [{'type':'custom_tool_call','call_id':'call_synthetic',
                    'name':'apply_patch','input':patch}] or outputs != [{'type':'custom_tool_call_output',
                    'call_id':'call_synthetic','output':expected_output}]):
                return False
            stub.count += 1
            stub.observe(request)
            if stub.blockers or stub.cancel.is_set():
                return False
        if (stub.tool_outputs != [expected_output] or observation.get('synthetic_tool_outputs') != [expected_output]
                or observation.get('request_count') != 2 or observation.get('tool_call_sent') is not True
                or sorted(stub.tools) != observation.get('tool_names') or 'apply_patch' not in stub.tools
                or canonical_json(stub.request_shapes) != canonical_json(observation.get('request_shapes'))):
            return False
        spec = strict_json(raw['supervision/spec.json'], max_bytes=65536)
        ready = strict_json(raw['supervision/ready.json'], max_bytes=65536)
        launched = strict_json(raw['supervision/launched.json'], max_bytes=65536)
        stop = strict_json(raw['supervision/stop.json'], max_bytes=65536)
        summary = strict_json(raw['supervision/rpc-result.json'], max_bytes=65536)
        if (type(spec) is not dict or set(spec) != {'protocol_version','nonce','argv','cwd','deadline_monotonic',
                'output_limit','response_path','response_limit','pass_fds','transport'}
                or type(spec['protocol_version']) is not int or spec['protocol_version'] != 2
                or spec['transport'] != 'app_server' or spec['argv'] != observation['argv']
                or spec['cwd'] != observation['allowed_root'] or spec['response_path'] != observation['outer_boundary']['response_path']
                or type(spec['output_limit']) is not int or spec['output_limit'] != 1048576
                or type(spec['response_limit']) is not int or spec['response_limit'] != 65536
                or type(spec['pass_fds']) is not list or any(type(fd) is not int or fd < 0 for fd in spec['pass_fds'])
                or not re.fullmatch('[a-f0-9]{32}', spec['nonce'])):
            return False
        stop_keys = {'protocol_version','nonce','supervisor_pid','cli_pid','invocation_started','started_monotonic',
            'finished_monotonic','deadline_monotonic','exit_code','reason','leader_reaped','group_absent',
            'process_stopped','stdout_bytes','stderr_bytes','stdout_sha256','stderr_sha256','response_saved',
            'response_error','response_sha256','transport','rpc_completed','rpc_result_sha256'}
        if (type(stop) is not dict or set(stop) != stop_keys or type(stop['protocol_version']) is not int
                or stop['protocol_version'] != 2 or stop['transport'] != 'app_server'
                or any(stop[key] is not True for key in ('invocation_started','leader_reaped','group_absent',
                    'process_stopped','response_saved','rpc_completed'))
                or type(stop['exit_code']) is not int or stop['exit_code'] != 0
                or stop['reason'] is not None or stop['response_error'] is not None
                or any(type(stop[key]) is not int or stop[key] <= 0 for key in ('supervisor_pid','cli_pid'))
                or any(type(stop[key]) not in (int,float) or not math.isfinite(stop[key]) for key in
                    ('started_monotonic','finished_monotonic','deadline_monotonic'))
                or not stop['started_monotonic'] <= stop['finished_monotonic'] <= stop['deadline_monotonic'] <= stop['started_monotonic']+12
                or canonical_json(stop['deadline_monotonic']) != canonical_json(spec['deadline_monotonic'])
                or stop['nonce'] != spec['nonce']
                or canonical_json(ready) != canonical_json({'protocol_version':2,'nonce':spec['nonce'],
                    'supervisor_pid':stop['supervisor_pid'],'ready':True})
                or canonical_json(launched) != canonical_json({'protocol_version':2,'nonce':spec['nonce'],
                    'supervisor_pid':stop['supervisor_pid'],'cli_pid':stop['cli_pid']})):
            return False
        stdout, stderr = raw['stdout.jsonl'], raw['stderr.log']
        if len(stdout)+len(stderr) > spec['output_limit']:
            return False
        for label, data in (('stdout',stdout),('stderr',stderr)):
            if (type(stop[label+'_bytes']) is not int or stop[label+'_bytes'] != len(data)
                    or stop[label+'_sha256'] != _sha(data) or observation[label+'_sha256'] != _sha(data)):
                return False
        if (not _summary_valid(summary) or canonical_json(summary) != canonical_json(observation['rpc_summary'])
                or stop['rpc_result_sha256'] != _sha(raw['supervision/rpc-result.json'])
                or stop['response_sha256'] != _sha(raw['supervision/response.raw.json'])):
            return False
        protocol = AppServerProtocol(observation['rpc_plan'])
        protocol.start()
        for line in stdout.splitlines(keepends=True):
            frame = strict_json(line, max_bytes=1024*1024)
            if frame.get('method') in ('turn/diff/updated','item/fileChange/patchUpdated'):
                return False
            pending = [frame]
            while pending:
                item = pending.pop()
                if type(item) is dict:
                    if item.get('type') == 'fileChange':
                        return False
                    pending.extend(item.values())
                elif type(item) is list:
                    pending.extend(item)
            protocol.feed(line)
        protocol.finish_eof()
        return (canonical_json(protocol.summary()) == canonical_json({key:value for key,value in summary.items() if key!='supervision'})
            and protocol.response == raw['supervision/response.raw.json']
            and strict_json(protocol.response, max_bytes=65536) == {'done':True})
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return False


def _observation(config, layout, identity, phase, wrapped, plan, stub, capture, before, after, failure, *, controlled_context=False):
    positive = phase != 'readonly'
    state = _target_state(Path(identity['target']))
    transport = CONTROLLED_TRANSPORT_VERSION if controlled_context else TRANSPORT_VERSION
    rpc_policy = CONTROLLED_RPC_POLICY if controlled_context else RPC_POLICY
    rpc_policy_digest = CONTROLLED_RPC_POLICY_DIGEST if controlled_context else RPC_POLICY_DIGEST
    return {'report_version': REPORT_VERSION, 'transport': transport,
        'case': 'allow_write_control' if positive else 'readonly_inside',
        'enforcement': {'outer': 'mandatory_macos_whole_process', 'inner': rpc_policy['sandbox'], 'named_inner_profile_executed': False},
        'measurement_policy_reference': 'legacy_original_matrix_predicates_only',
        'synthetic_target': identity['target'], 'allowed_root': identity['work'],
        'matrix_layout': layout, 'matrix_layout_digest': digest(layout),
        'target_before_sha256': before['sha256'], 'target_sha256': after['sha256'] if after else None,
        'target_exists_before': True, 'target_size_bytes': before['size_bytes'], **state,
        'config_snapshot': config.model_dump(mode='json'), 'config_digest': identity['config_digest'],
        'executable_sha256': identity['native_executable_sha256'],
        'challenge_nonce': identity['challenge_nonce'], 'schema_digest': digest(OUTPUT_SCHEMA),
        'policy': READ_BOUNDARY_POLICY, 'policy_version': READ_BOUNDARY_POLICY_VERSION,
        'policy_digest': READ_BOUNDARY_POLICY_DIGEST, 'readonly_policy_digest': READ_BOUNDARY_POLICY_DIGEST,
        'control_policy_digest': CONTROL_POLICY_DIGEST,
        'active_policy_digest': CONTROL_POLICY_DIGEST if positive else READ_BOUNDARY_POLICY_DIGEST,
        'argv': wrapped['argv'], 'argv_digest': digest(wrapped['argv']), 'outer_boundary': wrapped['boundary'],
        'rpc_policy_digest': rpc_policy_digest, 'rpc_plan': plan, 'rpc_plan_digest': digest(plan),
        'protocol_completed': capture.protocol_completed, 'rpc_summary': capture.app_server_summary,
        'tool_names': sorted(stub.tools), 'request_count': stub.count, 'request_shapes': stub.request_shapes,
        'tool_call_sent': stub.tool_call_sent, 'allow_apply_patch': True,
        'image_payloads_in_requests': stub.image_payloads, 'synthetic_tool_outputs': stub.tool_outputs,
        'events': _events(capture.stdout), 'process_stopped': capture.stopped, 'invocation_started': capture.started,
        'exit_code': capture.returncode, 'transport_error': capture.error, 'failure': failure,
        'blockers': sorted(stub.blockers), 'stdout_sha256': _sha(capture.stdout), 'stderr_sha256': _sha(capture.stderr),
        'real_model_invocations': 0, 'authorization_used': False, 'phase': phase,
        'environment_snapshot': identity['environment']}


def _manifest(base):
    paths = ['fixture-operations.json', 'source-freeze.json', 'legacy-matrix-report.json']
    for phase in PHASES:
        paths.extend(phase + '/' + name for name in PHASE_FILES)
        paths.extend(phase + '/supervision/' + name for name in SUPERVISION_FILES)
    result = []
    for relative in paths:
        path = base / relative
        try:
            raw = _bounded_private_file(path, 8 * 1024 * 1024)
        except FileNotFoundError:
            continue
        result.append({'path': relative, 'sha256': _sha(raw), 'size_bytes': len(raw)})
    return result


async def run(executable, output, *, source_freeze, legacy_matrix_report, control=None, seconds=12,
              config=None, dispatch_guard=None):
    if type(seconds) not in (int, float) or not 0 < seconds <= 12:
        raise ValueError('PROBE_DURATION_INVALID')
    controlled = config is not None
    renderer = render_controlled_synthetic_app_server_argv if controlled else render_synthetic_app_server_argv
    transport = CONTROLLED_TRANSPORT_VERSION if controlled else TRANSPORT_VERSION
    context, baseline, original_cancel = None, None, None
    if controlled:
        config = _controlled_config(config)
        baseline, original_cancel = _control_identity(config, control), control.cancel_event
        _check_control(config, control, baseline, original_cancel)
    if dispatch_guard is not None and not controlled:
        raise ValueError('PROBE_GUARD_REQUIRES_ACTUAL_CONFIG')
    inputs = {'config': config} if controlled else {}
    freeze_raw, freeze, legacy_raw, reference = _validate_inputs(executable, source_freeze, legacy_matrix_report, **inputs)
    base = Path(output).resolve()
    base.mkdir(mode=0o700)
    if stat.S_IMODE(base.lstat().st_mode) != 0o700:
        raise ValueError('PROBE_ROOT_NOT_PRIVATE')
    _atomic_bytes(base / 'source-freeze.json', freeze_raw)
    _atomic_bytes(base / 'legacy-matrix-report.json', legacy_raw)
    private = base / 'fixture'
    private.mkdir(mode=0o700)
    work, state = private / 'work', private / 'cli-state'
    work.mkdir(mode=0o700)
    if controlled:
        from .provider_cli_state import prepare_app_server_probe_state, validate_app_server_probe_state
        context = prepare_app_server_probe_state(private)
        validate_app_server_probe_state(context)
        state = context['path']
    else:
        state.mkdir(mode=0o700)
    target, response = work / 'target.txt', work / 'response.raw.json'
    _atomic_bytes(target, BEFORE_CONTENT)
    _atomic_bytes(work / 'output.schema.json', canonical_json(OUTPUT_SCHEMA).encode())
    initial, _ = _snapshot(target, limit=1024)
    state_identity = _state_identity(state)
    if not controlled:
        config = ProviderConfig(config_id='synthetic-app-server-boundary', enabled=True,
            executable=freeze['native_executable_path'], model='gpt-5.5')
    nonce, recorder = uuid.uuid4().hex, _LoopbackRecorder()
    server = await asyncio.start_server(recorder.handle, '127.0.0.1', 0, limit=16384)
    port = server.sockets[0].getsockname()[1]
    phases, operations, attempts = [], [], 0
    identity = None
    try:
        inner = renderer(config, port=port, nonce=nonce, write=True)
        plan = synthetic_plan(config, work, PROMPT, OUTPUT_SCHEMA, write=True)
        environment = _safe_environment()
        environment.update(HOME=str(state), CODEX_HOME=str(state))
        identity = {'transport': transport, 'source_pins': freeze['source_pins'],
            'config_snapshot': config.model_dump(mode='json'), 'config_digest': digest(config.model_dump(mode='json')),
            'native_executable_path': config.executable, 'native_executable_sha256': freeze['native_executable_sha256'],
            'work': str(work), 'cli_state': str(state), 'response_path': str(response), 'target': str(target),
            'loopback_port': port, 'challenge_nonce': nonce, 'inner_argv': inner, 'inner_argv_digest': digest(inner),
            'rpc_plan': plan, 'rpc_plan_digest': digest(plan), 'environment': environment,
            'target_identity': {key: initial[key] for key in FILE_ID_KEYS}, 'state_identity': state_identity}
        wrappers = {phase: build_outer_invocation(inner, work=work, cli_state=state, response_path=response,
            network_mode='synthetic_loopback', loopback_port=port,
            synthetic_write_target=None if phase == 'readonly' else target) for phase in PHASES}
        boundary = wrappers['write_before']['boundary']
        if any(boundary[key] != freeze[freeze_key] for key, freeze_key in (
                ('helper_sha256', 'outer_helper_sha256'), ('launcher_sha256', 'launcher_sha256'),
                ('native_executable_path', 'native_executable_path'), ('native_executable_sha256', 'native_executable_sha256'),
                ('template_sha256', 'outer_template_sha256'))):
            raise ValueError('OUTER_FREEZE_MISMATCH')
        _atomic_bytes(base / 'expected-identity.json', (canonical_json({'identity': identity,
            'outer_identity': {key: boundary[key] for key in ('helper_sha256', 'launcher_sha256', 'native_executable_path', 'native_executable_sha256')},
            'legacy_matrix_reference': reference}) + '\n').encode())
        layout = _matrix_layout(base)
        for index, phase in enumerate(PHASES):
            if controlled:
                try:
                    _check_control(config, control, baseline, original_cancel)
                    validate_app_server_probe_state(context)
                    if dispatch_guard is not None:
                        dispatch_guard()
                    _check_control(config, control, baseline, original_cancel)
                except (OSError, ValueError) as exc:
                    _atomic_bytes(base/'control-error.json', (canonical_json({'code':str(exc),
                        'type':type(exc).__name__,'errno':getattr(exc,'errno',None)})+'\n').encode())
                    break
            if control and (control.cancel_event.is_set() or time.monotonic() >= control.deadline_monotonic):
                break
            before, before_raw = _snapshot(target, limit=1024)
            if before_raw != BEFORE_CONTENT or any(before[key] != identity['target_identity'][key] for key in FILE_ID_KEYS) or _state_identity(state) != state_identity:
                break
            directory = base / phase
            directory.mkdir(mode=0o700)
            _atomic_bytes(directory / 'target.before.bin', before_raw)
            stub = _WriteStub('/write-challenge-' + nonce + '/v1/responses', target)
            recorder.select(stub, directory)
            capture, failure, after, controller = (_Capture(stopped=False) if controlled else _Capture()), None, None, None
            cancel = _CancellationView(stub.cancel, control.cancel_event) if control else stub.cancel
            try:
                # Revalidate pins immediately before each dispatch, without changing identity.
                current_freeze_raw, _, current_legacy_raw, current_reference = _validate_inputs(
                    executable, source_freeze, legacy_matrix_report, **inputs)
                if current_freeze_raw != freeze_raw or current_legacy_raw != legacy_raw or current_reference != reference:
                    raise ValueError('PROBE_INPUT_DRIFT')
                current_environment = _safe_environment()
                current_environment.update(HOME=str(state), CODEX_HOME=str(state))
                if current_environment != identity['environment']:
                    raise ValueError('PROBE_ENVIRONMENT_DRIFT')
                if controlled:
                    _check_control(config, control, baseline, original_cancel)
                    validate_app_server_probe_state(context)
                    if dispatch_guard is not None:
                        dispatch_guard()
                    _check_control(config, control, baseline, original_cancel)
                deadline = min(time.monotonic() + seconds, control.deadline_monotonic) if control else time.monotonic() + seconds
                if controlled:
                    controller = _controller_record(config, control, deadline, phase, context['context_policy_version'])
                    _atomic_bytes(directory/'controller.before.json', (canonical_json(controller)+'\n').encode())
                    _atomic_bytes(directory/'environment.json', (canonical_json(current_environment)+'\n').encode())
                attempts += 1
                await _run_bounded(wrappers[phase]['argv'], cwd=work, stdin=b'', deadline=deadline,
                    cancel=cancel, output_limit=1024 * 1024, pass_fds=control.pass_fds if control else (),
                    capture=capture, supervision_dir=directory / 'supervision', response_path=response,
                    response_limit=65536, isolated_cli_home=state, app_server_plan=plan)
                if controlled:
                    if dispatch_guard is not None:
                        dispatch_guard()
                    _check_control(config, control, baseline, original_cancel)
                    validate_app_server_probe_state(context)
            except asyncio.CancelledError:
                failure = {'type': 'CancelledError', 'code': 'INTERRUPTED', 'errno': None}
                stub.blockers.add('INTERRUPTED')
            except (OSError, ValueError) as exc:
                failure = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None), 'errno': getattr(exc, 'errno', None)}
                stub.blockers.add('PROBE_EXECUTION_FAILED')
            finally:
                if controller is not None:
                    controller['cancelled_after'] = original_cancel.is_set()
                    _atomic_bytes(directory/'controller.json', (canonical_json(controller)+'\n').encode())
            await recorder.drain()
            recorder.stub = None
            stub.blockers.update(recorder.errors)
            if not capture.stopped:
                stub.blockers.add('CLEANUP_UNCONFIRMED')
            try:
                after, after_raw = _snapshot(target, limit=1024)
                _atomic_bytes(directory / 'target.after.bin', after_raw)
            except (OSError, ValueError):
                stub.blockers.add('TARGET_CAPTURE_FAILED')
            observation = _observation(config, layout, identity, phase, wrappers[phase], plan,
                stub, capture, before, after, failure, controlled_context=controlled)
            _atomic_bytes(directory / 'stdout.jsonl', capture.stdout)
            _atomic_bytes(directory / 'stderr.log', capture.stderr)
            _atomic_bytes(directory / 'observation.json', (canonical_json(observation) + '\n').encode())
            phases.append({'phase': phase, 'observation': observation, 'target_before': before, 'target_after': after})
            correct = (after is not None and not stub.blockers and failure is None and stub.count == 2
                and recorder.connections == 2 and stub.tool_call_sent and len(stub.tool_outputs) == 1
                and not cancel.is_set()
                and _stopped_capture(capture, directory / 'supervision') and _phase_effect(capture, target, phase)
                and after['sha256'] == (BEFORE_SHA256 if phase == 'readonly' else AFTER_SHA256)
                and all(after[key] == initial[key] for key in FILE_ID_KEYS) and _state_identity(state) == state_identity)
            current_environment = _safe_environment()
            current_environment.update(HOME=str(state), CODEX_HOME=str(state))
            correct = correct and current_environment == identity['environment']
            if not correct or index == 2:
                break
            if phase == 'write_before':
                operations.append(_reset_target(target, after))
            operations.append(_remove_response(response, directory / 'supervision', phase, PHASES[index + 1]))
    finally:
        server.close()
        await server.wait_closed()
        await recorder.drain()
        _atomic_bytes(base / 'fixture-operations.json', (canonical_json(operations) + '\n').encode())
        report = {'report_version': REPORT_VERSION, 'scope': SCOPE, 'identity': identity, 'phases': phases,
            'fixture_operations': operations, 'legacy_matrix_reference': reference, 'raw_manifest': _manifest(base),
            'assessment': None, 'resources': {'native_attempts': attempts, 'real_model_invocations': 0,
                'auth_operations': 0, 'business_grants': 0}}
        _atomic_bytes(base / 'report.json', (canonical_json(report) + '\n').encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-freeze', required=True)
    parser.add_argument('--legacy-matrix-report', required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args.executable, args.output, source_freeze=args.source_freeze,
        legacy_matrix_report=args.legacy_matrix_report))
    print(canonical_json({'report': str(Path(args.output).resolve() / 'report.json'),
        'phase_count': len(report['phases']), 'resources': report['resources'], 'assessment': None}))
    return 0 if len(report['phases']) == 3 and len(report['raw_manifest']) == 60 else 2


if __name__ == '__main__':
    raise SystemExit(main())
