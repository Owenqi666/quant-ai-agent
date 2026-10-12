"""Re-read controlled synthetic qualification; never create business authority.

The locator retains the exact current 8+3 raw bundle. It establishes only
observed boundary effects. Original Session/Loop permits and real business
capture are separate requirements, including the isolated native auth context.
"""
from pathlib import Path
from .provider_contract import canonical_json, digest, strict_json
from .codex_cli_provider import _bounded_private_file

VERSION = 'app_server_current_synthetic_locator_v1'


def _need(condition, code='APP_SERVER_FRESH_UNVERIFIED'):
    if not condition:
        raise ValueError(code)


def qualification_locator(report_path, expected_identity):
    path = Path(report_path)
    value = {'version': VERSION, 'report_path': str(path),
             'expected_identity': dict(expected_identity)}
    _validate_locator(value)
    return value


def _validate_locator(value):
    _need(type(value) is dict and set(value) == {'version', 'report_path', 'expected_identity'}
          and value['version'] == VERSION)
    text = value['report_path']
    _need(type(text) is str and 0 < len(text) <= 4096)
    path = Path(text)
    expected = value['expected_identity']
    _need(path.is_absolute() and str(path) == text and '..' not in path.parts
          and not text.startswith('//') and path.name == 'report.json'
          and type(expected) is dict and type(expected.get('nonce')) is str
          and path.parent.name == 'fresh-' + expected['nonce']
          and str(path.parent.parent) == expected.get('artifact_dir')
          and not any(item.is_symlink() for item in (path, *path.parents)))
    return path


def verify_app_server_fresh_evidence(transport, *, config,
                                     expected_current_identity, original_controller=None):
    """Current source/config comparison plus full closed raw bundle readback.

    B2 deliberately uses its controlled no-auth context and historical business
    preparation policy. Compare that exact current static builder separately;
    never rename its business_qualification=false as a runtime authorization.
    """
    from .provider_app_server_business_identity import app_server_identity, business_dispatch_identity
    from .provider_app_server_fresh_probe import load_fresh_report
    value = transport.get('fresh_evidence') if type(transport) is dict else None
    path = _validate_locator(value)
    current, runtime_profile = business_dispatch_identity(config)
    _need(canonical_json(current) == canonical_json(expected_current_identity))
    legacy_current, synthetic_profile = app_server_identity(config)
    expected = value['expected_identity']
    _need(canonical_json(expected.get('business_static_identity')) == canonical_json(legacy_current)
          and expected.get('config_snapshot') == config.model_dump(mode='json')
          and expected.get('config_digest') == digest(config.model_dump())
          and synthetic_profile['source_digests'] == runtime_profile['source_digests']
          and synthetic_profile['executable'] == runtime_profile['executable']
          and legacy_current['outer_identity'] == current['outer_identity'])
    if original_controller is not None:
        _need(canonical_json(expected.get('controller')) == canonical_json(original_controller))
    loaded = load_fresh_report(path, expected_identity=expected)
    a = loaded['assessment']
    _need(a['fresh_matrix_observed'] is True
          and a['write_effect_prevention_observed'] is True
          and a['business_qualification'] is False)
    raw = _bounded_private_file(path, 2 * 1024 * 1024)
    report = strict_json(raw, max_bytes=2 * 1024 * 1024)
    _need(report['business_activation_eligible'] is False
          and report['resources'] == {'native_attempts': 11, 'real_model_invocations': 0,
                                    'auth_operations': 0, 'business_grants': 0})
    return {'version': VERSION, 'read_verified': True, 'write_verified': True,
            'report_digest': digest(report), 'report_sha256': loaded['report_sha256'],
            'fresh_identity_digest': expected['identity_digest'],
            'runtime_profile_digest': runtime_profile['profile_digest'],
            'business_qualification': False, 'auth_context_equivalence_claimed': False,
            'permission_cause_status': a['permission_cause_status']}
