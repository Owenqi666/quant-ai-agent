"""Offline, checksummed directory backup and new-directory-only restoration.

The manifest is an integrity inventory, not a cryptographic authenticity proof.
Only restore backups you trust: SQLite and preserved paper files are local input.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import time
from urllib.parse import quote

from ..storage import atomic_json, read_json
from .maintenance import workspace_lease

FORMAT = "paper-alpha-workbench-backup"
VERSION = 1
SUPPORTED_SCHEMAS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17"}
MAX_FILES = 100_000
MAX_BYTES = 20 * 1024 ** 3
MAX_MANIFEST_BYTES = 32 * 1024 ** 2
EXCLUDED = {".workspace.lock", ".worker.lock", "workbench.sqlite3.schema.lock",
            "workbench.sqlite3-wal", "workbench.sqlite3-shm", "workbench.sqlite3-journal"}
PATH_COLUMNS = {"papers": ("pdf_path",), "datasets": ("data_path", "metadata_path"),
                "attempts": ("output_dir",), "artifacts": ("path",)}


class BackupError(ValueError):
    pass


def _path(value):
    # Check lexical parents before resolve so a symlink cannot be hidden by it.
    path = Path(value).expanduser().absolute()
    if ".." in path.parts:
        raise BackupError("Paths must not contain '..'")
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise BackupError("Symlinks are not supported in backup or workspace paths")
    if path == Path(path.anchor):
        raise BackupError("A filesystem root is not a workspace or backup destination")
    return path


def _separate(first, second):
    if first == second or first.is_relative_to(second) or second.is_relative_to(first):
        raise BackupError("Source and destination must be separate, non-nested directories")


def _new_directory(path):
    if not path.parent.is_dir():
        raise BackupError("Destination parent directory must already exist")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BackupError("Destination already exists; restoration and backup never overwrite") from exc


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or "\x00" in value:
        raise BackupError("Invalid backup inventory path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(value).is_absolute():
        raise BackupError("Invalid backup inventory path")
    return value


def _inventory(root, *, exclude=()):
    files, directories = {}, []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(directory)
        for name in sorted(dirnames + filenames):
            path = directory / name
            relative = _relative(path.relative_to(root).as_posix())
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise BackupError(f"Only regular files and directories may be backed up: {relative}")
            if relative in exclude:
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            else:
                if info.st_nlink != 1:
                    raise BackupError(f"Hard-linked files are not supported: {relative}")
                files[relative] = (info.st_size, info.st_mtime_ns, info.st_ino)
            if len(files) + len(directories) > MAX_FILES:
                raise BackupError("Backup exceeds the 100,000-entry limit")
    if sum(item[0] for item in files.values()) > MAX_BYTES:
        raise BackupError("Backup exceeds the 20 GiB limit")
    return files, sorted(directories)


def _copy(source, destination=None):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BackupError("Backup source must be an independent regular file")
        if before.st_size > MAX_BYTES:
            raise BackupError("File exceeds backup size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            target = destination.open("xb") if destination else None
            try:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise BackupError("File exceeds backup size limit")
                    digest.update(chunk)
                    if target:
                        target.write(chunk)
                if target:
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                if target:
                    target.close()
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or size != before.st_size:
            raise BackupError("Source file changed during backup")
    finally:
        os.close(descriptor)
    return {"size": size, "sha256": digest.hexdigest()}


@contextmanager
def _worker_fence(root):
    descriptor = os.open(root / ".worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackupError("Worker lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("A worker or its engine child is still active; stop it before backup") from exc
        yield
    finally:
        os.close(descriptor)


def _open_database(path, *, readonly=True):
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + ("?mode=ro" if readonly else "?mode=rw"),
                                 uri=True, timeout=5)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _database_check(connection):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("SQLite foreign-key check failed")
    # Application backups only contain plain tables and indexes, never code-bearing schema.
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type IN ('trigger','view')").fetchone():
        raise BackupError("Backup database may not contain triggers or views")
    row = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    if not row or row[0] not in SUPPORTED_SCHEMAS:
        raise BackupError("Unsupported workbench database schema")
    for table, columns in PATH_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not {"id", *columns}.issubset(actual):
            raise BackupError("Backup database is missing required path columns")
    return row[0]


def create_backup(home: Path, out: Path):
    """Copy an offline workspace; manifest is written only after all checks pass."""
    home, out = _path(home), _path(out)
    _separate(home, out)
    if not home.is_dir() or not (home / "workbench.sqlite3").is_file():
        raise BackupError("Source is not an initialized workbench")
    if (home / ".restore-incomplete").exists():
        raise BackupError("An incomplete restoration cannot be backed up")
    # Existing destination detection before locks avoids surprising work on invalid requests.
    if out.exists():
        raise BackupError("Destination already exists; backup never overwrites")
    created = False
    try:
        with workspace_lease(home, exclusive=True), workspace_lease(out, exclusive=True, create=False), _worker_fence(home):
            initial, directories = _inventory(home, exclude=EXCLUDED)
            _new_directory(out)
            created = True
            files_root = out / "files"
            files_root.mkdir(mode=0o700)
            for name in directories:
                (files_root / name).mkdir(parents=True, exist_ok=True)
            database = files_root / "workbench.sqlite3"
            with closing(_open_database(home / "workbench.sqlite3")) as source:
                with closing(sqlite3.connect(database)) as destination:
                    deadline = time.monotonic() + 60
                    def progress(status, remaining, total):
                        if time.monotonic() > deadline:
                            raise BackupError("SQLite snapshot exceeded the 60-second limit")
                    source.backup(destination, pages=256, progress=progress)
                    schema = _database_check(destination)
                    destination.execute("PRAGMA journal_mode=DELETE")
            entries = {}
            for name in sorted(initial):
                if name != "workbench.sqlite3":
                    entries[name] = _copy(home / name, files_root / name)
            # WAL/SHM are never copied. SQLite's backup API creates the complete database.
            entries["workbench.sqlite3"] = _copy(database)
            final, final_dirs = _inventory(home, exclude=EXCLUDED)
            # The SQLite read may legitimately checkpoint the source database on close;
            # only application assets need exact stat stability after SQLite's own snapshot.
            initial.pop("workbench.sqlite3")
            final.pop("workbench.sqlite3")
            if initial != final or directories != final_dirs:
                raise BackupError("Workspace assets changed during backup")
            manifest = {"format": FORMAT, "version": VERSION,
                        "created_at": datetime.now(timezone.utc).isoformat(), "source_root": str(home),
                        "database_schema": schema, "files": entries, "directories": directories,
                        "total_bytes": sum(entry["size"] for entry in entries.values()),
                        "excluded_runtime_files": sorted(EXCLUDED)}
            for directory in [*(files_root / name for name in reversed(directories)), files_root]:
                _sync_directory(directory)
            atomic_json(out / "manifest.json", manifest)
            _sync_directory(out)
            _sync_directory(out.parent)
            verify_backup(out)
            return manifest
    except BaseException:
        if created:
            shutil.rmtree(out)
        raise


def verify_backup(backup: Path):
    """Verify exact inventory and every checksum without mutating a backup."""
    backup = _path(backup)
    manifest_path = backup / "manifest.json"
    if not backup.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError("Backup is incomplete: a regular manifest.json is required")
    if manifest_path.stat().st_nlink != 1:
        raise BackupError("Hard-linked manifest files are not supported")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BackupError("Backup manifest exceeds size limit")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise BackupError("Unsupported backup format")
    if manifest.get("database_schema") not in SUPPORTED_SCHEMAS:
        raise BackupError("Unsupported workbench database schema")
    source_root = manifest.get("source_root")
    if not isinstance(source_root, str) or not source_root.startswith("/") or ".." in source_root.split("/") or str(Path(source_root)) != source_root or source_root == "/":
        raise BackupError("Invalid source workspace provenance")
    entries, directories = manifest.get("files"), manifest.get("directories")
    if not isinstance(entries, dict) or not isinstance(directories, list) or len(entries) + len(directories) > MAX_FILES:
        raise BackupError("Invalid backup inventory")
    for name in directories:
        _relative(name)
        if name in EXCLUDED or name == ".restore-incomplete":
            raise BackupError("Runtime or incomplete-restoration directories are not allowed")
    if len(directories) != len(set(directories)):
        raise BackupError("Duplicate backup directories")
    total = 0
    for name, entry in entries.items():
        _relative(name)
        if name in EXCLUDED or name == ".restore-incomplete":
            raise BackupError("Runtime lock or incomplete-restoration file found in backup")
        if not isinstance(entry, dict) or set(entry) != {"size", "sha256"} or type(entry["size"]) is not int or entry["size"] < 0:
            raise BackupError("Invalid file metadata")
        fingerprint = entry["sha256"]
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise BackupError("Invalid file checksum")
        total += entry["size"]
    if total > MAX_BYTES or type(manifest.get("total_bytes")) is not int or manifest["total_bytes"] != total:
        raise BackupError("Invalid backup total size")
    if "workbench.sqlite3" not in entries:
        raise BackupError("Backup database is missing")
    if set(path.name for path in backup.iterdir()) != {"manifest.json", "files"}:
        raise BackupError("Backup contains untracked files")
    files_root = backup / "files"
    if not files_root.is_dir() or files_root.is_symlink():
        raise BackupError("Backup files directory is invalid")
    actual, actual_dirs = _inventory(files_root)
    if set(actual) != set(entries) or actual_dirs != sorted(directories):
        raise BackupError("Backup file inventory does not match its manifest")
    for name, expected in entries.items():
        if _copy(files_root / name) != expected:
            raise BackupError(f"Backup checksum mismatch: {name}")
    with closing(_open_database(files_root / "workbench.sqlite3")) as connection:
        if _database_check(connection) != manifest["database_schema"]:
            raise BackupError("Database schema does not match backup manifest")
    return manifest


def _rebase_database(path, old_root, new_root):
    changed = 0
    with closing(_open_database(path, readonly=False)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _database_check(connection)
            for table, columns in PATH_COLUMNS.items():
                for column in columns:
                    for identity, stored in connection.execute(f"SELECT id,{column} FROM {table}").fetchall():
                        if not isinstance(stored, str) or not Path(stored).is_absolute() or ".." in Path(stored).parts:
                            raise BackupError("Database contains an invalid workspace path")
                        original = Path(stored)
                        if not original.is_relative_to(old_root) or original == old_root:
                            raise BackupError("Database path points outside the original workspace")
                        rebased = new_root / original.relative_to(old_root)
                        # Missing output folders can be a legitimate failed materialization.
                        if table != "attempts" and not rebased.is_file():
                            raise BackupError("A database-referenced input or artifact is missing")
                        if table == "attempts" and rebased.exists() and not rebased.is_dir():
                            raise BackupError("An attempt output path is not a directory")
                        connection.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (str(rebased), identity))
                        changed += 1
            previous = connection.execute("SELECT value FROM settings WHERE key='historical_workspace_roots'").fetchone()
            roots = json.loads(previous[0]) if previous else []
            if not isinstance(roots, list) or any(not isinstance(value, str) or not value.startswith("/") for value in roots):
                raise BackupError("Invalid historical workspace root metadata")
            roots = sorted(set([*roots, str(old_root)]))
            connection.execute("INSERT INTO settings(key,value) VALUES ('historical_workspace_roots',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(roots),))
            _database_check(connection)
    return changed


def restore_backup(backup: Path, home: Path):
    """Restore to a newly created workspace and rebase only database path columns."""
    backup, home = _path(backup), _path(home)
    _separate(backup, home)
    if home.exists():
        raise BackupError("Destination already exists; restoration never overwrites")
    if not home.parent.is_dir():
        raise BackupError("Destination parent directory must already exist")
    manifest = verify_backup(backup)
    with workspace_lease(home, exclusive=True, create=False):
        return _restore_files(backup, home, manifest)


def _restore_files(backup, home, manifest):
    # Caller holds the sibling lease before mkdir, closing the creation/marker gap.
    created = False
    try:
        _new_directory(home)
        created = True
        marker = home / ".restore-incomplete"
        with marker.open("x") as stream:
            stream.write("Restoration is incomplete. Do not open this workspace.\n")
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(home)
        for name in manifest["directories"]:
            (home / name).mkdir(parents=True, exist_ok=True)
        for name, expected in manifest["files"].items():
            if _copy(backup / "files" / name, home / name) != expected:
                raise BackupError("Backup changed during restoration")
        changed = _rebase_database(home / "workbench.sqlite3", Path(manifest["source_root"]), home)
        for directory in [*(home / name for name in reversed(manifest["directories"])), home]:
            _sync_directory(directory)
        marker.unlink()
        _sync_directory(home)
        _sync_directory(home.parent)
        return {"restored": True, "home": str(home), "database_schema": manifest["database_schema"],
                "files": len(manifest["files"]), "rebased_paths": changed,
                "source_backup": str(backup), "source_root": manifest["source_root"]}
    except BaseException:
        if created:
            shutil.rmtree(home)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline Paper Alpha workspace backup/restore (no overwrite)")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Stop API and worker, then create a new backup directory")
    backup.add_argument("--home", required=True, type=Path)
    backup.add_argument("--out", required=True, type=Path)
    restore = commands.add_parser("restore", help="Verify and restore into a new workspace directory")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--home", required=True, type=Path)
    verify = commands.add_parser("verify", help="Read-only verification of a backup directory")
    verify.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            manifest = create_backup(args.home, args.out)
            result = {"backed_up": True, "backup": str(args.out.absolute()), "files": len(manifest["files"]),
                      "total_bytes": manifest["total_bytes"], "database_schema": manifest["database_schema"]}
        elif args.command == "restore":
            result = restore_backup(args.backup, args.home)
        else:
            manifest = verify_backup(args.backup)
            result = {"verified": True, "files": len(manifest["files"]), "total_bytes": manifest["total_bytes"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
