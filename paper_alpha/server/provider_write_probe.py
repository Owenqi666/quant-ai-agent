"""Exercise native apply_patch on self-created files using a loopback SSE stub.

No model backend is contacted. A narrowly writable positive control proves the
tool executes; the production read policy must deny writes inside and outside
its workdir. This report grants no business dispatch or model qualification.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]

from .codex_cli_provider import _Capture, _atomic_bytes, _bounded_private_file, _run_bounded
from .provider_cli_state import append_context_policy
from .provider_contract import canonical_json, digest
from .provider_read_boundary import (
    READ_BOUNDARY_POLICY, READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION,
    READ_BOUNDARY_POLICY_V2_DIGEST, build_read_boundary_argv,
    outer_boundary_identity, outer_evidence_matches_identity, temporary_read_denials,
)
from .provider_schema import ProviderCallControl, ProviderConfig
from .provider_outer_sandbox import build_outer_invocation, assess_outer_boundary
from .provider_read_probe import OUTPUT_SCHEMA, _CancellationView, _Stub, _events, _public_events, _synthetic_inner_argv

BEFORE_CONTENT = b'PAPER_ALPHA_SYNTHETIC_BEFORE\n'
AFTER_CONTENT = b'PAPER_ALPHA_SYNTHETIC_AFTER\n'
BEFORE_SHA256 = hashlib.sha256(BEFORE_CONTENT).hexdigest()
AFTER_SHA256 = hashlib.sha256(AFTER_CONTENT).hexdigest()
READONLY_POLICY_DIGEST = READ_BOUNDARY_POLICY_DIGEST
CONTROL_POLICY_V1 = {
    'policy_version': 'cli_named_write_control_v1',
    'profile_name': READ_BOUNDARY_POLICY['profile_name'],
    'filesystem': {':root': 'deny', ':minimal': 'read', '<work>': 'write'},
    'network': {'enabled': False},
    'scope': 'self_created_positive_control_only',
}
CONTROL_POLICY_V1_DIGEST = digest(CONTROL_POLICY_V1)
CONTROL_POLICY = {**CONTROL_POLICY_V1, 'policy_version': 'macos_outer_write_control_v2',
    'outer_scope': 'exact_self_created_target_only', 'outer_boundary': 'macos_whole_cli_sandbox_exec_v1'}
CONTROL_POLICY_DIGEST = digest(CONTROL_POLICY)
PROMPT = 'Synthetic local write-boundary check only. Execute the supplied synthetic patch, then return {"done":true}.'
CASES = ('allow_write_control', 'readonly_inside', 'readonly_outside')
EXPECTED_TOOLS = frozenset({'update_plan', 'request_user_input', 'view_image', 'apply_patch'})


def _patch_response(target: Path) -> bytes:
    patch = ('*** Begin Patch\n*** Update File: ' + str(target)
             + '\n@@\n-PAPER_ALPHA_SYNTHETIC_BEFORE\n+PAPER_ALPHA_SYNTHETIC_AFTER\n*** End Patch')
    item = {'type': 'custom_tool_call', 'id': 'ctc_synthetic', 'call_id': 'call_synthetic',
            'name': 'apply_patch', 'input': patch}
    response = {'id': 'resp_synthetic_patch', 'object': 'response', 'status': 'completed',
                'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
    return _events([
        {'type': 'response.created', 'sequence_number': 0,
         'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'sequence_number': 1, 'output_index': 0,
         'item': {**item, 'input': ''}},
        {'type': 'response.output_item.done', 'sequence_number': 2, 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'sequence_number': 3, 'response': response},
    ])


class _WriteStub(_Stub):
    def __init__(self, path, target):
        super().__init__(path, target, allow_apply_patch=True)

    def tool_response(self):
        return _patch_response(self.target)


def _control_argv(arguments: list[str], work: Path) -> list[str]:
    """Replace only work's permission for the explicit synthetic control."""
    arguments = list(arguments)
    prefix = 'permissions.' + READ_BOUNDARY_POLICY['profile_name'] + '.filesystem='
    positions = [index for index, value in enumerate(arguments) if value.startswith(prefix)]
    if len(positions) != 1 or positions[0] == 0 or arguments[positions[0] - 1] != '-c':
        raise ValueError('Control must replace exactly one known filesystem policy')
    arguments[positions[0]] = prefix + '{":root"="deny",":minimal"="read",' + canonical_json(str(work)) + '="write"}'
    return arguments


def _target_state(target: Path) -> dict:
    try:
        content = _bounded_private_file(target, 1024)
        return {'target_exists': True, 'target_after_sha256': hashlib.sha256(content).hexdigest(),
                'target_read_error': None}
    except FileNotFoundError:
        return {'target_exists': False, 'target_after_sha256': None, 'target_read_error': None}
    except (OSError, ValueError) as exc:
        return {'target_exists': target.exists(), 'target_after_sha256': None,
                'target_read_error': type(exc).__name__}


async def _challenge(config, base, case, *, max_seconds, control=None):
    private = base / case
    private.mkdir(mode=0o700)
    work, cli_state = private / 'work', private / 'cli-state'
    work.mkdir(mode=0o700)
    cli_state.mkdir(mode=0o700)
    target = (private / 'outside-target.txt') if case == 'readonly_outside' else (work / 'target.txt')
    target.write_bytes(BEFORE_CONTENT)
    schema, response = work / 'output.schema.json', work / 'response.raw.json'
    schema.write_text(canonical_json(OUTPUT_SCHEMA))
    nonce = uuid.uuid4().hex
    endpoint_path = '/write-challenge-' + nonce + '/v1/responses'
    stub, capture = _WriteStub(endpoint_path, target), _Capture()
    cancel = _CancellationView(stub.cancel, control.cancel_event) if control else stub.cancel
    server, arguments, failure, stage, cancelled = None, [], None, 'bind', False
    outer_boundary = None
    try:
        server = await asyncio.start_server(stub.handle, '127.0.0.1', 0, limit=16384)
        port = server.sockets[0].getsockname()[1]
        stage = 'argv'
        arguments = build_read_boundary_argv(config, work, schema, response)
        if case == 'allow_write_control':
            arguments = _control_argv(arguments, work)
        arguments = append_context_policy(arguments)
        for value in (
            'model_provider="paper_alpha_write_challenge"',
            'model_providers.paper_alpha_write_challenge.name="Synthetic local write challenge"',
            f'model_providers.paper_alpha_write_challenge.base_url="http://127.0.0.1:{port}/write-challenge-{nonce}/v1"',
            'model_providers.paper_alpha_write_challenge.wire_api="responses"',
            'model_providers.paper_alpha_write_challenge.requires_openai_auth=false',
            'model_providers.paper_alpha_write_challenge.request_max_retries=0',
            'model_providers.paper_alpha_write_challenge.stream_max_retries=0',
            'features.enable_request_compression=false',
        ):
            arguments.extend(['-c', value])
        wrapped = build_outer_invocation(arguments, work=work, cli_state=cli_state,
            response_path=response, network_mode='synthetic_loopback', loopback_port=port,
            synthetic_write_target=target if case == 'allow_write_control' else None)
        arguments, outer_boundary = wrapped['argv'], wrapped['boundary']
        stage = 'invoke'
        await _run_bounded(arguments, cwd=work, stdin=PROMPT.encode(),
            deadline=min(time.monotonic() + max_seconds, control.deadline_monotonic) if control else time.monotonic() + max_seconds,
            cancel=cancel, output_limit=262144, pass_fds=control.pass_fds if control else (),
            capture=capture, supervision_dir=private / 'supervision', response_path=response,
            response_limit=65536, isolated_cli_home=cli_state)
    except asyncio.CancelledError:
        cancelled = True
        failure = {'code': 'INTERRUPTED', 'type': 'CancelledError', 'errno': None}
        stub.blockers.add('INTERRUPTED')
    except Exception as exc:
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
    executable_hash = hashlib.sha256(_bounded_private_file(Path(config.executable).resolve(), 512 * 1024 * 1024)).hexdigest()
    observation = {'case': case, 'synthetic_target': str(target), 'allowed_root': str(work),
        'target_before_sha256': BEFORE_SHA256, **_target_state(target),
        'config_digest': digest(config.model_dump()), 'config_snapshot': config.model_dump(mode='json'),
        'challenge_nonce': nonce, 'temporary_read_denials': list(temporary_read_denials()), 'executable_sha256': executable_hash,
        'readonly_policy_digest': READONLY_POLICY_DIGEST, 'control_policy_digest': CONTROL_POLICY_DIGEST,
        'active_policy_digest': CONTROL_POLICY_DIGEST if case == 'allow_write_control' else READONLY_POLICY_DIGEST,
        'schema_digest': digest(OUTPUT_SCHEMA), 'schema_scope': 'synthetic_done_response',
        'argv': arguments, 'argv_digest': digest(arguments), 'outer_boundary': outer_boundary, 'tool_names': sorted(stub.tools),
        'request_shapes': stub.request_shapes, 'request_count': stub.count,
        'tool_call_sent': stub.tool_call_sent, 'synthetic_tool_outputs': stub.tool_outputs,
        'events': _public_events(capture.stdout), 'process_stopped': capture.stopped,
        'invocation_started': capture.started, 'exit_code': capture.returncode,
        'transport_error': capture.error, 'blockers': sorted(stub.blockers), 'failure': failure,
        'stdout_sha256': hashlib.sha256(capture.stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(capture.stderr).hexdigest()}
    _atomic_bytes(private / 'stdout.jsonl', capture.stdout)
    _atomic_bytes(private / 'stderr.log', capture.stderr)
    _atomic_bytes(private / 'observation.json', (canonical_json(observation) + '\n').encode())
    if cancelled:
        error = asyncio.CancelledError()
        error.observation = observation
        raise error
    return observation


def _assess_cases_legacy(results, *, readonly_digest, control_digest) -> dict:
    """Require actual successful writing and two explicit, unchanged refusals."""
    empty = {'positive_control_executed': False, 'readonly_inside_blocked': False,
             'readonly_outside_blocked': False, 'same_identity': False, 'write_boundary_observed': False}
    if not isinstance(results, list) or len(results) != 3 or not all(isinstance(case, dict) for case in results):
        return empty
    by_case = {case.get('case'): case for case in results}
    if set(by_case) != set(CASES):
        return empty
    def completed(case, active_policy):
        tools = case.get('tool_names')
        return (case.get('process_stopped') is True and case.get('invocation_started') is True
            and not case.get('blockers') and not case.get('failure') and not case.get('transport_error')
            and type(case.get('exit_code')) is int and case['exit_code'] == 0 and case.get('tool_call_sent') is True
            and type(case.get('request_count')) is int and 2 <= case['request_count'] <= 3
            and isinstance(tools, list) and all(isinstance(name, str) and name in EXPECTED_TOOLS for name in tools)
            and 'apply_patch' in tools and case.get('active_policy_digest') == active_policy
            and case.get('target_before_sha256') == BEFORE_SHA256 and case.get('target_exists') is True
            and case.get('target_read_error') is None and isinstance(case.get('synthetic_tool_outputs'), list))
    control = by_case['allow_write_control']
    positive = bool(completed(control, control_digest)
        and control.get('target_after_sha256') == AFTER_SHA256
        and any('success. updated' in str(value).lower() for value in control['synthetic_tool_outputs']))
    refusal_terms = ('operation not permitted', 'permission denied', 'access denied',
        'read-only file system', 'read-only filesystem', 'writing outside of the project',
        'rejected by user approval settings', 'write access denied')
    def refused(case):
        return bool(completed(case, readonly_digest) and case.get('target_after_sha256') == BEFORE_SHA256
            and any(any(term in str(value).lower() for term in refusal_terms) for value in case['synthetic_tool_outputs']))
    inside, outside = refused(by_case['readonly_inside']), refused(by_case['readonly_outside'])
    keys = ('config_digest', 'executable_sha256', 'readonly_policy_digest', 'control_policy_digest', 'schema_digest')
    same_identity = all(isinstance(control.get(key), str) and len(control[key]) == 64
        and all(char in '0123456789abcdef' for char in control[key])
        and all(case.get(key) == control[key] for case in results) for key in keys)
    same_identity = bool(same_identity and control['readonly_policy_digest'] == readonly_digest
        and control['control_policy_digest'] == control_digest and control['schema_digest'] == digest(OUTPUT_SCHEMA))
    return {'positive_control_executed': positive, 'readonly_inside_blocked': inside,
            'readonly_outside_blocked': outside, 'same_identity': same_identity,
            'write_boundary_observed': bool(positive and inside and outside and same_identity)}


def _outer_case_valid(item):
    if not isinstance(item, dict):
        return False
    boundary, argv = item.get('outer_boundary'), item.get('argv')
    positive = item.get('case') == 'allow_write_control'
    work = item.get('allowed_root')
    if not isinstance(work, str):
        return False
    target = str(Path(work).parent / 'outside-target.txt') if item.get('case') == 'readonly_outside' else str(Path(work) / 'target.txt')
    try:
        inner = _synthetic_inner_argv(item.get('config_snapshot'), boundary, item.get('challenge_nonce'), item.get('temporary_read_denials'), write_case=item.get('case'))
    except (KeyError, TypeError, ValueError):
        return False
    return bool(assess_outer_boundary(boundary, argv)['valid']
        and boundary.get('inner_argv') == inner and digest(item['config_snapshot']) == item.get('config_digest')
        and boundary.get('purpose') == ('synthetic_write_control' if positive else 'synthetic_read')
        and boundary.get('network_mode') == 'synthetic_loopback' and boundary.get('auth_exact_path') is None
        and boundary.get('work') == work and boundary.get('cli_state') == str(Path(work).parent / 'cli-state')
        and boundary.get('response_path') == str(Path(work) / 'response.raw.json')
        and item.get('synthetic_target') == target
        and boundary.get('synthetic_write_target') == (target if positive else None)
        and boundary.get('native_executable_sha256') == item.get('executable_sha256')
        and item.get('argv_digest') == digest(argv))


def assess_cases(results, *, legacy=False) -> dict:
    result = _assess_cases_legacy(results,
        readonly_digest=READ_BOUNDARY_POLICY_V2_DIGEST if legacy else READONLY_POLICY_DIGEST,
        control_digest=CONTROL_POLICY_V1_DIGEST if legacy else CONTROL_POLICY_DIGEST)
    if not legacy:
        valid = isinstance(results, list) and len(results) == len(CASES) and all(_outer_case_valid(item) for item in results)
        result['outer_boundary_verified'] = bool(valid)
        result['write_boundary_observed'] = bool(result['write_boundary_observed'] and valid)
    return result


def assess_report(report):
    """Preserve old write evidence; current reports require the outer wrapper."""
    if not isinstance(report, dict) or type(report.get('challenge_version')) is not int or report['challenge_version'] not in (1, 2):
        raise ValueError('Unsupported write-boundary report version')
    legacy = report['challenge_version'] == 1
    result = assess_cases(report.get('cases'), legacy=legacy)
    if not legacy:
        cases = report.get('cases', [])
        identity_matches = (report.get('readonly_policy_version') == READ_BOUNDARY_POLICY_VERSION
            and report.get('control_policy_version') == CONTROL_POLICY['policy_version']
            and report.get('process_stopped') is True and report.get('error') is None
            and type(report.get('real_model_invocations')) is int and report['real_model_invocations'] == 0
            and report.get('real_provider_requests') == 0 and report.get('credential_reads') == 0
            and report.get('real_dispatch_enabled') is False
            and digest(report.get('config_snapshot')) == report.get('config_digest')
            and all(item.get('config_snapshot') == report.get('config_snapshot')
                and item.get('temporary_read_denials') == report.get('temporary_read_denials') for item in cases)
            and all(item.get(key) == report.get(key) for item in cases for key in (
                'config_digest', 'executable_sha256', 'readonly_policy_digest', 'control_policy_digest', 'schema_digest'))
            and all(outer_evidence_matches_identity(item.get('outer_boundary'), report.get('outer_identity')) for item in cases))
        result['report_identity_matches'] = bool(identity_matches)
        result['write_boundary_observed'] = bool(result['write_boundary_observed'] and identity_matches)
    return result

def _positive_control_executed(case):
    tools = case.get('tool_names')
    return bool(case.get('case') == 'allow_write_control' and _outer_case_valid(case)
        and case.get('invocation_started') is True and case.get('process_stopped') is True
        and type(case.get('exit_code')) is int and case['exit_code'] == 0
        and not case.get('transport_error') and not case.get('failure') and case.get('blockers') == []
        and case.get('tool_call_sent') is True and type(case.get('request_count')) is int
        and 2 <= case['request_count'] <= 3 and isinstance(tools, list)
        and 'apply_patch' in tools and all(name in EXPECTED_TOOLS for name in tools)
        and case.get('target_before_sha256') == BEFORE_SHA256 and case.get('target_after_sha256') == AFTER_SHA256
        and case.get('target_exists') is True and case.get('target_read_error') is None
        and isinstance(case.get('synthetic_tool_outputs'), list)
        and any('success. updated' in str(value).lower() for value in case['synthetic_tool_outputs']))


async def run(executable, model, output_dir, *, max_seconds=12, config: ProviderConfig | None = None,
              control: ProviderCallControl | None = None):
    if type(max_seconds) is not int or not 1 <= max_seconds <= 12:
        raise ValueError('Each synthetic write case has a budget of 1..12 seconds')
    config = ProviderConfig.model_validate(config) if config is not None else ProviderConfig(
        config_id='synthetic-write-boundary', mode='codex_cli', enabled=True,
        executable=str(Path(executable).resolve(strict=True)), model=model)
    output_dir = Path(output_dir).absolute()
    if '..' in output_dir.parts or any(path.is_symlink() for path in (output_dir, *output_dir.parents)):
        raise ValueError('Output path must be unlinked and new')
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    results, error, cancelled, case_pending, all_processes_stopped = [], None, False, False, True
    try:
        for case in CASES:
            if control is not None and (control.cancel_event.is_set() or time.monotonic() >= control.deadline_monotonic):
                error = {'code': 'CANCELLED' if control.cancel_event.is_set() else 'TIMEOUT'}
                break
            case_pending = True
            options = {'control': control} if control is not None else {}
            value = await _challenge(config, output_dir, case, max_seconds=max_seconds, **options)
            case_pending = False
            results.append(value)
            all_processes_stopped = all_processes_stopped and value.get('process_stopped') is True
            if value['process_stopped'] is not True:
                error = {'code': 'OUTCOME_UNKNOWN'}
                break
            if case == 'allow_write_control' and not _positive_control_executed(value):
                error = {'code': 'POSITIVE_CONTROL_NOT_EXECUTED'}
                break
    except asyncio.CancelledError as exc:
        cancelled = True
        if isinstance(getattr(exc, 'observation', None), dict):
            results.append(exc.observation)
            case_pending = False
            all_processes_stopped = all_processes_stopped and exc.observation.get('process_stopped') is True
        error = {'code': 'INTERRUPTED', 'type': 'CancelledError'}
    except Exception as exc:
        error = {'code': 'PROBE_FAILED', 'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None)}
    report = {'challenge_version': 2, 'evidence_kind': 'synthetic_native_write_challenge',
        'real_model_invocations': 0, 'real_provider_requests': 0, 'credential_reads': 0, 'real_dispatch_enabled': False,
        'readonly_policy_version': READ_BOUNDARY_POLICY_VERSION, 'control_policy_version': CONTROL_POLICY['policy_version'],
        'outer_identity': outer_boundary_identity(), 'temporary_read_denials': list(temporary_read_denials()),
        **assess_cases(results), 'process_stopped': not case_pending and all_processes_stopped,
        'config_digest': digest(config.model_dump()), 'config_snapshot': config.model_dump(mode='json'),
        'executable_sha256': hashlib.sha256(_bounded_private_file(Path(config.executable).resolve(), 512 * 1024 * 1024)).hexdigest(),
        'readonly_policy_digest': READONLY_POLICY_DIGEST, 'control_policy_digest': CONTROL_POLICY_DIGEST,
        'schema_digest': digest(OUTPUT_SCHEMA), 'schema_scope': 'synthetic_done_response',
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'read_helper_sha256': hashlib.sha256((ROOT / 'paper_alpha/server/provider_read_boundary.py').read_bytes()).hexdigest(),
        'read_probe_sha256': hashlib.sha256((ROOT / 'paper_alpha/server/provider_read_probe.py').read_bytes()).hexdigest(),
        'cases': results, 'error': error,
        'limitations': ['Only native apply_patch and the exact named policy are exercised.',
                        'The writable positive control is temporary and is not the production policy.',
                        'This local synthetic test does not prove backend schema compatibility or model quality.']}
    report.update(assess_report(report))
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
    report = asyncio.run(run(args.executable, args.model, args.output_dir, max_seconds=args.max_seconds))
    print(json.dumps({key: report[key] for key in ('evidence_kind', 'real_model_invocations',
        'positive_control_executed', 'readonly_inside_blocked', 'readonly_outside_blocked', 'write_boundary_observed')}, indent=2))
    return 0 if report['write_boundary_observed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
