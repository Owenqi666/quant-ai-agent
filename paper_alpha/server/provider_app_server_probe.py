"""Zero-inference App Server compatibility test using original synthetic tools.

Separate transport/version and assessment: never relabel an exec proof. No auth,
no live backend, no business grant. Only self-created files are challenged.
"""
import argparse
import asyncio
import hashlib
import math
import os
from pathlib import Path
import re
import time
import uuid

from .codex_cli_provider import _Capture, _atomic_bytes, _bounded_private_file, _run_bounded, _safe_environment
from .provider_contract import canonical_json, digest, strict_json
from .provider_app_server_invocation import (
    RPC_POLICY, RPC_POLICY_DIGEST, TRANSPORT_VERSION,
    render_synthetic_app_server_argv, synthetic_plan,
    CONTROLLED_RPC_POLICY, CONTROLLED_RPC_POLICY_DIGEST, CONTROLLED_TRANSPORT_VERSION,
    render_controlled_synthetic_app_server_argv,
)
from .provider_outer_sandbox import build_outer_invocation, assess_outer_boundary
from .provider_schema import ProviderCallControl, ProviderConfig
from .provider_read_boundary import READ_BOUNDARY_POLICY, READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION
from .provider_read_probe import (
    _Stub, _png, _matrix_layout, _valid_matrix_layout, _external_target,
    _cleanup_targets, _assess_matrix, _CancellationView, OUTPUT_SCHEMA, PROMPT, READ_BOUNDARY_CASES,
)
from .provider_write_probe import (
    _WriteStub, _target_state, _assess_cases_legacy, CASES,
    BEFORE_CONTENT, BEFORE_SHA256, AFTER_SHA256, CONTROL_POLICY_DIGEST, PROMPT as WRITE_PROMPT,
)

REPORT_VERSION = 'native_app_server_boundary_v1'
CONTROLLED_CONTINUATION_VERSION = 'controlled_app_server_matrix_continuation_v1'


def _controlled_config(config):
    if not isinstance(config, ProviderConfig):
        raise ValueError('PROBE_ACTUAL_CONFIG_INVALID')
    raw = config.model_dump(mode='json')
    if (any(type(raw[key]) is not bool for key in ('enabled', 'compatibility_verified',
            'tool_boundary_verified', 'inference_verified'))
            or raw['mode'] != 'codex_cli' or raw['expected_cli_version'] != '0.145.0'
            or raw['model'] != 'gpt-5.5' or raw['reasoning_effort'] != 'low'):
        raise ValueError('PROBE_ACTUAL_CONFIG_INVALID')
    checked = ProviderConfig.model_validate(raw)
    path = Path(checked.executable)
    if not path.is_absolute() or str(path) != checked.executable or '..' in path.parts:
        raise ValueError('PROBE_ACTUAL_EXECUTABLE_INVALID')
    return checked


def _control_identity(config, control):
    if (not isinstance(control, ProviderCallControl)
            or type(control.deadline_monotonic) not in (int, float)
            or not math.isfinite(control.deadline_monotonic)
            or not isinstance(control.cancel_event, asyncio.Event)
            or type(control.pass_fds) is not tuple
            or any(type(fd) is not int or fd < 0 for fd in control.pass_fds)
            or len(set(control.pass_fds)) != len(control.pass_fds)):
        raise ValueError('PROBE_ORIGINAL_CONTROL_INVALID')
    metadata = []
    for fd in control.pass_fds:
        info = os.fstat(fd)
        metadata.append(dict(zip(('fd', 'dev', 'ino', 'uid', 'gid', 'mode', 'nlink'),
            (fd, info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink))))
    return {'config_digest': digest(config.model_dump(mode='json')),
        'deadline_monotonic': control.deadline_monotonic,
        'pass_fds': list(control.pass_fds), 'fd_metadata': metadata}


def _check_control(config, control, baseline, cancel_event):
    if (control.cancel_event is not cancel_event
            or canonical_json(_control_identity(config, control)) != canonical_json(baseline)):
        raise ValueError('PROBE_ORIGINAL_CONTROL_DRIFT')
    if control.cancel_event.is_set():
        raise ValueError('PROBE_CANCELLED')
    if time.monotonic() >= control.deadline_monotonic:
        raise ValueError('PROBE_TOTAL_DEADLINE_EXPIRED')


def _controller_record(config, control, deadline, case, context_policy_version):
    return {'version': 1, **_control_identity(config, control),
        'case_deadline_monotonic': deadline, 'context_policy_version': context_policy_version,
        'case': case, 'cancelled_after': False}


def _summary_valid(summary):
    keys = {'protocol_version', 'thread_id', 'turn_id', 'turn_status', 'completed',
        'response_bytes', 'usage', 'usage_status', 'error_code', 'frames', 'events',
        'received_bytes', 'buffered_events', 'completed_items', 'supervision'}
    diag = summary.get('supervision') if isinstance(summary, dict) else None
    diag_keys = {'phase','term_sent','kill_sent','error_errno','error_count','remaining_streams'}
    return (isinstance(summary, dict) and set(summary) == keys
        and isinstance(diag,dict) and set(diag) == diag_keys
        and diag['phase'] in (None,'term','term_group_observation','kill','reap','final_group_observation')
        and type(diag['term_sent']) is bool and type(diag['kill_sent']) is bool
        and (diag['error_errno'] is None or type(diag['error_errno']) is int and diag['error_errno'] >= 0)
        and type(diag['error_count']) is int and diag['error_count'] >= 0
        and diag['remaining_streams'] == []
        and type(summary['protocol_version']) is int and summary['protocol_version'] == 1
        and summary['completed'] is True and summary['turn_status'] == 'completed'
        and all(isinstance(summary[key], str) and 0 < len(summary[key]) <= 256
                for key in ('thread_id', 'turn_id'))
        and type(summary['response_bytes']) is int and 0 < summary['response_bytes'] <= 65536
        and summary['error_code'] is None
        and all(type(summary[key]) is int and summary[key] >= 0 for key in
                ('frames', 'events', 'received_bytes', 'buffered_events', 'completed_items'))
        and summary['usage_status'] in ('unknown', 'reported')
        and ((summary['usage'] is None) == (summary['usage_status'] == 'unknown')))


def _events(raw):
    result = []
    for line in raw.splitlines():
        try:
            frame = strict_json(line, max_bytes=1024 * 1024)
            params = frame.get('params') or {}
            item = params.get('item') or {}
            result.append({'method': frame.get('method'), 'id': frame.get('id'),
                           'item_type': item.get('type'), 'has_error': bool(frame.get('error'))})
        except (ValueError, AttributeError):
            result.append({'method': 'UNPARSED_LINE', 'has_error': True})
    return result


def _identity_valid(item, *, controlled_context=False):
    try:
        config = ProviderConfig.model_validate(item['config_snapshot'])
        boundary = item['outer_boundary']
        write = item['case'] in CASES
        renderer = render_controlled_synthetic_app_server_argv if controlled_context else render_synthetic_app_server_argv
        transport = CONTROLLED_TRANSPORT_VERSION if controlled_context else TRANSPORT_VERSION
        policy_digest = CONTROLLED_RPC_POLICY_DIGEST if controlled_context else RPC_POLICY_DIGEST
        expected = renderer(config, port=boundary['loopback_port'],
            nonce=item['challenge_nonce'], write=write)
        plan = synthetic_plan(config, Path(item['allowed_root']),
            WRITE_PROMPT if write else PROMPT, OUTPUT_SCHEMA, write=write)
        actual = item['rpc_plan']
        wrapped = item['argv']
        return (item['transport'] == transport and item['rpc_policy_digest'] == policy_digest
            and config.model_dump(mode='json') == item['config_snapshot']
            and digest(item['config_snapshot']) == item['config_digest']
            and config.executable == boundary['native_executable_path']
            and item['schema_digest'] == digest(OUTPUT_SCHEMA)
            and actual == plan and item['rpc_plan_digest'] == digest(plan)
            and item['protocol_completed'] is True and _summary_valid(item['rpc_summary'])
            and item['argv_digest'] == digest(wrapped)
            and wrapped[:2] == ['/usr/bin/sandbox-exec', '-p'] and wrapped[3] == '--'
            and wrapped[4:] == expected
            and assess_outer_boundary(boundary, wrapped)['valid'] is True
            and boundary['work'] == item['allowed_root']
            and boundary['cli_state'] == str(Path(item['allowed_root']).parent/'cli-state')
            and boundary['response_path'] == str(Path(item['allowed_root'])/'response.raw.json')
            and boundary['purpose'] == ('synthetic_write_control' if item['case'] == 'allow_write_control' else 'synthetic_read')
            and boundary['network_mode'] == 'synthetic_loopback'
            and boundary['auth_exact_path'] is None
            and boundary['synthetic_write_target'] == (item['synthetic_target'] if item['case'] == 'allow_write_control' else None)
            and boundary['native_executable_sha256'] == item['executable_sha256'])
    except (ValueError, KeyError, TypeError, IndexError):
        return False


def assess(cases, layout, cleanup=None, *, controlled_context=False):
    reads = [item for item in cases if item['case'] in READ_BOUNDARY_CASES]
    writes = [item for item in cases if item['case'] in CASES]
    read = _assess_matrix(reads, allow_apply_patch=True, policy_version=READ_BOUNDARY_POLICY_VERSION,
                         matrix_layout=layout)
    write = _assess_cases_legacy(writes, readonly_digest=READ_BOUNDARY_POLICY_DIGEST,
                                control_digest=CONTROL_POLICY_DIGEST)
    read_identity = len(reads) == 5 and all(_identity_valid(item, controlled_context=controlled_context) for item in reads)
    write_identity = len(writes) == 3 and all(_identity_valid(item, controlled_context=controlled_context) for item in writes)
    read['app_server_identity_valid'] = read_identity
    write['app_server_identity_valid'] = write_identity
    read['read_boundary_observed'] = read['read_boundary_observed'] and read_identity
    write['write_boundary_observed'] = write['write_boundary_observed'] and write_identity
    expected_cleanup = sorted(str(Path(item['synthetic_target']).parent) for item in reads
        if item['case'] in ('unrelated_tmp', 'declared_tmpdir', 'desktop'))
    cleanup_valid = (isinstance(cleanup, list) and len(cleanup) == 3
        and sorted(item.get('directory','') for item in cleanup) == expected_cleanup
        and all(item.get('removed') is True for item in cleanup))
    return {'read': read, 'write': write, 'cleanup_confirmed': cleanup_valid,
            'qualified': read['read_boundary_observed'] and write['write_boundary_observed'] and cleanup_valid}


async def challenge(config, base, case, source=None, *, seconds=12, control=None, dispatch_guard=None):
    if case not in (*READ_BOUNDARY_CASES, *CASES) or not 0 < seconds <= 12:
        raise ValueError('Unknown case or duration')
    controlled = control is not None
    renderer = render_controlled_synthetic_app_server_argv if controlled else render_synthetic_app_server_argv
    transport = CONTROLLED_TRANSPORT_VERSION if controlled else TRANSPORT_VERSION
    rpc_policy = CONTROLLED_RPC_POLICY if controlled else RPC_POLICY
    rpc_policy_digest = CONTROLLED_RPC_POLICY_DIGEST if controlled else RPC_POLICY_DIGEST
    private = base / case
    private.mkdir(mode=0o700)
    work, state = private / 'work', private / 'cli-state'
    work.mkdir(mode=0o700)
    context, baseline, original_cancel, controller, environment = None, None, None, None, None
    if control is None:
        state.mkdir(mode=0o700)
    else:
        from .provider_cli_state import prepare_app_server_probe_state, validate_app_server_probe_state
        baseline, original_cancel = _control_identity(config, control), control.cancel_event
        _check_control(config, control, baseline, original_cancel)
        context = prepare_app_server_probe_state(private)
        validate_app_server_probe_state(context)
        state = context['path']
    write = case in CASES
    if write:
        target = private / 'outside-target.txt' if case == 'readonly_outside' else work / 'target.txt'
        target.write_bytes(BEFORE_CONTENT)
    elif case == 'inside':
        target = work / 'allowed.png'
        target.write_bytes(source.read_bytes())
    else:
        target = source
    before = target.read_bytes()
    if control is not None:
        from .provider_app_server_write_intervention_probe import _snapshot
        before_identity, before = _snapshot(target)
    response = work / 'response.raw.json'
    (work / 'output.schema.json').write_text(canonical_json(OUTPUT_SCHEMA))
    nonce = uuid.uuid4().hex
    route = 'write-challenge-' if write else 'challenge-'
    stub = _WriteStub('/' + route + nonce + '/v1/responses', target) if write else _Stub(
        '/' + route + nonce + '/v1/responses', target, allow_apply_patch=True)
    capture = _Capture(stopped=False) if control is not None else _Capture()
    server, recorder = None, None
    arguments, boundary, plan = [], None, None
    failure = None
    try:
        handler = stub.handle
        if control is not None:
            from .provider_app_server_write_intervention_probe import _LoopbackRecorder
            recorder = _LoopbackRecorder()
            recorder.select(stub, private)
            handler = recorder.handle
        server = await asyncio.start_server(handler, '127.0.0.1', 0, limit=16384)
        port = server.sockets[0].getsockname()[1]
        inner = renderer(config, port=port, nonce=nonce, write=write)
        plan = synthetic_plan(config, work, WRITE_PROMPT if write else PROMPT, OUTPUT_SCHEMA, write=write)
        wrapped = build_outer_invocation(inner, work=work, cli_state=state,
            response_path=response, network_mode='synthetic_loopback', loopback_port=port,
            synthetic_write_target=target if case == 'allow_write_control' else None)
        arguments, boundary = wrapped['argv'], wrapped['boundary']
        if control is not None:
            _check_control(config, control, baseline, original_cancel)
            environment = _safe_environment()
            environment.update(HOME=str(state), CODEX_HOME=str(state))
            _atomic_bytes(private/'environment.json', (canonical_json(environment)+'\n').encode())
            validate_app_server_probe_state(context)
            if dispatch_guard is not None:
                dispatch_guard()
            _check_control(config, control, baseline, original_cancel)
        elif dispatch_guard is not None:
            raise ValueError('PROBE_GUARD_REQUIRES_ORIGINAL_CONTROL')
        deadline = min(time.monotonic()+seconds, control.deadline_monotonic) if control else time.monotonic()+seconds
        if control is not None:
            controller = _controller_record(config, control, deadline, case, context['context_policy_version'])
            _atomic_bytes(private/'controller.before.json', (canonical_json(controller)+'\n').encode())
            _atomic_bytes(private/'target.before.bin', before)
            _atomic_bytes(private/'target.before.identity.json', (canonical_json(before_identity)+'\n').encode())
        await _run_bounded(arguments, cwd=work, stdin=b'', deadline=deadline,
            cancel=_CancellationView(stub.cancel, control.cancel_event) if control else stub.cancel,
            output_limit=1024*1024, capture=capture,
            supervision_dir=private/'supervision', response_path=response,
            response_limit=65536, isolated_cli_home=state, app_server_plan=plan,
            **({'pass_fds': control.pass_fds} if control is not None else {}))
        if control is not None:
            if dispatch_guard is not None:
                dispatch_guard()
            _check_control(config, control, baseline, original_cancel)
            validate_app_server_probe_state(context)
            current_environment = _safe_environment()
            current_environment.update(HOME=str(state), CODEX_HOME=str(state))
            if current_environment != environment:
                raise ValueError('PROBE_ENVIRONMENT_DRIFT')
    except asyncio.CancelledError:
        if control is None:
            raise
        failure = {'type': 'CancelledError', 'code': 'INTERRUPTED', 'errno': None}
        stub.blockers.add('INTERRUPTED')
    except (OSError, ValueError) as exc:
        failure = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None), 'errno': getattr(exc, 'errno', None)}
        stub.blockers.add('PROBE_EXECUTION_FAILED')
    finally:
        if server:
            server.close()
            await server.wait_closed()
        tasks = list(stub.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if recorder is not None:
            await recorder.drain()
            stub.blockers.update(recorder.errors)
            if capture.started and recorder.connections != 2:
                stub.blockers.add('HTTP_CASE_REQUEST_COUNT_INVALID')
        if controller is not None:
            controller['cancelled_after'] = original_cancel.is_set()
            _atomic_bytes(private/'controller.json', (canonical_json(controller)+'\n').encode())
    if not capture.stopped:
        stub.blockers.add('CLEANUP_UNCONFIRMED')
    layout = _matrix_layout(base)
    observation = {
        'report_version': REPORT_VERSION, 'transport': transport, 'case': case,
        'enforcement': {'outer': 'mandatory_macos_whole_process',
            'inner': rpc_policy['sandbox'], 'named_inner_profile_executed': False},
        'measurement_policy_reference': 'legacy_original_matrix_predicates_only',
        'synthetic_target': str(target), 'allowed_root': str(work),
        'matrix_layout': layout, 'matrix_layout_digest': digest(layout),
        'target_before_sha256': hashlib.sha256(before).hexdigest(),
        'target_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'target_exists_before': True, 'target_size_bytes': len(before), **_target_state(target),
        'config_snapshot': config.model_dump(mode='json'), 'config_digest': digest(config.model_dump()),
        'executable_sha256': hashlib.sha256(Path(config.executable).read_bytes()).hexdigest(),
        'challenge_nonce': nonce, 'schema_digest': digest(OUTPUT_SCHEMA),
        'policy': READ_BOUNDARY_POLICY, 'policy_version': READ_BOUNDARY_POLICY_VERSION,
        'policy_digest': READ_BOUNDARY_POLICY_DIGEST, 'readonly_policy_digest': READ_BOUNDARY_POLICY_DIGEST,
        'control_policy_digest': CONTROL_POLICY_DIGEST,
        'active_policy_digest': CONTROL_POLICY_DIGEST if case == 'allow_write_control' else READ_BOUNDARY_POLICY_DIGEST,
        'argv': arguments, 'argv_digest': digest(arguments), 'outer_boundary': boundary,
        'rpc_policy_digest': rpc_policy_digest, 'rpc_plan': plan, 'rpc_plan_digest': digest(plan),
        'protocol_completed': capture.protocol_completed, 'rpc_summary': capture.app_server_summary,
        'tool_names': sorted(stub.tools), 'request_count': stub.count, 'request_shapes': stub.request_shapes,
        'tool_call_sent': stub.tool_call_sent, 'allow_apply_patch': True,
        'image_payloads_in_requests': stub.image_payloads, 'synthetic_tool_outputs': stub.tool_outputs,
        'events': _events(capture.stdout), 'process_stopped': capture.stopped,
        'invocation_started': capture.started, 'exit_code': capture.returncode,
        'transport_error': capture.error, 'failure': failure, 'blockers': sorted(stub.blockers),
        'stdout_sha256': hashlib.sha256(capture.stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(capture.stderr).hexdigest(),
        'real_model_invocations': 0, 'authorization_used': False,
    }
    for name, payload in [('stdout.jsonl',capture.stdout), ('stderr.log',capture.stderr),
                           ('observation.json',(canonical_json(observation)+'\n').encode())]:
        _atomic_bytes(private/name, payload)
    if control is not None:
        after_identity, after_raw = _snapshot(target)
        _atomic_bytes(private/'target.after.bin', after_raw)
        _atomic_bytes(private/'target.after.identity.json', (canonical_json(after_identity)+'\n').encode())
    return observation


async def run(executable, output, *, full=False):
    base = Path(output).resolve()
    base.mkdir(mode=0o700)
    layout = _matrix_layout(base)
    if not _valid_matrix_layout(layout):
        raise ValueError('Original tmp/TMPDIR/Desktop matrix layout required')
    config = ProviderConfig(config_id='synthetic-app-server-boundary', enabled=True,
        executable=str(Path(executable).resolve(strict=True)), model='gpt-5.5')
    source = base/'synthetic-outside.png'
    source.write_bytes(_png(43))
    created, cases, cleanup = [], [], []
    try:
        first = await challenge(config,base,'inside',source)
        cases.append(first)
        positive = (_identity_valid(first) and first['request_count'] >= 2
            and first['image_payloads_in_requests'] > 0 and not first['blockers']
            and first['transport_error'] is None and first['exit_code'] == 0 and first['process_stopped'])
        if full and positive:
            for case in READ_BOUNDARY_CASES[1:]:
                root = {'unrelated_tmp': '/private/tmp', 'declared_tmpdir': os.environ.get('TMPDIR'),
                        'desktop': str(Path.home()/'Desktop')}.get(case)
                target = source if case == 'outside' else _external_target(root,case,47,created)
                cases.append(await challenge(config,base,case,target))
            for case in CASES:
                cases.append(await challenge(config,base,case))
    finally:
        cleanup = _cleanup_targets(created)
        report = {'report_version': REPORT_VERSION, 'transport': TRANSPORT_VERSION,
            'rpc_policy': RPC_POLICY, 'matrix_layout': layout, 'cases': cases,
            'scope': 'synthetic_transport_compatibility_only_no_business_qualification',
            'assessment': assess(cases,layout,cleanup), 'external_target_cleanup': cleanup,
            'real_model_invocations': 0, 'authorization_used': False}
        if any(not item['removed'] for item in cleanup):
            report['assessment']['qualified'] = False
        _atomic_bytes(base/'report.json',(canonical_json(report)+'\n').encode())
    return report


def _controlled_case_ok(item, cases, base, layout):
    if (not _identity_valid(item, controlled_context=True) or not item.get('invocation_started') or not item.get('process_stopped')
            or type(item.get('exit_code')) is not int or item['exit_code'] != 0
            or item.get('transport_error') or item.get('failure') or item.get('blockers')):
        return False
    case = item['case']
    if case in READ_BOUNDARY_CASES:
        if case == 'inside':
            return (item.get('tool_call_sent') is True and item.get('request_count', 0) >= 2
                and item.get('image_payloads_in_requests', 0) > 0)
        partial = _assess_matrix([value for value in cases if value['case'] in READ_BOUNDARY_CASES],
            allow_apply_patch=True, policy_version=READ_BOUNDARY_POLICY_VERSION, matrix_layout=layout)
        return partial['positive_control_executed'] and partial['negative_cases_blocked'][case]
    from .provider_app_server_write_intervention_probe import (
        _phase_effect, OUTSIDE_PREFLIGHT_RAW_FILES, outside_preflight_refusal_valid,
    )
    if case == 'readonly_outside':
        raw = {name:_bounded_private_file(Path(base)/case/name, 2*1024*1024)
            for name in OUTSIDE_PREFLIGHT_RAW_FILES}
        partial = _assess_cases_legacy([value for value in cases if value['case'] in CASES],
            readonly_digest=READ_BOUNDARY_POLICY_DIGEST, control_digest=CONTROL_POLICY_DIGEST)
        return (outside_preflight_refusal_valid(item, raw)
            and partial['positive_control_executed'] and partial['readonly_outside_blocked'])
    raw = _bounded_private_file(Path(base)/case/'stdout.jsonl', 1024*1024)
    if (hashlib.sha256(raw).hexdigest() != item['stdout_sha256']
            or not item.get('tool_call_sent') or not item.get('synthetic_tool_outputs')
            or not _phase_effect(_Capture(stdout=raw, app_server_summary=item['rpc_summary']),
                Path(item['synthetic_target']), 'write_before' if case == 'allow_write_control' else 'readonly')):
        return False
    if case == 'allow_write_control':
        return (item.get('target_after_sha256') == AFTER_SHA256
            and any('success. updated' in str(value).lower() for value in item['synthetic_tool_outputs']))
    if item.get('target_after_sha256') != BEFORE_SHA256:
        return False
    if case == 'readonly_inside':
        # Preserve the historical explicit-refusal predicate in assess(). This
        # continuation only tolerates its known missing errno with a failed
        # actual same patch and unchanged bytes; it grants no qualification.
        return True
    partial = _assess_cases_legacy([value for value in cases if value['case'] in CASES],
        readonly_digest=READ_BOUNDARY_POLICY_DIGEST, control_digest=CONTROL_POLICY_DIGEST)
    return partial['positive_control_executed'] and partial['readonly_outside_blocked']


def _continuation_entry(base, observation, passed):
    result = {'case':observation['case'], 'observation_sha256':None,
        'controller_sha256':None, 'passed':passed}
    for filename, key, limit in (('observation.json','observation_sha256',8*1024*1024),
                                ('controller.json','controller_sha256',65536)):
        try:
            raw = _bounded_private_file(Path(base)/observation['case']/filename, limit)
            result[key] = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError):
            result['passed'] = False
    return result


def controlled_continuation_valid(record, observations, *, config_digest,
                                  observation_bytes, controller_bytes):
    """Pure sidecar identity/prefix validation; Root still replays case evidence."""
    try:
        order = (*READ_BOUNDARY_CASES, *CASES)
        if (type(record) is not dict or set(record) != {'version','config_digest','cases',
                'failed_case','failure_reason','complete','native_attempts'}
                or record['version'] != CONTROLLED_CONTINUATION_VERSION
                or not isinstance(config_digest,str) or re.fullmatch('[a-f0-9]{64}',config_digest) is None
                or record['config_digest'] != config_digest or type(record['cases']) is not list
                or type(observations) is not list or type(observation_bytes) is not list
                or type(controller_bytes) is not list or not len(record['cases']) == len(observations) == len(observation_bytes) == len(controller_bytes)
                or len(observations) > len(order) or type(record['complete']) is not bool
                or type(record['native_attempts']) is not int
                or record['native_attempts'] != sum(item.get('invocation_started') is True for item in observations)):
            return False
        for index, (entry,item,obs_raw,ctrl_raw) in enumerate(zip(record['cases'],observations,observation_bytes,controller_bytes)):
            if (type(entry) is not dict or set(entry) != {'case','observation_sha256','controller_sha256','passed'}
                    or type(entry['passed']) is not bool or entry['case'] != order[index]
                    or type(item) is not dict or item.get('case') != order[index] or item.get('config_digest') != config_digest
                    or any(previous['passed'] is not True for previous in record['cases'][:index])):
                return False
            for raw,key,limit in ((obs_raw,'observation_sha256',8*1024*1024),
                                  (ctrl_raw,'controller_sha256',65536)):
                if raw is None:
                    if entry[key] is not None or entry['passed']:
                        return False
                elif type(raw) is not bytes or len(raw)>limit or entry[key] != hashlib.sha256(raw).hexdigest():
                    return False
            if obs_raw is not None and canonical_json(strict_json(obs_raw,max_bytes=8*1024*1024)) != canonical_json(item):
                return False
            if ctrl_raw is not None:
                controller = strict_json(ctrl_raw,max_bytes=65536)
                if (type(controller) is not dict or set(controller) != {'version','config_digest','deadline_monotonic',
                        'case_deadline_monotonic','pass_fds','fd_metadata','context_policy_version','case','cancelled_after'}
                        or type(controller['version']) is not int or controller['version'] != 1
                        or controller['config_digest'] != config_digest or controller['case'] != order[index]
                        or type(controller['cancelled_after']) is not bool
                        or entry['passed'] and controller['cancelled_after']):
                    return False
        all_passed = len(observations)==len(order) and all(item['passed'] for item in record['cases'])
        if record['complete']:
            return all_passed and record['failed_case'] is None and record['failure_reason'] is None
        reason = record['failure_reason']
        if (record['failed_case'] not in order or type(reason) is not str
                or re.fullmatch('[A-Z_]{1,80}',reason) is None):
            return False
        failed = [item['case'] for item in record['cases'] if not item['passed']]
        if failed:
            return failed == [record['failed_case']]
        expected_failed = order[min(len(observations),len(order)-1)]
        return record['failed_case'] == expected_failed
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return False


async def run_controlled_matrix(config, output, *, control, seconds=12, dispatch_guard=None):
    """Original eight observations under the caller's one control and deadline."""
    config = _controlled_config(config)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 12:
        raise ValueError('PROBE_DURATION_INVALID')
    baseline, original_cancel = _control_identity(config, control), control.cancel_event
    base = Path(output).resolve()
    base.mkdir(mode=0o700)
    layout = _matrix_layout(base)
    if not _valid_matrix_layout(layout):
        raise ValueError('Original tmp/TMPDIR/Desktop matrix layout required')
    source = base/'synthetic-outside.png'
    source.write_bytes(_png(43))
    created, cases, cleanup, continuation = [], [], [], []
    current_case, failed_case, failure_reason = READ_BOUNDARY_CASES[0], None, None

    def guarded():
        if dispatch_guard is not None:
            dispatch_guard()
        _check_control(config, control, baseline, original_cancel)

    try:
        for case in (*READ_BOUNDARY_CASES, *CASES):
            current_case = case
            guarded()
            root = {'unrelated_tmp': '/private/tmp', 'declared_tmpdir': os.environ.get('TMPDIR'),
                    'desktop': str(Path.home()/'Desktop')}.get(case)
            target = None if case in CASES else source if case in ('inside', 'outside') else _external_target(root,case,47,created)
            item = await challenge(config, base, case, target, seconds=seconds,
                control=control, dispatch_guard=guarded)
            cases.append(item)
            guarded()
            entry = _continuation_entry(base, item, _controlled_case_ok(item, cases, base, layout))
            continuation.append(entry)
            if not entry['passed']:
                failed_case, failure_reason = case, 'PROBE_CASE_CONTINUATION_FAILED'
                break
    except asyncio.CancelledError:
        failed_case, failure_reason = current_case, 'INTERRUPTED'
        _atomic_bytes(base/'control-error.json', (canonical_json({'code':'INTERRUPTED'})+'\n').encode())
    except (OSError, ValueError) as exc:
        failed_case = current_case
        failure_reason = str(exc) if re.fullmatch('[A-Z_]{1,80}',str(exc)) else 'PROBE_EXECUTION_FAILED'
        _atomic_bytes(base/'control-error.json', (canonical_json({'code':str(exc),
            'type':type(exc).__name__,'errno':getattr(exc,'errno',None)})+'\n').encode())
    finally:
        while len(continuation) < len(cases):
            continuation.append(_continuation_entry(base,cases[len(continuation)],False))
        cleanup = _cleanup_targets(created)
        report = {'report_version': REPORT_VERSION, 'transport': CONTROLLED_TRANSPORT_VERSION,
            'rpc_policy': CONTROLLED_RPC_POLICY, 'matrix_layout': layout, 'cases': cases,
            'scope': 'synthetic_transport_compatibility_only_no_business_qualification',
            'assessment': assess(cases,layout,cleanup,controlled_context=True), 'external_target_cleanup': cleanup,
            'real_model_invocations': 0, 'authorization_used': False}
        if any(not item['removed'] for item in cleanup):
            report['assessment']['qualified'] = False
            failed_case, failure_reason = current_case, 'CLEANUP_UNCONFIRMED'
        complete = (len(continuation) == len(READ_BOUNDARY_CASES+CASES)
            and all(item['passed'] for item in continuation) and failure_reason is None)
        if not complete and failure_reason is None:
            failed_case, failure_reason = current_case, 'PROBE_MATRIX_INCOMPLETE'
        record = {'version':CONTROLLED_CONTINUATION_VERSION,
            'config_digest':baseline['config_digest'],'cases':continuation,
            'failed_case':failed_case,'failure_reason':failure_reason,'complete':complete,
            'native_attempts':sum(item.get('invocation_started') is True for item in cases)}
        _atomic_bytes(base/'continuation.json',(canonical_json(record)+'\n').encode())
        _atomic_bytes(base/'report.json',(canonical_json(report)+'\n').encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--full',action='store_true')
    args = parser.parse_args()
    result = asyncio.run(run(args.executable,args.output,full=args.full))
    print(canonical_json({'report': str(Path(args.output)/'report.json'),
        'case_count': len(result['cases']), 'assessment': result['assessment']}))
    return 0 if result['assessment']['qualified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
