"""Named read policy shared by synthetic challenges and bounded canaries.

The v1 and v2 descriptions are frozen for interpreting historical evidence.
Current dispatch additionally requires the macOS whole-process outer boundary.
No policy here authorizes dispatch or selects authentication/provider endpoints.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import tempfile

from .provider_cli_profile import build_cli_argv
from .provider_contract import canonical_json, digest
from .provider_schema import ProviderConfig

READ_BOUNDARY_PROFILE_NAME = 'provider_read_boundary'
READ_BOUNDARY_POLICY_V1 = {
    'policy_version': 'cli_named_read_boundary_v1',
    'profile_name': READ_BOUNDARY_PROFILE_NAME,
    'filesystem': {':root': 'deny', ':minimal': 'read', '<work>': 'read'},
    'network': {'enabled': False},
    'legacy_sandbox_argument': 'removed_to_select_named_permissions',
}
READ_BOUNDARY_POLICY_V1_DIGEST = digest(READ_BOUNDARY_POLICY_V1)
READ_BOUNDARY_POLICY_V2 = {
    'policy_version': 'cli_named_read_boundary_v2',
    'profile_name': READ_BOUNDARY_PROFILE_NAME,
    'filesystem': {':root': 'deny', ':slash_tmp': 'deny',
                   ':tmpdir': 'deny', '/tmp': 'deny', '/private/tmp': 'deny',
                   '/var/tmp': 'deny', '/private/var/tmp': 'deny',
                   '<declared_tmpdir_aliases>': 'deny', '<host_tmpdir_aliases>': 'deny',
                   '<work>': 'read'},
    'network': {'enabled': False},
    'legacy_sandbox_argument': 'removed_to_select_named_permissions',
    'tmp_paths': 'absolute_normalized_and_canonical_parents_denied_before_exact_work',
    'minimal_read': 'omitted_after_explicit_tmp_denial_failed_in_candidate_1',
}
READ_BOUNDARY_POLICY_V2_DIGEST = digest(READ_BOUNDARY_POLICY_V2)
READ_BOUNDARY_POLICY_VERSION = 'macos_outer_read_boundary_v3'
READ_BOUNDARY_POLICY = {**READ_BOUNDARY_POLICY_V2,
    'policy_version': READ_BOUNDARY_POLICY_VERSION,
    'outer_boundary': 'macos_whole_cli_sandbox_exec_v1',
    'outer_scope': 'exact_work_read_exact_response_write_private_cli_state_write',
    'synthetic_network': 'exact_loopback_port_no_auth',
    'native_network': 'default_cli_oauth_https_tls443_and_required_dns',
    'write_control': 'exact_self_created_target_only',
}
READ_BOUNDARY_POLICY_DIGEST = digest(READ_BOUNDARY_POLICY)
POLICIES = {READ_BOUNDARY_POLICY_V1['policy_version']: READ_BOUNDARY_POLICY_V1,
            READ_BOUNDARY_POLICY_V2['policy_version']: READ_BOUNDARY_POLICY_V2,
            READ_BOUNDARY_POLICY_VERSION: READ_BOUNDARY_POLICY}


def policy_for_version(version):
    """Select only a known frozen description; unknown evidence is unsupported."""
    if not isinstance(version, str) or version not in POLICIES:
        raise ValueError('Unsupported read-boundary policy version')
    return POLICIES[version]


def temporary_read_denials():
    """Explicit host tmp aliases, in addition to the CLI's special paths.

    No files in these directories are opened. The CLI resolves special paths;
    explicit normalized/canonical paths defend its implicit platform allowances.
    """
    roots = {'/tmp', '/private/tmp', '/var/tmp', '/private/var/tmp'}
    for value in (os.environ.get('TMPDIR'), tempfile.gettempdir()):
        if value:
            path = Path(value)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Temporary root must be an absolute normalized directory')
            roots.add(str(path.absolute()))
            roots.add(str(path.resolve(strict=True)))
    return tuple(sorted(roots))


def build_read_boundary_argv(config, work, schema, response) -> list[str]:
    """Apply v2 to explicit files in a host-owned exact workspace subtree."""
    config = ProviderConfig.model_validate(config)
    work = Path(work).absolute()
    if (not work.is_dir() or '..' in work.parts
            or any(path.is_symlink() for path in (work, *work.parents))):
        raise ValueError('Read-boundary workspace must be an existing unlinked absolute directory')
    for path in (Path(schema), Path(response)):
        if path.parent.absolute() != work or path.is_symlink():
            raise ValueError('Schema and response must be explicit unlinked files in the workspace')
    return render_read_boundary_argv(build_cli_argv(config, work, schema, response), str(work), temporary_read_denials())


def render_read_boundary_argv(inner, work, temporary_denials):
    """Pure named-permission rendering, also used by frozen argv templates."""
    arguments = list(inner)
    if '--sandbox' in arguments:
        position = arguments.index('--sandbox')
        del arguments[position:position + 2]
    # CLI 0.145 rejects this obsolete key with strict config.
    if 'tools.view_image=false' in arguments:
        position = arguments.index('tools.view_image=false')
        del arguments[position - 1:position + 1]
    if '--strict-config' not in arguments:
        arguments.append('--strict-config')
    entries = {':root': 'deny', ':slash_tmp': 'deny', ':tmpdir': 'deny'}
    entries.update({path: 'deny' for path in temporary_denials})
    if str(work) in entries:
        raise ValueError('Workspace must be a proper subtree of temporary roots')
    entries[str(work)] = 'read'
    filesystem = '{' + ','.join(canonical_json(path) + '=' + canonical_json(access)
                                  for path, access in entries.items()) + '}'
    for override in (
        f'default_permissions="{READ_BOUNDARY_PROFILE_NAME}"',
        f'permissions.{READ_BOUNDARY_PROFILE_NAME}.filesystem={filesystem}',
        f'permissions.{READ_BOUNDARY_PROFILE_NAME}.network.enabled=false',
    ):
        arguments.extend(['-c', override])
    return arguments


def outer_boundary_identity():
    """Static source/launcher pins only; no process, credential or model access."""
    from .provider_outer_sandbox import static_descriptor
    from .codex_cli_provider import _bounded_private_file
    launcher = Path('/usr/bin/sandbox-exec')
    return {'descriptor': static_descriptor(),
        'helper_sha256': hashlib.sha256(_bounded_private_file(
            Path(__file__).with_name('provider_outer_sandbox.py'), 2 * 1024 * 1024)).hexdigest(),
        'launcher_path': str(launcher),
        'launcher_sha256': hashlib.sha256(_bounded_private_file(launcher, 2 * 1024 * 1024)).hexdigest()}


def outer_evidence_matches_identity(boundary, identity):
    """Compare saved metadata without requiring already cleaned runtime paths."""
    if not isinstance(boundary, dict) or not isinstance(identity, dict):
        return False
    descriptor = identity.get('descriptor')
    return bool(isinstance(descriptor, dict)
        and all(boundary.get(key) == identity.get(key)
                for key in ('helper_sha256', 'launcher_path', 'launcher_sha256'))
        and all(boundary.get(key) == descriptor.get(key)
                for key in ('policy_version', 'template_version', 'template_sha256')))
