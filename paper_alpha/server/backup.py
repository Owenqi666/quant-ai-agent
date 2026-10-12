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
import re
import shutil
import sqlite3
import stat
import time
import uuid
from urllib.parse import quote

from ..storage import atomic_json, read_json
from .maintenance import workspace_lease

FORMAT = "paper-alpha-workbench-backup"
VERSION = 1
SUPPORTED_SCHEMAS = {str(version) for version in range(1, 30)}
MAX_FILES = 100_000
MAX_BYTES = 20 * 1024 ** 3
MAX_MANIFEST_BYTES = 32 * 1024 ** 2
EXCLUDED = {".workspace.lock", ".worker.lock", "workbench.sqlite3.schema.lock",
            "workbench.sqlite3-wal", "workbench.sqlite3-shm", "workbench.sqlite3-journal"}
PATH_COLUMNS = {"papers": ("pdf_path",), "datasets": ("data_path", "metadata_path"),
                "attempts": ("output_dir",), "artifacts": ("path",)}
PRIVATE_COMPONENTS = frozenset({"real_daily_sources", "raw", "secrets", "secret", "keys", "credentials", ".aws", ".ssh"})
PRIVATE_NAMES = frozenset({'credentials.json', 'secrets.json', 'secret.json', 'key.json', 'keys.json',
    'auth.json', 'api-key.txt', 'api_key.txt', 'apikey.txt', 'id_rsa', 'id_ed25519', 'provider-config.json'})
PREFIX_FILES = frozenset({"prefix.csv", "receipt.json", "metadata.json", "registration.json"})


def _private_input(relative):
    """Lexical only: never open/stat captured originals or credential folders.

    Legacy managed snapshots/inputs/raw.csv is a prior recovery contract and
    remains supported. New source captures (including their final rows) never
    belong in a portable backup.
    """
    parts = relative.split("/")
    return any(part.lower() in PRIVATE_COMPONENTS | PRIVATE_NAMES or part.lower() == ".env" or
               part.lower().startswith(".env.") or
               part.lower().endswith((".raw.json", ".key", ".pem", ".p12", ".pfx"))
               for part in parts)

# Schema26 stores daily private inputs by registration ID under the current root;
# it adds no absolute DB paths to PATH_COLUMNS. Inner research receipts stay raw.
SNAPSHOT_COLUMNS = {
    "daily_snapshot_registrations": {
        "id", "title", "status", "created_at", "updated_at", "idempotency_key",
        "request_digest", "input_digest", "contract_file_sha256", "contract_digest",
        "receipt_file_sha256", "raw_sha256", "raw_bytes", "contract_json", "receipt_json",
        "origin", "declaration_digest", "owner_token", "latest_attempt_id",
        "registered_dataset_id", "registration_digest", "development_content_digest",
        "development_index_digest", "error"
    },
    "daily_snapshot_attempts": {
        "id", "registration_id", "number", "status", "stage", "started_at", "finished_at",
        "owner_token", "code_digest", "report", "error"
    },
    "daily_snapshot_guard_boundaries": {
        "registration_id", "declaration_digest", "scope_digest", "development_start",
        "development_end", "test_start", "test_end", "declared_key_count", "created_at",
        "boundary_digest"
    },
    "daily_snapshot_guard_keys": {
        "registration_id", "date", "asset", "role"
    },
    "daily_snapshot_guard_observations": {
        "registration_id", "date", "asset", "row_sha256"
    },
    "daily_snapshot_guard_bindings": {
        "registration_id", "dataset_id", "registered_digest", "declaration_digest",
        "development_content_digest", "development_index_digest", "row_count", "created_at",
        "binding_digest"
    },
}


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


def _inventory(root, *, exclude=(), filter_private=False, reject_private=False, policy=None, private_specs=None):
    # Exact DB-owned model history exceptions never weaken the lexical filter.
    private_specs = private_specs or {}
    files, directories = {}, []
    def walk(directory, descriptor):
        with os.scandir(descriptor) as stream:
            names = sorted(entry.name for entry in stream)
        for name in names:
            path = directory / name
            relative = _relative(path.relative_to(root).as_posix())
            # The lexical filter comes before lstat or directory descent; using
            # os.walk/DirEntry.is_dir here could already stat a captured source.
            private = _private_input(relative) and relative not in private_specs
            if reject_private and private:
                raise BackupError('Private source or credential asset found in backup: ' + relative)
            if policy is not None and not policy(relative):
                continue
            if relative in exclude or (filter_private and private):
                continue
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if relative in private_specs and (not stat.S_ISREG(info.st_mode) or
                    info.st_size > private_specs[relative]['maximum']):
                raise BackupError('Owned proposal history must be a bounded regular file: ' + relative)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise BackupError(f"Only regular files and directories may be backed up: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise BackupError('Directory changed during backup inventory')
                    walk(path, child)
                finally:
                    os.close(child)
            else:
                if info.st_nlink != 1:
                    raise BackupError(f"Hard-linked files are not supported: {relative}")
                files[relative] = (info.st_size, info.st_mtime_ns, info.st_ino)
            if len(files) + len(directories) > MAX_FILES:
                raise BackupError("Backup exceeds the 100,000-entry limit")
    descriptor = _file_descriptor(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        walk(Path(root), descriptor)
    finally:
        os.close(descriptor)
    if sum(item[0] for item in files.values()) > MAX_BYTES:
        raise BackupError("Backup exceeds the 20 GiB limit")
    if not set(private_specs).issubset(files):
        raise BackupError('Mandatory owned proposal history is missing')
    return files, sorted(directories)


def _file_descriptor(path, flags, mode=0o600):
    """Open from a held directory chain; every ancestor refuses symlinks."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise BackupError('Backup paths must not contain parent traversal')
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        return os.open(path.name, flags | os.O_NOFOLLOW, mode, dir_fd=directory)
    finally:
        os.close(directory)


def _copy(source, destination=None, *, maximum=MAX_BYTES):
    descriptor = _file_descriptor(source, os.O_RDONLY | os.O_NONBLOCK)
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BackupError("Backup source must be an independent regular file")
        if before.st_size > maximum:
            raise BackupError("File exceeds backup size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            target = os.fdopen(_file_descriptor(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') if destination else None
            try:
                while chunk := stream.read(min(1024 * 1024, maximum - size + 1)):
                    size += len(chunk)
                    if size > maximum:
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
        reopened = _file_descriptor(source, os.O_RDONLY)
        try:
            current = os.fstat(reopened)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, 1):
                raise BackupError('Source path changed during backup')
        finally:
            os.close(reopened)
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


def _proposal_history_specs(connection):
    """Prove every DB owner before granting two exact private-file exceptions.

    No paths are opened here. The original ProposalRequests._load remains the
    final full historical validator; this limited preflight only makes its
    subsequent filesystem reads safe. Schema17 predates the proposal tables.
    """
    from .proposal_requests import MAX_RECORD_BYTES, _decode
    from .provider_contract_registry import request_handler
    from .provider_schema import ProviderConfig
    from ..storage import digest, json_text
    required = {
        'proposal_requests': {'id', 'research_id', 'base_revision_id', 'idempotency_key',
            'request_digest', 'payload', 'payload_digest', 'created_at', 'deadline_at',
            'status', 'state', 'state_digest', 'ledger_digest'},
        'proposal_attempts': {'id', 'proposal_id', 'sequence', 'payload', 'digest'},
        'proposal_events': {'id', 'proposal_id', 'sequence', 'payload', 'digest'},
    }
    if not connection.in_transaction:
        connection.execute('BEGIN')  # All discovery and validation share one view.
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        version = int(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0])
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        present = set(required) & tables
        if version <= 17 and not present:
            return {}
        if version <= 17 or present != set(required):
            raise ValueError('Proposal history tables differ from recorded schema')
        for table, columns in required.items():
            if {row[1] for row in connection.execute(f'PRAGMA table_info({table})')} != columns:
                raise ValueError('Proposal history columns differ')
            if connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] > MAX_FILES:
                raise ValueError('Proposal history exceeds inventory bound')
        # A foreign-key violation must not create an unowned private exception.
        for table in ('proposal_attempts', 'proposal_events'):
            if connection.execute(f'SELECT 1 FROM {table} t LEFT JOIN proposal_requests p ON p.id=t.proposal_id WHERE p.id IS NULL LIMIT 1').fetchone():
                raise ValueError('Unowned proposal history row')
        specs = {}
        for row in connection.execute('SELECT * FROM proposal_requests ORDER BY id'):
            identity = row['id']
            if not isinstance(identity, str) or not re.fullmatch(r'proposal_[0-9a-f]{64}', identity):
                raise ValueError('Invalid proposal owner identity')
            body, state = _decode(row['payload']), _decode(row['state'])
            request = request_handler(body['request']).parse_request(body['request'])
            config = ProviderConfig.model_validate(body['config'])
            if (digest(body) != row['payload_digest'] or digest(body['request']) != row['request_digest']
                    or identity != 'proposal_' + row['request_digest'] or digest(state) != row['state_digest']
                    or state['status'] != row['status'] or request.research_id != row['research_id']
                    or request.base_revision_id != row['base_revision_id']
                    or request.idempotency_key != row['idempotency_key']
                    or body['created_at'] != row['created_at'] or body['deadline_at'] != row['deadline_at']
                    or config.model_dump() != body['config'] or config.config_id != request.provider_config_id):
                raise ValueError('Frozen proposal owner differs')
            canonical = json_text(body['config']).encode('utf-8')
            if len(canonical) > MAX_RECORD_BYTES:
                raise ValueError('Frozen provider config exceeds bound')
            base = 'proposals/' + identity
            specs[base + '/provider-config.json'] = {
                'kind': 'frozen_provider_config', 'maximum': MAX_RECORD_BYTES,
                'sha256': hashlib.sha256(canonical).hexdigest()}
            events = list(connection.execute('SELECT * FROM proposal_events WHERE proposal_id=? ORDER BY sequence', (identity,)))
            if len(events) > 32 or digest([item['digest'] for item in events]) != row['ledger_digest']:
                raise ValueError('Proposal event ledger differs')
            anchors = {}
            for sequence, record in enumerate(events, 1):
                event = _decode(record['payload'])
                if (type(record['sequence']) is not int or type(event['sequence']) is not int
                        or record['sequence'] != sequence or event['sequence'] != sequence
                        or record['id'] != 'proposal_event_' + digest({'proposal': identity, 'sequence': sequence})
                        or digest(event) != record['digest']):
                    raise ValueError('Proposal event owner differs')
                if 'attempt_id' in event:
                    anchors[event['attempt_id']] = event['attempt_digest']
            attempts = list(connection.execute('SELECT * FROM proposal_attempts WHERE proposal_id=? ORDER BY sequence', (identity,)))
            if len(attempts) != state['provider_invocations'] or len(attempts) > request.budget.max_provider_invocations:
                raise ValueError('Proposal attempt charge differs')
            for sequence, record in enumerate(attempts, 1):
                attempt = _decode(record['payload'])
                expected_id = 'proposal_attempt_' + digest({'proposal': identity, 'sequence': sequence})
                if (type(record['sequence']) is not int or type(attempt['sequence']) is not int
                        or record['sequence'] != sequence or attempt['sequence'] != sequence
                        or record['id'] != expected_id or attempt['id'] != expected_id
                        or digest(attempt) != record['digest'] or anchors.get(expected_id) != record['digest']):
                    raise ValueError('Proposal attempt owner differs')
                raw_sha = attempt.get('raw_sha256')
                if attempt['status'] != 'running' and attempt['receipt'] is not None and raw_sha is not None:
                    if not isinstance(raw_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', raw_sha):
                        raise ValueError('Saved provider raw response anchor differs')
                    specs[base + '/attempts/' + expected_id + '/response.raw.json'] = {
                        'kind': 'saved_provider_response', 'maximum': request.budget.max_output_bytes,
                        'sha256': raw_sha}
        return specs
    except (ValueError, TypeError, KeyError, IndexError, RecursionError, sqlite3.Error) as exc:
        raise BackupError('Proposal history metadata failed closed verification') from exc
    finally:
        connection.row_factory = previous_factory


def _read_proposal_history_file(path, *, maximum):
    """Held ancestor FDs, regular/single-link bounded bytes and stable readback."""
    descriptor = _file_descriptor(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise BackupError('Proposal history file is not a bounded independent regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        pin = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_nlink)
        if len(data) != before.st_size or len(data) > maximum or pin(before) != pin(after):
            raise BackupError('Proposal history file changed during read')
        reopened = _file_descriptor(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if pin(os.fstat(reopened)) != pin(after):
                raise BackupError('Proposal history locator changed during read')
        finally:
            os.close(reopened)
        return data
    finally:
        os.close(descriptor)


def _check_proposal_history(connection, *, recorded_root=None, specs=None):
    """Original full saved history validation; no live qualification or dispatch."""
    from .proposal_requests import ProposalRequests, MAX_RECORD_BYTES
    from .service import Store
    verified_specs = _proposal_history_specs(connection)
    if specs is not None and specs != verified_specs:
        raise BackupError('Proposal history owners changed during backup')
    specs = verified_specs
    if not specs:
        return
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        root = Path(next(row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main')).parent
        facade = object.__new__(Store)  # Never initialize or migrate a historical DB.
        facade.root, facade.db_path = root, root / 'workbench.sqlite3'
        for relative, spec in specs.items():
            raw = _read_proposal_history_file(root / relative, maximum=spec['maximum'])
            if hashlib.sha256(raw).hexdigest() != spec['sha256']:
                raise BackupError('Owned proposal history fingerprint differs: ' + relative)

        class HistoricalRequests(ProposalRequests):
            def _read_path(self, path, limit=MAX_RECORD_BYTES):
                relative = _relative(Path(path).relative_to(self.store.root).as_posix())
                if _private_input(relative) and relative not in specs:
                    raise BackupError('Unowned private proposal history path')
                return _read_proposal_history_file(path, maximum=limit)

        proposals = HistoricalRequests(facade, configs={})
        for row in connection.execute('SELECT id FROM proposal_requests ORDER BY id'):
            proposals._load(connection, row['id'])
    except (ValueError, OSError, TypeError, KeyError, RecursionError, sqlite3.Error, RuntimeError) as exc:
        raise BackupError('Saved proposal history failed full historical verification') from exc
    finally:
        connection.row_factory = previous_factory


def _database_check(connection, *, verify_authority_files=True, recorded_root=None):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("SQLite foreign-key check failed")
    row = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    if not row or row[0] not in SUPPORTED_SCHEMAS:
        raise BackupError("Unsupported workbench database schema")
    _check_schema_code(connection, row[0])
    proposal_specs = _proposal_history_specs(connection)
    for table, columns in PATH_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not {"id", *columns}.issubset(actual):
            raise BackupError("Backup database is missing required path columns")
    if int(row[0]) >= 26:
        for table, required in SNAPSHOT_COLUMNS.items():
            actual = {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(actual):
                raise BackupError("Schema26 database is missing daily snapshot authority columns: " + table)
    if int(row[0]) >= 27:
        from .provider_loop_native_authority_schema import REQUIRED_COLUMNS as NATIVE_COLUMNS
        from .daily_revision_bindings import REQUIRED_COLUMNS as REVISION_COLUMNS
        from .daily_holdout_schema import REQUIRED_COLUMNS as HOLDOUT_COLUMNS
        for table, required in {**NATIVE_COLUMNS, **REVISION_COLUMNS, **HOLDOUT_COLUMNS}.items():
            actual = {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}
            if set(required) != actual:
                raise BackupError('Schema27 database has missing or extra immutable authority columns: ' + table)
    if int(row[0]) >= 28:
        from .real_daily_workspace_authority_schema import REQUIRED_COLUMNS as AUTHORITY_COLUMNS
        for table, required in AUTHORITY_COLUMNS.items():
            actual = {item[1] for item in connection.execute(f'PRAGMA table_info({table})')}
            if set(required) != actual:
                raise BackupError('Schema28 database has missing or extra real-daily authority columns: ' + table)
        if verify_authority_files:
            from .real_daily_workspace_authority import verify_workspace_metadata
            try:
                verify_workspace_metadata(connection)
            except (RuntimeError, ValueError, sqlite3.Error) as exc:
                raise BackupError('Schema28 real-daily authority history failed verification') from exc
    if int(row[0]) >= 29:
        from .real_daily_prefix_schema import REQUIRED_COLUMNS as PREFIX_COLUMNS
        for table, required in PREFIX_COLUMNS.items():
            actual = {item[1] for item in connection.execute(f'PRAGMA table_info({table})')}
            if set(required) != actual:
                raise BackupError('Schema29 main prefix has missing or extra columns: ' + table)
        if verify_authority_files:
            _check_main_history(connection, recorded_root=recorded_root)
    if verify_authority_files:
        _check_proposal_history(connection, recorded_root=recorded_root, specs=proposal_specs)
    return row[0]


def _workspace_inventory_policy(connection, *, reject_unowned=False):
    """Only DB-owned new prefixes and main attempts may enter the inventory."""
    schema = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
    if int(schema) < 29:
        return None
    from ..real_daily_main_task import is_main_task
    from ..real_daily_main_workflow import FILES, INPUTS
    prefixes = {}
    for row in connection.execute('SELECT i.id,o.status FROM rdw_normalization_intents i LEFT JOIN rdw_normalization_outcomes o ON o.intent_id=i.id'):
        prefixes[row[0]] = PREFIX_FILES | ({'failure.json'} if row[1] in {'failed', 'interrupted'} else set())
    attempts = set()
    for run_id, attempt_id, task in connection.execute('SELECT a.run_id,a.id,v.task FROM attempts a JOIN runs r ON r.id=a.run_id JOIN revisions v ON v.id=r.revision_id'):
        if is_main_task(json.loads(task)):
            attempts.add('runs/' + run_id + '/attempts/' + attempt_id)

    def allowed(relative):
        if relative == 'real_daily_prefixes':
            return True
        if relative.startswith('real_daily_prefixes/'):
            parts = relative.split('/')
            if parts[1] not in prefixes:
                if reject_unowned:
                    raise BackupError('Unowned main prefix asset found in backup: ' + relative)
                return False  # Unknown captures have no historical DB owner.
            if len(parts) == 2 or (len(parts) == 3 and parts[2] in prefixes[parts[1]]):
                return True
            raise BackupError('Untracked main prefix asset: ' + relative)
        for base in attempts:
            if relative == base:
                return True
            if not relative.startswith(base + '/'):
                continue
            name = relative[len(base) + 1:]
            if name in set(INPUTS.values()) | {'worker-job.json', 'worker.stdout.log', 'worker.stderr.log', 'output', 'exports'}:
                return True
            for container in ('output', 'exports'):
                if name == container + '/inputs' or (name.startswith(container + '/') and
                        name[len(container) + 1:] in FILES | {'manifest.json'}):
                    return True
            raise BackupError('Untracked main attempt asset: ' + relative)
        return True
    return allowed


def _check_main_history(connection, *, recorded_root=None):
    """Verify saved prefix bytes and original producer output, never compute.

    This runs only once all snapshot witnesses/assets exist; the intermediate
    SQLite copy deliberately performs the schema-only check.
    """
    from .real_daily_prefix_registration import assert_prefix_history, MAX_PREFIX_BYTES
    from .real_daily_source_handles import read_managed, parse_json
    from .real_daily_main_execution import verify_saved_main_result
    from .service import Store
    from ..storage import digest
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        state = assert_prefix_history(connection)
        root = Path(next(row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main')).parent
        historical = connection.execute("SELECT value FROM settings WHERE key='historical_workspace_roots'").fetchone()
        roots = json.loads(historical[0]) if historical else []
        facade = object.__new__(Store)  # No initialization, migration, or live access.
        facade.root, facade.db_path = root, root / 'workbench.sqlite3'
        facade._redacted_roots = sorted({str(root), str(recorded_root or root), *roots}, key=len, reverse=True)
        for registration in state['registrations'].values():
            for key, maximum in (('prefix', MAX_PREFIX_BYTES), ('receipt', 2 * 1024 * 1024), ('metadata', 2 * 1024 * 1024)):
                read_managed(root, registration[key + '_relative'], maximum=maximum,
                             expected_sha256=registration[key + '_sha256'])
            relative = state['intents'][registration['intent_id']]['output_relative'] + 'registration.json'
            raw, _ = read_managed(root, relative, maximum=2 * 1024 * 1024)
            if parse_json(raw) != registration:
                raise BackupError('Saved main prefix registration differs from immutable history')
        for outcome in state['outcomes'].values():
            if outcome['status'] not in {'failed', 'interrupted'}:
                continue
            relative = state['intents'][outcome['intent_id']]['output_relative'] + 'failure.json'
            try:
                descriptor = _file_descriptor(root / relative, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                continue  # A crash may persist its DB outcome before the file.
            else:
                os.close(descriptor)
            raw, _ = read_managed(root, relative, maximum=2 * 1024 * 1024)
            expected = {'schema_version': 1, 'intent_id': outcome['intent_id'],
                'error_code': outcome['error_code'], 'counters': outcome['counters'], 'automatic_retry_allowed': False}
            if parse_json(raw) != expected:
                raise BackupError('Saved normalization failure differs from immutable history')
        for row in connection.execute('SELECT r.* FROM runs r JOIN rdw_result_bindings b ON b.attempt_id=r.attempt_id'):
            verify_saved_main_result(facade, connection, row, recorded_root=recorded_root)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError('Schema29 main prefix/producer history failed verification') from exc
    finally:
        connection.row_factory = previous_factory


def _check_schema_code(connection, version):
    """Accept only the exact checked-in immutable audit triggers in schema27.

    Unknown, altered or missing triggers and every view are rejected. Earlier
    versions retain their original no-trigger backup restriction.
    """
    actual = list(connection.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('trigger','view')"))
    if int(version) < 27:
        if actual:
            raise BackupError('Backup database may not contain triggers or views')
        return
    from .db import verify_audit_triggers
    try:
        verify_audit_triggers(connection, version=int(version))
    except RuntimeError as exc:
        raise BackupError(str(exc)) from exc


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
            with closing(_open_database(home / "workbench.sqlite3")) as source:
                _database_check(source, verify_authority_files=False)
                policy = _workspace_inventory_policy(source)
                private_specs = _proposal_history_specs(source)
            initial, directories = _inventory(home, exclude=EXCLUDED, filter_private=True, policy=policy,
                                             private_specs=private_specs)
            _new_directory(out)
            created = True
            files_root = out / "files"
            files_root.mkdir(mode=0o700)
            for name in directories:
                (files_root / name).mkdir(parents=True, exist_ok=True)
            database = files_root / "workbench.sqlite3"
            with closing(_open_database(home / "workbench.sqlite3")) as source:
                # Source witnesses exist here; destination witnesses are copied
                # after SQLite's snapshot. Both complete views are verified.
                _database_check(source)
                if _proposal_history_specs(source) != private_specs:
                    raise BackupError('Proposal private owners changed before snapshot')
                with closing(sqlite3.connect(database)) as destination:
                    deadline = time.monotonic() + 60
                    def progress(status, remaining, total):
                        if time.monotonic() > deadline:
                            raise BackupError("SQLite snapshot exceeded the 60-second limit")
                    source.backup(destination, pages=256, progress=progress)
                    schema = _database_check(destination, verify_authority_files=False)
                    # SQLite backup is complete; release only the read-validation
                    # transaction. There are no pending business writes here.
                    destination.rollback()
                    destination.execute("PRAGMA journal_mode=DELETE")
            entries = {}
            for name in sorted(initial):
                if name != "workbench.sqlite3":
                    entries[name] = _copy(home / name, files_root / name,
                                         maximum=private_specs.get(name, {}).get('maximum', MAX_BYTES))
            with closing(_open_database(database)) as frozen:
                if _database_check(frozen, recorded_root=home) != schema:
                    raise BackupError('Frozen database schema changed during backup')
                if _proposal_history_specs(frozen) != private_specs:
                    raise BackupError('Frozen proposal private owners differ')
            # WAL/SHM are never copied. SQLite's backup API creates the complete database.
            entries["workbench.sqlite3"] = _copy(database)
            final, final_dirs = _inventory(home, exclude=EXCLUDED, filter_private=True, policy=policy,
                                          private_specs=private_specs)
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
                        "excluded_runtime_files": sorted(EXCLUDED),
                        "excluded_private_components": sorted(PRIVATE_COMPONENTS | PRIVATE_NAMES)}
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
        if name in EXCLUDED or name == ".restore-incomplete" or _private_input(name):
            raise BackupError("Runtime or incomplete-restoration directories are not allowed")
    if len(directories) != len(set(directories)):
        raise BackupError("Duplicate backup directories")
    total = 0
    for name, entry in entries.items():
        _relative(name)
        # Exact private ownership is proved from the frozen DB below, never
        # trusted merely because the manifest lists a plausible basename.
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
    with closing(_open_database(files_root / 'workbench.sqlite3')) as connection:
        _database_check(connection, verify_authority_files=False)
        policy = _workspace_inventory_policy(connection, reject_unowned=True)
        private_specs = _proposal_history_specs(connection)
    if not set(private_specs).issubset(entries):
        raise BackupError('Manifest omits mandatory owned proposal history')
    if any(_private_input(name) and name not in private_specs for name in entries):
        raise BackupError('Manifest includes unowned private asset')
    actual, actual_dirs = _inventory(files_root, reject_private=True, policy=policy,
                                   private_specs=private_specs)
    if set(actual) != set(entries) or actual_dirs != sorted(directories):
        raise BackupError("Backup file inventory does not match its manifest")
    for name, expected in entries.items():
        if _copy(files_root / name, maximum=private_specs.get(name, {}).get('maximum', MAX_BYTES)) != expected:
            raise BackupError(f"Backup checksum mismatch: {name}")
    with closing(_open_database(files_root / "workbench.sqlite3")) as connection:
        if _database_check(connection, recorded_root=Path(source_root)) != manifest["database_schema"]:
            raise BackupError("Database schema does not match backup manifest")
        if _proposal_history_specs(connection) != private_specs:
            raise BackupError('Proposal private owners changed during verification')
    return manifest


def _rebase_database(path, old_root, new_root):
    changed = 0
    with closing(_open_database(path, readonly=False)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _database_check(connection, recorded_root=old_root)
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
            # A restored queue is a recorded historical state, not permission to
            # spend the account's model quota. No original deadlines are reset.
            from .provider_recovery_schema import pause_dispatch
            previous_identity = connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()
            marker = {'source_workspace_identity': previous_identity[0] if previous_identity else None,
                      'restore_identity': uuid.uuid4().hex}
            connection.execute("INSERT INTO settings VALUES ('provider_restored_workspace',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(marker, sort_keys=True),))
            if previous_identity:
                connection.execute("UPDATE settings SET value=? WHERE key='provider_workspace_identity'", (uuid.uuid4().hex,))
            # Also mark legacy schemas before migration adds the proposal table.
            pause_dispatch(connection, 'restored_workspace')
            saved_version = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
            if int(saved_version) >= 28:
                from .real_daily_workspace_authority import rotate_restored_epoch
                rotate_restored_epoch(connection, new_root)
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
        with closing(_open_database(backup / 'files' / 'workbench.sqlite3')) as connection:
            private_specs = _proposal_history_specs(connection)
        for name, expected in manifest["files"].items():
            if _copy(backup / "files" / name, home / name,
                     maximum=private_specs.get(name, {}).get('maximum', MAX_BYTES)) != expected:
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
