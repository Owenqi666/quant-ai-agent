"""Isolated business HOME with one metadata-only native authentication link.

This helper neither grants permission nor starts a child. Host permit, fresh
qualification and lifecycle cleanup remain with the original controller.
Credential bytes are never opened, copied, hashed, or returned here.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat

from .provider_cli_state import (
    CliStateError, _native_cli_home, _auth_metadata, _check_parent,
    _app_server_directory,
)
from .provider_app_server_invocation import BUSINESS_CONTEXT_POLICY_VERSION
from .provider_contract import digest
from .provider_cli_profile import MAX_EXECUTABLE_BYTES

# P034's historical v1 never permitted native helper symlinks. Keep that
# interpretation even when the current runtime selects the separate v2 policy.
_LEGACY_CONTEXT_POLICY_VERSION = 'codex_app_server_business_context_v1'

_STATE_KEYS = {'path', 'home', 'codex_home', 'auth_file', 'auth_metadata',
               'native_auth_linked', 'context_policy_version'}
_DESCRIPTOR_KEYS = {'context_version', 'context_policy_version', 'home',
                    'codex_home', 'auth_exact_path', 'auth_metadata',
                    'native_auth_linked', 'bundled_skills_enabled', 'context_digest'}
_AUTH_FIELDS = ('dev', 'ino', 'uid', 'mode', 'size', 'mtime_ns', 'ctime_ns')
_MAX_ENTRIES = 4096
_MAX_DEPTH = 16


def _path(value):
    if type(value) is not str or not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_PATH_INVALID')
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or '..' in path.parts or path == PurePosixPath('/'):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_PATH_INVALID')
    value.encode('utf-8', 'strict')
    return path


def _checked_auth_source():
    source_home = _native_cli_home()
    _path(str(source_home))
    _check_parent(source_home)
    source = source_home / 'auth.json'
    metadata = _auth_metadata(source)
    # Hardlinks could mutate the host credential through an unbound alias.
    if source.lstat().st_nlink != 1:
        raise CliStateError('NATIVE_AUTH_UNSAFE')
    return source, dict(zip(_AUTH_FIELDS, metadata))


def _check_contents(directory, auth_file, expected_auth):
    total = 0
    seen_auth = False

    def walk(fd, depth, prefix):
        nonlocal total, seen_auth
        if depth > _MAX_DEPTH:
            raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_METADATA_LIMIT')
        with os.scandir(fd) as entries:
            for entry in entries:
                total += 1
                if total > _MAX_ENTRIES:
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_METADATA_LIMIT')
                relative = (*prefix, entry.name)
                info = entry.stat(follow_symlinks=False)
                if relative == ('auth.json',):
                    if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                            or os.readlink(entry.name, dir_fd=fd) != str(auth_file)):
                        raise CliStateError('BUSINESS_APP_SERVER_AUTH_LINK_INVALID')
                    seen_auth = True
                    continue
                if entry.name in {'auth.json', 'config.toml', '.env', 'skills'}:
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_POLLUTED')
                if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (info.st_dev, info.st_ino, info.st_uid, info.st_mode):
                            raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE')
                        walk(child, depth + 1, relative)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE')
    walk(directory, 0, ())
    if not seen_auth or dict(zip(_AUTH_FIELDS, _auth_metadata(auth_file))) != expected_auth or auth_file.lstat().st_nlink != 1:
        raise CliStateError('BUSINESS_APP_SERVER_AUTH_METADATA_CHANGED')


def validate_business_app_server_state(record):
    """Live, bounded metadata check; do not run while an unstopped child lives."""
    if (type(record) is not dict or set(record) != _STATE_KEYS
            or record['context_policy_version'] != _LEGACY_CONTEXT_POLICY_VERSION
            or record['native_auth_linked'] is not True
            or not all(isinstance(record[key], Path) for key in ('path', 'home', 'codex_home', 'auth_file'))
            or record['home'] != record['path'] or record['codex_home'] != record['path']
            or record['path'].name != 'cli-state'):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_ARGUMENT_INVALID')
    business_context_descriptor(record)
    source, metadata = _checked_auth_source()
    if source != record['auth_file'] or metadata != record['auth_metadata']:
        raise CliStateError('BUSINESS_APP_SERVER_AUTH_METADATA_CHANGED')
    parent = _app_server_directory(record['path'].parent)
    state = None
    try:
        state = _app_server_directory(record['path'])
        _check_contents(state, source, metadata)
        return dict(record)
    except OSError:
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE') from None
    finally:
        os.close(parent)
        if state is not None:
            os.close(state)


def prepare_business_app_server_state(parent):
    """Create exactly one private state without copying any host context."""
    parent = Path(parent)
    directory = _app_server_directory(parent)
    state_directory = None
    created = False
    linked = False
    try:
        source, metadata = _checked_auth_source()
        if source.is_relative_to(parent):
            raise CliStateError('BUSINESS_APP_SERVER_AUTH_LAYOUT_INVALID')
        os.mkdir('cli-state', mode=0o700, dir_fd=directory)
        created = True
        state = parent / 'cli-state'
        state_directory = os.open('cli-state', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.symlink(str(source), 'auth.json', dir_fd=state_directory)
        linked = True
        record = {'path': state, 'home': state, 'codex_home': state,
            'auth_file': source, 'auth_metadata': metadata, 'native_auth_linked': True,
            'context_policy_version': _LEGACY_CONTEXT_POLICY_VERSION}
        return validate_business_app_server_state(record)
    except FileExistsError:
        raise CliStateError('BUSINESS_APP_SERVER_STATE_ALREADY_EXISTS') from None
    except (OSError, CliStateError, ValueError) as exc:
        # No child is started here: remove only our exact new link/empty state.
        if linked:
            try:
                os.unlink('auth.json', dir_fd=state_directory)
            except OSError:
                pass
        if created:
            try:
                os.rmdir('cli-state', dir_fd=directory)
            except OSError:
                pass
        if isinstance(exc, CliStateError):
            raise
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_CREATE_FAILED') from None
    finally:
        if state_directory is not None:
            os.close(state_directory)
        os.close(directory)


def validate_business_context_descriptor(value):
    """Pure historical shape/digest check, without host/auth/runtime lookup."""
    if (type(value) is not dict or set(value) != _DESCRIPTOR_KEYS
            or type(value['context_version']) is not int or value['context_version'] != 1
            or value['context_policy_version'] != _LEGACY_CONTEXT_POLICY_VERSION
            or value['native_auth_linked'] is not True
            or value['bundled_skills_enabled'] is not False
            or value['home'] != value['codex_home']
            or value['context_digest'] != digest({k: v for k, v in value.items() if k != 'context_digest'})):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_DESCRIPTOR_INVALID')
    state, auth = _path(value['home']), _path(value['auth_exact_path'])
    metadata = value['auth_metadata']
    if (state.name != 'cli-state' or auth.name != 'auth.json' or auth.is_relative_to(state.parent)
            or type(metadata) is not dict or set(metadata) != set(_AUTH_FIELDS)
            or any(type(metadata[key]) is not int or metadata[key] < 0 for key in _AUTH_FIELDS)
            or metadata['uid'] != os.getuid() or not stat.S_ISREG(metadata['mode'])
            or metadata['mode'] & 0o077 or not 0 < metadata['size'] <= 1024 * 1024):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_DESCRIPTOR_INVALID')
    return dict(value)


def business_context_descriptor(record):
    value = {'context_version': 1, 'context_policy_version': record['context_policy_version'],
        'home': str(record['home']), 'codex_home': str(record['codex_home']),
        'auth_exact_path': str(record['auth_file']), 'auth_metadata': dict(record['auth_metadata']),
        'native_auth_linked': record['native_auth_linked'], 'bundled_skills_enabled': False}
    value['context_digest'] = digest(value)
    return validate_business_context_descriptor(value)


def existing_business_app_server_state(cli_state, auth_file):
    """Validate an original caller-owned state; never create/adopt authority."""
    source, metadata = _checked_auth_source()
    if source != Path(auth_file):
        raise CliStateError('BUSINESS_APP_SERVER_AUTH_LINK_INVALID')
    state = Path(cli_state)
    return validate_business_app_server_state({'path': state, 'home': state,
        'codex_home': state, 'auth_file': source, 'auth_metadata': metadata,
        'native_auth_linked': True, 'context_policy_version': _LEGACY_CONTEXT_POLICY_VERSION})


_V2_POLICY = 'codex_app_server_business_context_v2'
_V2_STATE_KEYS = _STATE_KEYS | {'context_version', 'native_executable'}
_V2_DESCRIPTOR_KEYS = _DESCRIPTOR_KEYS | {'native_executable'}
_NATIVE_FIELDS = {'path', 'sha256', 'size', 'dev', 'ino', 'uid', 'mode', 'nlink', 'mtime_ns', 'ctime_ns'}
_ALIASES = frozenset({'apply_patch', 'applypatch', 'codex-execve-wrapper'})
_PROFILE_EXECUTABLE_KEYS = {'configured', 'resolved', 'sha256', 'size_bytes', 'kind', 'chain_verified'}
_v1_descriptor = business_context_descriptor
_v1_validate_descriptor = validate_business_context_descriptor


def _native_metadata(path):
    path = Path(path)
    _path(str(path))
    if any(node.is_symlink() for node in (path, *path.parents)):
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_INVALID')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_mode & 0o111
            or info.st_mode & 0o022 or not 0 < info.st_size <= MAX_EXECUTABLE_BYTES):
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_INVALID')
    return dict(path=str(path), size=info.st_size, dev=info.st_dev, ino=info.st_ino, uid=info.st_uid,
                mode=info.st_mode, nlink=info.st_nlink, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


def _validate_native_descriptor(value):
    if (type(value) is not dict or set(value) != _NATIVE_FIELDS
            or type(value['sha256']) is not str or re.fullmatch('[a-f0-9]{64}', value['sha256']) is None
            or any(type(value[key]) is not int or value[key] < 0 for key in _NATIVE_FIELDS - {'path', 'sha256'})
            or not stat.S_ISREG(value['mode']) or not value['mode'] & 0o111 or value['mode'] & 0o022
            or value['nlink'] != 1 or not 0 < value['size'] <= MAX_EXECUTABLE_BYTES):
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_INVALID')
    _path(value['path'])
    return dict(value)


def _bind_native_executable(identity):
    """Bind caller's already verified profile, hashing executable bytes only."""
    if (type(identity) is not dict or set(identity) != _PROFILE_EXECUTABLE_KEYS
            or identity['kind'] != 'native_binary' or identity['chain_verified'] is not False
            or type(identity['resolved']) is not str or type(identity['sha256']) is not str
            or re.fullmatch('[a-f0-9]{64}', identity['sha256']) is None
            or type(identity['size_bytes']) is not int):
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_INVALID')
    before = _native_metadata(identity['resolved'])
    if before['size'] != identity['size_bytes']:
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_CHANGED')
    fd = os.open(before['path'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    fingerprint = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before['dev'], before['ino'], before['size']):
            raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_CHANGED')
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            if total > MAX_EXECUTABLE_BYTES:
                raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_INVALID')
            fingerprint.update(chunk)
    finally:
        os.close(fd)
    if (_native_metadata(before['path']) != before or total != before['size']
            or fingerprint.hexdigest() != identity['sha256']):
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_CHANGED')
    return {**before, 'sha256': fingerprint.hexdigest()}


def _check_arg0_root(fd, native):
    """Pinned macOS 0.145.0 helper aliases, without following their symlinks."""
    root = os.fstat(fd)
    if root.st_uid != os.getuid() or stat.S_IMODE(root.st_mode) != 0o700:
        raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
    rows = []
    with os.scandir(fd) as entries:
        for row in entries:
            rows.append(row)
            if len(rows) > 32:
                raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_METADATA_LIMIT')
    for entry in rows:
        info = entry.stat(follow_symlinks=False)
        if (re.fullmatch(r'codex-arg0[0-9A-Za-z]{6}', entry.name) is None
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
            records = []
            with os.scandir(child) as children:
                for item in children:
                    records.append(item)
                    if len(records) > 4:
                        raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
            if {item.name for item in records} != _ALIASES | {'.lock'}:
                raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
            for item in records:
                before = item.stat(follow_symlinks=False)
                if before.st_uid != os.getuid() or before.st_nlink != 1:
                    raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
                if item.name == '.lock':
                    if not stat.S_ISREG(before.st_mode) or before.st_size != 0 or before.st_mode & 0o022:
                        raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
                elif (not stat.S_ISLNK(before.st_mode)
                      or os.readlink(item.name, dir_fd=child) != native['path']):
                    raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
                after = item.stat(follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns):
                    raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
        finally:
            os.close(child)
    if _native_metadata(native['path']) != {key: value for key, value in native.items() if key != 'sha256'}:
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_CHANGED')


def _check_contents_v2(directory, auth_file, expected_auth, native):
    total, seen_auth = 0, False

    def walk(fd, depth, prefix):
        nonlocal total, seen_auth
        if depth > _MAX_DEPTH:
            raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_METADATA_LIMIT')
        with os.scandir(fd) as entries:
            for entry in entries:
                total += 1
                if total > _MAX_ENTRIES:
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_METADATA_LIMIT')
                relative = (*prefix, entry.name)
                info = entry.stat(follow_symlinks=False)
                if relative == ('auth.json',):
                    if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                            or os.readlink(entry.name, dir_fd=fd) != str(auth_file)):
                        raise CliStateError('BUSINESS_APP_SERVER_AUTH_LINK_INVALID')
                    seen_auth = True
                    continue
                if entry.name in {'auth.json', 'config.toml', '.env', 'skills'}:
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_POLLUTED')
                if relative in {('tmp',), ('tmp', 'arg0')} and (
                        not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022):
                    raise CliStateError('BUSINESS_APP_SERVER_ARG0_UNSAFE')
                if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (info.st_dev, info.st_ino, info.st_uid, info.st_mode):
                            raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE')
                        if relative == ('tmp', 'arg0'):
                            _check_arg0_root(child, native)
                        else:
                            walk(child, depth + 1, relative)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE')
    walk(directory, 0, ())
    if not seen_auth or dict(zip(_AUTH_FIELDS, _auth_metadata(auth_file))) != expected_auth or auth_file.lstat().st_nlink != 1:
        raise CliStateError('BUSINESS_APP_SERVER_AUTH_METADATA_CHANGED')


def validate_business_app_server_state_v2(record, *, native_executable_identity):
    """Only after confirmed stop: metadata-check bound native aliases and auth."""
    if (type(record) is not dict or set(record) != _V2_STATE_KEYS or record['context_version'] != 2
            or type(record['context_version']) is not int or record['context_policy_version'] != _V2_POLICY
            or record['native_auth_linked'] is not True
            or not all(isinstance(record[key], Path) for key in ('path', 'home', 'codex_home', 'auth_file'))
            or record['home'] != record['path'] or record['codex_home'] != record['path']
            or record['path'].name != 'cli-state'):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_ARGUMENT_INVALID')
    business_context_descriptor(record)
    expected = native_executable_identity
    if (type(expected) is not dict or set(expected) != _PROFILE_EXECUTABLE_KEYS
            or expected.get('kind') != 'native_binary' or expected.get('chain_verified') is not False
            or (expected.get('resolved'), expected.get('sha256'), expected.get('size_bytes')) != (
                record['native_executable']['path'], record['native_executable']['sha256'], record['native_executable']['size'])
            or _native_metadata(record['native_executable']['path']) != {
                key: value for key, value in record['native_executable'].items() if key != 'sha256'}):
        raise CliStateError('BUSINESS_APP_SERVER_NATIVE_IDENTITY_CHANGED')
    source, metadata = _checked_auth_source()
    if source != record['auth_file'] or metadata != record['auth_metadata']:
        raise CliStateError('BUSINESS_APP_SERVER_AUTH_METADATA_CHANGED')
    parent = _app_server_directory(record['path'].parent)
    state = None
    try:
        state = _app_server_directory(record['path'])
        _check_contents_v2(state, source, metadata, record['native_executable'])
        return dict(record)
    except OSError:
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_UNSAFE') from None
    finally:
        os.close(parent)
        if state is not None:
            os.close(state)


def prepare_business_app_server_state_v2(parent, *, native_executable_identity):
    native = _bind_native_executable(native_executable_identity)
    old = prepare_business_app_server_state(parent)
    record = {**old, 'context_version': 2, 'context_policy_version': _V2_POLICY, 'native_executable': native}
    return validate_business_app_server_state_v2(record, native_executable_identity=native_executable_identity)


def existing_business_app_server_state_v2(cli_state, auth_file, *, native_executable_identity):
    native = _bind_native_executable(native_executable_identity)
    source, metadata = _checked_auth_source()
    if source != Path(auth_file):
        raise CliStateError('BUSINESS_APP_SERVER_AUTH_LINK_INVALID')
    state = Path(cli_state)
    record = {'path': state, 'home': state, 'codex_home': state, 'auth_file': source,
              'auth_metadata': metadata, 'native_auth_linked': True, 'context_version': 2,
              'context_policy_version': _V2_POLICY, 'native_executable': native}
    return validate_business_app_server_state_v2(record, native_executable_identity=native_executable_identity)


def business_context_descriptor(record):
    if record.get('context_version') != 2:
        return _v1_descriptor(record)
    value = {'context_version': 2, 'context_policy_version': record['context_policy_version'],
        'home': str(record['home']), 'codex_home': str(record['codex_home']),
        'auth_exact_path': str(record['auth_file']), 'auth_metadata': dict(record['auth_metadata']),
        'native_auth_linked': record['native_auth_linked'], 'bundled_skills_enabled': False,
        'native_executable': dict(record['native_executable'])}
    value['context_digest'] = digest(value)
    return validate_business_context_descriptor(value)


def validate_business_context_descriptor(value):
    if type(value) is dict and value.get('context_version') == 1:
        return _v1_validate_descriptor(value)
    if (type(value) is not dict or set(value) != _V2_DESCRIPTOR_KEYS
            or type(value['context_version']) is not int or value['context_version'] != 2
            or value['context_policy_version'] != _V2_POLICY or value['native_auth_linked'] is not True
            or value['bundled_skills_enabled'] is not False or value['home'] != value['codex_home']
            or value['context_digest'] != digest({key: val for key, val in value.items() if key != 'context_digest'})):
        raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_DESCRIPTOR_INVALID')
    # Reuse the historical auth/home pure shape verifier with its original
    # version. This does not adopt v1's live symlink policy for the new lane.
    auth_view = {key: value[key] for key in _DESCRIPTOR_KEYS if key != 'context_digest'}
    auth_view.update(context_version=1, context_policy_version=_LEGACY_CONTEXT_POLICY_VERSION)
    auth_view['context_digest'] = digest(auth_view)
    _v1_validate_descriptor(auth_view)
    _validate_native_descriptor(value['native_executable'])
    return dict(value)


def describe_business_app_server_state_metadata(record_or_path, *, max_entries=512):
    """Bounded diagnostic metadata only: never open contents or follow links."""
    result = {'diagnostic_version': 1, 'entries': [], 'truncated': False, 'error': None}
    state = record_or_path.get('path') if type(record_or_path) is dict else record_or_path
    root = None
    try:
        if type(max_entries) is not int or not 1 <= max_entries <= 512:
            raise CliStateError('BUSINESS_APP_SERVER_CONTEXT_ARGUMENT_INVALID')
        root = _app_server_directory(Path(state))
        kinds = {stat.S_IFDIR: 'directory', stat.S_IFREG: 'regular', stat.S_IFLNK: 'symlink',
                 stat.S_IFIFO: 'fifo', stat.S_IFSOCK: 'socket', stat.S_IFCHR: 'char', stat.S_IFBLK: 'block'}

        def walk(fd, depth, prefix):
            if depth > 8:
                result['truncated'] = True
                return
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(result['entries']) >= max_entries:
                        result['truncated'] = True
                        return
                    info = entry.stat(follow_symlinks=False)
                    relative = (*prefix, entry.name)
                    row = {'relative_path': '/'.join(relative), 'type': kinds.get(stat.S_IFMT(info.st_mode), 'unknown'),
                           'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid,
                           'nlink': info.st_nlink, 'size': info.st_size}
                    if stat.S_ISLNK(info.st_mode):
                        row['target'] = os.readlink(entry.name, dir_fd=fd)[:4096]
                    result['entries'].append(row)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            walk(child, depth + 1, relative)
                        finally:
                            os.close(child)
        walk(root, 0, ())
    except OSError as exc:
        result['error'] = {'code': 'BUSINESS_APP_SERVER_CONTEXT_METADATA_UNAVAILABLE', 'errno': exc.errno}
    except (ValueError, TypeError) as exc:
        result['error'] = {'code': getattr(exc, 'code', 'BUSINESS_APP_SERVER_CONTEXT_ARGUMENT_INVALID'), 'errno': None}
    finally:
        if root is not None:
            os.close(root)
    return result
