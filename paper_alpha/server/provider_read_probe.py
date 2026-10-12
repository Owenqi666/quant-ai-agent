"""Challenge native view_image using synthetic files and a loopback-only SSE stub.

No model backend is contacted. This script deliberately asks the installed CLI
to execute its native image tool using a fixed local response. The challenge
does not authorize real dispatch, and only images created by this script are
targets. It does not modify global CLI configuration or a research workspace.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import struct
import tempfile
import time
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[2]

from .codex_cli_provider import _Capture, _atomic_bytes, _bounded_private_file, _run_bounded
from .provider_contract import canonical_json, digest, strict_json
from .provider_cli_state import append_context_policy
from .provider_read_boundary import (
    READ_BOUNDARY_POLICY, READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION,
    build_read_boundary_argv, policy_for_version, outer_boundary_identity, outer_evidence_matches_identity, render_read_boundary_argv, temporary_read_denials,
)
from .provider_schema import ProviderCallControl, ProviderConfig
from .provider_cli_profile import render_cli_argv
from .provider_outer_sandbox import build_outer_invocation, assess_outer_boundary

READ_BOUNDARY_CASES = ('inside', 'outside', 'unrelated_tmp', 'declared_tmpdir', 'desktop')
READ_MATRIX_LAYOUT_VERSION = 'native_read_matrix_layout_v1'
MAX_PROBE_SECONDS = 12
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_TOOL_DECLARATIONS = 16
ALLOWED_TOOL_NAMES = frozenset({'update_plan', 'request_user_input', 'view_image'})
PROMPT = 'Synthetic local tool-boundary check only. Follow the supplied synthetic tool response, then return {"done":true}.'
OUTPUT_SCHEMA = {'type': 'object', 'properties': {'done': {'type': 'boolean'}},
                 'required': ['done'], 'additionalProperties': False}


class _CancellationView:
    """Read external cancellation without letting a local refusal mutate it."""
    def __init__(self, local, external):
        self.local, self.external = local, external

    def is_set(self):
        return self.local.is_set() or self.external.is_set()


def _png(seed: int) -> bytes:
    """Create an identifiable synthetic RGB image using only the standard library."""
    def chunk(kind, value):
        return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value) & 0xffffffff)
    pixels = bytes((seed % 256, (seed * 3) % 256, (seed * 7) % 256))
    rows = b''.join(b'\0' + pixels * 8 for _ in range(8))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 8, 8, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def _events(items):
    return b''.join(('event: ' + item['type'] + '\ndata: ' + canonical_json(item) + '\n\n').encode()
                   for item in items)


def _tool_response(target: Path) -> bytes:
    item = {'type': 'function_call', 'id': 'fc_synthetic', 'call_id': 'call_synthetic',
            'name': 'view_image', 'arguments': canonical_json({'path': str(target)})}
    response = {'id': 'resp_synthetic_tool', 'object': 'response', 'status': 'completed',
                'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
    return _events([
        {'type': 'response.created', 'sequence_number': 0, 'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'sequence_number': 1, 'output_index': 0,
         'item': {**item, 'arguments': ''}},
        {'type': 'response.output_item.done', 'sequence_number': 2, 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'sequence_number': 3, 'response': response},
    ])


def _done_response() -> bytes:
    item = {'type': 'message', 'id': 'msg_synthetic', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': '{"done":true}', 'annotations': []}]}
    response = {'id': 'resp_synthetic_done', 'object': 'response', 'status': 'completed',
                'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
    return _events([
        {'type': 'response.created', 'sequence_number': 0, 'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'sequence_number': 1, 'output_index': 0,
         'item': {**item, 'content': []}},
        {'type': 'response.output_text.delta', 'sequence_number': 2, 'output_index': 0,
         'content_index': 0, 'item_id': item['id'], 'delta': '{"done":true}'},
        {'type': 'response.output_item.done', 'sequence_number': 3, 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'sequence_number': 4, 'response': response},
    ])


class _Stub:
    def __init__(self, path, target, *, allow_apply_patch=False):
        if type(allow_apply_patch) is not bool:
            raise ValueError('Tool policy requires an explicit boolean')
        self.path, self.target = path, target
        self.allow_apply_patch = allow_apply_patch
        self.count = 0
        self.tools = []
        self.input_types = []
        self.image_payloads = 0
        self.tool_outputs = []
        self.blockers = set()
        self.tasks = set()
        self.cancel = asyncio.Event()
        self.tool_call_sent = False
        self.request_shapes = []

    @staticmethod
    def _keys(value, limit=32):
        return [key[:128] for key in sorted(value)[:limit]]

    def _observe_declarations(self, declarations):
        summaries = []
        if not isinstance(declarations, list) or len(declarations) > MAX_TOOL_DECLARATIONS:
            self.blockers.add('TOOL_DECLARATION_INVALID')
            declarations = declarations[:MAX_TOOL_DECLARATIONS] if isinstance(declarations, list) else []
        for tool in declarations:
            if not isinstance(tool, dict):
                self.blockers.add('TOOL_DECLARATION_INVALID')
                summaries.append({'type': type(tool).__name__, 'name': None, 'keys': []})
                continue
            name = tool.get('name')
            kind = tool.get('type')
            summaries.append({'type': kind if isinstance(kind, str) and len(kind) <= 128 else None,
                              'name': name if isinstance(name, str) and 1 <= len(name) <= 128 else None,
                              'keys': self._keys(tool)})
            # Retain only bounded structural names, including unknown names,
            # rather than silently dropping an untested tool declaration.
            if isinstance(name, str) and 1 <= len(name) <= 128:
                if name not in self.tools:
                    self.tools.append(name)
            else:
                self.blockers.add('TOOL_DECLARATION_INVALID')
            patch = self.allow_apply_patch and kind == 'custom' and name == 'apply_patch'
            if not patch and tool.get('type') != 'function':
                self.blockers.add('TOOL_TYPE_UNSUPPORTED')
            if tool.get('type') == 'namespace' or 'namespace' in tool:
                self.blockers.add('TOOL_NAMESPACE_UNSUPPORTED')
            if not patch and (not isinstance(name, str) or name not in ALLOWED_TOOL_NAMES):
                self.blockers.add('TOOL_NAME_UNSUPPORTED')
        return summaries

    def observe(self, value):
        if not isinstance(value, dict):
            raise ValueError('Not an object')
        shape = {'top_level_keys': self._keys(value, 64), 'tools_present': 'tools' in value,
                 'tools_type': type(value.get('tools')).__name__ if 'tools' in value else 'missing',
                 'tools_count': len(value['tools']) if isinstance(value.get('tools'), list) else None,
                 'additional_tool_items': []}
        self.request_shapes.append(shape)
        self._observe_declarations(value.get('tools', []))
        pending = [value.get('input', [])]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                kind = item.get('type')
                if kind == 'additional_tools':
                    # Responses-lite can place a tool catalog in input rather
                    # than top-level tools. It must meet the identical allowlist.
                    declarations = self._observe_declarations(item.get('tools'))
                    if len(shape['additional_tool_items']) < MAX_TOOL_DECLARATIONS:
                        shape['additional_tool_items'].append({'keys': self._keys(item),
                            'fields': {key[:128]: {'type': type(item[key]).__name__,
                                                  'count': len(item[key]) if isinstance(item[key], (list, dict)) else None}
                                       for key in sorted(item)[:32] if key != 'type'},
                            'declarations': declarations})
                    else:
                        self.blockers.add('TOOL_DECLARATION_INVALID')
                if isinstance(kind, str) and kind not in self.input_types:
                    self.input_types.append(kind)
                if kind == 'input_image':
                    self.image_payloads += 1
                if kind in ('function_call_output', 'custom_tool_call_output'):
                    output = item.get('output')
                    # Only our call output is retained, never system/user instructions.
                    if item.get('call_id') == 'call_synthetic':
                        self.tool_outputs.append(output)
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)

    def tool_response(self):
        return _tool_response(self.target)

    async def handle(self, reader, writer):
        current = asyncio.current_task()
        self.tasks.add(current)
        status, body, content_type = '400 Bad Request', b'{"error":{"message":"Synthetic challenge rejected."}}', 'application/json'
        try:
            self.count += 1
            if self.count > 3:
                self.blockers.add('REQUEST_LIMIT')
                self.cancel.set()
                return
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            if len(header) > 16384:
                raise ValueError('Header too large')
            lines = header.decode('latin-1').split('\r\n')
            if lines[0].split(' ')[:2] != ['POST', self.path]:
                raise ValueError('Unexpected request')
            headers = {}
            for line in lines[1:]:
                if line:
                    key, value = line.split(':', 1)
                    key = key.strip().lower()
                    if key in headers:
                        raise ValueError('Duplicate header')
                    headers[key] = value.strip()
            if 'authorization' in headers or 'proxy-authorization' in headers:
                self.blockers.add('AUTHORIZATION_HEADER_PRESENT')
                self.cancel.set()
                return
            if 'transfer-encoding' in headers or headers.get('content-encoding', 'identity') != 'identity':
                raise ValueError('Unsupported request encoding')
            length = int(headers.get('content-length', '-1'))
            if not 0 <= length <= MAX_REQUEST_BYTES:
                raise ValueError('Request body too large')
            value = strict_json(await asyncio.wait_for(reader.readexactly(length), 2), max_bytes=MAX_REQUEST_BYTES)
            self.observe(value)
            if self.blockers:
                self.cancel.set()
                return
            status, content_type = '200 OK', 'text/event-stream'
            body = self.tool_response() if self.count == 1 else _done_response()
            if self.count == 1:
                self.tool_call_sent = True
        except (OSError, ValueError, UnicodeError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
            self.blockers.add('HTTP_PROTOCOL_ERROR')
        finally:
            try:
                writer.write((f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body)
                await asyncio.wait_for(writer.drain(), .5)
            except (OSError, TimeoutError):
                pass
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), .5)
            except (OSError, TimeoutError):
                pass
            self.tasks.discard(current)


def _public_events(raw):
    result = []
    for line in raw.splitlines():
        try:
            event = strict_json(line, max_bytes=262144)
            if not isinstance(event, dict):
                continue
            item = event.get('item') if isinstance(event.get('item'), dict) else {}
            result.append({'type': event.get('type'), 'item_type': item.get('type'),
                'status': item.get('status'), 'has_error': bool(event.get('error') or item.get('error') or item.get('type') == 'error')})
        except ValueError:
            result.append({'type': 'UNPARSED_LINE', 'item_type': None, 'status': None, 'has_error': True})
    return result


def _normalized_path(value):
    if not isinstance(value, str) or not value or '\0' in value:
        return None
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        return None
    return path


def _matrix_layout(matrix_root):
    """Record host root identity; only synthetic target paths use these roots."""
    declared = os.environ.get('TMPDIR')
    path = Path(declared).absolute() if declared else None
    return {'layout_version': READ_MATRIX_LAYOUT_VERSION, 'matrix_root': str(Path(matrix_root).absolute()),
            'slash_tmp_root': str(Path('/tmp').resolve()),
            'declared_tmpdir': str(path) if path else None,
            'declared_tmpdir_root': str(path.resolve()) if path else None,
            'desktop_root': str((Path.home() / 'Desktop').resolve())}


def _valid_matrix_layout(layout):
    if not isinstance(layout, dict) or set(layout) != {
            'layout_version', 'matrix_root', 'slash_tmp_root', 'declared_tmpdir',
            'declared_tmpdir_root', 'desktop_root'}:
        return False
    paths = {key: _normalized_path(value) for key, value in layout.items() if key != 'layout_version'}
    if layout['layout_version'] != READ_MATRIX_LAYOUT_VERSION or any(value is None for value in paths.values()):
        return False
    host = _matrix_layout(paths['matrix_root'])
    if any(layout[key] != host[key] for key in layout if key != 'matrix_root'):
        return False
    root, slash = paths['matrix_root'], paths['slash_tmp_root']
    # The failed layout under /tmp is a required control. Relocating the
    # matrix onto Desktop cannot substitute for this layout.
    return (slash in (Path('/tmp'), Path('/private/tmp'))
            and root != slash and root.is_relative_to(slash)
            and str(root.resolve()) == str(root)
            and paths['declared_tmpdir_root'] != paths['desktop_root'])


def _valid_case_layout(item, layout):
    if not isinstance(item, dict) or not _valid_matrix_layout(layout):
        return False
    case = item.get('case')
    if case not in READ_BOUNDARY_CASES:
        return False
    if item.get('matrix_layout') != layout or item.get('matrix_layout_digest') != digest(layout):
        return False
    root = Path(layout['matrix_root'])
    target = _normalized_path(item.get('synthetic_target'))
    if target is None or item.get('allowed_root') != str(root / case / 'work'):
        return False
    if case == 'inside':
        return target == root / 'inside/work/allowed.png'
    if case == 'outside':
        return target == root / 'synthetic-outside.png'
    category_root = Path(layout[{'unrelated_tmp': 'slash_tmp_root',
                                 'declared_tmpdir': 'declared_tmpdir_root',
                                 'desktop': 'desktop_root'}[case]])
    return (target.name == 'synthetic.png' and target.parent.parent == category_root
            and re.fullmatch('paper-alpha-read-' + case + r'-[A-Za-z0-9_-]{6,64}', target.parent.name) is not None
            and not target.is_relative_to(root))


async def _challenge(config, base, case, target, *, max_seconds, allow_apply_patch=False, control=None, matrix_layout=None):
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= MAX_PROBE_SECONDS:
        raise ValueError('Probe duration must be positive and at most 12 seconds')
    matrix_layout = _matrix_layout(base) if matrix_layout is None else matrix_layout
    private = base / case
    private.mkdir(mode=0o700)
    work, cli_state = private / 'work', private / 'cli-state'
    work.mkdir(mode=0o700)
    cli_state.mkdir(mode=0o700)
    schema, response = work / 'output.schema.json', work / 'response.raw.json'
    schema.write_text(canonical_json(OUTPUT_SCHEMA))
    # Both targets are synthesized beneath base. The allowed positive control
    # is copied inside this case's workspace; the negative target stays outside.
    if case == 'inside':
        inside = work / 'allowed.png'
        inside.write_bytes(target.read_bytes())
        target = inside
    target_bytes = _bounded_private_file(target, 1024)
    target_before_sha256 = hashlib.sha256(target_bytes).hexdigest()
    nonce = uuid.uuid4().hex
    endpoint_path = '/challenge-' + nonce + '/v1/responses'
    stub = _Stub(endpoint_path, target, allow_apply_patch=allow_apply_patch)
    cancel = _CancellationView(stub.cancel, control.cancel_event) if control else stub.cancel
    capture = _Capture()
    server, arguments, failure, stage, cancelled = None, [], None, 'bind', False
    outer_boundary = None
    try:
        server = await asyncio.start_server(stub.handle, '127.0.0.1', 0, limit=16384)
        port = server.sockets[0].getsockname()[1]
        stage = 'argv'
        arguments = append_context_policy(build_read_boundary_argv(config, work, schema, response))
        overrides = ['model_provider="paper_alpha_challenge"',
            'model_providers.paper_alpha_challenge.name="Synthetic local challenge"',
            f'model_providers.paper_alpha_challenge.base_url="http://127.0.0.1:{port}/challenge-{nonce}/v1"',
            'model_providers.paper_alpha_challenge.wire_api="responses"',
            'model_providers.paper_alpha_challenge.requires_openai_auth=false',
            'model_providers.paper_alpha_challenge.request_max_retries=0',
            'model_providers.paper_alpha_challenge.stream_max_retries=0',
            'features.enable_request_compression=false']
        for value in overrides:
            arguments.extend(['-c', value])
        wrapped = build_outer_invocation(arguments, work=work, cli_state=cli_state,
            response_path=response, network_mode='synthetic_loopback', loopback_port=port)
        arguments, outer_boundary = wrapped['argv'], wrapped['boundary']
        stage = 'invoke'
        await _run_bounded(arguments, cwd=work, stdin=PROMPT.encode(),
            deadline=min(time.monotonic() + max_seconds, control.deadline_monotonic) if control else time.monotonic() + max_seconds,
            cancel=cancel, output_limit=262144, pass_fds=control.pass_fds if control else (),
            capture=capture, supervision_dir=private / 'supervision',
            response_path=response, response_limit=65536, isolated_cli_home=cli_state)
    except asyncio.CancelledError:
        cancelled = True
        failure = {'code': 'INTERRUPTED', 'type': 'CancelledError', 'errno': None}
        stub.blockers.add('INTERRUPTED')
    except (OSError, ValueError) as exc:
        code = {'bind': 'LOOPBACK_BIND_FAILED', 'argv': 'ARGUMENT_BUILD_FAILED', 'invoke': 'TRANSPORT_FAILED'}[stage]
        failure = {'code': code, 'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None)}
        stub.blockers.add(code)
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = list(stub.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if not capture.stopped:
        stub.blockers.add('CLEANUP_UNCONFIRMED')
    observation = {'case': case, 'synthetic_target': str(target),
        'matrix_layout': matrix_layout, 'matrix_layout_digest': digest(matrix_layout),
        'target_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'target_before_sha256': target_before_sha256, 'target_exists_before': True,
        'target_size_bytes': len(target_bytes),
        'config_digest': digest(config.model_dump()), 'config_snapshot': config.model_dump(mode='json'),
        'challenge_nonce': nonce, 'temporary_read_denials': list(temporary_read_denials()), 'executable_sha256': hashlib.sha256(Path(config.executable).read_bytes()).hexdigest(),
        'argv': arguments, 'argv_digest': digest(arguments), 'outer_boundary': outer_boundary, 'policy_version': READ_BOUNDARY_POLICY_VERSION,
        'policy_digest': READ_BOUNDARY_POLICY_DIGEST, 'policy': READ_BOUNDARY_POLICY, 'allowed_root': str(work),
        'schema_digest': digest(OUTPUT_SCHEMA), 'schema_scope': 'synthetic_done_response',
        'tool_names': sorted(stub.tools), 'request_count': stub.count, 'allow_apply_patch': allow_apply_patch,
        'request_shapes': stub.request_shapes,
        'tool_call_sent': stub.tool_call_sent,
        'image_payloads_in_requests': stub.image_payloads, 'input_types': sorted(stub.input_types),
        'synthetic_tool_outputs': stub.tool_outputs,
        'events': _public_events(capture.stdout), 'process_stopped': capture.stopped,
        'invocation_started': capture.started, 'exit_code': capture.returncode,
        'transport_error': capture.error, 'blockers': sorted(stub.blockers), 'failure': failure,
        'stdout_sha256': hashlib.sha256(capture.stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(capture.stderr).hexdigest()}
    # Logs originate solely from this synthetic challenge. They are bounded,
    # private developer diagnostics, never API/public provider output.
    _atomic_bytes(private / 'stdout.jsonl', capture.stdout)
    _atomic_bytes(private / 'stderr.log', capture.stderr)
    _atomic_bytes(private / 'observation.json', (canonical_json(observation) + '\n').encode())
    if cancelled:
        error = asyncio.CancelledError()
        error.observation = observation
        raise error
    return observation


def _assess_v1(results, *, allow_apply_patch=False) -> dict:
    """A negative result is evidence only after a working positive control."""
    if type(allow_apply_patch) is not bool:
        raise ValueError('Tool policy requires an explicit boolean')
    if (not isinstance(results, list) or any(not isinstance(item, dict) for item in results)
            or any(not all(key in item for key in ('case', 'process_stopped', 'blockers', 'tool_call_sent',
                    'request_count', 'image_payloads_in_requests', 'synthetic_tool_outputs')) for item in results)):
        return {'positive_control_executed': False, 'negative_control_blocked': False,
                'same_identity': False, 'read_boundary_observed': False}
    by_case = {item['case']: item for item in results}
    positive, negative = by_case.get('inside'), by_case.get('outside')
    def tested_tools(case):
        names = case.get('tool_names')
        allowed = ALLOWED_TOOL_NAMES | ({'apply_patch'} if allow_apply_patch else set())
        return (isinstance(names, list) and len(names) <= MAX_TOOL_DECLARATIONS
                and all(isinstance(name, str) and name in allowed for name in names)
                and 'view_image' in names)
    positive_executed = bool(positive and positive['process_stopped'] and not positive['blockers']
        and tested_tools(positive) and positive['tool_call_sent']
        and positive['request_count'] >= 2 and positive['image_payloads_in_requests'] > 0)
    refusal_terms = ('operation not permitted', 'permission denied', 'access denied', 'not allowed')
    negative_blocked = bool(negative and negative['process_stopped'] and not negative['blockers']
        and tested_tools(negative) and negative['tool_call_sent']
        and negative['request_count'] >= 2 and negative['image_payloads_in_requests'] == 0
        and any(any(term in str(value).lower() for term in refusal_terms)
                for value in negative['synthetic_tool_outputs']))
    identity_keys = ('config_digest', 'executable_sha256', 'policy_digest', 'schema_digest')
    same_identity = bool(positive and negative and len(results) == 2 and all(
        isinstance(positive.get(key), str) and len(positive[key]) == 64
        and all(char in '0123456789abcdef' for char in positive[key])
        and positive[key] == negative.get(key) for key in identity_keys))
    return {'positive_control_executed': positive_executed, 'negative_control_blocked': negative_blocked,
            'same_identity': same_identity, 'read_boundary_observed': positive_executed and negative_blocked and same_identity}


def _assess_matrix(results, *, allow_apply_patch, policy_version, matrix_layout=None) -> dict:
    """The complete v2 five-case and layout rules, selected by frozen policy."""
    policy = policy_for_version(policy_version)
    policy_digest = digest(policy)
    if type(allow_apply_patch) is not bool:
        raise ValueError('Tool policy requires an explicit boolean')
    empty = {'positive_control_executed': False, 'negative_control_blocked': False,
             'same_identity': False, 'read_boundary_observed': False,
             'negative_cases_blocked': {name: False for name in READ_BOUNDARY_CASES[1:]}}
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        return empty
    by_case = {item.get('case'): item for item in results if isinstance(item.get('case'), str)}
    exact = len(results) == len(READ_BOUNDARY_CASES) and set(by_case) == set(READ_BOUNDARY_CASES)
    positive = by_case.get('inside')
    matrix_layout = positive.get('matrix_layout') if matrix_layout is None and positive else matrix_layout
    layout_valid = _valid_matrix_layout(matrix_layout)
    targets = [item.get('synthetic_target') for item in results]
    distinct_targets = all(isinstance(target, str) for target in targets) and len(set(targets)) == len(READ_BOUNDARY_CASES)
    def valid_observation(item, *, inside):
        if not isinstance(item, dict) or not layout_valid or not _valid_case_layout(item, matrix_layout):
            return False
        target, work = item.get('synthetic_target'), item.get('allowed_root')
        if not isinstance(target, str) or not isinstance(work, str):
            return False
        target, work = Path(target), Path(work)
        if not target.is_absolute() or not work.is_absolute() or '..' in target.parts or '..' in work.parts:
            return False
        if target.is_relative_to(work) != inside:
            return False
        sha = item.get('target_sha256')
        return (item.get('policy_version') == policy_version
            and item.get('policy_digest') == policy_digest
            and item.get('policy') == policy
            and item.get('target_exists_before') is True
            and type(item.get('target_size_bytes')) is int and 0 < item['target_size_bytes'] <= 1024
            and isinstance(sha, str) and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)
            and sha == item.get('target_before_sha256')
            and item.get('invocation_started') is True and item.get('process_stopped') is True
            and type(item.get('exit_code')) is int and item['exit_code'] == 0
            and type(item.get('request_count')) is int and 2 <= item['request_count'] <= 3
            and type(item.get('image_payloads_in_requests')) is int
            and isinstance(item.get('synthetic_tool_outputs'), list)
            and item.get('blockers') == []
            and item.get('transport_error') is None and item.get('failure') is None)
    blocked = {}
    for name in READ_BOUNDARY_CASES[1:]:
        negative = by_case.get(name)
        pair = [positive, {**negative, 'case': 'outside'}] if positive and negative else []
        assessment = _assess_v1(pair, allow_apply_patch=allow_apply_patch)
        blocked[name] = bool(valid_observation(negative, inside=False) and assessment['negative_control_blocked'])
    first_pair = [positive, by_case['outside']] if positive and 'outside' in by_case else []
    positive_assessment = _assess_v1(first_pair, allow_apply_patch=allow_apply_patch)
    positive_executed = bool(valid_observation(positive, inside=True) and positive_assessment['positive_control_executed'])
    identity_keys = ('config_digest', 'executable_sha256', 'policy_digest', 'schema_digest')
    same_identity = bool(exact and positive and all(
        isinstance(positive.get(key), str) and len(positive[key]) == 64
        and all(char in '0123456789abcdef' for char in positive[key])
        and all(positive[key] == item.get(key) for item in results) for key in identity_keys))
    negative_blocked = exact and distinct_targets and all(blocked.values())
    return {'positive_control_executed': positive_executed,
            'negative_control_blocked': negative_blocked, 'same_identity': same_identity,
            'negative_cases_blocked': blocked, 'matrix_layout_valid': layout_valid,
            'distinct_targets': distinct_targets,
            'read_boundary_observed': positive_executed and negative_blocked and same_identity}


def _synthetic_inner_argv(snapshot, boundary, nonce, denials, *, write_case=None):
    """Pure current-probe reconstruction from closed configuration and fixed stub."""
    if not isinstance(snapshot, dict) or not isinstance(nonce, str) or re.fullmatch(r'[a-f0-9]{32}', nonce) is None:
        raise ValueError('Invalid synthetic invocation identity')
    if (not isinstance(denials, list) or not denials or any(_normalized_path(value) is None for value in denials)
            or denials != sorted(set(denials))
            or not {'/tmp', '/private/tmp', '/var/tmp', '/private/var/tmp'} <= set(denials)):
        raise ValueError('Invalid frozen temporary denials')
    config = ProviderConfig.model_validate(snapshot)
    if config.model_dump(mode='json') != snapshot or config.executable != boundary['native_executable_path']:
        raise ValueError('Synthetic configuration differs from its canonical executable')
    work = boundary['work']
    inner = render_cli_argv(config, work, str(Path(work) / 'output.schema.json'), boundary['response_path'],
                            executable=boundary['native_executable_path'])
    inner = render_read_boundary_argv(inner, work, denials)
    if write_case == 'allow_write_control':
        from .provider_write_probe import _control_argv
        inner = _control_argv(inner, Path(work))
    inner = append_context_policy(inner)
    provider = 'paper_alpha_write_challenge' if write_case is not None else 'paper_alpha_challenge'
    name = 'Synthetic local write challenge' if write_case is not None else 'Synthetic local challenge'
    route = 'write-challenge-' if write_case is not None else 'challenge-'
    for value in (f'model_provider="{provider}"', f'model_providers.{provider}.name="{name}"',
            f'model_providers.{provider}.base_url="http://127.0.0.1:{boundary["loopback_port"]}/{route}{nonce}/v1"',
            f'model_providers.{provider}.wire_api="responses"',
            f'model_providers.{provider}.requires_openai_auth=false',
            f'model_providers.{provider}.request_max_retries=0',
            f'model_providers.{provider}.stream_max_retries=0', 'features.enable_request_compression=false'):
        inner.extend(['-c', value])
    return inner


def _outer_case_valid(item):
    if not isinstance(item, dict):
        return False
    boundary, argv = item.get('outer_boundary'), item.get('argv')
    try:
        inner = _synthetic_inner_argv(item.get('config_snapshot'), boundary, item.get('challenge_nonce'), item.get('temporary_read_denials'))
    except (KeyError, TypeError, ValueError):
        return False
    return bool(assess_outer_boundary(boundary, argv)['valid']
        and boundary.get('inner_argv') == inner and digest(item['config_snapshot']) == item.get('config_digest')
        and isinstance(boundary, dict) and boundary.get('purpose') == 'synthetic_read'
        and boundary.get('network_mode') == 'synthetic_loopback'
        and boundary.get('auth_exact_path') is None and boundary.get('synthetic_write_target') is None
        and boundary.get('work') == item.get('allowed_root')
        and boundary.get('cli_state') == str(Path(item['allowed_root']).parent / 'cli-state')
        and boundary.get('response_path') == str(Path(item['allowed_root']) / 'response.raw.json')
        and boundary.get('native_executable_sha256') == item.get('executable_sha256')
        and item.get('argv_digest') == digest(argv))


def assess_cases(results, *, allow_apply_patch=False, policy_version=READ_BOUNDARY_POLICY_VERSION, matrix_layout=None) -> dict:
    """Legacy reports retain their rules; current v3 also proves whole-CLI wrapping."""
    policy_for_version(policy_version)
    if policy_version == 'cli_named_read_boundary_v1':
        return _assess_v1(results, allow_apply_patch=allow_apply_patch)
    result = _assess_matrix(results, allow_apply_patch=allow_apply_patch,
                            policy_version=policy_version, matrix_layout=matrix_layout)
    if policy_version == READ_BOUNDARY_POLICY_VERSION:
        outer = isinstance(results, list) and len(results) == len(READ_BOUNDARY_CASES) and all(
            _outer_case_valid(item) for item in results)
        result['outer_boundary_verified'] = bool(outer)
        result['read_boundary_observed'] = bool(result['read_boundary_observed'] and outer)
    return result


def assess_report(report, *, allow_apply_patch=False) -> dict:
    """Interpret saved evidence using its frozen policy, without dispatch authority.

    Historical source hashes are evidence identity, not compared to today's
    source. Current Native dispatch separately requires the current v3 identity.
    """
    if not isinstance(report, dict):
        raise ValueError('Read-boundary report must be an object')
    version = report.get('policy_version')
    policy = policy_for_version(version)
    cases = report.get('cases', [])
    if not isinstance(cases, list) or any(not isinstance(item, dict) for item in cases):
        raise ValueError('Read-boundary cases must be a list of objects')
    result = assess_cases(cases, allow_apply_patch=allow_apply_patch, policy_version=version,
                          matrix_layout=report.get('matrix_layout'))
    identity_matches = (report.get('policy_digest') == digest(policy)
        and type(report.get('challenge_version')) is int
        and report['challenge_version'] == ({'cli_named_read_boundary_v1': 1, 'cli_named_read_boundary_v2': 2, READ_BOUNDARY_POLICY_VERSION: 3}[version])
        and report.get('process_stopped') is True and not report.get('error')
        and type(report.get('real_model_invocations')) is int and report['real_model_invocations'] == 0
        and report.get('real_dispatch_enabled') is False
        and all(item.get('policy_version') == version and item.get('policy_digest') == digest(policy)
                and item.get('policy') == policy
                and item.get('config_digest') == report.get('config_digest')
                and item.get('schema_digest') == report.get('schema_digest') for item in cases))
    if version in ('cli_named_read_boundary_v2', READ_BOUNDARY_POLICY_VERSION):
        identity_matches = identity_matches and report.get('temporary_targets_cleaned') is True
        identity_matches = identity_matches and report.get('real_provider_requests') == 0 and report.get('credential_reads') == 0
        layout = report.get('matrix_layout')
        identity_matches = identity_matches and _valid_matrix_layout(layout)
        identity_matches = identity_matches and report.get('matrix_layout_digest') == digest(layout)
        identity_matches = identity_matches and all(item.get('matrix_layout') == layout
            and item.get('matrix_layout_digest') == report.get('matrix_layout_digest') for item in cases)
        cleanup = report.get('temporary_target_cleanup')
        identity_matches = identity_matches and isinstance(cleanup, list) and len(cleanup) == 3
        cleanup_items = cleanup if isinstance(cleanup, list) else []
        identity_matches = identity_matches and all(isinstance(item, dict) and set(item) == {'directory', 'removed'}
            and item.get('removed') is True and _normalized_path(item.get('directory')) is not None for item in cleanup_items)
        if identity_matches:
            expected_dirs = {str(Path(item['synthetic_target']).parent) for item in cases
                             if item.get('case') in READ_BOUNDARY_CASES[2:]}
            identity_matches = (len(expected_dirs) == 3
                and {item['directory'] for item in cleanup_items} == expected_dirs)
    if version == READ_BOUNDARY_POLICY_VERSION:
        outer_identity = report.get('outer_identity')
        identity_matches = identity_matches and isinstance(outer_identity, dict)
        identity_matches = identity_matches and digest(report.get('config_snapshot')) == report.get('config_digest')
        identity_matches = identity_matches and all(item.get('config_snapshot') == report.get('config_snapshot')
            and item.get('temporary_read_denials') == report.get('temporary_read_denials') for item in cases)
        identity_matches = identity_matches and all(
            outer_evidence_matches_identity(item.get('outer_boundary'), outer_identity) for item in cases)
    result['report_identity_matches'] = identity_matches
    result['read_boundary_observed'] = result['read_boundary_observed'] and identity_matches
    return result


def _external_target(root, label, seed, created):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError('Synthetic target root must be an existing unlinked directory')
    directory = Path(tempfile.mkdtemp(prefix='paper-alpha-read-' + label + '-', dir=root))
    created.append(directory)
    target = directory / 'synthetic.png'
    target.write_bytes(_png(seed))
    return target


def _cleanup_targets(created):
    summaries = []
    for directory in created:
        removed = False
        try:
            if directory.is_symlink():
                raise ValueError('Linked synthetic directory')
            target = directory / 'synthetic.png'
            if target.is_symlink():
                raise ValueError('Linked synthetic image')
            target.unlink(missing_ok=True)
            directory.rmdir()
            removed = not directory.exists()
        except (OSError, ValueError):
            pass
        summaries.append({'directory': str(directory), 'removed': removed})
    return summaries


async def run(executable, model, output_dir, *, max_seconds=12, config: ProviderConfig | None = None,
              allow_apply_patch=False, control: ProviderCallControl | None = None):
    if type(allow_apply_patch) is not bool:
        raise ValueError('Tool policy requires an explicit boolean')
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= MAX_PROBE_SECONDS:
        raise ValueError('Probe duration must be positive and at most 12 seconds')
    if config is None:
        config = ProviderConfig(config_id='synthetic-read-boundary', mode='codex_cli', enabled=True,
                                executable=str(Path(executable).resolve(strict=True)), model=model)
    else:
        config = ProviderConfig.model_validate(config)
    output_dir = Path(output_dir).absolute()
    if '..' in output_dir.parts or any(path.is_symlink() for path in (output_dir, *output_dir.parents)):
        raise ValueError('Output path must be unlinked and new')
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    image = output_dir / 'synthetic-outside.png'
    image.write_bytes(_png(71))
    results, error, cancelled, case_pending, all_stopped = [], None, False, False, True
    created, cleanup = [], []
    layout = _matrix_layout(output_dir)
    targets = {'inside': image, 'outside': image}
    try:
        # Preserve the original sibling tmp target; additional negatives expand
        # evidence coverage and never replace the old failed layout.
        for case in READ_BOUNDARY_CASES:
            if control is not None and (control.cancel_event.is_set() or time.monotonic() >= control.deadline_monotonic):
                error = {'code': 'CANCELLED' if control.cancel_event.is_set() else 'TIMEOUT'}
                break
            if case == 'unrelated_tmp':
                targets[case] = _external_target('/private/tmp' if Path('/private/tmp').is_dir() else '/tmp', case, 72, created)
            elif case == 'declared_tmpdir':
                declared = os.environ.get('TMPDIR')
                if not declared or not Path(declared).is_absolute() or '..' in Path(declared).parts:
                    raise ValueError('Declared absolute TMPDIR required for qualification')
                targets[case] = _external_target(declared, case, 73, created)
            elif case == 'desktop':
                targets[case] = _external_target(Path.home() / 'Desktop', case, 74, created)
            case_pending = True
            options = {'control': control} if control is not None else {}
            value = await _challenge(config, output_dir, case, targets[case], max_seconds=max_seconds,
                                     allow_apply_patch=allow_apply_patch, matrix_layout=layout, **options)
            case_pending = False
            results.append(value)
            all_stopped = all_stopped and value.get('process_stopped') is True
            if not all_stopped:
                error = {'code': 'OUTCOME_UNKNOWN'}
                break
            if case == 'inside' and not (_assess_v1([value], allow_apply_patch=allow_apply_patch)['positive_control_executed']
                    and type(value.get('exit_code')) is int and value['exit_code'] == 0
                    and not value.get('transport_error') and not value.get('failure') and _outer_case_valid(value)):
                error = {'code': 'POSITIVE_CONTROL_NOT_EXECUTED'}
                break
    except asyncio.CancelledError as exc:
        cancelled = True
        if isinstance(getattr(exc, 'observation', None), dict):
            results.append(exc.observation)
            case_pending = False
            all_stopped = all_stopped and exc.observation.get('process_stopped') is True
        error = {'code': 'INTERRUPTED'}
    except (OSError, ValueError) as exc:
        error = {'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None)}
    finally:
        cleanup = _cleanup_targets(created)
    assessment = assess_cases(results, allow_apply_patch=allow_apply_patch, matrix_layout=layout)
    targets_cleaned = all(item['removed'] for item in cleanup)
    if not targets_cleaned:
        error = {'code': 'SYNTHETIC_TARGET_CLEANUP_UNCONFIRMED'}
    report = {'challenge_version': 3, 'evidence_kind': 'synthetic_native_tool_challenge',
              'real_model_invocations': 0, 'real_provider_requests': 0, 'credential_reads': 0,
              'real_dispatch_enabled': False, 'process_stopped': not case_pending and all_stopped,
              'allow_apply_patch': allow_apply_patch, 'outer_identity': outer_boundary_identity(), 'temporary_read_denials': list(temporary_read_denials()), 'matrix_layout': layout, 'matrix_layout_digest': digest(layout),
              **assessment, 'policy_version': READ_BOUNDARY_POLICY_VERSION, 'policy_digest': READ_BOUNDARY_POLICY_DIGEST,
              'config_digest': digest(config.model_dump()), 'config_snapshot': config.model_dump(mode='json'), 'schema_digest': digest(OUTPUT_SCHEMA),
              'schema_scope': 'synthetic_done_response',
              'helper_sha256': hashlib.sha256((ROOT / 'paper_alpha/server/provider_read_boundary.py').read_bytes()).hexdigest(),
              'cases': results, 'error': error, 'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'resource_limits': {'max_cli_probe_processes': 5, 'max_seconds_each': max_seconds,
                                  'max_request_bytes': MAX_REQUEST_BYTES, 'max_requests_per_case': 3},
              'cli_probe_processes_started': sum(item.get('invocation_started') is True for item in results),
              'temporary_targets_cleaned': targets_cleaned, 'temporary_target_cleanup': cleanup,
              'limitations': ['This local synthetic challenge does not prove backend or model compatibility.',
                              'Only the native view_image handler and the exact named policy are exercised.']}
    report['read_boundary_observed'] = report['read_boundary_observed'] and not error and targets_cleaned
    _atomic_bytes(output_dir / 'report.json', (canonical_json(report) + '\n').encode())
    if cancelled:
        interrupted = asyncio.CancelledError()
        interrupted.report = report
        raise interrupted
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--max-seconds', type=int, default=12)
    args = parser.parse_args(argv)
    if not 1 <= args.max_seconds <= MAX_PROBE_SECONDS:
        parser.error('max-seconds must be in 1..12')
    report = asyncio.run(run(args.executable, args.model, args.output_dir, max_seconds=args.max_seconds))
    print(json.dumps({key: report[key] for key in ('evidence_kind', 'real_model_invocations',
        'positive_control_executed', 'negative_control_blocked', 'read_boundary_observed')}, indent=2))
    return 0 if report['read_boundary_observed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
