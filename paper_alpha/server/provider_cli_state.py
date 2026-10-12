"""Private CLI state for the explicitly scoped local schema canary.

Authentication stays in Codex's native path: this module only checks metadata
and creates a temporary link. It never reads, copies, hashes or logs credential
contents. Native token-refresh writes have not been verified by login-status
checks. The caller must retain this state until process cleanup is confirmed.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile

CONTEXT_POLICY_VERSION = 'codex_cli_context_v1'
_CONTEXT_SETTING = 'project_doc_max_bytes=0'
_MAX_AUTH_BYTES = 1024 * 1024


class CliStateError(ValueError):
    """A bounded code with no source credential path or contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _native_cli_home() -> Path:
    configured = os.environ.get('CODEX_HOME')
    return Path(configured).expanduser() if configured else Path.home() / '.codex'


def _check_parent(parent: Path) -> None:
    if (not parent.is_absolute() or '..' in parent.parts or
            any(path.is_symlink() for path in (parent, *parent.parents))):
        raise CliStateError('CLI_STATE_PARENT_INVALID')
    try:
        value = parent.stat()
    except OSError:
        raise CliStateError('CLI_STATE_PARENT_INVALID') from None
    if not stat.S_ISDIR(value.st_mode):
        raise CliStateError('CLI_STATE_PARENT_INVALID')


def _check_global_skills() -> None:
    # CODEX_HOME isolation does not hide this separate HOME-based skill root.
    # Fail closed on any entry, including an empty directory or broken symlink.
    try:
        (Path.home() / '.agents' / 'skills').lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise CliStateError('GLOBAL_USER_SKILLS_UNCHECKABLE') from None
    raise CliStateError('GLOBAL_USER_SKILLS_PRESENT')


def _auth_metadata(source: Path) -> tuple[int, ...]:
    try:
        value = source.lstat()
    except OSError:
        raise CliStateError('NATIVE_AUTH_UNAVAILABLE') from None
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
            or value.st_mode & 0o077 or not 0 < value.st_size <= _MAX_AUTH_BYTES):
        raise CliStateError('NATIVE_AUTH_UNSAFE')
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def prepare_cli_state(parent: Path, *, with_native_auth: bool) -> dict:
    """Create a fresh 0700 state directory, sibling to caller-owned work.

    ``parent`` must already exist. No global config, auth or environment is
    modified. With native auth enabled, only an owner-private regular auth file
    is linked; any HOME-based user skill root blocks this narrowly scoped mode.
    No successful return grants permission for model inference or dispatch.
    """
    if type(with_native_auth) is not bool:
        raise CliStateError('CLI_STATE_ARGUMENT_INVALID')
    parent = Path(parent)
    _check_parent(parent)
    _check_global_skills()
    source = None
    before = None
    if with_native_auth:
        home = _native_cli_home()
        if (not home.is_absolute() or '..' in home.parts or
                any(path.is_symlink() for path in (home, *home.parents))):
            raise CliStateError('NATIVE_AUTH_UNSAFE')
        source = home / 'auth.json'
        before = _auth_metadata(source)
    state = None
    try:
        state = Path(tempfile.mkdtemp(prefix='cli-state-', dir=parent))
        state.chmod(0o700)
        if source is not None:
            (state / 'auth.json').symlink_to(source)
            if _auth_metadata(source) != before:
                raise CliStateError('NATIVE_AUTH_CHANGED')
        return {'path': state, 'native_auth_linked': source is not None,
                'context_policy_version': CONTEXT_POLICY_VERSION}
    except (OSError, CliStateError) as exc:
        # No child has been started by this helper, so construction rollback is
        # safe. Lifecycle cleanup after a successful return belongs to caller.
        if state is not None:
            shutil.rmtree(state, ignore_errors=True)
        if isinstance(exc, CliStateError):
            raise
        raise CliStateError('CLI_STATE_CREATE_FAILED') from None


def append_context_policy(argv: list[str]) -> list[str]:
    """Return a new argv with the observed project-document policy appended."""
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        raise CliStateError('CLI_STATE_ARGUMENT_INVALID')
    return [*argv, '-c', _CONTEXT_SETTING]


# Independent probe lane. The existing canary helper/global HOME checks above
# remain unchanged and are never bypassed for existing callers.
APP_SERVER_CONTEXT_POLICY_VERSION = 'codex_app_server_isolated_context_v2'
_APP_SERVER_STATE_KEYS = {'path', 'native_auth_linked', 'context_policy_version', 'home', 'codex_home'}
_APP_SERVER_CONTEXT_MAX_ENTRIES = 4096
_APP_SERVER_CONTEXT_MAX_DEPTH = 16


def _app_server_directory(path: Path) -> int:
    """Open an absolute directory component by component, without following links."""
    if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
            or path == Path('/') or str(path).startswith('//') or len(str(path)) > 4096
            or any(ord(char) < 32 for char in str(path))):
        raise CliStateError('APP_SERVER_STATE_PATH_INVALID')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CliStateError('APP_SERVER_STATE_DIRECTORY_UNSAFE')
        return directory
    except (OSError, CliStateError) as exc:
        os.close(directory)
        if isinstance(exc, CliStateError):
            raise
        raise CliStateError('APP_SERVER_STATE_DIRECTORY_UNSAFE') from None


def _app_server_state_metadata(directory: int) -> None:
    """Bounded metadata only; never open auth, config or any state file content."""
    total = 0
    def check(current, depth, prefix):
        nonlocal total
        if depth > _APP_SERVER_CONTEXT_MAX_DEPTH:
            raise CliStateError('APP_SERVER_STATE_METADATA_LIMIT')
        with os.scandir(current) as entries:
            for entry in entries:
                total += 1
                if total > _APP_SERVER_CONTEXT_MAX_ENTRIES:
                    raise CliStateError('APP_SERVER_STATE_METADATA_LIMIT')
                relative = (*prefix, entry.name)
                if (relative in (('auth.json',), ('config.toml',), ('.env',), ('skills',), ('.agents', 'skills'))):
                    raise CliStateError('APP_SERVER_STATE_CONTEXT_POLLUTED')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                    raise CliStateError('APP_SERVER_STATE_CONTEXT_UNSAFE')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (
                                info.st_dev, info.st_ino, info.st_uid, info.st_mode):
                            raise CliStateError('APP_SERVER_STATE_CONTEXT_UNSAFE')
                        check(child, depth + 1, relative)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CliStateError('APP_SERVER_STATE_CONTEXT_UNSAFE')
    try:
        check(directory, 0, ())
    except OSError:
        raise CliStateError('APP_SERVER_STATE_CONTEXT_UNSAFE') from None


def validate_app_server_probe_state(result) -> dict:
    """Check only the isolated probe HOME; normal regular Native state may remain."""
    if (not isinstance(result, dict) or set(result) != _APP_SERVER_STATE_KEYS
            or result['native_auth_linked'] is not False
            or result['context_policy_version'] != APP_SERVER_CONTEXT_POLICY_VERSION
            or not all(isinstance(result[key], Path) for key in ('path', 'home', 'codex_home'))
            or result['home'] != result['path'] or result['codex_home'] != result['path']
            or result['path'].name != 'cli-state'):
        raise CliStateError('APP_SERVER_STATE_ARGUMENT_INVALID')
    parent = _app_server_directory(result['path'].parent)
    state = None
    try:
        state = _app_server_directory(result['path'])
        _app_server_state_metadata(state)
        return dict(result)
    finally:
        os.close(parent)
        if state is not None:
            os.close(state)


def prepare_app_server_probe_state(parent: Path) -> dict:
    """Create exactly one new parent/cli-state with zero native-auth linkage."""
    try:
        parent = Path(parent)
    except (TypeError, ValueError):
        raise CliStateError('APP_SERVER_STATE_ARGUMENT_INVALID') from None
    directory = _app_server_directory(parent)
    created = False
    state = parent / 'cli-state'
    try:
        # mkdir is exclusive: no pre-existing state, including a broken link,
        # is reused or cleaned up by this helper.
        os.mkdir('cli-state', mode=0o700, dir_fd=directory)
        created = True
        result = {'path': state, 'native_auth_linked': False,
            'context_policy_version': APP_SERVER_CONTEXT_POLICY_VERSION,
            'home': state, 'codex_home': state}
        return validate_app_server_probe_state(result)
    except FileExistsError:
        raise CliStateError('APP_SERVER_STATE_ALREADY_EXISTS') from None
    except (OSError, CliStateError) as exc:
        if created:
            # No child has started. Remove only the empty directory just
            # created, never a pre-existing or recursively populated state.
            try:
                os.rmdir('cli-state', dir_fd=directory)
            except OSError:
                pass
        if isinstance(exc, CliStateError):
            raise
        raise CliStateError('APP_SERVER_STATE_CREATE_FAILED') from None
    finally:
        os.close(directory)
