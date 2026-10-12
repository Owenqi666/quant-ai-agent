"""Disabled business preparation; no process, authorization or qualification.

Live preparation binds the original mandatory outer wrapper to a bounded plan.
Historical matching is pure: the caller supplies a trusted freshly built identity,
never a readiness flag or a self-digested envelope as authority. B2-B5 must supply
fresh controls, capture, context isolation and original authority before dispatch.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import PurePosixPath
import re

from .provider_app_server_invocation import (
    BUSINESS_TRANSPORT_VERSION, BUSINESS_RPC_POLICY_DIGEST, business_plan,
    render_business_app_server_argv,
)
from .provider_app_server_profile import build_app_server_profile, validate_app_server_profile
from .provider_contract import canonical_json, digest
from .provider_outer_sandbox import (
    build_outer_invocation, assess_outer_boundary, static_descriptor,
    _saved_static_descriptor,
)
from .provider_read_boundary import (
    READ_BOUNDARY_POLICY_VERSION, READ_BOUNDARY_POLICY_DIGEST,
    outer_boundary_identity, outer_evidence_matches_identity,
)
from .provider_schema import ProviderConfig

IDENTITY_POLICY_VERSION = 'native_app_server_business_identity_v1'
INNER_TEMPLATE_VERSION = 'native_app_server_business_inner_argv_v1'
CONTEXT_POLICY_VERSION = 'app_server_isolated_home_pending_fresh_context_v1'
MAX_IDENTITY_BYTES = 768 * 1024
MAX_INVOCATION_BYTES = 1024 * 1024
_HEX = re.compile(r'[a-f0-9]{64}')
_IDENTITY_KEYS = {
    'identity_version', 'identity_policy_version', 'transport_version', 'profile',
    'native_inner_argv', 'read_policy_version', 'read_policy_digest',
    'context_policy_version', 'outer_identity', 'identity_digest',
    'prepared_plan_digest', 'prepared_layout',
}
_INVOCATION_KEYS = {
    'invocation_version', 'transport_version', 'execution_state', 'identity',
    'identity_digest', 'rpc_plan', 'rpc_plan_digest', 'prompt_sha256',
    'output_schema_digest', 'argv', 'argv_digest', 'outer_boundary',
}


class AppServerPreparationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _bounded(value, limit):
    # Depth is rejected before encoding, including cyclic Python containers.
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            raise AppServerPreparationError('APP_SERVER_PREPARATION_DEPTH_LIMIT')
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)
    if len(canonical_json(value).encode('utf-8')) > limit:
        raise AppServerPreparationError('APP_SERVER_PREPARATION_SIZE_LIMIT')


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 for char in value)):
        raise AppServerPreparationError('APP_SERVER_PREPARATION_PATH_INVALID')
    path = PurePosixPath(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or path == PurePosixPath('/')):
        raise AppServerPreparationError('APP_SERVER_PREPARATION_PATH_INVALID')
    return path


def _validated_identity(value):
    _bounded(value, MAX_IDENTITY_BYTES)
    if (not isinstance(value, dict) or set(value) != _IDENTITY_KEYS
            or type(value['identity_version']) is not int or value['identity_version'] != 1
            or value['identity_policy_version'] != IDENTITY_POLICY_VERSION
            or value['transport_version'] != BUSINESS_TRANSPORT_VERSION
            or value['read_policy_version'] != READ_BOUNDARY_POLICY_VERSION
            or value['read_policy_digest'] != READ_BOUNDARY_POLICY_DIGEST
            or value['context_policy_version'] != CONTEXT_POLICY_VERSION
            or value['identity_digest'] != digest({k: v for k, v in value.items() if k != 'identity_digest'})):
        raise AppServerPreparationError('APP_SERVER_IDENTITY_INVALID')
    profile = validate_app_server_profile(value['profile'])
    if (profile['static_checks']['passed'] is not True
            or profile['executable']['kind'] != 'native_binary'
            or profile['transport_version'] != BUSINESS_TRANSPORT_VERSION
            or profile['rpc_policy_digest'] != BUSINESS_RPC_POLICY_DIGEST):
        raise AppServerPreparationError('APP_SERVER_STATIC_PROFILE_FAILED')
    executable = profile['executable']['resolved']
    _path(executable)
    expected = render_business_app_server_argv(profile['config_snapshot'], executable=executable)
    template = value['native_inner_argv']
    if (not isinstance(template, dict)
            or set(template) != {'template_version', 'argv_template', 'argv_template_digest'}
            or template['template_version'] != INNER_TEMPLATE_VERSION
            or template['argv_template'] != expected or profile['argv_template'] != expected
            or template['argv_template_digest'] != digest(expected)):
        raise AppServerPreparationError('APP_SERVER_INNER_TEMPLATE_INVALID')
    outer = value['outer_identity']
    if (not isinstance(outer, dict)
            or set(outer) != {'descriptor', 'helper_sha256', 'launcher_path', 'launcher_sha256'}
            or outer['descriptor'] != static_descriptor()
            or outer['launcher_path'] != '/usr/bin/sandbox-exec'
            or any(not isinstance(outer[k], str) or not _HEX.fullmatch(outer[k])
                   for k in ('helper_sha256', 'launcher_sha256'))
            or outer['helper_sha256'] != profile['source_digests']['paper_alpha/server/provider_outer_sandbox.py']):
        raise AppServerPreparationError('APP_SERVER_OUTER_IDENTITY_INVALID')
    layout = value['prepared_layout']
    plan_digest = value['prepared_plan_digest']
    if layout is None:
        if plan_digest is not None:
            raise AppServerPreparationError('APP_SERVER_PREPARED_IDENTITY_INVALID')
    else:
        if (not isinstance(layout, dict)
                or set(layout) != {'work', 'cli_state', 'response_path', 'auth_exact_path'}
                or not isinstance(plan_digest, str) or not _HEX.fullmatch(plan_digest)):
            raise AppServerPreparationError('APP_SERVER_PREPARED_IDENTITY_INVALID')
        work, state, response = _path(layout['work']), _path(layout['cli_state']), _path(layout['response_path'])
        if (work == state or state.parent != work.parent or response != work / 'response.raw.json'):
            raise AppServerPreparationError('APP_SERVER_PREPARED_IDENTITY_INVALID')
        auth = _path(layout['auth_exact_path'])
        if auth.name != 'auth.json' or auth.is_relative_to(work.parent):
            raise AppServerPreparationError('APP_SERVER_PREPARED_IDENTITY_INVALID')
    return profile


def app_server_identity(config):
    """Static current identity only; no version/auth query or inference."""
    profile = build_app_server_profile(config)
    value = {
        'identity_version': 1, 'identity_policy_version': IDENTITY_POLICY_VERSION,
        'transport_version': BUSINESS_TRANSPORT_VERSION, 'profile': profile,
        'native_inner_argv': {'template_version': INNER_TEMPLATE_VERSION,
            'argv_template': deepcopy(profile['argv_template']),
            'argv_template_digest': profile['argv_template_digest']},
        'read_policy_version': READ_BOUNDARY_POLICY_VERSION,
        'read_policy_digest': READ_BOUNDARY_POLICY_DIGEST,
        'context_policy_version': CONTEXT_POLICY_VERSION,
        'outer_identity': outer_boundary_identity(),
        'prepared_plan_digest': None, 'prepared_layout': None,
    }
    value['identity_digest'] = digest(value)
    _validated_identity(value)
    return deepcopy(value), deepcopy(profile)


def prepare_business_app_server_invocation(config, *, work, cli_state, response_path,
                                          prompt, schema, auth_file=None):
    """Build metadata for a disabled entry. This never dispatches anything.

    Validate the whole plan before executable/source/outer/auth metadata checks.
    Auth, if supplied by a future host controller, is only passed to the original
    outer builder; it is never read or copied here.
    """
    plan = business_plan(config, work, prompt, schema)
    config = ProviderConfig.model_validate(config)
    work_path, state_path, response = _path(plan['cwd']), _path(str(cli_state)), _path(str(response_path))
    if (work_path == state_path or state_path.parent != work_path.parent
            or response != work_path / 'response.raw.json'):
        raise AppServerPreparationError('APP_SERVER_PREPARATION_LAYOUT_INVALID')
    if auth_file is None:
        raise AppServerPreparationError('APP_SERVER_NATIVE_AUTH_METADATA_REQUIRED')
    auth = _path(str(auth_file))
    if auth.name != 'auth.json' or auth.is_relative_to(work_path.parent):
        raise AppServerPreparationError('APP_SERVER_PREPARATION_LAYOUT_INVALID')
    identity, profile = app_server_identity(config)
    wrapped = build_outer_invocation(profile['argv_template'], work=str(work_path),
        cli_state=str(state_path), response_path=str(response), network_mode='native_tls',
        auth_file=auth_file)
    # Binding held by the host for this prepared request, separate from the
    # transport-wide static identity. Recomputing an envelope's digests cannot
    # replace this expected identity in the caller's dispatch evidence.
    identity['prepared_plan_digest'] = digest(plan)
    identity['prepared_layout'] = {key: wrapped['boundary'][key] for key in
        ('work', 'cli_state', 'response_path', 'auth_exact_path')}
    identity['identity_digest'] = digest({k: v for k, v in identity.items() if k != 'identity_digest'})
    value = {
        'invocation_version': 1, 'transport_version': BUSINESS_TRANSPORT_VERSION,
        'execution_state': 'disabled_preparation', 'identity': identity,
        'identity_digest': identity['identity_digest'], 'rpc_plan': plan,
        'rpc_plan_digest': digest(plan),
        'prompt_sha256': hashlib.sha256(plan['prompt'].encode('utf-8')).hexdigest(),
        'output_schema_digest': digest(plan['output_schema']),
        'argv': wrapped['argv'], 'argv_digest': digest(wrapped['argv']),
        'outer_boundary': wrapped['boundary'],
    }
    if not app_server_invocation_boundaries_match(value, identity):
        raise AppServerPreparationError('APP_SERVER_INVOCATION_BINDING_FAILED')
    return deepcopy(value)


def app_server_invocation_boundaries_match(invocation, identity):
    """Pure matching against caller-supplied current identity, never authority.

    Does not stat cleaned work/state/response paths, run a CLI or build a current
    profile. Saved digests cannot prove authenticity against a privileged writer.
    """
    try:
        _bounded(invocation, MAX_INVOCATION_BYTES)
        profile = _validated_identity(identity)
        if (not isinstance(invocation, dict) or set(invocation) != _INVOCATION_KEYS
                or type(invocation['invocation_version']) is not int
                or invocation['invocation_version'] != 1
                or invocation['transport_version'] != BUSINESS_TRANSPORT_VERSION
                or invocation['execution_state'] != 'disabled_preparation'
                or canonical_json(invocation['identity']) != canonical_json(identity)
                or invocation['identity_digest'] != identity['identity_digest']
                or identity['prepared_layout'] is None):
            return False
        boundary, argv, plan = invocation['outer_boundary'], invocation['argv'], invocation['rpc_plan']
        if not isinstance(boundary, dict) or not isinstance(plan, dict):
            return False
        rebuilt = business_plan(profile['config_snapshot'], plan.get('cwd'),
                                plan.get('prompt'), plan.get('output_schema'))
        work, state, response = _path(boundary['work']), _path(boundary['cli_state']), _path(boundary['response_path'])
        return bool(
            plan == rebuilt and canonical_json(plan) == canonical_json(rebuilt)
            and invocation['rpc_plan_digest'] == digest(rebuilt) == identity['prepared_plan_digest']
            and {key: boundary[key] for key in identity['prepared_layout']} == identity['prepared_layout']
            and invocation['prompt_sha256'] == hashlib.sha256(rebuilt['prompt'].encode('utf-8')).hexdigest()
            and invocation['output_schema_digest'] == digest(profile['wire_schema']) == digest(rebuilt['output_schema'])
            and str(work) == plan['cwd'] and state.parent == work.parent and state != work
            and response == work / 'response.raw.json'
            and boundary['inner_argv'] == profile['argv_template']
            and invocation['argv_digest'] == digest(argv)
            and assess_outer_boundary(boundary, argv)['valid'] is True
            and outer_evidence_matches_identity(boundary, identity['outer_identity'])
            and boundary['native_executable_path'] == profile['executable']['resolved']
            and boundary['native_executable_sha256'] == profile['executable']['sha256']
            and boundary['purpose'] == 'native_real' and boundary['network_mode'] == 'native_tls'
            and boundary['synthetic_write_target'] is None and boundary['loopback_port'] is None
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, UnicodeError):
        return False


# Original B1 preparation above remains disabled and keeps its own matcher.
from .provider_app_server_invocation import (
    BUSINESS_RUNTIME_TRANSPORT_VERSION as BUSINESS_DISPATCH_TRANSPORT_VERSION,
    BUSINESS_RUNTIME_RPC_POLICY_DIGEST, BUSINESS_CONTEXT_POLICY_VERSION,
    render_business_runtime_app_server_argv, business_runtime_plan,
)
from .provider_app_server_profile import (
    build_business_runtime_profile, validate_business_runtime_profile,
)

DISPATCH_IDENTITY_POLICY_VERSION = 'native_app_server_business_identity_v2'
DISPATCH_INNER_TEMPLATE_VERSION = 'native_app_server_business_inner_argv_v2'
_DISPATCH_INVOCATION_KEYS = _INVOCATION_KEYS | {'business_context'}


def _validated_dispatch_identity(value, *, historical=False):
    if type(historical) is not bool:
        raise AppServerPreparationError('APP_SERVER_DISPATCH_IDENTITY_INVALID')
    transport = value.get('transport_version') if type(value) is dict else None
    allowed = {
        'codex_app_server_business_stdio_v3', 'codex_app_server_business_stdio_v4',
        'codex_app_server_business_stdio_v5', 'codex_app_server_business_stdio_v6',
        'codex_app_server_business_stdio_v7',
        'codex_app_server_business_stdio_v8', 'codex_app_server_business_stdio_v9', 'codex_app_server_business_stdio_v10',
    } if historical else {BUSINESS_DISPATCH_TRANSPORT_VERSION}
    if type(transport) is not str or transport not in allowed:
        raise AppServerPreparationError('APP_SERVER_DISPATCH_IDENTITY_INVALID')
    _bounded(value, MAX_IDENTITY_BYTES)
    if (type(value) is not dict or set(value) != _IDENTITY_KEYS
            or type(value['identity_version']) is not int or value['identity_version'] != 2
            or value['identity_policy_version'] != DISPATCH_IDENTITY_POLICY_VERSION
            or value['transport_version'] != transport
            or value['context_policy_version'] != BUSINESS_CONTEXT_POLICY_VERSION
            or value['read_policy_version'] != READ_BOUNDARY_POLICY_VERSION
            or value['read_policy_digest'] != READ_BOUNDARY_POLICY_DIGEST
            or value['identity_digest'] != digest({k: v for k, v in value.items() if k != 'identity_digest'})):
        raise AppServerPreparationError('APP_SERVER_DISPATCH_IDENTITY_INVALID')
    profile = validate_business_runtime_profile(value['profile'], historical=historical)
    if (profile['static_checks']['passed'] is not True
            or profile['executable']['kind'] != 'native_binary'
            or profile['transport_version'] != transport):
        raise AppServerPreparationError('APP_SERVER_DISPATCH_PROFILE_FAILED')
    executable = profile['executable']['resolved']
    _path(executable)
    argv = render_business_runtime_app_server_argv(profile['config_snapshot'], executable=executable)
    template = value['native_inner_argv']
    if (type(template) is not dict or set(template) != {'template_version', 'argv_template', 'argv_template_digest'}
            or template['template_version'] != DISPATCH_INNER_TEMPLATE_VERSION
            or canonical_json(template['argv_template']) != canonical_json(argv)
            or template['argv_template_digest'] != digest(argv)):
        raise AppServerPreparationError('APP_SERVER_DISPATCH_INNER_TEMPLATE_INVALID')
    if historical:
        saved_outer_policy = (
            'macos_outer_read_boundary_v5'
            if transport in {'codex_app_server_business_stdio_v6', 'codex_app_server_business_stdio_v7', 'codex_app_server_business_stdio_v8', 'codex_app_server_business_stdio_v9', 'codex_app_server_business_stdio_v10'} else
            'macos_outer_read_boundary_v4'
            if transport == 'codex_app_server_business_stdio_v5'
            else 'macos_outer_read_boundary_v3'
        )
        expected_outer_descriptor = _saved_static_descriptor(saved_outer_policy)
    else:
        expected_outer_descriptor = static_descriptor()
    outer = value['outer_identity']
    if (type(outer) is not dict or set(outer) != {'descriptor', 'helper_sha256', 'launcher_path', 'launcher_sha256'}
            or canonical_json(outer['descriptor']) != canonical_json(expected_outer_descriptor)
            or outer['launcher_path'] != '/usr/bin/sandbox-exec'
            or any(type(outer[key]) is not str or _HEX.fullmatch(outer[key]) is None for key in ('helper_sha256', 'launcher_sha256'))
            or outer['helper_sha256'] != profile['source_digests']['paper_alpha/server/provider_outer_sandbox.py']):
        raise AppServerPreparationError('APP_SERVER_DISPATCH_OUTER_IDENTITY_INVALID')
    layout = value['prepared_layout']
    if layout is None:
        if value['prepared_plan_digest'] is not None:
            raise AppServerPreparationError('APP_SERVER_DISPATCH_LAYOUT_INVALID')
    else:
        if (type(layout) is not dict or set(layout) != {'work', 'cli_state', 'response_path', 'auth_exact_path'}
                or type(value['prepared_plan_digest']) is not str or _HEX.fullmatch(value['prepared_plan_digest']) is None):
            raise AppServerPreparationError('APP_SERVER_DISPATCH_LAYOUT_INVALID')
        work, state, response, auth = (_path(layout[key]) for key in ('work', 'cli_state', 'response_path', 'auth_exact_path'))
        if (state != work.parent / 'cli-state' or state == work or response != work / 'response.raw.json'
                or auth.name != 'auth.json' or auth.is_relative_to(work.parent)):
            raise AppServerPreparationError('APP_SERVER_DISPATCH_LAYOUT_INVALID')
    return profile


def business_dispatch_identity(config):
    """Current runtime identity; static checks do not grant business dispatch."""
    profile = build_business_runtime_profile(config)
    value = {'identity_version': 2, 'identity_policy_version': DISPATCH_IDENTITY_POLICY_VERSION,
        'transport_version': BUSINESS_DISPATCH_TRANSPORT_VERSION, 'profile': profile,
        'native_inner_argv': {'template_version': DISPATCH_INNER_TEMPLATE_VERSION,
            'argv_template': deepcopy(profile['argv_template']), 'argv_template_digest': profile['argv_template_digest']},
        'read_policy_version': READ_BOUNDARY_POLICY_VERSION, 'read_policy_digest': READ_BOUNDARY_POLICY_DIGEST,
        'context_policy_version': BUSINESS_CONTEXT_POLICY_VERSION, 'outer_identity': outer_boundary_identity(),
        'prepared_plan_digest': None, 'prepared_layout': None}
    value['identity_digest'] = digest(value)
    _validated_dispatch_identity(value)
    return deepcopy(value), deepcopy(profile)


def prepare_business_dispatch_invocation(config, *, work, cli_state, response_path,
        prompt, schema, auth_file, business_context=None):
    """Build a controller-bound runtime envelope; never create a grant/child.

    The host must already own an execution permit, current fresh gate and the
    original deadline/lease. This module cannot turn this envelope into either.
    """
    from .provider_app_server_business_context import (
        existing_business_app_server_state_v2, validate_business_app_server_state_v2,
        business_context_descriptor,
    )
    plan = business_runtime_plan(config, work, prompt, schema)
    work_path, state_path, response = _path(plan['cwd']), _path(str(cli_state)), _path(str(response_path))
    if state_path != work_path.parent / 'cli-state' or state_path == work_path or response != work_path / 'response.raw.json':
        raise AppServerPreparationError('APP_SERVER_DISPATCH_LAYOUT_INVALID')
    if auth_file is None:
        raise AppServerPreparationError('APP_SERVER_NATIVE_AUTH_METADATA_REQUIRED')
    identity, profile = business_dispatch_identity(config)
    state = (existing_business_app_server_state_v2(cli_state, auth_file,
                native_executable_identity=profile['executable']) if business_context is None
             else validate_business_app_server_state_v2(business_context,
                native_executable_identity=profile['executable']))
    context = business_context_descriptor(state)
    if str(state['path']) != str(state_path) or str(state['auth_file']) != str(auth_file):
        raise AppServerPreparationError('APP_SERVER_DISPATCH_CONTEXT_INVALID')
    wrapped = build_outer_invocation(profile['argv_template'], work=str(work_path), cli_state=str(state_path),
        response_path=str(response), network_mode='native_tls', auth_file=auth_file)
    identity['prepared_plan_digest'] = digest(plan)
    identity['prepared_layout'] = {key: wrapped['boundary'][key] for key in ('work', 'cli_state', 'response_path', 'auth_exact_path')}
    identity['identity_digest'] = digest({k: v for k, v in identity.items() if k != 'identity_digest'})
    value = {'invocation_version': 2, 'transport_version': BUSINESS_DISPATCH_TRANSPORT_VERSION,
        'execution_state': 'controller_bound_dispatch', 'identity': identity, 'identity_digest': identity['identity_digest'],
        'rpc_plan': plan, 'rpc_plan_digest': digest(plan),
        'prompt_sha256': hashlib.sha256(plan['prompt'].encode('utf-8')).hexdigest(),
        'output_schema_digest': digest(plan['output_schema']), 'argv': wrapped['argv'],
        'argv_digest': digest(wrapped['argv']), 'outer_boundary': wrapped['boundary'], 'business_context': context}
    if not business_dispatch_invocation_boundaries_match(value, identity):
        raise AppServerPreparationError('APP_SERVER_DISPATCH_INVOCATION_BINDING_FAILED')
    return deepcopy(value)


def business_dispatch_invocation_boundaries_match(invocation, expected_identity, *, historical=False):
    """Pure matcher; historical V3/V4 is saved interpretation, never dispatch.

    All live callers use the default current V5 branch.
    Original journal/authorization is separate.

    A current static identity can reconstruct the exact prepared layout/plan
    only for comparing current pins. It cannot establish who permitted those
    bytes, which must be proven by the original prepared/started/finished journal.
    Neither this matcher nor a saved self-digest authorizes a replay.
    """
    from .provider_app_server_business_context import validate_business_context_descriptor
    try:
        _bounded(invocation, MAX_INVOCATION_BYTES)
        profile = _validated_dispatch_identity(expected_identity, historical=historical)
        if (type(invocation) is not dict or set(invocation) != _DISPATCH_INVOCATION_KEYS
                or type(invocation['invocation_version']) is not int or invocation['invocation_version'] != 2
                or invocation['transport_version'] != expected_identity['transport_version']
                or invocation['execution_state'] != 'controller_bound_dispatch'):
            return False
        boundary, plan, argv = invocation['outer_boundary'], invocation['rpc_plan'], invocation['argv']
        if type(boundary) is not dict or type(plan) is not dict:
            return False
        if historical and invocation['transport_version'] == 'codex_app_server_business_stdio_v8':
            from .provider_app_server_invocation import business_runtime_v8_plan
            rebuilt = business_runtime_v8_plan(profile['config_snapshot'], plan.get('cwd'), plan.get('prompt'), plan.get('output_schema'))
            schema_digest = digest(rebuilt['output_schema'])
        elif historical and invocation['transport_version'] == 'codex_app_server_business_stdio_v10':
            from .provider_app_server_invocation import business_runtime_v10_plan
            rebuilt = business_runtime_v10_plan(profile['config_snapshot'], plan.get('cwd'), plan.get('prompt'), plan.get('output_schema'))
            schema_digest = digest(rebuilt['output_schema'])
        elif historical and invocation['transport_version'] == 'codex_app_server_business_stdio_v9':
            from .provider_app_server_invocation import business_runtime_v9_plan
            rebuilt = business_runtime_v9_plan(profile['config_snapshot'], plan.get('cwd'), plan.get('prompt'), plan.get('output_schema'))
            schema_digest = digest(rebuilt['output_schema'])
        elif historical:
            rebuilt = business_plan(profile['config_snapshot'], plan.get('cwd'), plan.get('prompt'), plan.get('output_schema'))
            schema_digest = profile['wire_schema_digest']
        else:
            from .provider_app_server_invocation import business_runtime_contract
            rebuilt = business_runtime_plan(profile['config_snapshot'], plan.get('cwd'), plan.get('prompt'), plan.get('output_schema'))
            lane = business_runtime_contract(rebuilt['prompt'], rebuilt['output_schema'])
            schema_digest = profile['rpc_policy']['operation_contracts'][lane]['schema_digest']
        context = validate_business_context_descriptor(invocation['business_context'])
        if (context['context_version'] != 2
                or context['context_policy_version'] != BUSINESS_CONTEXT_POLICY_VERSION
                or context['native_executable']['path'] != profile['executable']['resolved']
                or context['native_executable']['sha256'] != profile['executable']['sha256']
                or context['native_executable']['size'] != profile['executable']['size_bytes']):
            return False
        current = deepcopy(expected_identity)
        if current['prepared_layout'] is None:
            current['prepared_plan_digest'] = digest(rebuilt)
            current['prepared_layout'] = {key: boundary[key] for key in ('work', 'cli_state', 'response_path', 'auth_exact_path')}
            current['identity_digest'] = digest({k: v for k, v in current.items() if k != 'identity_digest'})
        _validated_dispatch_identity(current, historical=historical)
        return bool(
            canonical_json(invocation['identity']) == canonical_json(current)
            and invocation['identity_digest'] == current['identity_digest']
            and canonical_json(plan) == canonical_json(rebuilt)
            and invocation['rpc_plan_digest'] == digest(rebuilt) == current['prepared_plan_digest']
            and {key: boundary[key] for key in current['prepared_layout']} == current['prepared_layout']
            and context['home'] == context['codex_home'] == boundary['cli_state']
            and context['auth_exact_path'] == boundary['auth_exact_path']
            and invocation['prompt_sha256'] == hashlib.sha256(rebuilt['prompt'].encode('utf-8')).hexdigest()
            and invocation['output_schema_digest'] == schema_digest == digest(rebuilt['output_schema'])
            and boundary['work'] == plan['cwd']
            and canonical_json(boundary['inner_argv']) == canonical_json(profile['argv_template'])
            and invocation['argv_digest'] == digest(argv)
            and assess_outer_boundary(boundary, argv, historical=historical)['valid'] is True
            and outer_evidence_matches_identity(boundary, current['outer_identity'])
            and boundary['native_executable_path'] == profile['executable']['resolved']
            and boundary['native_executable_sha256'] == profile['executable']['sha256']
            and boundary['purpose'] == 'native_real' and boundary['network_mode'] == 'native_tls'
            and boundary['synthetic_write_target'] is None and boundary['loopback_port'] is None
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, UnicodeError, OSError):
        return False
