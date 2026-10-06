"""Append-only temporal reservations tied to market content, across researches.

The worker computes all history through validation.end, not only the named
train and validation slices. Reservation checks therefore use that entire
prefix. These are conservative declarations, not claims that the person has
never viewed the underlying data.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import re
import uuid

from .db import connect
from .research_guard_schema import GuardCheck, GuardPage, GuardStatus
from ..evidence import sha256
from ..evaluation import validate_config
from ..storage import digest, json_text, read_json
from . import observation_identity

SCHEMA = """
CREATE TABLE research_guard_data(data_sha256 TEXT PRIMARY KEY,created_at TEXT NOT NULL);
CREATE TABLE research_guard_boundaries(revision_id TEXT PRIMARY KEY REFERENCES revisions(id),research_id TEXT NOT NULL REFERENCES researches(id),dataset_id TEXT NOT NULL REFERENCES datasets(id),data_sha256 TEXT NOT NULL REFERENCES research_guard_data(data_sha256),task_digest TEXT NOT NULL,registered_digest TEXT NOT NULL,metadata_digest TEXT NOT NULL,test_start TEXT NOT NULL,test_end TEXT NOT NULL,development_start TEXT NOT NULL,development_end TEXT NOT NULL,origin TEXT NOT NULL CHECK(origin IN ('legacy','created')),created_at TEXT NOT NULL);
CREATE INDEX research_guard_content ON research_guard_boundaries(data_sha256,revision_id);
CREATE TABLE research_guard_unresolved(revision_id TEXT PRIMARY KEY REFERENCES revisions(id),dataset_id TEXT NOT NULL REFERENCES datasets(id),data_sha256 TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE research_guard_events(id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES research_guard_boundaries(revision_id),run_id TEXT REFERENCES runs(id),kind TEXT NOT NULL CHECK(kind IN ('submitted','started','retried')),created_at TEXT NOT NULL);
CREATE INDEX research_guard_events_revision ON research_guard_events(revision_id,kind);
"""


def _error(message, status=409):
    from .service import ServiceError
    return ServiceError(message, status)


def _metadata(dataset, *, verify_files):
    try:
        metadata = json.loads(dataset['metadata'])
        fingerprint = metadata['market_sha256']
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
            raise ValueError('missing market content digest')
        if verify_files:
            stored = read_json(dataset['metadata_path'])
            actual = sha256(dataset['data_path'])
            if (stored != metadata or actual != fingerprint
                    or digest({'data': actual, 'metadata': metadata}) != dataset['sha256']):
                raise ValueError('registered dataset contents changed')
        return fingerprint, metadata
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _error('Temporal guard cannot verify the registered data content') from exc


def _bounds(task, metadata):
    try:
        config = validate_config(task['evaluation'], metadata['calendar_dates'])
        return {'test': config['splits']['test'],
                'development': {'start': metadata['calendar_dates'][0],
                                'end': config['splits']['validation']['end']}}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise _error('Temporal guard cannot resolve the development and reserved ranges') from exc


def _overlap(first, last, other_first, other_last):
    start, end = max(first, other_first), min(last, other_last)
    return {'start': start, 'end': end} if start <= end else None


def _registry_reason(connection, data_sha256, *, ignore_revision_id=None):
    """Stream original revisions to detect missing or altered reservations.

    Checking only the submitted revision allows deletion of an older boundary
    to erase its test range. The immutable business revisions remain the
    authority for coverage, even across re-registered dataset identities.
    Do not build an unbounded task list: SQLite yields one bounded task at a
    time, and the first integrity discrepancy refuses the content scope.
    """
    rows = connection.execute('''SELECT v.id,v.research_id,v.task,v.digest,
        r.dataset_id,d.metadata,d.sha256 AS registered_digest,
        b.revision_id AS bound_revision_id,b.research_id AS bound_research_id,
        b.dataset_id AS bound_dataset_id,b.data_sha256 AS bound_data_sha256,
        b.task_digest,b.registered_digest AS bound_registered_digest,b.metadata_digest,
        b.test_start,b.test_end,b.development_start,b.development_end
        FROM revisions v JOIN researches r ON r.id=v.research_id
        JOIN datasets d ON d.id=r.dataset_id
        LEFT JOIN research_guard_boundaries b ON b.revision_id=v.id
        WHERE (CASE WHEN json_valid(d.metadata) THEN json_extract(d.metadata,'$.market_sha256') ELSE NULL END)=?
           OR b.data_sha256=?
        ORDER BY v.created_at,v.id''', (data_sha256, data_sha256))
    for row in rows:
        if row['id'] == ignore_revision_id:
            continue
        try:
            metadata = json.loads(row['metadata'])
            task = json.loads(row['task'])
            bounds = _bounds(task, metadata)
            if (row['bound_revision_id'] != row['id'] or row['bound_research_id'] != row['research_id']
                    or row['bound_dataset_id'] != row['dataset_id']
                    or row['bound_data_sha256'] != data_sha256
                    or metadata.get('market_sha256') != data_sha256
                    or row['task_digest'] != row['digest'] or digest(task) != row['digest']
                    or row['bound_registered_digest'] != row['registered_digest']
                    or row['metadata_digest'] != digest(metadata)
                    or row['test_start'] != bounds['test']['start'] or row['test_end'] != bounds['test']['end']
                    or row['development_start'] != bounds['development']['start']
                    or row['development_end'] != bounds['development']['end']):
                raise ValueError('Boundary binding differs')
        except (ValueError, TypeError, KeyError, AttributeError):
            return f"Historical temporal registry is missing or changed for revision {row['id']}"
    return None


def _check(connection, data_sha256, bounds, *, ignore_revision_id=None):
    unresolved = connection.execute('SELECT revision_id FROM research_guard_unresolved WHERE data_sha256=? OR data_sha256 IS NULL ORDER BY revision_id LIMIT 1', (data_sha256,)).fetchone()
    if unresolved:
        return f"Historical temporal boundary is unresolved for revision {unresolved['revision_id']}"
    registry_reason = _registry_reason(connection, data_sha256, ignore_revision_id=ignore_revision_id)
    if registry_reason:
        return registry_reason
    development, test = bounds['development'], bounds['test']
    row = connection.execute('SELECT revision_id,test_start,test_end FROM research_guard_boundaries WHERE data_sha256=? AND test_start<=? AND test_end>=? ORDER BY created_at,revision_id LIMIT 1',
        (data_sha256, development['end'], development['start'])).fetchone()
    if row:
        return ('Development history overlaps a reserved test interval '
                f"{row['test_start']}..{row['test_end']} from revision {row['revision_id']}")
    row = connection.execute('SELECT revision_id,development_start,development_end FROM research_guard_boundaries WHERE data_sha256=? AND development_start<=? AND development_end>=? ORDER BY created_at,revision_id LIMIT 1',
        (data_sha256, test['end'], test['start'])).fetchone()
    if row:
        return ('Proposed test interval overlaps previously declared development history '
                f"{row['development_start']}..{row['development_end']} from revision {row['revision_id']}")
    return None


def _observation_registry_reason(connection, dataset, *, ignore_revision_id=None, allow_new=False):
    try:
        observation_identity.assert_scope(connection, dataset, ignore_revision_id=ignore_revision_id, allow_new=allow_new)
        # The old business revisions/boundaries remain authoritative. A deleted
        # byte reservation must not be hidden by a new normalized index either.
        sources = connection.execute('''SELECT DISTINCT d.metadata FROM revisions v
            JOIN researches r ON r.id=v.research_id JOIN datasets d ON d.id=r.dataset_id
            WHERE (? IS NULL OR v.id<>?)''', (ignore_revision_id, ignore_revision_id))
        for source in sources:
            checksum = json.loads(source['metadata'])['market_sha256']
            reason = _registry_reason(connection, checksum, ignore_revision_id=ignore_revision_id)
            if reason:
                return reason
            unresolved = connection.execute('SELECT revision_id FROM research_guard_unresolved WHERE data_sha256=? OR data_sha256 IS NULL LIMIT 1', (checksum,)).fetchone()
            if unresolved:
                return 'Historical temporal boundary is unresolved for revision ' + unresolved['revision_id']
        return None
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return str(exc) if isinstance(exc, ValueError) else 'Observation reuse protection could not verify historical inputs'


def _observation_reason(connection, dataset, bounds, *, ignore_revision_id=None, allow_new=False):
    reason = _observation_registry_reason(connection, dataset, ignore_revision_id=ignore_revision_id, allow_new=allow_new)
    if reason:
        return reason
    try:
        return observation_identity.overlap_reason(connection, dataset, bounds, ignore_revision_id=ignore_revision_id)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 'Observation reuse protection could not verify the candidate input'


def _insert_boundary(connection, dataset, revision, *, origin, verify_files):
    data_sha256, metadata = _metadata(dataset, verify_files=verify_files)
    task = json.loads(revision['task']) if isinstance(revision['task'], str) else revision['task']
    if digest(task) != revision['digest']:
        raise _error('Temporal guard found a changed revision task digest')
    bounds = _bounds(task, metadata)
    created = datetime.now(timezone.utc).isoformat()
    connection.execute('INSERT OR IGNORE INTO research_guard_data VALUES (?,?)', (data_sha256, created))
    connection.execute('INSERT INTO research_guard_boundaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (revision['id'], revision['research_id'], dataset['id'], data_sha256, revision['digest'],
         dataset['sha256'], digest(metadata), bounds['test']['start'], bounds['test']['end'],
         bounds['development']['start'], bounds['development']['end'], origin, created))
    return data_sha256, bounds


def register_revision(connection, dataset, revision):
    """Called inside Store's existing mutation transaction, after revision insert.

Rollback removes both revision and boundary if any reservation is violated.
"""
    data_sha256, metadata = _metadata(dataset, verify_files=True)
    task = json.loads(revision['task']) if isinstance(revision['task'], str) else revision['task']
    reason = _check(connection, data_sha256, _bounds(task, metadata), ignore_revision_id=revision['id'])
    if reason:
        raise _error(reason)
    observation_identity.register_revision(connection, dataset, revision)
    reason = _observation_reason(connection, dataset, _bounds(task, metadata), ignore_revision_id=revision['id'])
    if reason:
        raise _error(reason)
    _insert_boundary(connection, dataset, revision, origin='created', verify_files=False)


def assert_revision_safe(connection, dataset, revision):
    """Check a queued/retried/current revision without declaring a new boundary."""
    data_sha256, metadata = _metadata(dataset, verify_files=True)
    row = connection.execute('SELECT * FROM research_guard_boundaries WHERE revision_id=?',
                             (revision['id'],)).fetchone()
    task = json.loads(revision['task']) if isinstance(revision['task'], str) else revision['task']
    bounds = _bounds(task, metadata)
    if (row is None or row['data_sha256'] != data_sha256 or row['task_digest'] != revision['digest']
            or digest(task) != revision['digest'] or row['dataset_id'] != dataset['id']
            or row['research_id'] != revision['research_id'] or row['registered_digest'] != dataset['sha256']
            or row['metadata_digest'] != digest(metadata)
            or row['test_start'] != bounds['test']['start'] or row['test_end'] != bounds['test']['end']
            or row['development_start'] != bounds['development']['start']
            or row['development_end'] != bounds['development']['end']):
        raise _error('Temporal guard boundary is missing or does not match its immutable revision and dataset')
    reason = _check(connection, data_sha256, bounds)
    if reason:
        raise _error(reason)
    reason = _observation_reason(connection, dataset, bounds)
    if reason:
        raise _error(reason)


def record_event(connection, revision_id, run_id, kind):
    connection.execute('INSERT INTO research_guard_events VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), revision_id, run_id, kind, datetime.now(timezone.utc).isoformat()))


def seed_history(connection):
    """Add legacy declarations; never update business rows, files or receipts.

No claim of pristine history is manufactured. Conflicting declarations are
all retained and visible. Malformed old inputs stay unguarded and are refused
when submitted/claimed, rather than making database migration destructive.
"""
    rows = connection.execute('SELECT v.* FROM revisions v LEFT JOIN research_guard_boundaries b ON b.revision_id=v.id LEFT JOIN research_guard_unresolved u ON u.revision_id=v.id WHERE b.revision_id IS NULL AND u.revision_id IS NULL ORDER BY v.created_at,v.id').fetchall()
    seeded = 0
    for row in rows:
        revision = dict(row)
        research = connection.execute('SELECT * FROM researches WHERE id=?', (revision['research_id'],)).fetchone()
        dataset = connection.execute('SELECT * FROM datasets WHERE id=?', (research['dataset_id'],)).fetchone()
        try:
            _insert_boundary(connection, dict(dataset), revision, origin='legacy', verify_files=False)
        except ValueError as exc:
            try:
                data_sha256, _ = _metadata(dict(dataset), verify_files=False)
            except ValueError:
                # Retain a content identity even when old metadata is damaged,
                # if the old market file is still available for a digest read.
                try:
                    data_sha256 = sha256(dataset['data_path'])
                except (OSError, TypeError):
                    data_sha256 = None
            connection.execute('INSERT INTO research_guard_unresolved VALUES (?,?,?,?,?)',
                (revision['id'], dataset['id'], data_sha256, str(exc), datetime.now(timezone.utc).isoformat()))
            continue
        seeded += 1
    return {'seeded': seeded, 'unresolved_revision_ids': [r['revision_id'] for r in connection.execute('SELECT revision_id FROM research_guard_unresolved ORDER BY revision_id')]}


class ResearchGuard:
    def __init__(self, store):
        self.store = store

    def check(self, dataset_id, task):
        """Read-only preflight; Store mutation remains the atomic authority."""
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            dataset = self.store._one(connection, 'datasets', dataset_id)
            data_sha256, metadata = _metadata(dataset, verify_files=True)
            bounds = _bounds(task, metadata)
            reason = _check(connection, data_sha256, bounds)
            reason = reason or _observation_reason(connection, dataset, bounds, allow_new=True)
            return GuardCheck(data_sha256=data_sha256, allowed=reason is None,
                              reason=reason, **bounds).model_dump()

    def get(self, research_id):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._get(connection, research_id)

    def _get(self, connection, research_id):
        research = self.store._one(connection, 'researches', research_id)
        dataset = self.store._one(connection, 'datasets', research['dataset_id'])
        limitations = [
            'Reservations apply globally to identical market CSV bytes, including re-registration with different titles or metadata versions.',
            'Exact engine-equivalent full date/asset/OHLCV rows also retain reservations across accepted encodings, within-session asset ordering, cropping, appending and asset subsets. Conflict checks use the actual shared observation dates.',
            'Development exposure conservatively includes every calendar session from the first available row through validation.end, including trailing-window history.',
            'Declarations do not prove that a person has never viewed the data. Started run counts report recorded execution only.',
            'Final-test execution is not available. Asset renaming, changed OHLCV values, different-source ambiguity and all other information overlap require separately reviewed provenance; this synthetic daily reuse index does not authenticate market sources.',
            'Missing or damaged observation indexes bound to historical revisions conservatively block execution; they are never rebuilt as an execution-time authorization.',
        ]
        try:
            data_sha256, _ = _metadata(dataset, verify_files=True)
        except ValueError as exc:
            return GuardStatus(research_id=research_id, dataset_id=dataset['id'], data_sha256=None,
                status='unresolved', reason=str(exc), boundaries=[], conflicts=[], total_boundaries=0,
                omitted_boundaries=0, total_conflicts=0, omitted_conflicts=0, limitations=limitations).model_dump()
        observation_reason = _observation_registry_reason(connection, dataset)
        try:
            related = observation_identity.related_dataset_ids(connection, dataset['id']) if observation_reason is None else [dataset['id']]
        except ValueError as exc:
            related, observation_reason = [dataset['id']], str(exc)
        placeholders = ','.join('?' for _ in related)
        scope = f'(b.data_sha256=? OR b.dataset_id IN ({placeholders}))'
        values = (data_sha256, *related)
        total_boundaries = connection.execute('SELECT COUNT(*) FROM research_guard_boundaries b WHERE ' + scope, values).fetchone()[0]
        rows = list(connection.execute('SELECT b.*,(SELECT COUNT(*) FROM runs r WHERE r.revision_id=b.revision_id) AS submitted_runs,(SELECT COUNT(*) FROM runs r WHERE r.revision_id=b.revision_id AND r.attempt_count>0) AS started_runs FROM research_guard_boundaries b WHERE ' + scope + ' ORDER BY b.created_at,b.revision_id LIMIT 1000', values))
        conflict_join = f'''FROM research_guard_boundaries b JOIN research_guard_boundaries r
            ON b.development_start<=r.test_end AND b.development_end>=r.test_start
            WHERE {scope} AND (r.data_sha256=? OR r.dataset_id IN ({placeholders}))
            AND ((b.data_sha256=? AND r.data_sha256=?) OR EXISTS (
                SELECT 1 FROM observation_rows first JOIN observation_rows other
                ON first.row_sha256=other.row_sha256 AND first.date=other.date AND first.asset=other.asset
                JOIN observation_rows current
                ON current.row_sha256=first.row_sha256 AND current.date=first.date AND current.asset=first.asset
                WHERE first.dataset_id=b.dataset_id AND other.dataset_id=r.dataset_id
                AND current.dataset_id=?
                AND first.date BETWEEN b.development_start AND b.development_end
                AND first.date BETWEEN r.test_start AND r.test_end))'''
        conflict_values = (*values, *values, data_sha256, data_sha256, dataset['id'])
        total_conflicts = connection.execute('SELECT COUNT(*) ' + conflict_join, conflict_values).fetchone()[0]
        conflicts = [dict(r) for r in connection.execute('SELECT b.revision_id,r.revision_id AS reserved_by_revision_id,MAX(b.development_start,r.test_start) AS start,MIN(b.development_end,r.test_end) AS end ' + conflict_join + ' ORDER BY b.revision_id,r.revision_id LIMIT 1000', conflict_values)]
        conflicts = [{'revision_id': r['revision_id'], 'reserved_by_revision_id': r['reserved_by_revision_id'],
                      'overlap': {'start': r['start'], 'end': r['end']}} for r in conflicts]
        registry_reason = observation_reason or _registry_reason(connection, data_sha256)
        missing = registry_reason is not None
        missing = missing or connection.execute('SELECT 1 FROM research_guard_unresolved WHERE data_sha256=? OR data_sha256 IS NULL LIMIT 1', (data_sha256,)).fetchone() is not None
        status = 'unresolved' if missing else 'historical_conflict' if total_conflicts else 'protected'
        reason = (registry_reason or 'Some historical revisions have unresolved boundary inputs') if missing else ('Preserved historical development ranges overlap reserved test intervals' if total_conflicts else None)
        boundaries = [{'revision_id': r['revision_id'], 'research_id': r['research_id'], 'dataset_id': r['dataset_id'],
            'test': {'start': r['test_start'], 'end': r['test_end']},
            'development': {'start': r['development_start'], 'end': r['development_end']},
            'origin': r['origin'], 'task_digest': r['task_digest'], 'submitted_runs': r['submitted_runs'],
            'started_runs': r['started_runs']} for r in rows[:1000]]
        return GuardStatus(research_id=research_id, dataset_id=dataset['id'], data_sha256=data_sha256,
            status=status, reason=reason, boundaries=boundaries, conflicts=conflicts,
            total_boundaries=total_boundaries, omitted_boundaries=max(0, total_boundaries-1000),
            total_conflicts=total_conflicts, omitted_conflicts=max(0, total_conflicts-1000),
            limitations=limitations).model_dump()

    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise _error('Use a page size of 1..100 and nonnegative offset', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM researches').fetchone()[0]
            ids = connection.execute('SELECT id FROM researches ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (limit, offset))
            return GuardPage(items=[self._get(connection, row['id']) for row in ids],
                             total=total, limit=limit, offset=offset).model_dump()
