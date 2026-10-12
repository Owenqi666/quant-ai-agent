"""Independent static App Server business identity; no launch or authority.

Saved digest consistency is separate from current source/executable comparison.
Only the full research_provider_v2 schema is represented by this profile.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from . import provider_cli_profile as legacy
from .provider_contract import canonical_json, digest
from .provider_schema import DIGEST_PATTERN, ProviderConfig
from .provider_research_contract import (
    model_output_schema, PROMPT_TEMPLATE, PROMPT_VERSION,
)

PROFILE_VERSION = 2
POLICY_VERSION = 'codex_app_server_business_policy_v1'
TRANSPORT_VERSION = 'codex_app_server_business_stdio_v1'
CONTRACT_ID = 'research_provider_v2'
CONTEXT_TRANSPORT = 'full_research_provider_v2'
MAX_PROFILE_BYTES = 512 * 1024
_ROOT = Path(__file__).resolve().parents[2]
_ADDITIONAL_SOURCE_NAMES = (
    'paper_alpha/server/provider_app_server_invocation.py',
    'paper_alpha/server/provider_app_server_profile.py',
    'paper_alpha/server/provider_app_server_business_identity.py',
    'paper_alpha/server/provider_app_server_protocol.py',
    'paper_alpha/server/provider_app_server_probe.py',
    'paper_alpha/server/provider_app_server_write_observability.py',
    'paper_alpha/server/provider_app_server_write_intervention_probe.py',
    'paper_alpha/server/provider_app_server_fresh_probe.py',
    'paper_alpha/server/provider_loop_native_authority.py',
    'paper_alpha/server/provider_correction_native.py',
    'paper_alpha/server/native_correction_start.py',
)
# The union is code-owned. Caller file lists and historical subsets cannot
# replace a current execution boundary fingerprint.
APP_SERVER_SOURCE_NAMES = tuple(dict.fromkeys(
    legacy._SOURCE_NAMES + legacy._RESEARCH_SOURCE_NAMES + _ADDITIONAL_SOURCE_NAMES))
_B1_SOURCE_NAMES = APP_SERVER_SOURCE_NAMES
_RUNTIME_ADDITIONAL_SOURCE_NAMES = (
    'paper_alpha/server/provider_app_server_business_context.py',
    'paper_alpha/server/provider_app_server_capture.py',
    'paper_alpha/server/provider_app_server_evidence.py',
    'paper_alpha/server/provider_app_server_qualification.py',
    'paper_alpha/server/provider_native_transport_identity.py',
)
_ADDITIONAL_SOURCE_NAMES += _RUNTIME_ADDITIONAL_SOURCE_NAMES
APP_SERVER_SOURCE_NAMES = tuple(dict.fromkeys((*APP_SERVER_SOURCE_NAMES, *_RUNTIME_ADDITIONAL_SOURCE_NAMES)))


def _transport_contract():
    # The renderer owner defines these independent business constants. Lazy
    # import avoids introducing a cycle into the unchanged legacy profile.
    from .provider_app_server_invocation import (
        BUSINESS_TRANSPORT_VERSION, BUSINESS_RPC_POLICY, BUSINESS_RPC_POLICY_DIGEST,
        render_business_app_server_argv,
    )
    if BUSINESS_TRANSPORT_VERSION != TRANSPORT_VERSION or digest(BUSINESS_RPC_POLICY) != BUSINESS_RPC_POLICY_DIGEST:
        raise ValueError('BUSINESS_APP_SERVER_POLICY_MISMATCH')
    return BUSINESS_RPC_POLICY, BUSINESS_RPC_POLICY_DIGEST, render_business_app_server_argv


class AppServerCheckSet(legacy.ProfileCheckSet):
    sources_complete: bool
    business_transport_valid: bool


class AppServerStaticChecks(AuthorModel):
    passed: bool
    checks: AppServerCheckSet
    diagnostics: list[legacy.ProfileDiagnostic] = Field(max_length=1000)

    @model_validator(mode='after')
    def consistent_result(self):
        expected = all(self.checks.model_dump().values()) and not any(
            item.severity == 'error' for item in self.diagnostics)
        if self.passed != expected:
            raise ValueError('Business App Server static result differs')
        return self


class AppServerProfile(AuthorModel):
    profile_version: Literal[2]
    transport_version: Literal['codex_app_server_business_stdio_v1']
    policy_version: Literal['codex_app_server_business_policy_v1']
    contract_id: Literal['research_provider_v2']
    purpose: Literal['generate_candidates']
    context_transport: Literal['full_research_provider_v2']
    config_id: str = Field(min_length=1, max_length=128)
    mode: Literal['codex_cli', 'fixture']
    config_snapshot: dict
    config_digest: str = Field(pattern=DIGEST_PATTERN)
    executable: legacy.ExecutableIdentity
    expected_cli_version: str = Field(min_length=1, max_length=128)
    model: str | None = Field(max_length=128)
    reasoning_effort: Literal['low', 'medium', 'high', 'xhigh']
    argv_template: list[str] | None = Field(default=None, max_length=128)
    argv_template_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    rpc_policy: dict
    rpc_policy_digest: str = Field(pattern=DIGEST_PATTERN)
    wire_schema: dict
    wire_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    business_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    prompt_version: str = Field(min_length=1, max_length=128)
    prompt_digest: str = Field(pattern=DIGEST_PATTERN)
    source_digests: dict[str, str]
    static_checks: AppServerStaticChecks
    limitations: list[str] = Field(max_length=30)
    profile_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode='before')
    @classmethod
    def required_fields_and_integer(cls, value):
        if isinstance(value, dict):
            if type(value.get('profile_version')) is not int:
                raise ValueError('App Server profile version requires an integer')
            if not {'argv_template', 'argv_template_digest'} <= set(value):
                raise ValueError('Unknown argv identity must be explicit')
        return value

    @model_validator(mode='after')
    def bound_identity(self):
        config = ProviderConfig.model_validate(self.config_snapshot)
        snapshot = config.model_dump(mode='json')
        if snapshot != self.config_snapshot or digest(snapshot) != self.config_digest:
            raise ValueError('Business App Server configuration digest differs')
        if (self.config_id, self.mode, self.model, self.reasoning_effort, self.expected_cli_version) != (
                config.config_id, config.mode, config.model, config.reasoning_effort, config.expected_cli_version):
            raise ValueError('Business App Server configuration fields differ')
        if self.executable.configured != config.executable:
            raise ValueError('Configured executable differs')
        policy, policy_digest, renderer = _transport_contract()
        if (canonical_json(self.rpc_policy) != canonical_json(policy)
                or digest(self.rpc_policy) != self.rpc_policy_digest or self.rpc_policy_digest != policy_digest):
            raise ValueError('Business App Server RPC policy differs')
        if (canonical_json(self.wire_schema) != canonical_json(model_output_schema())
                or digest(self.wire_schema) != self.wire_schema_digest):
            raise ValueError('Only the full research_provider_v2 wire schema is supported')
        if self.business_schema_digest != self.wire_schema_digest:
            raise ValueError('This profile has no wire schema transformation')
        if (self.prompt_version != PROMPT_VERSION or self.prompt_digest !=
                hashlib.sha256(PROMPT_TEMPLATE.encode('utf-8')).hexdigest()):
            raise ValueError('Business App Server prompt identity differs')
        sources = self.source_digests
        if (set(sources) - set(APP_SERVER_SOURCE_NAMES) or any(
                not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                for value in sources.values())):
            raise ValueError('Business App Server source identity differs')
        # Exact saved B1 profiles retain their original closed source version;
        # current builders always fingerprint the full integrated union below.
        expected_sources = _B1_SOURCE_NAMES if set(sources) == set(_B1_SOURCE_NAMES) else APP_SERVER_SOURCE_NAMES
        complete = set(sources) == set(expected_sources)
        if self.static_checks.checks.sources_complete is not complete:
            raise ValueError('Business App Server source coverage result differs')
        unavailable = {item.path for item in self.static_checks.diagnostics
                       if item.code == 'SOURCE_IDENTITY_UNAVAILABLE' and item.severity == 'error'}
        if set(expected_sources) - set(sources) != unavailable:
            raise ValueError('Missing source identities require exact diagnostics')
        expected = None
        if self.executable.resolved is not None:
            try:
                expected = renderer(config, executable=self.executable.resolved)
            except ValueError:
                pass
        valid_transport = expected is not None
        if self.static_checks.checks.business_transport_valid is not valid_transport:
            raise ValueError('Business App Server transport result differs')
        if self.argv_template != expected or self.argv_template_digest != (digest(expected) if expected is not None else None):
            raise ValueError('Business App Server argv differs from its fixed renderer')
        if (self.executable.sha256 is None or self.executable.size_bytes is None) and self.static_checks.checks.executable_available:
            raise ValueError('Executable availability requires observed hash and size')
        value = self.model_dump(mode='json')
        if digest({key: item for key, item in value.items() if key != 'profile_digest'}) != self.profile_digest:
            raise ValueError('Business App Server profile digest differs')
        return self


def validate_app_server_profile(value) -> dict:
    """Saved structure/self consistency only; fresh current comparison is required."""
    if len(canonical_json(value).encode('utf-8')) > MAX_PROFILE_BYTES:
        raise ValueError('BUSINESS_APP_SERVER_PROFILE_LIMIT')
    return AppServerProfile.model_validate(value).model_dump(mode='json')


def build_app_server_profile(config) -> dict:
    """Reuse static frozen research checks; never CLI/auth/model/authority access."""
    config = ProviderConfig.model_validate(config)
    old = legacy.build_profile(config, contract_id=CONTRACT_ID)
    checks = dict(old['static_checks']['checks'])
    diagnostics = list(old['static_checks']['diagnostics'])
    sources = dict(old['source_digests'])
    for name in _ADDITIONAL_SOURCE_NAMES:
        try:
            sources[name] = hashlib.sha256(legacy._read_regular(_ROOT / name, 2 * 1024 * 1024)).hexdigest()
        except (OSError, ValueError):
            diagnostics.append({'code': 'SOURCE_IDENTITY_UNAVAILABLE', 'path': name,
                'message': 'A required App Server execution boundary source could not be fingerprinted.',
                'severity': 'error'})
    checks['sources_complete'] = set(sources) == set(APP_SERVER_SOURCE_NAMES)
    policy, policy_digest, renderer = _transport_contract()
    argv = None
    try:
        if old['executable']['resolved'] is not None:
            argv = renderer(config, executable=old['executable']['resolved'])
    except ValueError:
        pass
    checks['business_transport_valid'] = argv is not None
    if argv is None:
        diagnostics.append({'code': 'BUSINESS_APP_SERVER_ARGV_UNAVAILABLE', 'path': 'argv_template',
            'message': 'The executable and configuration could not form the fixed App Server business argv.',
            'severity': 'error'})
    value = {'profile_version': PROFILE_VERSION, 'transport_version': TRANSPORT_VERSION,
        'policy_version': POLICY_VERSION, 'contract_id': CONTRACT_ID, 'purpose': 'generate_candidates',
        'context_transport': CONTEXT_TRANSPORT, 'config_id': config.config_id, 'mode': config.mode,
        'config_snapshot': config.model_dump(mode='json'), 'config_digest': digest(config.model_dump()),
        'executable': old['executable'], 'expected_cli_version': config.expected_cli_version,
        'model': config.model, 'reasoning_effort': config.reasoning_effort,
        'argv_template': argv, 'argv_template_digest': digest(argv) if argv is not None else None,
        'rpc_policy': policy, 'rpc_policy_digest': policy_digest,
        'wire_schema': old['wire_schema'], 'wire_schema_digest': old['wire_schema_digest'],
        'business_schema_digest': old['business_schema_digest'], 'prompt_version': old['prompt_version'],
        'prompt_digest': old['prompt_digest'], 'source_digests': sources,
        'static_checks': {'passed': all(checks.values()) and not any(item['severity'] == 'error' for item in diagnostics),
            'checks': checks, 'diagnostics': diagnostics},
        'limitations': [
            'This independent business profile supports only full research_provider_v2 generate_candidates context.',
            'Static profile checks never launch a process, authenticate, probe a version, or authorize dispatch.',
            'RPC ExternalSandbox describes the mandatory external wrapper; it grants no tool or read permission.',
            'Frozen schema identity does not establish real backend schema compatibility or model behavior.',
            'Executable hashing does not verify its full interpreter, package, or dynamic dependency chain.',
            'Expected CLI version is configured; actual version and effective boundary remain unobserved.',
            'Saved self-consistency must be compared with fresh current source and executable identities.',
            'Local digests do not attest authenticity against an equal-privilege local writer.',
        ]}
    value['profile_digest'] = digest(value)
    return validate_app_server_profile(value)


RUNTIME_PROFILE_VERSION = 3


def validate_business_runtime_profile(value, *, historical=False) -> dict:
    """Closed runtime validation; historical V3/V4/V5/V6 is saved-evidence replay.

    Current builders never opt into the historical interpretation. Neither
    branch checks fresh source pins or grants an execution permit.
    """
    from copy import deepcopy
    from .provider_app_server_invocation import (
        BUSINESS_RUNTIME_TRANSPORT_VERSION, BUSINESS_RUNTIME_POLICY_VERSION,
        BUSINESS_CONTEXT_POLICY_VERSION, BUSINESS_RUNTIME_RPC_POLICY,
        BUSINESS_RUNTIME_RPC_POLICY_DIGEST, render_business_runtime_app_server_argv,
        _business_runtime_config, _runtime_rpc_contract,
    )
    if type(historical) is not bool:
        raise ValueError('BUSINESS_APP_SERVER_RUNTIME_PROFILE_INVALID')
    transport = value.get('transport_version') if type(value) is dict else None
    policy, policy_digest = _runtime_rpc_contract(transport, historical=historical)
    policy_version = policy['policy_version']
    expected_keys = set(AppServerProfile.model_fields) | {'context_policy_version'}
    if (type(value) is not dict or set(value) != expected_keys
            or type(value['profile_version']) is not int or value['profile_version'] != RUNTIME_PROFILE_VERSION
            or value['transport_version'] != transport
            or value['policy_version'] != policy_version
            or value['context_policy_version'] != BUSINESS_CONTEXT_POLICY_VERSION
            or value['profile_digest'] != digest({k: v for k, v in value.items() if k != 'profile_digest'})
            or canonical_json(value['rpc_policy']) != canonical_json(policy)
            or value['rpc_policy_digest'] != policy_digest
            or len(canonical_json(value).encode('utf-8')) > MAX_PROFILE_BYTES):
        raise ValueError('BUSINESS_APP_SERVER_RUNTIME_PROFILE_INVALID')
    config = _business_runtime_config(value['config_snapshot'])
    resolved = value['executable'].get('resolved') if type(value['executable']) is dict else None
    argv = None if resolved is None else render_business_runtime_app_server_argv(config, executable=resolved)
    if (value['argv_template'] != argv or value['argv_template_digest'] != (digest(argv) if argv is not None else None)
            or set(value['source_digests']) != set(APP_SERVER_SOURCE_NAMES)
            or value['static_checks']['checks']['sources_complete'] is not True
            or value['static_checks']['checks']['business_transport_valid'] is not (argv is not None)):
        raise ValueError('BUSINESS_APP_SERVER_RUNTIME_PROFILE_BINDING_INVALID')
    # Reuse the original schema/config/executable/prompt/check validation under
    # its own original renderer. The supplied new transport is never relabelled
    # and returned as B1; only this private validation copy is transformed.
    old = deepcopy(value)
    del old['context_policy_version']
    old.update(profile_version=PROFILE_VERSION, transport_version=TRANSPORT_VERSION,
               policy_version=POLICY_VERSION)
    policy, policy_digest, renderer = _transport_contract()
    old['rpc_policy'], old['rpc_policy_digest'] = policy, policy_digest
    old_argv = None if resolved is None else renderer(config, executable=resolved)
    old['argv_template'], old['argv_template_digest'] = old_argv, digest(old_argv) if old_argv is not None else None
    old['profile_digest'] = digest({k: v for k, v in old.items() if k != 'profile_digest'})
    validate_app_server_profile(old)
    return deepcopy(value)


def build_business_runtime_profile(config) -> dict:
    """Fingerprint current runtime boundary without CLI/auth/version queries."""
    from .provider_app_server_invocation import (
        BUSINESS_RUNTIME_TRANSPORT_VERSION, BUSINESS_RUNTIME_POLICY_VERSION,
        BUSINESS_CONTEXT_POLICY_VERSION, BUSINESS_RUNTIME_RPC_POLICY,
        BUSINESS_RUNTIME_RPC_POLICY_DIGEST, render_business_runtime_app_server_argv,
        _business_runtime_config,
    )
    checked = _business_runtime_config(config)
    value = build_app_server_profile(checked)
    argv = None
    if value['executable']['resolved'] is not None:
        argv = render_business_runtime_app_server_argv(checked, executable=value['executable']['resolved'])
    value.update(profile_version=RUNTIME_PROFILE_VERSION,
        transport_version=BUSINESS_RUNTIME_TRANSPORT_VERSION, policy_version=BUSINESS_RUNTIME_POLICY_VERSION,
        context_policy_version=BUSINESS_CONTEXT_POLICY_VERSION,
        rpc_policy=BUSINESS_RUNTIME_RPC_POLICY, rpc_policy_digest=BUSINESS_RUNTIME_RPC_POLICY_DIGEST,
        argv_template=argv, argv_template_digest=digest(argv) if argv is not None else None)
    value['limitations'].append('The business context contains only an exact metadata-checked native auth link; synthetic fresh context does not prove its equivalence.')
    value['profile_digest'] = digest({k: v for k, v in value.items() if k != 'profile_digest'})
    return validate_business_runtime_profile(value)
