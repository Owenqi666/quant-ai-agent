"""Pure projection of one business App Server turn; no I/O or authority.

Raw stdout is replayed through the original protocol. A successful protocol
turn alone does not authorize business tools or prove owned process cleanup.
"""
from __future__ import annotations

import hashlib
import re

from .provider_app_server_protocol import AppServerProtocol, AppServerProtocolError
from .provider_contract import canonical_json, digest, strict_json
from .provider_schema import ProviderUsage
from .provider_supervision_journal import validate_stop

CAPTURE_VERSION = 'business_app_server_capture_v1'
CAPTURE_VERSION_V2 = 'business_app_server_capture_v2'
CAPTURE_VERSION_V3 = 'business_app_server_capture_v3'
_TRANSPORT_CAPTURE_VERSIONS = {
    'codex_app_server_business_stdio_v1': CAPTURE_VERSION,
    'codex_app_server_business_stdio_v2': CAPTURE_VERSION,
    'codex_app_server_business_stdio_v3': CAPTURE_VERSION,
    'codex_app_server_business_stdio_v4': CAPTURE_VERSION_V2,
    'codex_app_server_business_stdio_v5': CAPTURE_VERSION_V2,
    'codex_app_server_business_stdio_v6': CAPTURE_VERSION_V2,
    'codex_app_server_business_stdio_v7': CAPTURE_VERSION_V3,
    'codex_app_server_business_stdio_v8': CAPTURE_VERSION_V3,
    'codex_app_server_business_stdio_v9': CAPTURE_VERSION_V3,
    'codex_app_server_business_stdio_v10': CAPTURE_VERSION_V3,
    'codex_app_server_business_stdio_v11': CAPTURE_VERSION_V3,
}
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 65536
BUSINESS_ITEMS = frozenset({'userMessage', 'reasoning', 'agentMessage'})
_SUPERVISION_KEYS = {'phase', 'term_sent', 'kill_sent', 'error_errno',
                     'error_count', 'remaining_streams'}
_SUPERVISION_PHASES = {None, 'term', 'term_group_observation', 'kill', 'reap',
                       'final_group_observation'}


class AppServerCaptureError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def capture_version_for_transport(version):
    # The expected runtime transport comes from the code-owned context owner.
    # Synthetic and exec transports can never be projected as business turns.
    if type(version) is not str or version not in _TRANSPORT_CAPTURE_VERSIONS:
        raise AppServerCaptureError('APP_SERVER_CAPTURE_TRANSPORT_INVALID')
    return _TRANSPORT_CAPTURE_VERSIONS[version]


def _business_items(message, *, allow_null_client_id=False, allow_null_memory_citation=False):
    params = message.get('params', {})
    if type(params) is not dict:
        params = {}
    items = []
    if message.get('method') in {'item/started', 'item/completed'}:
        items.append(params.get('item'))
    if message.get('method') in {'turn/started', 'turn/completed'}:
        turn = params.get('turn')
        if type(turn) is dict:
            items.extend(turn.get('items', []))
    if type(message.get('result')) is dict:
        turn = message['result'].get('turn')
        if type(turn) is dict:
            items.extend(turn.get('items', []))
    for item in items:
        if type(item) is not dict or item.get('type') not in BUSINESS_ITEMS:
            return 'APP_SERVER_BUSINESS_TOOL_FORBIDDEN'
        if item['type'] == 'agentMessage':
            agent_keys = {'id', 'type', 'text', 'phase'}
            if allow_null_memory_citation:
                agent_keys.add('memoryCitation')
            if (set(item) - agent_keys or allow_null_memory_citation
                    and item.get('memoryCitation') is not None):
                return 'APP_SERVER_BUSINESS_ITEM_INVALID'
        if item['type'] == 'reasoning':
            if set(item) - {'id', 'type', 'summary', 'content'} or any(
                    type(item[key]) is not list or any(type(value) is not str for value in item[key])
                    for key in ('summary', 'content') if key in item):
                return 'APP_SERVER_BUSINESS_ITEM_INVALID'
        if item['type'] == 'userMessage':
            user_keys = {'id', 'type', 'content'}
            if allow_null_client_id:
                user_keys.add('clientId')
            if (set(item) - user_keys or type(item.get('content', [])) is not list
                    or allow_null_client_id and item.get('clientId') is not None):
                return 'APP_SERVER_BUSINESS_ITEM_INVALID'
            for content in item.get('content', []):
                if (type(content) is not dict or set(content) - {'type', 'text', 'text_elements'}
                        or content.get('type') != 'text' or type(content.get('text')) is not str
                        or content.get('text_elements', []) != []):
                    return 'APP_SERVER_BUSINESS_ITEM_INVALID'
    return None


def _supervision(value, stop):
    """Validate the original supervisor's closed cleanup diagnostics.

    Diagnostics supplement, but never replace, the owned stop/group proof.
    A transient errno can coexist with a later confirmed absence observation.
    """
    if (type(value) is not dict or set(value) != _SUPERVISION_KEYS
            or value['phase'] is not None and type(value['phase']) is not str
            or value['phase'] not in _SUPERVISION_PHASES
            or type(value['term_sent']) is not bool or type(value['kill_sent']) is not bool
            or type(value['error_count']) is not int or not 0 <= value['error_count'] <= 4096
            or value['error_errno'] is not None and (
                type(value['error_errno']) is not int or not 1 <= value['error_errno'] <= 4095)
            or value['error_count'] == 0 and value['error_errno'] is not None
            or type(value['remaining_streams']) is not list
            or any(type(stream) is not str or stream not in {'stdout', 'stderr'} for stream in value['remaining_streams'])
            or value['remaining_streams'] != sorted(set(value['remaining_streams']))):
        raise AppServerCaptureError('APP_SERVER_CAPTURE_SUPERVISION_INVALID')
    if stop.get('invocation_started') is True:
        if value['phase'] is None or (stop.get('process_stopped') is True and (
                value['phase'] not in {'reap', 'final_group_observation'} or value['remaining_streams'])):
            raise AppServerCaptureError('APP_SERVER_CAPTURE_SUPERVISION_INVALID')
    elif (value['phase'] is not None or value['term_sent'] or value['kill_sent']
          or value['error_count'] or value['remaining_streams']):
        raise AppServerCaptureError('APP_SERVER_CAPTURE_SUPERVISION_INVALID')
    return value


def project_business_capture(*, plan, stdout, stderr, response, rpc_result, stop,
                             transport_version):
    """Return a closed projection; invalid owned evidence raises a stable error.

    Platform/protocol failure returns valid=False, with its actual stable code.
    Reported token totals map to the existing DTO; missing usage stays unknown.
    Neither one turn nor a successful exit implies a backend model-call count.
    """
    capture_version = capture_version_for_transport(transport_version)
    projection_v2 = capture_version in {CAPTURE_VERSION_V2, CAPTURE_VERSION_V3}
    if (type(stdout) is not bytes or type(stderr) is not bytes
            or type(rpc_result) is not bytes or (response is not None and type(response) is not bytes)
            or len(stdout) + len(stderr) > MAX_CAPTURE_BYTES or len(rpc_result) > 65536
            or response is not None and len(response) > MAX_RESPONSE_BYTES):
        raise AppServerCaptureError('APP_SERVER_CAPTURE_LIMIT')
    if type(plan) is not dict or plan.get('allow_file_changes') is not False:
        raise AppServerCaptureError('APP_SERVER_BUSINESS_PLAN_INVALID')
    try:
        machine = AppServerProtocol(plan)
        supplied = strict_json(rpc_result, max_bytes=65536)
        if type(supplied) is not dict or type(stop) is not dict:
            raise ValueError('Invalid object')
        if (stop.get('protocol_version') != 2 or type(stop.get('protocol_version')) is not int
                or stop.get('transport') != 'app_server'
                or _sha(rpc_result) != stop.get('rpc_result_sha256')
                or _sha(stdout) != stop.get('stdout_sha256') or len(stdout) != stop.get('stdout_bytes')
                or _sha(stderr) != stop.get('stderr_sha256') or len(stderr) != stop.get('stderr_bytes')
                or type(stop.get('rpc_completed')) is not bool
                or supplied.get('completed') is not stop['rpc_completed']
                or type(stop.get('response_saved')) is not bool
                or stop['response_saved'] is not (response is not None)
                or stop.get('response_sha256') != (_sha(response) if response is not None else None)):
            raise ValueError('Evidence differs')
    except (ValueError, TypeError, KeyError, AppServerProtocolError) as exc:
        raise AppServerCaptureError('APP_SERVER_CAPTURE_EVIDENCE_MISMATCH') from exc
    error = None
    machine.start()
    turns_started = 0
    business_error = None
    try:
        for line in stdout.splitlines(keepends=True):
            message = strict_json(line, max_bytes=1048576)
            if type(message) is dict:
                business_error = business_error or _business_items(
                    message, allow_null_client_id=projection_v2,
                    allow_null_memory_citation=capture_version == CAPTURE_VERSION_V3)
                if message.get('method') == 'turn/started':
                    turns_started += 1
            machine.feed(line)
        machine.finish_eof()
    except AppServerProtocolError as exc:
        error = exc.code
    except (ValueError, TypeError, KeyError, RecursionError):
        error = 'APP_SERVER_INVALID_JSON'
    replay = machine.summary()
    if set(supplied) != set(replay) | {'supervision'}:
        raise AppServerCaptureError('APP_SERVER_CAPTURE_RPC_DRIFT')
    supervision = _supervision(supplied['supervision'], stop)
    protocol_projection = {key: supplied[key] for key in replay}
    if canonical_json(replay) != canonical_json(protocol_projection):
        raise AppServerCaptureError('APP_SERVER_CAPTURE_RPC_DRIFT')
    if response is not None and machine.response != response:
        raise AppServerCaptureError('APP_SERVER_CAPTURE_RESPONSE_DRIFT')
    stopped = stop.get('process_stopped') is True
    try:
        entry = {'schema_version': 2, 'protocol_version': 2, 'transport': 'app_server',
                 'phase': 'app_server', 'nonce': stop['nonce'],
                 'deadline_monotonic': stop['deadline_monotonic'], 'output_limit': MAX_CAPTURE_BYTES}
        validate_stop(stop, entry)
        if (type(stop['nonce']) is not str or re.fullmatch('[a-f0-9]{32}', stop['nonce']) is None
                or stop['started_monotonic'] > stop['deadline_monotonic']):
            raise ValueError('Deadline differs')
    except (ValueError, KeyError, TypeError):
        stopped = False
        error = 'OUTCOME_UNKNOWN'
    if stopped:
        # V1 is the frozen historical projection, including its original
        # business-first error precedence. V2 records that diagnosis separately
        # and keeps the original RPC/lifecycle failure as the primary result.
        error = ((error or stop.get('reason') or stop.get('response_error'))
                 if projection_v2 else
                 (business_error or error or stop.get('reason') or stop.get('response_error')))
        if error is None and stop['finished_monotonic'] > stop['deadline_monotonic']:
            error = 'TIMEOUT'
        if error is None and stop.get('exit_code') != 0:
            error = 'APP_SERVER_EXIT_NONZERO'
        if error is None and (not machine.complete or response is None or stop.get('invocation_started') is not True):
            error = 'APP_SERVER_INCOMPLETE'
    primary_error = error
    if projection_v2:
        error = primary_error or business_error
    usage = ProviderUsage(observed_turns=1 if machine.complete or turns_started else 0)
    reported = replay.get('usage')
    if reported is not None:
        total = reported['total']
        usage = ProviderUsage(input_tokens=total['inputTokens'], output_tokens=total['outputTokens'],
            cached_input_tokens=total['cachedInputTokens'], reasoning_tokens=total['reasoningOutputTokens'],
            observed_turns=usage.observed_turns, complete=error is None,
            raw=[{'source': 'app_server.thread/tokenUsage/updated', 'dto_scope': 'reported_total',
                  'reported': reported}])
    result = {'capture_version': capture_version, 'transport_version': transport_version,
            'valid': error is None, 'error': error, 'process_stopped': stopped,
            'protocol_completed': machine.complete, 'thread_id': replay.get('thread_id'),
            'turn_id': replay.get('turn_id'), 'response_sha256': _sha(response) if response is not None else None,
            'rpc_result_sha256': _sha(rpc_result), 'plan_digest': digest(plan),
            'protocol_summary': replay, 'supervision': supervision,
            'usage': usage.model_dump(), 'reported_model': None}
    if projection_v2:
        result.update(primary_error=primary_error, secondary_capture_error=business_error)
    return result
