"""Bounded local source identity, never a search for an enclosing checkout.

A portable commit is a local builder declaration whose file inventory is checked.
It is not a signed attestation or verification of a remote Git repository.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

MAX_FILES = 2000
MAX_BYTES = 128 * 1024 * 1024


def _git(root: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(root), *args],
                                   env=env, stderr=subprocess.DEVNULL, timeout=5)


def clean_checkout_commit(root: Path, files: dict[str, bytes] | None = None) -> str | None:
    """Only an actual, clean checkout root can supply HEAD, not its parent."""
    root = Path(root).absolute()
    if root.is_symlink() or not (root / '.git').exists() or (root / '.git').is_symlink():
        return None
    root = root.resolve()
    try:
        if Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != root:
            return None
        if _git(root, 'status', '--porcelain', '--untracked-files=normal').strip():
            return None
        commit = _git(root, 'rev-parse', 'HEAD').decode().strip()
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            return None
        # A bundled tracked input must be the captured HEAD blob, even if local
        # index flags hide a modification from `git status`.
        if files is not None:
            tracked = set(_git(root, 'ls-files', '-z').decode().split('\0'))
            for name, data in files.items():
                if name in tracked and _git(root, 'show', f'{commit}:{name}') != data:
                    return None
        if _git(root, 'rev-parse', 'HEAD').decode().strip() != commit:
            return None
        return commit
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _read(root_fd: int, name: str, limit: int) -> bytes:
    parts = PurePosixPath(name).parts
    if (not name or name.startswith('/') or '\\' in name or '\x00' in name
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('Unsafe inventory path')
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise ValueError('Unbounded or linked source file')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                data = stream.read(limit + 1)
            after = os.fstat(fd)
            if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError('Source file changed during identity read')
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate manifest key')
        value[key] = item
    return value


def portable_source_commit(root: Path) -> str | None:
    """Return the builder's commit only when every bounded package file matches."""
    root = Path(root).absolute()
    if root.is_symlink():
        return None
    fd = None
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        manifest = json.loads(_read(fd, 'RELEASE_MANIFEST.json', 1024 * 1024), object_pairs_hook=_unique)
        commit = manifest.get('source_commit')
        if (manifest.get('schema_version') != 1 or manifest.get('format') != 'personal-local-source-bundle'
                or manifest.get('source_commit_verified') is not True
                or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit)):
            return None
        files = manifest.get('files')
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
            return None
        encoded = (json.dumps(files, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        if hashlib.sha256(encoded).hexdigest() != manifest.get('content_sha256'):
            return None
        total = 0
        for name, expected in files.items():
            if not isinstance(expected, dict) or type(expected.get('size')) is not int or not 0 <= expected['size'] <= MAX_BYTES:
                return None
            total += expected['size']
            if total > MAX_BYTES:
                return None
            data = _read(fd, name, min(expected['size'], MAX_BYTES))
            if expected != {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
                return None
        # An extra Python module must not inherit the identity of a pristine
        # package. Editable-install metadata and bytecode remain outside scope.
        actual = set()
        visited = 0
        for folder, dirs, names in os.walk(root / 'paper_alpha', followlinks=False):
            visited += len(dirs) + len(names)
            if visited > MAX_FILES * 3 or any((Path(folder) / item).is_symlink() for item in dirs):
                return None
            dirs[:] = [item for item in dirs if item != '__pycache__']
            actual.update((Path(folder) / name).relative_to(root).as_posix() for name in names if name.endswith('.py'))
        expected_python = {name for name in files if name.startswith('paper_alpha/') and name.endswith('.py')}
        return commit if actual == expected_python and actual else None
    except (OSError, ValueError, TypeError, KeyError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
