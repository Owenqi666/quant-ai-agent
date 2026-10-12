"""Pure, bounded interpretation of a synthetic CLI schema canary.

No process, filesystem, authentication or dispatch authority lives here. Inputs
are the exact captured bytes; they are hashed, never repaired or supplemented.
An accepted synthetic response establishes this response's shape, not research
quality, a production tool boundary or general model compatibility. JSONL usage
is observed information: missing usage, cost and model-call counts stay unknown.
Unknown event/item types prevent a claim that no tool activity was observed.
The caller owns preserving raw artifacts and proving execution containment.
"""
from __future__ import annotations

import hashlib
import re

from .provider_contract import parse_model_output, strict_json
from .provider_schema import ProposalModelOutput

MAX_LOG_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 65536
MAX_EVENTS = 4096
_EVENT_TYPES = {'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                'error', 'item.started', 'item.updated', 'item.completed'}
_TEXT_ITEMS = {'agent_message', 'reasoning'}
_TOOL_ITEMS = {'command_execution', 'file_change', 'mcp_tool_call', 'web_search',
               'todo_list', 'plan_update', 'update_plan', 'request_user_input',
               'view_image', 'image_view', 'image_generation', 'collab_agent_tool_call'}
# CLI 0.145.0 exposes cacheWriteInputTokens as a separate int64 in its
# TokenUsageBreakdown schema and cache_write_input_tokens in exec JSONL.
# Preserve that observation independently; never add it to input or infer cost.
_USAGE_FIELDS = {'input_tokens', 'output_tokens', 'cached_input_tokens',
                 'cache_write_input_tokens', 'reasoning_output_tokens'}
_ERROR_CODES = {
    'AUTH_REQUIRED': {'invalid_api_key', 'authentication_error', 'unauthorized',
        'authentication_required', 'token_expired', 'invalid_authentication_token', 'access_token_expired'},
    'MODEL_UNAVAILABLE': {'model_not_found', 'model_not_available', 'unsupported_model',
        'model_access_denied', 'model_not_supported'},
    'SCHEMA_REJECTED': {'invalid_json_schema', 'unsupported_json_schema', 'invalid_response_format',
        'invalid_schema', 'unsupported_schema', 'response_format_schema_invalid'},
    'NETWORK_ERROR': {'connection_error', 'network_error', 'api_connection_error', 'request_timeout',
        'connect_timeout', 'dns_error', 'timeout'},
}
_SAFE_CODES = set().union(*_ERROR_CODES.values()) | {'invalid_request_error', 'rate_limit_exceeded', 'insufficient_quota'}
_SAFE_TYPES = _SAFE_CODES | {'error', 'permission_error', 'not_found_error', 'server_error', 'api_error', 'rate_limit_error'}
_SAFE_PARAMS = {'model', 'response_format', 'response_format.json_schema', 'text.format', 'text.format.schema'}
_CODE_REASONS = {
    'invalid_api_key': 'invalid_credentials', 'invalid_authentication_token': 'invalid_credentials',
    'authentication_required': 'authentication_required',
    'token_expired': 'authentication_expired', 'access_token_expired': 'authentication_expired',
    'model_access_denied': 'model_access_denied',
    'unsupported_model': 'model_unsupported', 'model_not_supported': 'model_unsupported',
    'invalid_json_schema': 'schema_invalid', 'invalid_schema': 'schema_invalid',
    'response_format_schema_invalid': 'schema_invalid', 'invalid_response_format': 'response_format_invalid',
    'unsupported_json_schema': 'schema_unsupported', 'unsupported_schema': 'schema_unsupported',
    'connection_error': 'network_connection_failed', 'api_connection_error': 'network_connection_failed',
    'request_timeout': 'network_timeout', 'connect_timeout': 'network_timeout', 'timeout': 'network_timeout',
    'dns_error': 'network_dns_failure',
}


def build_canary_output(proposal_id, attempt_id, context_digest) -> dict:
    """The exact synthetic target, including the ordinary frozen v1 identity."""
    return ProposalModelOutput.model_validate({
        'schema_version': 1, 'proposal_id': proposal_id, 'attempt_id': attempt_id,
        'context_digest': context_digest, 'outcome': 'abstain', 'draft': None,
        'abstention': {'reason_code': 'EVIDENCE_INSUFFICIENT',
            'reason': 'Synthetic schema canary; no research evidence was supplied.',
            'missing_requirements': ['configuration:research_evidence'], 'evidence_ids': []},
    }).model_dump()


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else '<invalid>'


def _error_records(value):
    """Only error envelopes are examined; agent messages are not error data."""
    if not isinstance(value, dict):
        return []
    records = []
    if any(key in value for key in ('code', 'message', 'param')):
        records.append(value)
    if isinstance(value.get('error'), dict):
        records.extend(_error_records(value['error']))
    return records


def _classify(records, text):
    # Structured service codes win over incidental wording or HTTP status.
    for record in records:
        codes = [record.get('code'), record.get('type')]
        for category, accepted in _ERROR_CODES.items():
            if any(isinstance(code, str) and code.lower() in accepted for code in codes):
                return category, 'structured_error', next(code for code in codes
                    if isinstance(code, str) and code.lower() in accepted)
        if record.get('type') == 'invalid_request_error' and isinstance(record.get('param'), str) and record['param'] in {
                'response_format', 'response_format.json_schema', 'text.format', 'text.format.schema'}:
            return 'SCHEMA_REJECTED', 'structured_error', 'invalid_request_error'
    messages = [record.get('message', '') for record in records if isinstance(record.get('message'), str)]
    haystack = ('\n'.join(messages) + '\n' + text).lower()
    patterns = (
        ('AUTH_REQUIRED', r'please (?:run|use) codex login|authentication (?:failed|required)|401 unauthorized|access token (?:has )?expired'),
        ('MODEL_UNAVAILABLE', r'model[^\n]{0,160}(?:does not exist|is not (?:available|supported)|not found|do not have access)'),
        ('SCHEMA_REJECTED', r'invalid (?:json )?schema (?:for|in)|invalid_json_schema|unsupported json schema'),
        ('NETWORK_ERROR', r'stream disconnected|connection (?:reset|refused|timed out)|error sending request|dns (?:resolution|lookup) failed'),
    )
    for category, pattern in patterns:
        if re.search(pattern, haystack):
            return category, 'diagnostic_text', None
    return None, None, None


def _diagnostic_evidence(records, text, category, source, remote_code):
    """Keep fixed reasons and known structural labels, never arbitrary prose.

    A model-not-found code alone does not distinguish nonexistence from lack of
    access. Text refines a reason only when the error channel states it clearly.
    """
    def allowed(value, catalog):
        return value.lower() if isinstance(value, str) and value.lower() in catalog else None

    selected = None
    if source == 'structured_error':
        selected = next((record for record in records
            if any(isinstance(record.get(key), str) and record[key] == remote_code for key in ('code', 'type'))), None)
    if selected is None:
        selected = next((record for record in records if any(allowed(record.get(key), catalog)
            for key, catalog in (('code', _SAFE_CODES), ('type', _SAFE_TYPES), ('param', _SAFE_PARAMS)))), {})
    evidence = {'reason': 'unknown', 'match_source': 'none',
        'code': allowed(selected.get('code'), _SAFE_CODES),
        'type': allowed(selected.get('type'), _SAFE_TYPES),
        'param': allowed(selected.get('param'), _SAFE_PARAMS)}
    messages = ([selected.get('message', '')] if source == 'structured_error' else
                [record.get('message', '') for record in records] + [text])
    haystack = '\n'.join(message for message in messages if isinstance(message, str)).lower()
    reason = None
    if category == 'MODEL_UNAVAILABLE':
        account_pattern = (r'model[^\n]{0,160}(?:not supported|not available)\s+'
            r'(?:(?:when\s+)?using\b[^\n.;]{0,80}\bwith\b|for\b|with\b|on\b)'
            r'[^\n.;]{0,60}\b(?:account|subscription|plan)\b')
        ambiguous = bool(re.search(r'\bor\b[^\n]{0,100}(?:access|permission|exist)', haystack))
        if re.search(account_pattern, haystack):
            reason = 'model_not_supported_for_account'
        elif not ambiguous and re.search(r'model[^\n]{0,160}does not exist', haystack):
            reason = 'model_unknown'
        elif not ambiguous and re.search(r'(?:do not have access|access (?:is )?denied)[^\n]{0,80}(?:model)?', haystack):
            reason = 'model_access_denied'
    if reason is not None:
        evidence.update(reason=reason, match_source='diagnostic_text')
        return evidence
    if source == 'structured_error':
        for field in ('code', 'type'):
            if evidence[field] in _CODE_REASONS:
                evidence.update(reason=_CODE_REASONS[evidence[field]], match_source='structured_' + field)
                return evidence
        if evidence['type'] == 'invalid_request_error' and evidence['param'] in _SAFE_PARAMS - {'model'}:
            evidence.update(reason='schema_parameter_rejected', match_source='structured_param')
            return evidence
    text_reasons = {
        'AUTH_REQUIRED': (
            ('authentication_expired', r'access token (?:has )?expired'),
            ('authentication_required', r'please (?:run|use) codex login|authentication required'),
        ),
        'SCHEMA_REJECTED': (
            ('schema_unsupported', r'unsupported json schema'),
            ('schema_invalid', r'invalid (?:json )?schema (?:for|in)|invalid_json_schema'),
        ),
        'NETWORK_ERROR': (
            ('network_stream_disconnected', r'stream disconnected'),
            ('network_timeout', r'connection timed out'),
            ('network_dns_failure', r'dns (?:resolution|lookup) failed'),
            ('network_connection_failed', r'connection (?:reset|refused)|error sending request'),
        ),
    }
    for reason, pattern in text_reasons.get(category, ()):
        if re.search(pattern, haystack):
            evidence.update(reason=reason, match_source='diagnostic_text')
            break
    return evidence


def _events(raw):
    observed_types, item_types, unknown_events, unknown_items = set(), set(), set(), set()
    tool_keys, error_records, usage_records, diagnostics = set(), [], [], []
    started = completed = failed = count = 0
    open_turn = False
    complete = True
    for line in raw.splitlines():
        if not line.strip():
            continue
        count += 1
        if count > MAX_EVENTS:
            complete = False
            diagnostics.append('EVENT_COUNT_LIMIT')
            break
        try:
            event = strict_json(line, max_bytes=MAX_LOG_BYTES)
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise ValueError('Invalid event envelope')
        except (ValueError, TypeError):
            complete = False
            diagnostics.append('INVALID_JSONL')
            continue
        kind = event['type']
        observed_types.add(_label(kind))
        if kind not in _EVENT_TYPES:
            unknown_events.add(_label(kind))
            complete = False
        elif kind == 'turn.started':
            if open_turn:
                complete = False
                diagnostics.append('OVERLAPPING_TURNS')
            started += 1
            open_turn = True
        elif kind == 'turn.completed':
            completed += 1
            if not open_turn:
                complete = False
                diagnostics.append('UNMATCHED_TURN_COMPLETION')
            open_turn = False
            usage_records.append(event.get('usage'))
        elif kind in {'turn.failed', 'error'}:
            failed += 1
            error_records.extend(_error_records(event))
            if kind == 'turn.failed':
                open_turn = False
        elif kind.startswith('item.'):
            item = event.get('item')
            if not isinstance(item, dict) or not isinstance(item.get('type'), str):
                unknown_items.add('<invalid>')
                complete = False
                continue
            item_type = item['type']
            item_types.add(_label(item_type))
            if item_type == 'error':
                failed += 1
                error_records.extend(_error_records(item))
            elif item_type in _TOOL_ITEMS:
                item_id = item.get('id')
                tool_keys.add((item_type, item_id if isinstance(item_id, str) else str(count)))
            elif item_type not in _TEXT_ITEMS:
                unknown_items.add(_label(item_type))
                complete = False
    complete = bool(complete and started and started == completed and not open_turn and not failed)
    totals = {field: None for field in _USAGE_FIELDS}
    field_values = {field: [] for field in _USAGE_FIELDS}
    usage_complete = bool(complete and len(usage_records) == completed)
    unknown_usage = set()
    valid_records = 0
    for record in usage_records:
        if not isinstance(record, dict):
            usage_complete = False
            continue
        unknown_usage.update(_label(key) for key in set(record) - _USAGE_FIELDS)
        if not {'input_tokens', 'output_tokens'}.issubset(record):
            usage_complete = False
        invalid = False
        for field in _USAGE_FIELDS:
            if field not in record:
                continue
            value = record[field]
            if type(value) is not int or not 0 <= value <= 10**12:
                usage_complete = False
                invalid = True
                continue
            field_values[field].append(value)
        if not invalid:
            valid_records += 1
    for field, values in field_values.items():
        if complete and len(values) == completed and completed:
            totals[field] = sum(values)
        elif values:
            # A sum of only the turns that happened to report this field is not
            # an attempt total. Entirely unreported optional fields stay null.
            usage_complete = False
    if unknown_usage:
        usage_complete = False
    return {
        'event_stream_complete': complete, 'event_count': count,
        'observed_event_types': sorted(observed_types), 'observed_item_types': sorted(item_types),
        'unknown_event_types': sorted(unknown_events), 'unknown_item_types': sorted(unknown_items),
        'tool_events_observed': len(tool_keys),
        'tool_observation_complete': not unknown_events and not unknown_items and complete,
        'turns_started': started, 'turns_completed': completed, 'failure_events': failed,
        'diagnostics': sorted(set(diagnostics)),
        'usage': {'input_tokens': totals['input_tokens'], 'output_tokens': totals['output_tokens'],
            'cached_input_tokens': totals['cached_input_tokens'],
            'cache_write_input_tokens': totals['cache_write_input_tokens'],
            'reasoning_tokens': totals['reasoning_output_tokens'], 'complete': usage_complete,
            'usage_events_observed': valid_records, 'unknown_fields': sorted(unknown_usage),
            'model_calls': None, 'cost': None},
    }, error_records


def summarize_cli_capture(*, stdout: bytes, stderr: bytes, exit_code: int | None,
                          process_stopped: bool, transport_error: str | None) -> dict:
    """Strict transport-only projection shared with the native research pilot.

    This never interprets model prose as service diagnostics or approves a
    research response. Only bounded structural labels and numeric usage leave
    this function; the ordinary worker validates the exact response separately.
    """
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes) or type(process_stopped) is not bool:
        raise ValueError('Invalid capture types')
    limited = len(stdout) + len(stderr) > MAX_LOG_BYTES
    events, records = _events(b'' if limited else stdout)
    text = stderr[:MAX_LOG_BYTES].decode('utf-8', errors='replace')
    if not limited:
        for line in stderr.splitlines():
            if line.lstrip().startswith(b'{'):
                try:
                    records.extend(_error_records(strict_json(line, max_bytes=MAX_LOG_BYTES)))
                except (ValueError, TypeError):
                    pass
    classified, source, remote_code = _classify(records, text)
    error = None
    if not process_stopped or transport_error in {'SUPERVISION_UNCONFIRMED', 'PROCESS_CLEANUP_FAILED', 'OUTCOME_UNKNOWN'}:
        error = 'OUTCOME_UNKNOWN'
    elif limited:
        error = 'OUTPUT_LIMIT'
    elif transport_error is not None:
        error = transport_error if transport_error in {'TIMEOUT', 'CANCELLED', 'OUTPUT_LIMIT'} else classified or 'CLI_FAILED'
    elif classified:
        error = classified
    elif events['tool_events_observed'] or events['unknown_item_types'] or events['unknown_event_types']:
        error = 'UNEXPECTED_TOOL_ACTIVITY'
    elif exit_code != 0 or events['failure_events']:
        error = 'CLI_FAILED'
    elif not events['event_stream_complete']:
        error = 'INCOMPLETE_EVENT_STREAM'
    if not process_stopped or transport_error is not None or exit_code != 0:
        events['usage']['complete'] = False
        for field in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                      'cache_write_input_tokens', 'reasoning_tokens'):
            events['usage'][field] = None
    return {'error': error,
        'diagnostic_evidence': _diagnostic_evidence(records, text, classified, source, remote_code),
        'events': {key: value for key, value in events.items() if key != 'usage'}, 'usage': events['usage'],
        'raw_sha256': {'stdout': hashlib.sha256(stdout).hexdigest(), 'stderr': hashlib.sha256(stderr).hexdigest()},
        'captured_bytes': {'stdout': len(stdout), 'stderr': len(stderr)}}


def evaluate_canary_result(*, stdout: bytes, stderr: bytes, response: bytes | None,
                           exit_code: int | None, process_stopped: bool,
                           transport_error: str | None, proposal_id: str,
                           attempt_id: str, context_digest: str) -> dict:
    """Classify one captured attempt without performing or authorizing a call."""
    if (not isinstance(stdout, bytes) or not isinstance(stderr, bytes)
            or (response is not None and not isinstance(response, bytes))
            or type(process_stopped) is not bool
            or (exit_code is not None and type(exit_code) is not int)
            or (transport_error is not None and (not isinstance(transport_error, str)
                or not re.fullmatch(r'[A-Z0-9_]{1,80}', transport_error)))):
        raise ValueError('Canary capture arguments violate the bounded input contract')
    expected = build_canary_output(proposal_id, attempt_id, context_digest)
    log_limit = len(stdout) + len(stderr) > MAX_LOG_BYTES
    events, records = _events(stdout if not log_limit else b'')
    diagnostic_text = stderr[:MAX_LOG_BYTES].decode('utf-8', errors='replace')
    for line in stderr.splitlines() if not log_limit else []:
        if line.lstrip().startswith(b'{'):
            try:
                records.extend(_error_records(strict_json(line, max_bytes=MAX_LOG_BYTES)))
            except (ValueError, TypeError):
                pass
    classified, error_source, remote_code = _classify(records, diagnostic_text)
    diagnostic_evidence = _diagnostic_evidence(records, diagnostic_text, classified, error_source, remote_code)
    parsed, response_error = None, None
    if response is not None:
        try:
            parsed = parse_model_output(response, proposal_id=proposal_id, attempt_id=attempt_id,
                context_digest=context_digest, candidate_limit=1, max_bytes=MAX_RESPONSE_BYTES).model_dump()
        except (ValueError, TypeError) as exc:
            response_error = getattr(exc, 'code', 'INVALID_RESPONSE')
    matched = parsed == expected
    if parsed is not None and not matched:
        response_error = 'SYNTHETIC_RESPONSE_MISMATCH'
    elif response is None:
        response_error = 'MISSING_RESPONSE'
    error = None
    if not process_stopped or transport_error in {'SUPERVISION_UNCONFIRMED', 'PROCESS_CLEANUP_FAILED', 'OUTCOME_UNKNOWN'}:
        error = 'OUTCOME_UNKNOWN'
    elif log_limit or (response is not None and len(response) > MAX_RESPONSE_BYTES):
        error = 'OUTPUT_LIMIT'
    elif transport_error is not None:
        error = transport_error if transport_error in {'TIMEOUT', 'CANCELLED', 'OUTPUT_LIMIT'} else classified or 'CLI_FAILED'
    elif classified:
        error = classified
    elif events['tool_events_observed'] or events['unknown_item_types'] or events['unknown_event_types']:
        error = 'UNEXPECTED_TOOL_ACTIVITY'
    elif exit_code != 0 or events['failure_events']:
        error = 'CLI_FAILED'
    elif not matched or not events['event_stream_complete']:
        error = 'INVALID_RESPONSE'
    if not process_stopped or transport_error is not None or exit_code != 0:
        # A complete-looking prefix of stdout does not prove complete usage
        # when the invocation's end remains unknown or was interrupted.
        events['usage']['complete'] = False
        for field in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                      'cache_write_input_tokens', 'reasoning_tokens'):
            events['usage'][field] = None
    return {
        'result_version': 1, 'status': 'unknown' if error == 'OUTCOME_UNKNOWN' else 'failed' if error else 'passed',
        'error': {'code': error, 'source': error_source if error == classified else 'capture',
                  'remote_code': remote_code if error == classified else None} if error else None,
        'diagnostic_evidence': diagnostic_evidence,
        'exit_code': exit_code, 'process_stopped': process_stopped, 'transport_error': transport_error,
        'response_schema_valid': parsed is not None, 'fixed_response_matched': matched,
        'response_validation_error': response_error,
        'parsed_output': parsed,
        'raw_sha256': {name: hashlib.sha256(value).hexdigest() if value is not None else None
            for name, value in (('stdout', stdout), ('stderr', stderr), ('response', response))},
        'captured_bytes': {'stdout': len(stdout), 'stderr': len(stderr),
                           'response': len(response) if response is not None else None},
        'events': {key: value for key, value in events.items() if key != 'usage'},
        'usage': events['usage'],
        'real_dispatch_enabled': False,
    }
