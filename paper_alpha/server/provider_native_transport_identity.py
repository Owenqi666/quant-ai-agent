"""Versioned current Native identities; no process or authority creation.

The legacy exec identity keeps its exact shape. App Server uses the independent
business dispatch identity, never the disabled B1 preparation envelope.
"""
from __future__ import annotations

import re

from ..storage import digest
from .provider_schema import ProviderConfig

_HEX = re.compile(r'[a-f0-9]{64}')


def native_transport(config) -> str:
    raw = config.model_dump() if hasattr(config, 'model_dump') else config
    if not isinstance(raw, dict):
        raise ValueError('Native configuration is not an object')
    value = raw.get('native_transport', 'exec')
    if type(value) is not str or value not in {'exec', 'app_server'}:
        raise ValueError('Unsupported Native transport')
    if value == 'app_server' and raw.get('mode') != 'codex_cli':
        raise ValueError('App Server cannot be selected by a fixture provider')
    return value


def current_native_identity(config, *, contract_id):
    checked = ProviderConfig.model_validate(config)
    transport = native_transport(checked)
    if checked.mode != 'codex_cli':
        raise ValueError('Current Native identity requires a real Native configuration')
    if transport == 'exec':
        # Keep the original exec implementation and original saved interpretation.
        from .provider_native import _identity
        return _identity(checked, contract_id=contract_id)
    from .provider_research_schema import CONTRACT_ID
    if contract_id != CONTRACT_ID:
        raise ValueError('App Server supports only research_provider_v2')
    from .provider_app_server_business_identity import business_dispatch_identity
    return business_dispatch_identity(checked)


def native_profile_digest(identity) -> str:
    if not isinstance(identity, dict):
        raise ValueError('Native identity is not an object')
    transport = identity.get('transport_version')
    if transport is None:
        template = identity.get('native_inner_argv')
        if not isinstance(template, dict) or template.get('template_version') != 'native_inner_argv_v1':
            raise ValueError('Unsupported exec identity')
        value = identity.get('profile_digest')
    else:
        from .provider_app_server_business_identity import BUSINESS_DISPATCH_TRANSPORT_VERSION
        if (transport != BUSINESS_DISPATCH_TRANSPORT_VERSION
                or type(identity.get('identity_version')) is not int or identity['identity_version'] != 2):
            raise ValueError('Unsupported App Server identity')
        profile = identity.get('profile')
        if (not isinstance(profile, dict) or profile.get('transport_version') != transport
                or type(profile.get('profile_version')) is not int or profile['profile_version'] != 3):
            raise ValueError('App Server profile belongs to another transport')
        value = profile.get('profile_digest')
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError('Native identity has no exact profile digest')
    return value


def authority_profile_digest(profile, config) -> str:
    """Fixture evidence has its own explicit digest; Native never uses fallback."""
    raw = config.model_dump() if hasattr(config, 'model_dump') else config
    if not isinstance(raw, dict) or raw.get('mode') not in {'fixture', 'codex_cli'}:
        raise ValueError('Authority profile has no exact provider mode')
    if raw['mode'] == 'fixture':
        if native_transport(raw) != 'exec':
            raise ValueError('Fixture cannot adopt a Native transport')
        return digest(profile)
    return native_profile_digest(profile['native_identity'])


def app_server_operation_supported(config, operation) -> bool:
    return native_transport(config) != 'app_server' or operation == 'generate_candidates'
