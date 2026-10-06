"""Read-only local diagnostics; never initialize, migrate, repair or delete data."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import stat
import time
import tomllib
from urllib.parse import quote

from .db import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[2]
BUCKETS = {'papers', 'datasets', 'dataset-imports', 'imports', 'runs', 'monthly', 'revisions'}


@contextmanager
def _probe_lock(path):
    """Keep a successfully acquired existing lock until the observation ends."""
    descriptor = None
    state = 'unavailable'
    try:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                state = 'unsafe'
            else:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    state = 'free'
                except BlockingIOError:
                    state = 'held'
        except FileNotFoundError:
            state = 'missing'
        except OSError:
            state = 'unavailable'
        yield state
    finally:
        if descriptor is not None:
            os.close(descriptor)


def capacity(root: Path, max_entries=100_000, max_seconds=2.0):
    """Bounded descriptor-relative traversal; renamed links cannot escape root."""
    counts = {}
    seen = skipped = unreadable = 0
    truncated = False
    started = time.monotonic()
    stack = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(root, flags)
        try:
            stack.append((descriptor, os.scandir(descriptor), 'workspace_root'))
        except OSError:
            os.close(descriptor)
            raise
        while stack:
            if seen >= max_entries or time.monotonic() - started >= max_seconds:
                truncated = True
                break
            descriptor, entries, bucket = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                os.close(descriptor)
                stack.pop()
                continue
            except OSError:
                unreadable += 1
                entries.close()
                os.close(descriptor)
                stack.pop()
                continue
            seen += 1
            try:
                info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    skipped += 1
                    continue
                group = entry.name if len(stack) == 1 and entry.name in BUCKETS else bucket
                if len(stack) == 1 and stat.S_ISDIR(info.st_mode) and entry.name not in BUCKETS:
                    group = 'other'
                if stat.S_ISDIR(info.st_mode):
                    if len(stack) >= 128:
                        unreadable += 1
                        continue
                    child = os.open(entry.name, flags, dir_fd=descriptor)
                    try:
                        actual = os.fstat(child)
                        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                            raise OSError('Directory identity changed during scan')
                        iterator = os.scandir(child)
                    except OSError:
                        os.close(child)
                        raise
                    stack.append((child, iterator, group))
                elif stat.S_ISREG(info.st_mode):
                    item = counts.setdefault(group, {'files': 0, 'logical_bytes': 0})
                    item['files'] += 1
                    item['logical_bytes'] += info.st_size
                else:
                    skipped += 1
            except OSError:
                unreadable += 1
    except OSError:
        unreadable += 1
    finally:
        for descriptor, entries, _ in stack:
            entries.close()
            os.close(descriptor)
    return {'groups': counts, 'logical_bytes': sum(v['logical_bytes'] for v in counts.values()),
            'files': sum(v['files'] for v in counts.values()), 'entries_examined': seen,
            'skipped_links_or_special_files': skipped, 'unreadable_entries': unreadable,
            'complete': not truncated and not unreadable, 'max_entries': max_entries,
            'max_seconds': max_seconds, 'max_depth': 128,
            'scope': 'Best-effort live inventory; logical sizes, not allocated disk bytes. Hard links count per path. No files deleted.'}


def _database(root, lease_state):
    path = root / 'workbench.sqlite3'
    if path.is_symlink() or not path.is_file():
        return {'status': 'missing_or_unsafe'}
    wal, shm = Path(str(path) + '-wal'), Path(str(path) + '-shm')
    if wal.is_symlink() or shm.is_symlink():
        return {'status': 'unsafe_sidecar'}
    # Quiescent and WAL-free: immutable mode avoids creating SQLite sidecars.
    # Existing live WAL: read its committed records, never silently ignore WAL.
    immutable = lease_state == 'free' and not wal.exists()
    if not immutable and not (wal.is_file() and shm.is_file()):
        return {'status': 'unavailable', 'reason': 'No safe read snapshot available; retry after services stop.'}
    uri = 'file:' + quote(str(path), safe='/') + ('?mode=ro&immutable=1' if immutable else '?mode=ro')
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 2
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute('BEGIN')
        row = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        schema = row[0] if row else None
        result = {'status': 'ok', 'schema': schema, 'supported_schema': SCHEMA_VERSION,
                  'snapshot': 'quiescent_immutable' if immutable else 'live_read_transaction'}
        if schema != str(SCHEMA_VERSION):
            return {**result, 'status': 'unsupported_schema', 'migration_applied': False}
        result['run_counts'] = {row[0]: row[1] for row in connection.execute('SELECT status,COUNT(*) FROM runs GROUP BY status')}
        result['monthly_counts'] = {row[0]: row[1] for row in connection.execute('SELECT status,COUNT(*) FROM monthly_experiments GROUP BY status')}
        result['monthly_recent_failures'] = [dict(row) for row in connection.execute(
            "SELECT id,status,updated_at,error IS NOT NULL AS has_error FROM monthly_experiments WHERE status IN ('failed','interrupted') ORDER BY created_at DESC,id DESC LIMIT 10")]
        # Free-text errors can include private paths, snippets and tokens. Export only a presence flag.
        result['recent_failures'] = [dict(row) for row in connection.execute(
            "SELECT id,status,finished_at,error IS NOT NULL AS has_error FROM runs WHERE status IN ('failed','interrupted') ORDER BY created_at DESC,id DESC LIMIT 10")]
        stamp = connection.execute('SELECT MAX(last_seen) FROM workers').fetchone()[0]
        if stamp is not None and (type(stamp) not in (int, float) or not math.isfinite(stamp)):
            return {**result, 'status': 'invalid_heartbeat', 'worker': {'recent_heartbeat': False, 'reason': 'Stored heartbeat is not a finite timestamp.'}}
        age = time.time() - stamp if stamp is not None else None
        result['worker'] = {'recent_heartbeat': age is not None and 0 <= age < 30,
                            'heartbeat_age_seconds': age,
                            'scope': 'Heartbeat observation only; does not prove API or process health.'}
        return result
    except sqlite3.Error:
        return {'status': 'unavailable', 'reason': 'Database cannot be read within the bounded snapshot; no repair attempted.'}
    finally:
        if connection is not None:
            connection.close()


def diagnose(root: Path, *, max_entries=100_000, max_seconds=2.0):
    if not 1 <= max_entries <= 1_000_000 or not 0 < max_seconds <= 30:
        raise ValueError('Diagnostic inventory limits are outside the supported range')
    root = Path(root).absolute()
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    result = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'application_version': version, 'python': platform.python_version(), 'platform': platform.system(),
              'workspace_scope': hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
              'read_only': True, 'ai_enabled': False,
              'scope': 'No business writes, initialization, migrations, repairs or deletions. Live SQLite can update or recreate WAL/shared-memory coordination sidecars. No titles, quotes, raw errors or filesystem paths exported.'}
    if root.is_symlink() or not root.is_dir():
        return {**result, 'status': 'missing_or_unsafe_workspace'}
    with _probe_lock(root.parent / f'.{root.name}.workspace.lock') as lease:
        with _probe_lock(root / '.worker.lock') as worker:
            result['locks'] = {'workspace_exclusive_probe': lease, 'worker_exclusive_probe': worker,
                               'scope': 'Instantaneous advisory lock observations, not process identities.'}
            result['database'] = _database(root, lease)
    result['capacity'] = capacity(root, max_entries, max_seconds)
    result['status'] = 'ok' if result['database']['status'] == 'ok' and result['capacity']['complete'] else 'attention'
    result['archive_guidance'] = 'For archival, stop services and use the existing complete backup/verify commands. No selective or automatic deletion is supported.'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--out', type=Path, help='Optional NEW JSON file outside the workspace; omitting writes JSON to stdout')
    args = parser.parse_args(argv)
    try:
        if args.out and args.out.resolve().is_relative_to(args.home.resolve()):
            raise ValueError('Diagnostic output must be outside the inspected workspace')
        result = diagnose(args.home)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.out:
            with args.out.open('x') as stream:
                stream.write(rendered)
        print(rendered, end='')
        return 0 if result['status'] == 'ok' else 2
    except (ValueError, OSError) as exc:
        parser.exit(2, f'Diagnostic request failed ({type(exc).__name__}); no business-data changes applied.\n')


if __name__ == '__main__':
    raise SystemExit(main())
