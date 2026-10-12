"""Whole-process macOS isolation, separate from inner CLI permissions.

The builder never starts a process, reads authentication contents or grants
dispatch. Historical assessment is pure: deleted work/state/auth paths need
not exist. Current callers additionally supply pinned binary/source identities.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

POLICY_VERSION = 'macos_outer_read_boundary_v5'
TEMPLATE_VERSION = 'macos_outer_seatbelt_template_v6'
LAUNCHER = '/usr/bin/sandbox-exec'
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_AUTH_BYTES = 1024 * 1024
_HEX = re.compile(r'^[a-f0-9]{64}$')
_STATE_NAME = re.compile(r'^cli-state(?:-[A-Za-z0-9_-]{6,64})?$')
_MODES = {'synthetic_loopback', 'native_tls'}
_RUNTIME_READ = ('/System/Library', '/usr/lib')
_SYSCTLS = ('hw.activecpu', 'hw.byteorder', 'hw.cpufamily', 'hw.cputype',
            'hw.logicalcpu', 'hw.machine', 'hw.ncpu', 'hw.pagesize', 'hw.pagesize_compat',
            'hw.physicalcpu', 'kern.argmax', 'kern.osrelease', 'kern.ostype',
            'kern.osversion', 'sysctl.proc_cputype')
_TLS_NETWORK_RULE = '(allow network-outbound (remote tcp "*:443") (remote udp "*:53") (remote tcp "*:53"))\n'
_TLS_MACH = ('com.apple.mDNSResponder', 'com.apple.networkd',
             'com.apple.trustd', 'com.apple.trustd.agent')
_TLS_READ = ('/private/etc/resolv.conf', '/private/var/run/resolv.conf',
             '/System/Library/Keychains/SystemRootCertificates.keychain')
_BOOTSTRAP_READ = ('/etc/codex/requirements.toml', '/private/etc/codex/requirements.toml',
                   '/etc/codex/managed_config.toml', '/private/etc/codex/managed_config.toml',
                   '/etc/codex/config.toml', '/private/etc/codex/config.toml')
_BOOTSTRAP_METADATA = ('/etc', '/etc/codex', '/private/etc', '/private/etc/codex')
_BOOTSTRAP_RULES = ('/etc/codex/rules', '/private/etc/codex/rules')
_BASE_TEMPLATE = '''(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow file-read* (literal "/"))
(allow file-read* file-write-data (literal "/dev/null") (literal "/dev/zero"))
(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))
(allow file-read-data file-write-data (literal "/dev/fd/0") (literal "/dev/fd/1") (literal "/dev/fd/2"))
(allow mach-lookup (global-name "com.apple.secinitd"))
(allow system-mac-syscall (mac-policy-name "vnguard"))
(allow system-mac-syscall (require-all (mac-policy-name "Sandbox") (mac-syscall-number 67)))
'''
# Complete fixed outer3/template4 snapshot; historical rendering never uses
# later mutable current constants or observes saved runtime paths.
_HISTORICAL_TEMPLATE_JSON = '{"base":"(version 1)\\n(deny default)\\n(allow process-exec)\\n(allow process-fork)\\n(allow signal (target same-sandbox))\\n(allow process-info* (target same-sandbox))\\n(allow file-read* (literal \\"/\\"))\\n(allow file-read* file-write-data (literal \\"/dev/null\\") (literal \\"/dev/zero\\"))\\n(allow file-read* (literal \\"/dev/random\\") (literal \\"/dev/urandom\\"))\\n(allow file-read-data file-write-data (literal \\"/dev/fd/0\\") (literal \\"/dev/fd/1\\") (literal \\"/dev/fd/2\\"))\\n(allow mach-lookup (global-name \\"com.apple.secinitd\\"))\\n(allow system-mac-syscall (mac-policy-name \\"vnguard\\"))\\n(allow system-mac-syscall (require-all (mac-policy-name \\"Sandbox\\") (mac-syscall-number 67)))\\n","execution":"absolute_system_sandbox_exec_whole_inner_argv_same_supervised_process_group","filesystem":{"auth":"exact_read_only_native_tls_only","response":"exact_file_write","state":"exact_subtree_read_write","synthetic_control":"exact_target_txt_write_loopback_only","work":"read_only"},"network":{"native_tls":"outbound_tcp_443_and_dns_tcp_udp_53_not_a_domain_allowlist","native_tls_mach_names":["com.apple.mDNSResponder","com.apple.networkd","com.apple.trustd","com.apple.trustd.agent"],"native_tls_read_literals":["/private/etc/resolv.conf","/private/var/run/resolv.conf","/System/Library/Keychains/SystemRootCertificates.keychain"],"native_tls_rule":"(allow network-outbound (remote tcp \\"*:443\\") (remote udp \\"*:53\\") (remote tcp \\"*:53\\"))\\n","synthetic_loopback":"exact_loopback_port_no_auth_or_dns"},"policy_version":"macos_outer_read_boundary_v3","runtime_read_subpaths":["/System/Library","/usr/lib"],"schema_version":1,"sysctl_names":["hw.activecpu","hw.byteorder","hw.cpufamily","hw.cputype","hw.logicalcpu","hw.machine","hw.ncpu","hw.pagesize","hw.pagesize_compat","hw.physicalcpu","kern.argmax","kern.osrelease","kern.ostype","kern.osversion","sysctl.proc_cputype"],"system_bootstrap_constraints":"fixed_root_owned_non_symlink_readonly_files_or_absent; etc_is_only_private_etc_alias; no_content_observation_by_builder","system_bootstrap_metadata_literals":["/etc","/etc/codex","/private/etc","/private/etc/codex"],"system_bootstrap_read_literals":["/etc/codex/requirements.toml","/private/etc/codex/requirements.toml","/etc/codex/managed_config.toml","/private/etc/codex/managed_config.toml","/etc/codex/config.toml","/private/etc/codex/config.toml"],"system_bootstrap_rules_constraints":"exact_directory_listing_for_absence_only; builder_requires_canonical_directory_absent; no_children_read_or_admin_rule_bypass","system_bootstrap_rules_directory_literals":["/etc/codex/rules","/private/etc/codex/rules"],"template_version":"macos_outer_seatbelt_template_v4"}'
_TLS_RESOLVER_RULES = '(allow network-outbound (literal "/private/var/run/mDNSResponder"))\n(allow mach-lookup (global-name "com.apple.SystemConfiguration.DNSConfiguration") (global-name "com.apple.SystemConfiguration.configd"))\n(allow sysctl-read (sysctl-name "net.routetable.0.0.3.0"))\n(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.system.notification_center"))\n(allow ipc-posix-shm-read* (ipc-posix-name "apple.shm.notification_center"))\n(allow file-read* (literal "/private/etc/hosts"))\n(allow file-read-metadata (literal "/var") (literal "/var/run") (literal "/private/var") (literal "/private/var/run"))\n'
# Complete fixed outer4/template5 snapshot preserves original V5 evidence.
_SAVED_OUTER4_TEMPLATE5_JSON = '{"base":"(version 1)\\n(deny default)\\n(allow process-exec)\\n(allow process-fork)\\n(allow signal (target same-sandbox))\\n(allow process-info* (target same-sandbox))\\n(allow file-read* (literal \\"/\\"))\\n(allow file-read* file-write-data (literal \\"/dev/null\\") (literal \\"/dev/zero\\"))\\n(allow file-read* (literal \\"/dev/random\\") (literal \\"/dev/urandom\\"))\\n(allow file-read-data file-write-data (literal \\"/dev/fd/0\\") (literal \\"/dev/fd/1\\") (literal \\"/dev/fd/2\\"))\\n(allow mach-lookup (global-name \\"com.apple.secinitd\\"))\\n(allow system-mac-syscall (mac-policy-name \\"vnguard\\"))\\n(allow system-mac-syscall (require-all (mac-policy-name \\"Sandbox\\") (mac-syscall-number 67)))\\n","execution":"absolute_system_sandbox_exec_whole_inner_argv_same_supervised_process_group","filesystem":{"auth":"exact_read_only_native_tls_only","response":"exact_file_write","state":"exact_subtree_read_write","synthetic_control":"exact_target_txt_write_loopback_only","work":"read_only"},"network":{"native_tls":"outbound_tcp_443_and_dns_tcp_udp_53_not_a_domain_allowlist","native_tls_mach_names":["com.apple.mDNSResponder","com.apple.networkd","com.apple.trustd","com.apple.trustd.agent"],"native_tls_read_literals":["/private/etc/resolv.conf","/private/var/run/resolv.conf","/System/Library/Keychains/SystemRootCertificates.keychain"],"native_tls_resolver_metadata_constraints":"fixed_root_var_alias_private_var_only; canonical_var_uid0_gid0_mode0755; canonical_var_run_uid0_gid1_mode0775; no_world_write_or_user_owner; hosts_root_regular_single_link_no_group_or_world_write_or_absent; metadata_only","native_tls_resolver_rules":"(allow network-outbound (literal \\"/private/var/run/mDNSResponder\\"))\\n(allow mach-lookup (global-name \\"com.apple.SystemConfiguration.DNSConfiguration\\") (global-name \\"com.apple.SystemConfiguration.configd\\"))\\n(allow sysctl-read (sysctl-name \\"net.routetable.0.0.3.0\\"))\\n(allow mach-lookup (global-name \\"com.apple.system.opendirectoryd.libinfo\\") (global-name \\"com.apple.system.notification_center\\"))\\n(allow ipc-posix-shm-read* (ipc-posix-name \\"apple.shm.notification_center\\"))\\n(allow file-read* (literal \\"/private/etc/hosts\\"))\\n(allow file-read-metadata (literal \\"/var\\") (literal \\"/var/run\\") (literal \\"/private/var\\") (literal \\"/private/var/run\\"))\\n","native_tls_rule":"(allow network-outbound (remote tcp \\"*:443\\") (remote udp \\"*:53\\") (remote tcp \\"*:53\\"))\\n","synthetic_loopback":"exact_loopback_port_no_auth_or_dns"},"policy_version":"macos_outer_read_boundary_v4","runtime_read_subpaths":["/System/Library","/usr/lib"],"schema_version":1,"sysctl_names":["hw.activecpu","hw.byteorder","hw.cpufamily","hw.cputype","hw.logicalcpu","hw.machine","hw.ncpu","hw.pagesize","hw.pagesize_compat","hw.physicalcpu","kern.argmax","kern.osrelease","kern.ostype","kern.osversion","sysctl.proc_cputype"],"system_bootstrap_constraints":"fixed_root_owned_non_symlink_readonly_files_or_absent; etc_is_only_private_etc_alias; no_content_observation_by_builder","system_bootstrap_metadata_literals":["/etc","/etc/codex","/private/etc","/private/etc/codex"],"system_bootstrap_read_literals":["/etc/codex/requirements.toml","/private/etc/codex/requirements.toml","/etc/codex/managed_config.toml","/private/etc/codex/managed_config.toml","/etc/codex/config.toml","/private/etc/codex/config.toml"],"system_bootstrap_rules_constraints":"exact_directory_listing_for_absence_only; builder_requires_canonical_directory_absent; no_children_read_or_admin_rule_bypass","system_bootstrap_rules_directory_literals":["/etc/codex/rules","/private/etc/codex/rules"],"template_version":"macos_outer_seatbelt_template_v5"}'
# Complete fixed outer5/template6 snapshot for saved V6; current policy is shared.
_SAVED_OUTER5_TEMPLATE6_JSON = '{"base":"(version 1)\\n(deny default)\\n(allow process-exec)\\n(allow process-fork)\\n(allow signal (target same-sandbox))\\n(allow process-info* (target same-sandbox))\\n(allow file-read* (literal \\"/\\"))\\n(allow file-read* file-write-data (literal \\"/dev/null\\") (literal \\"/dev/zero\\"))\\n(allow file-read* (literal \\"/dev/random\\") (literal \\"/dev/urandom\\"))\\n(allow file-read-data file-write-data (literal \\"/dev/fd/0\\") (literal \\"/dev/fd/1\\") (literal \\"/dev/fd/2\\"))\\n(allow mach-lookup (global-name \\"com.apple.secinitd\\"))\\n(allow system-mac-syscall (mac-policy-name \\"vnguard\\"))\\n(allow system-mac-syscall (require-all (mac-policy-name \\"Sandbox\\") (mac-syscall-number 67)))\\n","execution":"absolute_system_sandbox_exec_whole_inner_argv_same_supervised_process_group","filesystem":{"auth":"exact_read_only_native_tls_only","response":"exact_file_write","state":"exact_subtree_read_write","synthetic_control":"exact_target_txt_write_loopback_only","work":"read_only"},"network":{"native_tls":"outbound_tcp_443_and_dns_tcp_udp_53_not_a_domain_allowlist","native_tls_mach_names":["com.apple.mDNSResponder","com.apple.networkd","com.apple.trustd","com.apple.trustd.agent"],"native_tls_read_literals":["/private/etc/resolv.conf","/private/var/run/resolv.conf","/System/Library/Keychains/SystemRootCertificates.keychain"],"native_tls_resolver_metadata_constraints":"fixed_root_var_alias_private_var_only; canonical_var_uid0_gid0_mode0755; canonical_var_run_uid0_gid1_mode0775; no_world_write_or_user_owner; hosts_root_regular_single_link_no_group_or_world_write_or_absent; metadata_only","native_tls_resolver_rules":"(allow network-outbound (literal \\"/private/var/run/mDNSResponder\\"))\\n(allow mach-lookup (global-name \\"com.apple.SystemConfiguration.DNSConfiguration\\") (global-name \\"com.apple.SystemConfiguration.configd\\"))\\n(allow sysctl-read (sysctl-name \\"net.routetable.0.0.3.0\\"))\\n(allow mach-lookup (global-name \\"com.apple.system.opendirectoryd.libinfo\\") (global-name \\"com.apple.system.notification_center\\"))\\n(allow ipc-posix-shm-read* (ipc-posix-name \\"apple.shm.notification_center\\"))\\n(allow file-read* (literal \\"/private/etc/hosts\\"))\\n(allow file-read-metadata (literal \\"/var\\") (literal \\"/var/run\\") (literal \\"/private/var\\") (literal \\"/private/var/run\\"))\\n","native_tls_rule":"(allow network-outbound (remote tcp \\"*:443\\") (remote udp \\"*:53\\") (remote tcp \\"*:53\\"))\\n","native_tls_security_server_rule":"(allow mach-lookup (global-name \\"com.apple.SecurityServer\\"))\\n","synthetic_loopback":"exact_loopback_port_no_auth_or_dns"},"policy_version":"macos_outer_read_boundary_v5","runtime_read_subpaths":["/System/Library","/usr/lib"],"schema_version":1,"sysctl_names":["hw.activecpu","hw.byteorder","hw.cpufamily","hw.cputype","hw.logicalcpu","hw.machine","hw.ncpu","hw.pagesize","hw.pagesize_compat","hw.physicalcpu","kern.argmax","kern.osrelease","kern.ostype","kern.osversion","sysctl.proc_cputype"],"system_bootstrap_constraints":"fixed_root_owned_non_symlink_readonly_files_or_absent; etc_is_only_private_etc_alias; no_content_observation_by_builder","system_bootstrap_metadata_literals":["/etc","/etc/codex","/private/etc","/private/etc/codex"],"system_bootstrap_read_literals":["/etc/codex/requirements.toml","/private/etc/codex/requirements.toml","/etc/codex/managed_config.toml","/private/etc/codex/managed_config.toml","/etc/codex/config.toml","/private/etc/codex/config.toml"],"system_bootstrap_rules_constraints":"exact_directory_listing_for_absence_only; builder_requires_canonical_directory_absent; no_children_read_or_admin_rule_bypass","system_bootstrap_rules_directory_literals":["/etc/codex/rules","/private/etc/codex/rules"],"template_version":"macos_outer_seatbelt_template_v6"}'
_TLS_SECURITY_SERVER_RULE = '(allow mach-lookup (global-name "com.apple.SecurityServer"))\n'
_TEMPLATE = json.loads(_SAVED_OUTER4_TEMPLATE5_JSON)
_TEMPLATE.update(policy_version=POLICY_VERSION, template_version=TEMPLATE_VERSION)
_TEMPLATE['network']['native_tls_security_server_rule'] = _TLS_SECURITY_SERVER_RULE

_BOUNDARY_KEYS = {'schema_version', 'policy_version', 'template_version', 'template_sha256',
    'helper_sha256', 'launcher_path', 'launcher_sha256', 'native_executable_path',
    'native_executable_sha256', 'work', 'cli_state', 'response_path', 'network_mode',
    'purpose', 'loopback_port', 'auth_exact_path', 'synthetic_write_target',
    'policy_text', 'policy_sha256', 'inner_argv', 'argv_digest'}
_CURRENT_KEYS = {'helper_sha256', 'launcher_sha256', 'native_executable_path',
                 'native_executable_sha256'}


class OuterSandboxError(ValueError):
    """A closed code; never disclose credential paths or contents in errors."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _template(*, historical=False):
    if type(historical) is not bool:
        raise OuterSandboxError('OUTER_HISTORICAL_FLAG_INVALID')
    return json.loads(_HISTORICAL_TEMPLATE_JSON if historical else _json(_TEMPLATE))


def _saved_static_descriptor(policy_version):
    """Select one exact immutable saved outer policy; no current fallback."""
    if type(policy_version) is not str:
        raise OuterSandboxError('OUTER_HISTORICAL_VERSION_INVALID')
    if policy_version == 'macos_outer_read_boundary_v3':
        value = json.loads(_HISTORICAL_TEMPLATE_JSON)
    elif policy_version == 'macos_outer_read_boundary_v4':
        value = json.loads(_SAVED_OUTER4_TEMPLATE5_JSON)
    elif policy_version == 'macos_outer_read_boundary_v5':
        value = json.loads(_SAVED_OUTER5_TEMPLATE6_JSON)
    else:
        raise OuterSandboxError('OUTER_HISTORICAL_VERSION_INVALID')
    value['template_sha256'] = _digest(value)
    return value


def static_descriptor(*, historical=False):
    """Closed old/current runtime identity; no filesystem/process observation."""
    value = _template(historical=historical)
    value['template_sha256'] = _digest(value)
    return value


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 for char in value)):
        raise OuterSandboxError('OUTER_PATH_INVALID')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value or path == Path('/'):
        raise OuterSandboxError('OUTER_PATH_INVALID')
    return path


def _active_path(value, *, directory=False, owner_private=False):
    path = _path(str(value))
    try:
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError('symlink')
        info = path.lstat()
        if path.resolve(strict=True) != path:
            raise ValueError('canonical')
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not correct_type or (not directory and info.st_nlink != 1):
            raise ValueError('type')
        if owner_private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('ownership')
        return path, info
    except (OSError, ValueError):
        raise OuterSandboxError('OUTER_PATH_UNSAFE') from None


def _sha_file(path, limit):
    # No use of this function on auth files: only source and executables.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
                raise ValueError('size')
            result = hashlib.sha256()
            read = 0
            while chunk := stream.read(1024 * 1024):
                read += len(chunk)
                if read > limit:
                    raise ValueError('limit')
                result.update(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or read != before.st_size:
                raise ValueError('changed')
            return result.hexdigest()
    except (OSError, ValueError):
        raise OuterSandboxError('OUTER_IDENTITY_UNVERIFIED') from None


def _validate_system_bootstrap():
    # Check metadata only. Do not let SBPL literal canonicalization expand a
    # system-file exception via an administrator-path symlink or user write.
    def checked(path, *, directory=False, missing_ok=False):
        try:
            info = Path(path).lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not expected or info.st_uid != 0 or info.st_mode & 0o022
                or (not directory and info.st_nlink != 1)):
            raise ValueError('system metadata')
        return info
    try:
        checked('/private', directory=True)
        checked('/private/etc', directory=True)
        alias = Path('/etc').lstat()
        if (not stat.S_ISLNK(alias.st_mode) or alias.st_uid != 0
                or os.readlink('/etc') not in ('private/etc', '/private/etc')):
            raise ValueError('system alias')
        checked('/private/etc/codex', directory=True, missing_ok=True)
        try:
            Path('/private/etc/codex/rules').lstat()
        except FileNotFoundError:
            pass
        else:
            # Existing administrator rules need a separately supported setup.
            # Never enumerate, read, ignore or replace them to run a probe.
            raise ValueError('existing administrator rules')
        for path in _BOOTSTRAP_READ:
            if path.startswith('/private/'):
                checked(path, missing_ok=True)
    except (OSError, ValueError):
        raise OuterSandboxError('OUTER_SYSTEM_BOOTSTRAP_UNSAFE') from None


def _validate_native_resolver_metadata():
    """Validate fixed system alias/hosts metadata without observing contents."""
    try:
        alias = Path('/var').lstat()
        if (not stat.S_ISLNK(alias.st_mode) or alias.st_uid != 0 or alias.st_gid != 0
                or alias.st_mode & 0o002
                or os.readlink('/var') not in ('private/var', '/private/var')):
            raise ValueError('var alias')
        # The fixed run directory is group daemon-writable on this supported
        # system. Only UID0/GID1/mode0775 is accepted; no blanket write bypass.
        expected = (('/private/var', 0, 0o755), ('/private/var/run', 1, 0o775))
        canonical = {}
        for name, gid, mode in expected:
            info = Path(name).lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != gid
                    or stat.S_IMODE(info.st_mode) != mode or info.st_mode & 0o002):
                raise ValueError('canonical directory')
            canonical[name] = info
        for name, target in (('/var', '/private/var'), ('/var/run', '/private/var/run')):
            info = Path(name).stat() if name == '/var' else Path(name).lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != (canonical[target].st_dev, canonical[target].st_ino)):
                raise ValueError('alias directory identity')
        try:
            hosts = Path('/private/etc/hosts').lstat()
        except FileNotFoundError:
            hosts = None
        if hosts is not None and (not stat.S_ISREG(hosts.st_mode) or hosts.st_uid != 0
                or hosts.st_nlink != 1 or hosts.st_mode & 0o022):
            raise ValueError('hosts metadata')
    except (OSError, ValueError):
        raise OuterSandboxError('OUTER_NATIVE_RESOLVER_METADATA_UNSAFE') from None


def _arguments(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 256
            or any(not isinstance(item, str) or not item or '\0' in item or len(item) > 8192 for item in value)
            or len(_json(value).encode()) > 131072):
        raise OuterSandboxError('OUTER_ARGUMENTS_INVALID')
    if any('danger-full-access' in item or item.startswith('--dangerously-') for item in value):
        raise OuterSandboxError('OUTER_BYPASS_FORBIDDEN')
    return list(value)


def _scope(value):
    work, state = _path(value['work']), _path(value['cli_state'])
    if (work.name != 'work' or not _STATE_NAME.fullmatch(state.name) or work.parent != state.parent
            or work == state or work.parent in {Path('/private/tmp'), Path('/tmp'), Path('/var/tmp'), Path('/private/var/tmp')}
            or len(work.parts) < 5):
        raise OuterSandboxError('OUTER_SCOPE_INVALID')
    response = _path(value['response_path']) if value['response_path'] is not None else None
    if response is not None and response != work / 'response.raw.json':
        raise OuterSandboxError('OUTER_RESPONSE_INVALID')
    mode, port = value['network_mode'], value['loopback_port']
    auth = _path(value['auth_exact_path']) if value['auth_exact_path'] is not None else None
    target = _path(value['synthetic_write_target']) if value['synthetic_write_target'] is not None else None
    if not isinstance(mode, str) or mode not in _MODES:
        raise OuterSandboxError('OUTER_NETWORK_INVALID')
    if mode == 'synthetic_loopback':
        if type(port) is not int or not 1 <= port <= 65535 or auth is not None:
            raise OuterSandboxError('OUTER_NETWORK_INVALID')
        if target is not None and target != work / 'target.txt':
            raise OuterSandboxError('OUTER_CONTROL_INVALID')
        purpose = 'synthetic_write_control' if target is not None else 'synthetic_read'
    else:
        if port is not None or target is not None or auth is None or auth.name != 'auth.json':
            raise OuterSandboxError('OUTER_NETWORK_INVALID')
        if auth.is_relative_to(work.parent):
            raise OuterSandboxError('OUTER_AUTH_INVALID')
        purpose = 'native_real'
    if value['purpose'] != purpose:
        raise OuterSandboxError('OUTER_PURPOSE_INVALID')
    executable = _path(value['native_executable_path'])
    if executable in (work, state) or executable.is_relative_to(work.parent):
        raise OuterSandboxError('OUTER_EXECUTABLE_INVALID')
    return work, state, response, auth, target, executable


def _rule(operation, filter_name, paths):
    return '(allow ' + operation + ' ' + ' '.join('(' + filter_name + ' ' + _json(str(path)) + ')' for path in paths) + ')\n'


def _policy(value, *, historical=False):
    if type(historical) is not bool:
        raise OuterSandboxError('OUTER_HISTORICAL_FLAG_INVALID')
    template = (_saved_static_descriptor(value['policy_version']) if historical
                else _template())
    work, state, response, auth, target, executable = _scope(value)
    text = template['base']
    text += _rule('file-read* file-map-executable', 'subpath', template['runtime_read_subpaths'])
    text += _rule('file-read* file-map-executable', 'literal', [executable])
    text += _rule('file-read*', 'literal', template['system_bootstrap_read_literals'])
    text += _rule('file-read-metadata', 'literal', template['system_bootstrap_metadata_literals'])
    text += _rule('file-read*', 'literal', template['system_bootstrap_rules_directory_literals'])
    # Literal metadata ancestors permit traversal, never their content/subtree.
    ancestors = sorted({str(item) for path in (work, state, executable, auth) if path is not None
                        for item in path.parents if item != Path('/')})
    text += _rule('file-read-metadata', 'literal', ancestors)
    text += _rule('file-read*', 'subpath', [work, state])
    text += _rule('file-write*', 'subpath', [state])
    if response is not None:
        text += _rule('file-write*', 'literal', [response])
    if target is not None:
        text += _rule('file-write*', 'literal', [target])
    if auth is not None:
        text += _rule('file-read*', 'literal', [auth])
    text += _rule('sysctl-read', 'sysctl-name', template['sysctl_names'])
    if value['network_mode'] == 'synthetic_loopback':
        text += '(allow network-outbound (remote tcp ' + _json('localhost:' + str(value['loopback_port'])) + '))\n'
    else:
        text += template['network']['native_tls_rule']
        text += _rule('mach-lookup', 'global-name', template['network']['native_tls_mach_names'])
        text += _rule('file-read*', 'literal', template['network']['native_tls_read_literals'])
        text += template['network'].get('native_tls_resolver_rules', '')
        text += template['network'].get('native_tls_security_server_rule', '')
    return text


def assess_outer_boundary(boundary, argv, *, expected_identity=None, historical=False):
    """Pure reconstruction. Optional current pins are supplied by the caller.

    A historical structurally valid envelope alone never qualifies dispatch.
    This function deliberately performs no lstat/read/resolve on saved paths.
    """
    try:
        if type(historical) is not bool:
            raise OuterSandboxError('OUTER_HISTORICAL_FLAG_INVALID')
        descriptor = (_saved_static_descriptor(boundary.get('policy_version')
                      if isinstance(boundary, dict) else None) if historical
                      else static_descriptor())
        if (not isinstance(boundary, dict) or set(boundary) != _BOUNDARY_KEYS
                or type(boundary['schema_version']) is not int or boundary['schema_version'] != 1
                or boundary['policy_version'] != descriptor['policy_version']
                or boundary['template_version'] != descriptor['template_version']
                or boundary['template_sha256'] != descriptor['template_sha256']
                or boundary['launcher_path'] != LAUNCHER):
            raise OuterSandboxError('OUTER_EVIDENCE_INVALID')
        for key in ('helper_sha256', 'launcher_sha256', 'native_executable_sha256', 'policy_sha256', 'argv_digest'):
            if not isinstance(boundary[key], str) or not _HEX.fullmatch(boundary[key]):
                raise OuterSandboxError('OUTER_EVIDENCE_INVALID')
        inner = _arguments(boundary['inner_argv'])
        if inner[0] != boundary['native_executable_path']:
            raise OuterSandboxError('OUTER_EXECUTABLE_INVALID')
        policy = _policy(boundary, historical=historical)
        rebuilt = [LAUNCHER, '-p', policy, '--', *inner]
        if (boundary['policy_text'] != policy or boundary['policy_sha256'] != hashlib.sha256(policy.encode()).hexdigest()
                or argv != rebuilt or boundary['argv_digest'] != _digest(rebuilt)):
            raise OuterSandboxError('OUTER_EVIDENCE_INVALID')
        if expected_identity is not None:
            if (not isinstance(expected_identity, dict) or set(expected_identity) != _CURRENT_KEYS
                    or any(boundary[key] != expected_identity[key] for key in _CURRENT_KEYS)):
                raise OuterSandboxError('OUTER_IDENTITY_DRIFT')
        return {'valid': True, 'reason_code': 'OUTER_BOUNDARY_STRUCTURALLY_VERIFIED'}
    except (OuterSandboxError, KeyError, TypeError, ValueError) as exc:
        return {'valid': False, 'reason_code': getattr(exc, 'code', 'OUTER_EVIDENCE_INVALID')}


def build_outer_invocation(argv, *, work, cli_state, response_path=None,
                           network_mode='synthetic_loopback', loopback_port=None,
                           auth_file=None, synthetic_write_target=None):
    """Render a whole-CLI wrapper over checked host-created paths.

    synthetic_write_target is exclusively the original write probe's exact
    target.txt positive control. No business/client request may supply it.
    """
    if sys.platform != 'darwin':
        raise OuterSandboxError('OUTER_SANDBOX_UNAVAILABLE')
    _validate_system_bootstrap()
    if network_mode == 'native_tls':
        _validate_native_resolver_metadata()
    inner = _arguments(argv)
    try:
        executable = _path(inner[0]).resolve(strict=True)
    except (OSError, ValueError):
        raise OuterSandboxError('OUTER_EXECUTABLE_INVALID') from None
    executable, _ = _active_path(executable)
    if not os.access(executable, os.X_OK):
        raise OuterSandboxError('OUTER_EXECUTABLE_INVALID')
    inner[0] = str(executable)
    work, _ = _active_path(work, directory=True, owner_private=True)
    state, _ = _active_path(cli_state, directory=True, owner_private=True)
    _active_path(work.parent, directory=True, owner_private=True)
    launcher, launcher_info = _active_path(LAUNCHER)
    if launcher_info.st_uid != 0 or launcher_info.st_mode & 0o022 or not os.access(launcher, os.X_OK):
        raise OuterSandboxError('OUTER_SANDBOX_UNAVAILABLE')
    response = _path(str(response_path)) if response_path is not None else None
    if response is not None and (response.is_symlink() or response.exists()):
        raise OuterSandboxError('OUTER_RESPONSE_INVALID')
    auth = _path(str(auth_file)) if auth_file is not None else None
    if auth is not None:
        auth, auth_info = _active_path(auth, owner_private=True)
        if not 0 < auth_info.st_size <= MAX_AUTH_BYTES:
            raise OuterSandboxError('OUTER_AUTH_INVALID')
        link = state / 'auth.json'
        try:
            if not link.is_symlink() or link.resolve(strict=True) != auth:
                raise ValueError('auth link')
        except (OSError, ValueError):
            raise OuterSandboxError('OUTER_AUTH_INVALID') from None
    target = _path(str(synthetic_write_target)) if synthetic_write_target is not None else None
    if target is not None:
        target, target_info = _active_path(target)
        if not 0 < target_info.st_size <= 1024 or target_info.st_uid != os.getuid():
            raise OuterSandboxError('OUTER_CONTROL_INVALID')
    purpose = ('native_real' if network_mode == 'native_tls' else
               'synthetic_write_control' if target is not None else 'synthetic_read')
    boundary = {'schema_version': 1, 'policy_version': POLICY_VERSION,
        'template_version': TEMPLATE_VERSION, 'template_sha256': static_descriptor()['template_sha256'],
        'helper_sha256': _sha_file(Path(__file__).resolve(), 2 * 1024 * 1024),
        'launcher_path': LAUNCHER, 'launcher_sha256': _sha_file(launcher, 8 * 1024 * 1024),
        'native_executable_path': str(executable), 'native_executable_sha256': _sha_file(executable, MAX_EXECUTABLE_BYTES),
        'work': str(work), 'cli_state': str(state), 'response_path': str(response) if response is not None else None,
        'network_mode': network_mode, 'purpose': purpose, 'loopback_port': loopback_port,
        'auth_exact_path': str(auth) if auth is not None else None,
        'synthetic_write_target': str(target) if target is not None else None}
    policy = _policy(boundary)
    actual = [LAUNCHER, '-p', policy, '--', *inner]
    boundary.update(policy_text=policy, policy_sha256=hashlib.sha256(policy.encode()).hexdigest(),
                    inner_argv=inner, argv_digest=_digest(actual))
    if not assess_outer_boundary(boundary, actual)['valid']:
        raise OuterSandboxError('OUTER_EVIDENCE_INVALID')
    return {'argv': actual, 'boundary': boundary}
