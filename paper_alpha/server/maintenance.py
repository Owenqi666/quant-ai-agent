"""Cooperative local maintenance fence shared by API, worker and backup CLI."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


@contextmanager
def workspace_lease(root: Path, *, exclusive: bool = False, create: bool = True):
    """Hold a nonblocking shared service lease or exclusive maintenance lease.

    Services acquire this before opening the store and retain it for their
    lifetime. The stable sibling lock exists before a workspace directory does,
    so a restore can fence initialization before creating its destination.
    The separate inherited worker lock also fences orphaned compute children.
    Locks are released by closing descriptors, never explicit unlock. Lock files
    must not be deleted while another process may be using the workspace.
    """
    root = Path(root).absolute()
    if root.is_symlink():
        raise RuntimeError("Workspace root must not be a symlink")
    if create:
        root.parent.mkdir(parents=True, exist_ok=True)
    # The old in-workspace filename remains excluded from backups for older runs.
    lock_path = root.parent / f".{root.name}.workspace.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Workspace lease must be a regular file")
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            message = "Stop the API and worker before maintenance" if exclusive else "Workspace maintenance is active"
            raise RuntimeError(message) from exc
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise RuntimeError("Workspace root must not be a symlink")
        yield descriptor
    finally:
        os.close(descriptor)
