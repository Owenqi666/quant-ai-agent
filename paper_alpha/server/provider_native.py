"""One-shot native research pilot; ordinary registry dispatch remains closed.

Host authorization expresses intent only. Every call independently rechecks
the executable, source, configuration, native tool boundary and authentication.
Native tool challenges are packaged runtime modules. Host authorization is
separate from the fresh execution checks performed for each request.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re
import shutil
import tempfile
import time

from .codex_cli_provider import _Capture, _atomic_bytes, _bounded_private_file, _run_bounded
from .provider_canary_result import MAX_LOG_BYTES, summarize_cli_capture
from .provider_cli_profile import (build_profile, contract_id_for_schema, RESEARCH_CONTRACT_ID,
                                   SUPPORTED_CONTRACT_IDS)
from .provider_cli_state import append_context_policy, prepare_cli_state, CONTEXT_POLICY_VERSION
from .provider_contract import canonical_json, digest, strict_json
from .provider_read_boundary import build_read_boundary_argv, READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION, outer_boundary_identity, outer_evidence_matches_identity, render_read_boundary_argv, temporary_read_denials
from .provider_schema import (PreparedProposalRequest, ProviderAttemptResult, ProviderCallControl,
                              ProviderConfig, ProviderError, ProviderReadiness, ProviderReceipt, ProviderUsage,
                              CONTRACT_ID)
from .provider_read_probe import run as probe_read, assess_report as assess_read_report, OUTPUT_SCHEMA
from .provider_write_probe import run as probe_write, assess_report as assess_write_report, CONTROL_POLICY_DIGEST
from .provider_outer_sandbox import build_outer_invocation, assess_outer_boundary

ROOT = Path(__file__).resolve().parents[2]
_SOURCE_NAMES = ('paper_alpha/server/provider_native.py', 'paper_alpha/server/provider_canary_result.py',
                 'paper_alpha/server/provider_cli_state.py', 'paper_alpha/server/provider_read_boundary.py',
                 'paper_alpha/server/provider_read_probe.py', 'paper_alpha/server/provider_write_probe.py',
                 'paper_alpha/server/provider_outer_sandbox.py')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _save(path, value):
    _atomic_bytes(path, (canonical_json(value) + '\n').encode())


def _identity(config, *, contract_id=CONTRACT_ID):
    profile = build_profile(config, contract_id=contract_id)
    tmp_denials = temporary_read_denials()
    inner_template = append_context_policy(render_read_boundary_argv(
        profile['argv_template'], '<cwd>', tmp_denials))
    return {'contract_id': contract_id, 'profile_digest': profile['profile_digest'],
        'config_digest': digest(config.model_dump()),
        'temporary_read_denials': list(tmp_denials),
        'native_inner_argv': {'template_version': 'native_inner_argv_v1',
            'argv_template': inner_template, 'argv_template_digest': digest(inner_template)},
        'native_executable_path': profile['executable']['resolved'], 'native_executable_sha256': profile['executable']['sha256'],
        'read_policy_version': READ_BOUNDARY_POLICY_VERSION,
        'read_policy_digest': READ_BOUNDARY_POLICY_DIGEST, 'outer_identity': outer_boundary_identity(), 'context_policy_version': CONTEXT_POLICY_VERSION,
        'source_digests': {name: hashlib.sha256(_bounded_private_file(ROOT / name, 2 * 1024 * 1024)).hexdigest()
                           for name in _SOURCE_NAMES}}, profile


def _boundaries_match(report, kind, identity, profile):
    cases = report.get('cases', [])
    expected = {'config_digest': identity['config_digest'],
                'executable_sha256': profile['executable']['sha256'], 'schema_digest': digest(OUTPUT_SCHEMA)}
    if kind == 'write':
        expected.update(readonly_policy_digest=READ_BOUNDARY_POLICY_DIGEST,
                        control_policy_digest=CONTROL_POLICY_DIGEST)
        try:
            matched = report.get('challenge_version') == 2 and assess_write_report(report)['write_boundary_observed']
        except (TypeError, ValueError, KeyError):
            matched = False
        top_expected = expected
        sources = {'source_sha256': 'paper_alpha/server/provider_write_probe.py',
                   'read_probe_sha256': 'paper_alpha/server/provider_read_probe.py',
                   'read_helper_sha256': 'paper_alpha/server/provider_read_boundary.py'}
    else:
        expected['policy_digest'] = READ_BOUNDARY_POLICY_DIGEST
        try:
            matched = assess_read_report(report, allow_apply_patch=True)['read_boundary_observed']
        except (TypeError, ValueError, KeyError):
            matched = False
        matched = matched and report.get('policy_version') == READ_BOUNDARY_POLICY_VERSION
        top_expected = {key: value for key, value in expected.items() if key != 'executable_sha256'}
        top_expected['policy_version'] = READ_BOUNDARY_POLICY_VERSION
        matched = matched and report.get('allow_apply_patch') is True
        sources = {'source_sha256': 'paper_alpha/server/provider_read_probe.py',
                   'helper_sha256': 'paper_alpha/server/provider_read_boundary.py'}
    return bool(matched and report.get('temporary_read_denials') == identity['temporary_read_denials']
        and report.get('outer_identity') == identity['outer_identity']
        and all(outer_evidence_matches_identity(case.get('outer_boundary'), identity['outer_identity']) for case in cases)
        and report.get('process_stopped') is True and not report.get('error')
        and all(report.get(key) == value for key, value in top_expected.items())
        and all(report.get(key) == identity['source_digests'][name] for key, name in sources.items())
        and all(case.get('process_stopped') is True and type(case.get('exit_code')) is int
                and case['exit_code'] == 0 and not case.get('transport_error') and not case.get('failure')
                and all(case.get(key) == value for key, value in expected.items()) for case in cases))


def _invocation_boundaries_match(invocation, identity):
    """Verify actual outer argv using retained metadata after temporary cleanup."""
    if not isinstance(invocation, dict) or not isinstance(identity, dict):
        return False
    boundary, argv = invocation.get('outer_boundary'), invocation.get('argv')
    template = identity.get('native_inner_argv')
    if not isinstance(template, dict) or set(template) != {'template_version', 'argv_template', 'argv_template_digest'}:
        return False
    inner = template['argv_template']
    if template['template_version'] != 'native_inner_argv_v1' or not isinstance(inner, list) or template['argv_template_digest'] != digest(inner):
        return False
    if not isinstance(boundary, dict):
        return False
    work = boundary.get('work')
    if not isinstance(work, str):
        return False
    replacements = {'<cwd>': work, '<schema>': str(Path(work) / 'output.schema.json'), '<response>': str(Path(work) / 'response.raw.json')}
    expected_inner = [replacements.get(item, item).replace(canonical_json('<cwd>'), canonical_json(work)) for item in inner]
    return bool(assess_outer_boundary(boundary, argv)['valid']
        and boundary.get('inner_argv') == expected_inner
        and boundary.get('purpose') == 'native_real' and boundary.get('network_mode') == 'native_tls'
        and boundary.get('synthetic_write_target') is None and boundary.get('loopback_port') is None
        and outer_evidence_matches_identity(boundary, identity.get('outer_identity'))
        and boundary.get('native_executable_sha256') == identity.get('native_executable_sha256') == invocation.get('native_executable_sha256')
        and boundary.get('native_executable_path') == identity.get('native_executable_path'))


class _Stopped(Exception):
    def __init__(self, code):
        self.code = code


class NativeResearchProvider:
    def __init__(self, config: ProviderConfig, *, host_authorized=False, contract_id=CONTRACT_ID,
                 research_authorization=None):
        if type(host_authorized) is not bool:
            raise ValueError('Host authorization requires an explicit boolean')
        self.config = ProviderConfig.model_validate(config).model_copy(deep=True)
        self._authorized = host_authorized
        self.contract_id = contract_id
        self._research_authorization = research_authorization
        self._attempted = False

    def _configuration_error(self):
        if not self._authorized:
            return 'HOST_AUTHORIZATION_REQUIRED'
        if self.contract_id not in SUPPORTED_CONTRACT_IDS:
            return 'CONTRACT_UNSUPPORTED'
        if self.config.native_transport == 'app_server' and self.contract_id != RESEARCH_CONTRACT_ID:
            return 'APP_SERVER_OPERATION_UNSUPPORTED'
        if not Path(self.config.executable).is_absolute():
            return 'ABSOLUTE_EXECUTABLE_REQUIRED'
        if (not self.config.enabled or self.config.mode != 'codex_cli' or
                self.config.expected_cli_version != '0.145.0' or self.config.model != 'gpt-5.5' or
                self.config.reasoning_effort != 'low'):
            return 'NATIVE_PROFILE_UNSUPPORTED'
        return None

    def _research_permit(self, request, *, phase):
        # A host boolean, copied receipt or arbitrary callable is not an
        # evaluation permit. The fixed authority re-reads its exact durable
        # slot, source/profile/config identities and live queue ownership.
        from .provider_evaluation_runs import ResearchEvaluationAuthority
        from .provider_host_authority import HostResearchAuthority
        from .provider_loop_native_authority import LoopNativeResearchAuthority
        host = type(self._research_authorization) is HostResearchAuthority
        loop = type(self._research_authorization) is LoopNativeResearchAuthority
        if type(self._research_authorization) not in {ResearchEvaluationAuthority, HostResearchAuthority, LoopNativeResearchAuthority}:
            raise _Stopped('RESEARCH_V2_REAL_UNQUALIFIED')
        permit = self._research_authorization.verify_native(request, phase=phase)
        keys = {'evaluation_id', 'plan_digest', 'case_id', 'permit_id', 'proposal_id', 'attempt_id',
                'request_digest', 'config_digest', 'profile_digest', 'prepared_digest',
                'dispatch_intent_digest', 'contract_id', 'operation'}
        if host:
            keys = {'grant_id', 'grant_digest', 'session_id', 'permit_id', 'proposal_id', 'attempt_id',
                    'request_digest', 'config_digest', 'profile_digest', 'prepared_digest',
                    'dispatch_intent_digest', 'contract_id', 'operation', 'source_kind'}
        if loop:
            keys = {'authority_id', 'grant_id', 'grant_digest', 'program_id', 'plan_digest', 'call_id',
                    'session_id', 'reservation_token', 'permit_id', 'proposal_id', 'attempt_id',
                    'request_digest', 'context_digest', 'config_digest', 'profile_digest', 'prepared_digest',
                    'target_digest', 'dispatch_intent_digest', 'contract_id', 'operation', 'source_kind'}
        allowed = ((permit.get('operation'), permit.get('source_kind')) in
                   {('extract_material', 'paper_selection'), ('generate_candidates', 'accepted_material')}) if host and isinstance(permit, dict) else isinstance(permit, dict) and permit.get('operation') == 'generate_candidates'
        if loop and isinstance(permit, dict):
            allowed = (permit.get('authority_id') == 'research_loop_native_v1' and
                       (permit.get('operation'), permit.get('source_kind')) in {
                        ('extract_material', 'paper_selection'), ('generate_candidates', 'accepted_material'),
                        ('generate_candidates', 'research_revision'), ('repair_candidate', 'research_revision'),
                        ('decide_next_action', 'research_revision')})
        if (not isinstance(permit, dict) or set(permit) != keys
                or permit['contract_id'] != RESEARCH_CONTRACT_ID
                or not allowed
                or permit['proposal_id'] != request.proposal_id or permit['attempt_id'] != request.attempt_id
                or permit['config_digest'] != digest(request.config.model_dump())):
            raise _Stopped('RESEARCH_AUTHORIZATION_INVALID')
        # Re-serialize a closed, bounded summary before saving it. It contains
        # only IDs and digests, never an authority object or a private path.
        id_keys = {'evaluation_id', 'case_id', 'permit_id', 'proposal_id', 'attempt_id'}
        if host:
            id_keys = {'grant_id', 'session_id', 'permit_id', 'proposal_id', 'attempt_id'}
        if loop:
            id_keys = {'grant_id', 'program_id', 'call_id', 'session_id', 'reservation_token', 'permit_id', 'proposal_id', 'attempt_id'}
        if any(not isinstance(permit[key], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', permit[key])
               for key in id_keys):
            raise _Stopped('RESEARCH_AUTHORIZATION_INVALID')
        for key in keys - id_keys - {'contract_id', 'operation', 'source_kind', 'authority_id'}:
            if key == 'dispatch_intent_digest' and phase == 'entry' and permit[key] is None:
                continue
            if not isinstance(permit[key], str) or not re.fullmatch(r'[a-f0-9]{64}', permit[key]):
                raise _Stopped('RESEARCH_AUTHORIZATION_INVALID')
        if phase == 'entry' and permit['dispatch_intent_digest'] is not None:
            raise _Stopped('RESEARCH_AUTHORIZATION_INVALID')
        return strict_json(canonical_json(permit).encode(), max_bytes=16384)

    def _research_operation(self, request):
        from .provider_research_contract import PROMPT_TEMPLATE, PROMPT_VERSION
        from .provider_contract_registry import contract_handler
        from .paper_material_spans import validate_context_mode
        from .provider_host_authority import HostResearchAuthority
        from .provider_loop_native_authority import LoopNativeResearchAuthority
        span_variant = False
        if request.input_text.startswith(PROMPT_TEMPLATE):
            template, version = PROMPT_TEMPLATE, PROMPT_VERSION
        else:
            from .provider_material_spans_contract import PROMPT_TEMPLATE as SPAN_TEMPLATE, PROMPT_VERSION as SPAN_VERSION
            if not request.input_text.startswith(SPAN_TEMPLATE):
                raise _Stopped('RESEARCH_PREPARED_MISMATCH')
            template, version, span_variant = SPAN_TEMPLATE, SPAN_VERSION, True
        try:
            payload = strict_json(request.input_text[len(template):].encode(),
                                  max_bytes=request.budget.max_context_bytes)
            matched = (isinstance(payload, dict) and type(payload.get('schema_version')) is int
                and payload.get('schema_version') == 2
                and payload.get('contract_id') == RESEARCH_CONTRACT_ID
                and payload.get('prompt_version') == version
                and payload.get('proposal_id') == request.proposal_id
                and payload.get('attempt_id') == request.attempt_id
                and payload.get('context_digest') == request.context_digest
                and isinstance(payload.get('context'), dict)
                and digest(payload['context']) == request.context_digest)
            if matched:
                context = payload['context']
                actual_span = validate_context_mode(context, operation=payload.get('operation'))
                handler = contract_handler(schema_version=2, contract_id=RESEARCH_CONTRACT_ID)
                matched = (actual_span == span_variant
                    and request.output_schema == handler.output_schema(context))
        except (ValueError, TypeError, RecursionError):
            matched = False
        if not matched:
            raise _Stopped('RESEARCH_PREPARED_MISMATCH')
        if self.config.native_transport == 'app_server' and payload.get('operation') != 'generate_candidates':
            raise _Stopped('APP_SERVER_OPERATION_UNSUPPORTED')
        is_host = type(self._research_authorization) is HostResearchAuthority
        is_loop = type(self._research_authorization) is LoopNativeResearchAuthority
        if span_variant and (not (is_host or is_loop) or payload.get('operation') != 'extract_material'):
            raise _Stopped('RESEARCH_OPERATION_UNQUALIFIED')
        allowed_operations = {'extract_material', 'generate_candidates'} if is_host else {'generate_candidates'}
        if is_loop:
            allowed_operations = {'extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action'}
        if payload.get('operation') not in allowed_operations:
            raise _Stopped('RESEARCH_OPERATION_UNQUALIFIED')

    async def check_ready(self) -> ProviderReadiness:
        """Static only: no subprocess, authentication or inference on reads."""
        code = self._configuration_error()
        if code is None and self.contract_id == RESEARCH_CONTRACT_ID and self._research_authorization is None:
            code = 'RESEARCH_V2_REAL_UNQUALIFIED'
        return ProviderReadiness(provider='codex_cli', configured=code is None, checked_at=_now(),
            errors=[ProviderError(code=code, message='Native pilot configuration is unavailable.')] if code else [])

    async def propose(self, request: PreparedProposalRequest, control: ProviderCallControl) -> ProviderAttemptResult:
        if self.config.native_transport == 'app_server':
            return await self._propose_app_server(request, control)
        started = time.monotonic()
        receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
            started_at=_now(), finished_at=_now(), elapsed_seconds=0, requested_model=self.config.model, fixture=False)
        raw, error, temporary, prepared = None, None, None, False
        captures, evidence = [], {'native_pilot_version': 1, 'automatic_provider_retry': False,
            'host_authorized': self._authorized, 'boundary_checks': {}, 'dispatch_started': False,
            'model_calls': None, 'cost': None, 'context_policy_version': CONTEXT_POLICY_VERSION,
            'contract_id': self.contract_id}
        all_stopped, real_capture = True, None
        artifacts = Path(control.artifact_dir).absolute()

        def stop(code):
            raise _Stopped(code)

        def check_control():
            if control.cancel_event.is_set():
                stop('CANCELLED')
            if time.monotonic() >= deadline:
                stop('TIMEOUT')

        try:
            if self._attempted:
                stop('NATIVE_PROVIDER_ALREADY_USED')
            self._attempted = True
            code = self._configuration_error()
            if code:
                stop(code)
            if request.config != self.config:
                stop('CONFIGURATION_ERROR')
            if not math.isfinite(control.deadline_monotonic):
                stop('INVALID_DEADLINE')
            deadline = min(control.deadline_monotonic, started + request.budget.max_seconds)
            check_control()
            if any(value is not None for value in (request.budget.hard_token_limit,
                    request.budget.hard_model_call_limit, request.budget.hard_cost_limit)):
                stop('UNSUPPORTED_BUDGET')
            try:
                contract_id = contract_id_for_schema(request.output_schema)
            except (TypeError, ValueError):
                stop('SCHEMA_MISMATCH')
            # A v1 native pilot and its host boolean cannot grant v2 authority.
            # Keep this check ahead of artifacts, probes, authentication and CLI.
            if contract_id == RESEARCH_CONTRACT_ID and self._research_authorization is None:
                stop('RESEARCH_V2_REAL_UNQUALIFIED')
            if contract_id != self.contract_id:
                stop('CONTRACT_MISMATCH')
            input_bytes = request.input_text.encode('utf-8')
            if len(input_bytes) > request.budget.max_context_bytes:
                stop('INPUT_LIMIT')
            if contract_id == RESEARCH_CONTRACT_ID:
                self._research_operation(request)
            permit = self._research_permit(request, phase='entry') if contract_id == RESEARCH_CONTRACT_ID else None
            if permit is not None:
                evidence['research_authorization_entry'] = permit
            if '..' in artifacts.parts or any(path.is_symlink() for path in (artifacts, *artifacts.parents)):
                stop('ARTIFACT_CONFLICT')
            artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
            prepared = True
            identity, profile = _identity(self.config, contract_id=contract_id)
            if not profile['static_checks']['passed'] or profile['executable']['kind'] != 'native_binary':
                stop('NATIVE_EXECUTABLE_UNVERIFIED')
            evidence['identity'] = identity
            if permit is not None and permit['profile_digest'] != profile['profile_digest']:
                stop('PROFILE_DRIFT')
            evidence['requested_budget'] = request.budget.model_dump()
            evidence['effective_max_log_bytes'] = min(request.budget.max_log_bytes, MAX_LOG_BYTES)
            evidence['synthetic_probe_limits'] = {'max_log_bytes_per_case': 262144,
                'max_response_bytes_per_case': 65536, 'max_seconds_per_case': 12, 'max_cases': 5}
            evidence['deadline_scope'] = 'all_boundary_auth_and_real_subprocesses'
            _save(artifacts / 'profile.json', profile)
            _atomic_bytes(artifacts / 'input.txt', input_bytes)
            _save(artifacts / 'output.schema.json', request.output_schema)
            scoped = ProviderCallControl(deadline_monotonic=deadline, artifact_dir=artifacts,
                                        cancel_event=control.cancel_event, pass_fds=control.pass_fds)
            for kind, probe in (('write', probe_write), ('read', probe_read)):
                check_control()
                all_stopped = False
                options = {'allow_apply_patch': True} if kind == 'read' else {}
                try:
                    report = await probe(self.config.executable, self.config.model, artifacts / (kind + '-boundary'),
                        max_seconds=12, config=self.config, control=scoped, **options)
                except asyncio.CancelledError as exc:
                    all_stopped = getattr(exc, 'report', {}).get('process_stopped') is True
                    raise
                all_stopped = report.get('process_stopped') is True
                evidence['boundary_checks'][kind] = {'process_stopped': all_stopped,
                    'verified': _boundaries_match(report, kind, identity, profile)}
                if not all_stopped:
                    stop('OUTCOME_UNKNOWN')
                check_control()
                if not evidence['boundary_checks'][kind]['verified']:
                    stop(kind.upper() + '_BOUNDARY_UNVERIFIED')
                if _identity(self.config, contract_id=contract_id)[0] != identity:
                    stop('PROFILE_DRIFT')

            temporary = Path(tempfile.mkdtemp(prefix='paper-alpha-native-research-')).resolve()
            work = temporary / 'work'
            work.mkdir(mode=0o700)
            state = prepare_cli_state(temporary, with_native_auth=True)
            evidence['native_auth_linked'] = state['native_auth_linked']
            executable = profile['executable']['resolved']

            async def probe(arguments, label):
                check_control()
                capture = _Capture()
                captures.append(capture)
                await _run_bounded([executable, *arguments], cwd=work,
                    deadline=min(deadline, time.monotonic() + 5), cancel=control.cancel_event,
                    output_limit=min(request.budget.max_log_bytes, 65536), capture=capture,
                    pass_fds=control.pass_fds, supervision_dir=temporary / label, isolated_cli_home=state['path'])
                if not capture.stopped:
                    stop('OUTCOME_UNKNOWN')
                check_control()
                return capture

            version = await probe(['--version'], 'version')
            if version.error or version.returncode != 0 or version.stdout.decode(errors='replace').strip() != 'codex-cli 0.145.0':
                stop('CLI_VERSION_UNVERIFIED')
            receipt.cli_version = '0.145.0'
            auth = await probe(['login', 'status'], 'auth')
            auth_text = (auth.stdout + auth.stderr).decode(errors='replace').lower()
            if auth.error or auth.returncode != 0 or 'logged in' not in auth_text or 'chatgpt' not in auth_text:
                stop('AUTH_REQUIRED')
            evidence['native_authentication_confirmed'] = True
            schema_path, response_path = work / 'output.schema.json', work / 'response.raw.json'
            _save(schema_path, request.output_schema)
            argv = append_context_policy(build_read_boundary_argv(self.config, work, schema_path, response_path))
            wrapped = build_outer_invocation(argv, work=work, cli_state=state['path'],
                response_path=response_path, network_mode='native_tls',
                auth_file=(state['path'] / 'auth.json').resolve(strict=True))
            argv = wrapped['argv']
            invocation = {'argv': argv, 'outer_boundary': wrapped['boundary'], 'identity': identity,
                'native_executable_sha256': profile['executable']['sha256'],
                'input_sha256': hashlib.sha256(input_bytes).hexdigest(), 'schema_digest': digest(request.output_schema),
                'effective_max_log_bytes': evidence['effective_max_log_bytes'], 'automatic_provider_retry': False}
            if not _invocation_boundaries_match(invocation, identity):
                stop('OUTER_BOUNDARY_UNVERIFIED')
            _save(artifacts / 'invocation.json', invocation)
            # Bind the complete actual argv to the separately retained dispatch
            # and final evidence. This detects artifact drift; it is not a
            # signature against a host able to replace all files and code.
            evidence['invocation_digest'] = digest(invocation)
            check_control()
            if _identity(self.config, contract_id=contract_id)[0] != identity:
                stop('PROFILE_DRIFT')
            real_capture = _Capture()
            captures.append(real_capture)
            dispatch = {'provider_invocations': 1, 'model_calls': None,
                'proposal_id': request.proposal_id, 'attempt_id': request.attempt_id, 'identity': identity,
                'invocation_digest': evidence['invocation_digest']}
            if permit is not None:
                check_control()
                dispatched = self._research_permit(request, phase='dispatch')
                if {**dispatched, 'dispatch_intent_digest': None} != permit:
                    stop('RESEARCH_AUTHORIZATION_CHANGED')
                evidence['research_authorization_dispatch'] = dispatched
                dispatch['research_authorization'] = dispatched
            _save(artifacts / 'dispatch-intent.json', dispatch)
            check_control()
            await _run_bounded(argv, cwd=work, stdin=input_bytes, deadline=deadline,
                cancel=control.cancel_event, output_limit=evidence['effective_max_log_bytes'],
                capture=real_capture, pass_fds=control.pass_fds, response_path=response_path,
                response_limit=request.budget.max_output_bytes, supervision_dir=temporary / 'real',
                isolated_cli_home=state['path'])
            summary = summarize_cli_capture(stdout=real_capture.stdout, stderr=real_capture.stderr,
                exit_code=real_capture.returncode, process_stopped=real_capture.stopped,
                transport_error=real_capture.error)
            evidence['capture'] = summary
            usage = summary['usage']
            # Frozen v1 has no cache-write field: retain its separate observation
            # in raw and the structural capture, never fold it into input tokens.
            receipt.usage = ProviderUsage(**{key: usage[key] for key in
                ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens', 'complete')},
                observed_turns=summary['events']['turns_started'], tool_events=summary['events']['tool_events_observed'],
                raw=[{'cache_write_input_tokens': usage['cache_write_input_tokens']}]
                    if usage['cache_write_input_tokens'] is not None else [])
            try:
                raw = _bounded_private_file(response_path, request.budget.max_output_bytes)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                stop('INVALID_RESPONSE_FILE')
            if summary['error']:
                stop(summary['error'])
            check_control()
            if _identity(self.config, contract_id=contract_id)[0] != identity:
                stop('PROFILE_DRIFT')
            if raw is None:
                stop('MISSING_RESPONSE')
        except _Stopped as exc:
            error = ProviderError(code=exc.code, message='Native research attempt stopped; inspect its bounded evidence.')
        except asyncio.CancelledError:
            error = ProviderError(code='CANCELLED', message='Native research attempt was cancelled.')
        except Exception as exc:
            code = getattr(exc, 'code', 'NATIVE_ATTEMPT_FAILED')
            if not isinstance(code, str) or not re.fullmatch('[A-Z0-9_]{1,80}', code):
                code = 'NATIVE_ATTEMPT_FAILED'
            error = ProviderError(code=code, message='Native research attempt could not complete its controlled execution.')
        finally:
            receipt.finished_at = _now()
            receipt.elapsed_seconds = max(0, time.monotonic() - started)
            receipt.process_stopped = all_stopped and all(capture.stopped for capture in captures)
            if real_capture is not None:
                receipt.invocation_started = real_capture.started
                receipt.exit_code = real_capture.returncode
                receipt.stdout_bytes, receipt.stderr_bytes = len(real_capture.stdout), len(real_capture.stderr)
            if not receipt.process_stopped:
                error = ProviderError(code='OUTCOME_UNKNOWN', message='A subprocess stop was not confirmed; retry is forbidden.')
            if error is not None:
                receipt.usage.complete = False
            if raw is not None:
                receipt.response_sha256 = hashlib.sha256(raw).hexdigest()
            evidence['dispatch_started'] = receipt.invocation_started
            if temporary is not None:
                # Keep only bounded supervisor termination metadata before the
                # private native auth state and credential-adjacent logs vanish.
                fields = {'protocol_version', 'nonce', 'invocation_started', 'reason', 'leader_reaped',
                          'group_absent', 'process_stopped', 'exit_code', 'stdout_bytes', 'stderr_bytes',
                          'stdout_sha256', 'stderr_sha256', 'response_saved', 'response_sha256'}
                evidence['supervision_metadata'] = {}
                for label in ('version', 'auth', 'real'):
                    try:
                        stop_bytes = _bounded_private_file(temporary / label / 'stop.json', 65536)
                        metadata = strict_json(stop_bytes, max_bytes=65536)
                        if not isinstance(metadata, dict):
                            raise ValueError('Supervisor metadata must be an object')
                        _save(artifacts / (label + '-supervision.json'),
                              {key: value for key, value in metadata.items() if key in fields})
                        evidence['supervision_metadata'][label] = {
                            'saved': True, 'stop_sha256': hashlib.sha256(stop_bytes).hexdigest()}
                    except (OSError, ValueError):
                        evidence['supervision_metadata'][label] = {'saved': False}
                if receipt.process_stopped:
                    try:
                        shutil.rmtree(temporary)
                    except OSError:
                        evidence['retained_private_directory'] = str(temporary)
                        error = ProviderError(code='PRIVATE_STATE_CLEANUP_FAILED', message='Private CLI state cleanup failed.')
                        receipt.usage.complete = False
                else:
                    evidence['retained_private_directory'] = str(temporary)
            if prepared:
                try:
                    if raw is not None:
                        _atomic_bytes(artifacts / 'response.raw.json', raw)
                    _save(artifacts / 'native-evidence.json', evidence)
                    _save(artifacts / 'receipt.json', {'receipt': receipt.model_dump(),
                        'error': error.model_dump() if error else None})
                except OSError:
                    error = ProviderError(code='ARTIFACT_ERROR', message='Native attempt evidence could not be saved completely.')
        return ProviderAttemptResult(raw, error, receipt)

    async def _propose_app_server(self, request, control):
        """One generation through the original permits, journal and Supervisor.

        The old exec path above is kept intact. Synthetic qualification is
        re-run per attempt; it never supplies an authority or refunds a slot.
        """
        from .provider_app_server_business_identity import (
            business_dispatch_identity, prepare_business_dispatch_invocation,
            business_dispatch_invocation_boundaries_match)
        from .provider_app_server_business_context import (
            prepare_business_app_server_state_v2, validate_business_app_server_state_v2,
            describe_business_app_server_state_metadata)
        from .provider_app_server_invocation import BUSINESS_RUNTIME_TRANSPORT_VERSION
        from .provider_app_server_fresh_probe import prepare_fresh_identity, run_fresh_boundary
        from .provider_app_server_qualification import qualification_locator, verify_app_server_fresh_evidence
        from .provider_app_server_evidence import persist_business_capture
        from .provider_supervision_journal import ACTIVE_JOURNAL

        started = time.monotonic()
        receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
            started_at=_now(), finished_at=_now(), elapsed_seconds=0,
            requested_model=self.config.model, fixture=False)
        evidence = {'native_pilot_version': 2, 'transport_version': BUSINESS_RUNTIME_TRANSPORT_VERSION,
            'contract_id': self.contract_id, 'host_authorized': self._authorized,
            'automatic_provider_retry': False, 'dispatch_started': False,
            'model_calls': None, 'cost': None, 'boundary_checks': {},
            'supervision_metadata': {}}
        artifacts = Path(control.artifact_dir).absolute()
        raw = error = temporary = real_capture = invocation = identity = state = None
        prepared = False
        all_stopped = True
        persisted = False
        captures = []

        def stop(code):
            raise _Stopped(code)

        def check_control():
            if control.cancel_event.is_set():
                stop('CANCELLED')
            if time.monotonic() >= deadline:
                stop('TIMEOUT')

        try:
            if self._attempted:
                stop('NATIVE_PROVIDER_ALREADY_USED')
            self._attempted = True
            code = self._configuration_error()
            if code:
                stop(code)
            if request.config != self.config:
                stop('CONFIGURATION_ERROR')
            if self.contract_id != RESEARCH_CONTRACT_ID:
                stop('APP_SERVER_OPERATION_UNSUPPORTED')
            if not math.isfinite(control.deadline_monotonic):
                stop('INVALID_DEADLINE')
            deadline = min(control.deadline_monotonic, started + request.budget.max_seconds)
            check_control()
            if any(v is not None for v in (request.budget.hard_token_limit,
                    request.budget.hard_model_call_limit, request.budget.hard_cost_limit)):
                stop('UNSUPPORTED_BUDGET')
            if contract_id_for_schema(request.output_schema) != RESEARCH_CONTRACT_ID:
                stop('SCHEMA_MISMATCH')
            input_bytes = request.input_text.encode('utf-8')
            if len(input_bytes) > request.budget.max_context_bytes:
                stop('INPUT_LIMIT')
            self._research_operation(request)
            # The strict pure generation-plan validator rejects other operations
            # before even static identity, artifacts, native auth or probes.
            from .provider_app_server_invocation import business_runtime_plan
            business_runtime_plan(self.config, '/private/tmp/app-server-validation-work',
                                  request.input_text, request.output_schema)
            permit = self._research_permit(request, phase='entry')
            evidence['research_authorization_entry'] = permit
            journal = ACTIVE_JOURNAL.get()
            if journal is None or journal.binding.get('proposal_id') != request.proposal_id or journal.binding.get('attempt_id') != request.attempt_id:
                stop('APP_SERVER_ORIGINAL_JOURNAL_REQUIRED')
            if '..' in artifacts.parts or any(p.is_symlink() for p in (artifacts, *artifacts.parents)):
                stop('ARTIFACT_CONFLICT')
            artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
            prepared = True
            identity, profile = business_dispatch_identity(self.config)
            if not profile['static_checks']['passed'] or profile['executable']['kind'] != 'native_binary':
                stop('NATIVE_EXECUTABLE_UNVERIFIED')
            if permit['profile_digest'] != profile['profile_digest']:
                stop('PROFILE_DRIFT')
            evidence['identity'] = identity
            evidence['context_policy_version'] = identity['context_policy_version']
            evidence['requested_budget'] = request.budget.model_dump()
            evidence['effective_max_log_bytes'] = min(request.budget.max_log_bytes, MAX_LOG_BYTES)
            evidence['deadline_scope'] = 'whole_attempt_including_fresh_and_metadata'
            evidence['real_generation_seconds_cap'] = 180
            _save(artifacts / 'profile.json', profile)
            _atomic_bytes(artifacts / 'input.txt', input_bytes)
            _save(artifacts / 'output.schema.json', request.output_schema)
            scoped = ProviderCallControl(deadline_monotonic=deadline, artifact_dir=artifacts,
                cancel_event=control.cancel_event, pass_fds=control.pass_fds)
            expected = prepare_fresh_identity(self.config, scoped)
            all_stopped = False
            measured = await run_fresh_boundary(self.config, scoped, expected_identity=expected, seconds=12)
            report = measured['report']
            # A successful full re-read includes every original stopped group.
            if report.get('failure') is not None:
                evidence['fresh_evidence'] = qualification_locator(measured['report_path'], expected)
                stop('APP_SERVER_FRESH_UNVERIFIED')
            evidence['fresh_evidence'] = qualification_locator(measured['report_path'], expected)
            checked = verify_app_server_fresh_evidence(evidence, config=self.config,
                expected_current_identity=identity, original_controller=expected['controller'])
            all_stopped = True
            evidence['boundary_checks'] = {
                'read': {'verified': checked['read_verified'], 'process_stopped': True},
                'write': {'verified': checked['write_verified'], 'process_stopped': True}}
            evidence['fresh_assessment'] = checked
            check_control()
            if business_dispatch_identity(self.config)[0] != identity:
                stop('PROFILE_DRIFT')

            temporary = Path(tempfile.mkdtemp(prefix='paper-alpha-app-server-business-')).resolve()
            work = temporary / 'work'; work.mkdir(mode=0o700)
            state = prepare_business_app_server_state_v2(temporary,
                native_executable_identity=profile['executable'])
            evidence['native_auth_linked'] = state['native_auth_linked']
            executable = profile['executable']['resolved']

            async def metadata(arguments, label):
                check_control()
                evidence['controller_phase'] = 'before_' + label + '_validation'
                validate_business_app_server_state_v2(state,
                    native_executable_identity=profile['executable'])
                evidence['controller_phase'] = 'before_' + label + '_command'
                capture = _Capture(); captures.append(capture)
                await _run_bounded([executable, *arguments], cwd=work,
                    deadline=min(deadline, time.monotonic() + 5), cancel=control.cancel_event,
                    output_limit=min(request.budget.max_log_bytes, 65536), capture=capture,
                    pass_fds=control.pass_fds, supervision_dir=temporary / label,
                    isolated_cli_home=state['path'])
                if not capture.stopped:
                    stop('OUTCOME_UNKNOWN')
                evidence['controller_phase'] = 'after_' + label + '_command'
                check_control()
                return capture

            version = await metadata(['--version'], 'version')
            if version.error or version.returncode != 0 or version.stdout.decode(errors='replace').strip() != 'codex-cli 0.145.0':
                stop('CLI_VERSION_UNVERIFIED')
            receipt.cli_version = '0.145.0'
            auth = await metadata(['login', 'status'], 'auth')
            auth_text = (auth.stdout + auth.stderr).decode(errors='replace').lower()
            if auth.error or auth.returncode != 0 or 'logged in' not in auth_text or 'chatgpt' not in auth_text:
                stop('AUTH_REQUIRED')
            evidence['native_authentication_confirmed'] = True
            response_path = work / 'response.raw.json'
            invocation = prepare_business_dispatch_invocation(self.config, work=work,
                cli_state=state['path'], response_path=response_path, prompt=request.input_text,
                schema=request.output_schema, auth_file=state['auth_file'], business_context=state)
            if not business_dispatch_invocation_boundaries_match(invocation, identity):
                stop('OUTER_BOUNDARY_UNVERIFIED')
            _save(artifacts / 'invocation.json', invocation)
            evidence['invocation_digest'] = digest(invocation)
            check_control()
            validate_business_app_server_state_v2(state,
                native_executable_identity=profile['executable'])
            if business_dispatch_identity(self.config)[0] != identity:
                stop('PROFILE_DRIFT')
            dispatched = self._research_permit(request, phase='dispatch')
            if {**dispatched, 'dispatch_intent_digest': None} != permit:
                stop('RESEARCH_AUTHORIZATION_CHANGED')
            evidence['research_authorization_dispatch'] = dispatched
            dispatch = {'provider_invocations': 1, 'model_calls': None,
                'proposal_id': request.proposal_id, 'attempt_id': request.attempt_id,
                'transport_version': BUSINESS_RUNTIME_TRANSPORT_VERSION,
                'identity': identity, 'invocation_digest': evidence['invocation_digest'],
                'research_authorization': dispatched}
            _save(artifacts / 'dispatch-intent.json', dispatch)
            real_capture = _Capture(); captures.append(real_capture)
            await _run_bounded(invocation['argv'], cwd=work,
                deadline=min(deadline, time.monotonic() + 180), cancel=control.cancel_event,
                output_limit=evidence['effective_max_log_bytes'], capture=real_capture,
                pass_fds=control.pass_fds, response_path=response_path,
                response_limit=request.budget.max_output_bytes, supervision_dir=temporary / 'real',
                isolated_cli_home=state['path'], app_server_plan=invocation['rpc_plan'])
            retained = persist_business_capture(supervision_dir=temporary / 'real',
                artifact_dir=artifacts / 'app-server-capture', invocation=invocation,
                expected_identity=invocation['identity'], journal_dir=journal.root,
                expected_attempt_binding=journal.binding)
            persisted = True
            summary = retained['capture']
            evidence['capture'] = summary
            receipt.usage = ProviderUsage.model_validate(summary['usage'])
            receipt.reported_model = summary['reported_model']
            raw = retained['response']
            if not summary['valid']:
                stop(summary['error'] or 'APP_SERVER_CAPTURE_UNVERIFIED')
            check_control()
            if business_dispatch_identity(self.config)[0] != identity:
                stop('PROFILE_DRIFT')
        except _Stopped as exc:
            # Only validated stable codes are exposed; never raw stderr,
            # prompts, private paths or credential-bearing exception text.
            message = 'Native App Server attempt stopped; inspect its retained evidence.'
            captured = evidence.get('capture')
            if type(captured) is dict:
                codes = [(label, captured.get(key)) for label, key in (
                    ('primary', 'primary_error'), ('capture', 'secondary_capture_error'))]
                safe = [label + '=' + code for label, code in codes
                        if type(code) is str and re.fullmatch('[A-Z0-9_]{1,80}', code)]
                if safe:
                    message += ' ' + '; '.join(safe) + '.'
            error = ProviderError(code=exc.code, message=message)
        except asyncio.CancelledError:
            error = ProviderError(code='CANCELLED', message='Native App Server attempt was cancelled.')
        except Exception as exc:
            code = getattr(exc, 'code', 'APP_SERVER_ATTEMPT_FAILED')
            if not isinstance(code, str) or not re.fullmatch('[A-Z0-9_]{1,80}', code):
                code = 'APP_SERVER_ATTEMPT_FAILED'
            error = ProviderError(code=code, message='Native App Server attempt could not complete its controlled execution.')
        finally:
            receipt.finished_at = _now(); receipt.elapsed_seconds = max(0, time.monotonic() - started)
            receipt.process_stopped = all_stopped and all(c.stopped for c in captures)
            if real_capture is not None:
                receipt.invocation_started = real_capture.started
                receipt.exit_code = real_capture.returncode
                receipt.stdout_bytes = len(real_capture.stdout); receipt.stderr_bytes = len(real_capture.stderr)
            if not receipt.process_stopped:
                error = ProviderError(code='OUTCOME_UNKNOWN', message='A subprocess stop was not confirmed; retry is forbidden.')
            if raw is not None:
                receipt.response_sha256 = hashlib.sha256(raw).hexdigest()
            if error is not None:
                receipt.usage.complete = False
            evidence['dispatch_started'] = receipt.invocation_started
            if temporary is not None:
                if error is not None and state is not None:
                    try:
                        diagnostics = describe_business_app_server_state_metadata(state)
                        _save(artifacts / 'context-metadata.json', diagnostics)
                        evidence['context_metadata_diagnostics'] = {
                            'saved': True, 'digest': digest(diagnostics),
                            'scope': 'metadata_only_no_auth_or_file_content_reads'}
                    except (OSError, ValueError):
                        evidence['context_metadata_diagnostics'] = {'saved': False}
                for label in ('version', 'auth', 'real'):
                    try:
                        stop_bytes = _bounded_private_file(temporary / label / 'stop.json', 65536)
                        metadata_value = strict_json(stop_bytes, max_bytes=65536)
                        _save(artifacts / (label + '-supervision.json'), metadata_value)
                        evidence['supervision_metadata'][label] = {'saved': True,
                            'stop_sha256': hashlib.sha256(stop_bytes).hexdigest()}
                    except (OSError, ValueError):
                        evidence['supervision_metadata'][label] = {'saved': False}
                # Preserve temporary proof if the business capture was not
                # durably saved; never call incomplete persistence ready.
                can_cleanup = receipt.process_stopped and error is None and (real_capture is None or persisted)
                if can_cleanup:
                    try:
                        shutil.rmtree(temporary)
                    except OSError:
                        evidence['retained_private_directory'] = str(temporary)
                        error = ProviderError(code='PRIVATE_STATE_CLEANUP_FAILED', message='Private CLI state cleanup failed.')
                        receipt.usage.complete = False
                else:
                    evidence['retained_private_directory'] = str(temporary)
            if prepared:
                try:
                    if raw is not None:
                        _atomic_bytes(artifacts / 'response.raw.json', raw)
                    _save(artifacts / 'native-evidence.json', evidence)
                    _save(artifacts / 'receipt.json', {'receipt': receipt.model_dump(),
                        'error': error.model_dump() if error else None})
                    # A narrowly versioned failure proof is available only
                    # before auth after a successful supervised version read.
                    # Missing capture elsewhere remains unknown. Old failed
                    # attempts are never upgraded or granted a replacement.
                    if error is not None and evidence.get('controller_phase') == 'before_auth_validation':
                        from .provider_research_qualification import write_app_server_pre_dispatch_failure
                        try:
                            write_app_server_pre_dispatch_failure(artifacts,
                                failure_phase='after_version_before_auth_validation_phase',
                                error_code=error.code, receipt=receipt.model_dump(),
                                identity=identity,
                                entry_permit=evidence['research_authorization_entry'],
                                journal_root=journal.root,
                                expected_attempt_binding=journal.binding,
                                fresh_assessment=evidence['fresh_assessment'],
                                version_stop=strict_json(_bounded_private_file(
                                    artifacts / 'version-supervision.json', 65536), max_bytes=65536))
                        except Exception:
                            # No weakened interpretation or manufactured zero
                            # on incomplete evidence; the acceptor stays strict.
                            _save(artifacts / 'pre-dispatch-proof-error.json', {
                                'saved': False, 'phase': 'before_auth_validation',
                                'scope': 'incomplete_failure_proof_remains_unknown'})
                except OSError:
                    error = ProviderError(code='ARTIFACT_ERROR', message='Native attempt evidence could not be saved completely.')
        return ProviderAttemptResult(raw, error, receipt)
