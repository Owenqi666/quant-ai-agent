"""Additive, exact-engine observation identities for the synthetic daily grid.

An index is a conservative reuse detector, not provenance authentication. The
original datasets/revisions remain authoritative; a missing historical index is
never silently regenerated on an execution path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from ..evaluation import FIELDS, MAX_FILE_BYTES, MAX_PANEL_CELLS, load_market
from ..evidence import sha256
from ..storage import digest, json_text, read_json

CANONICAL_VERSION = 'daily-synthetic-ohlcv-float64-rows-v1'
MAX_SCOPE_DATASETS = 1000
SCHEMA = """
CREATE TABLE observation_identities(dataset_id TEXT PRIMARY KEY REFERENCES datasets(id),canonical_version TEXT NOT NULL,market_sha256 TEXT NOT NULL,registered_digest TEXT NOT NULL,metadata_digest TEXT NOT NULL,panel_sha256 TEXT NOT NULL,row_count INTEGER NOT NULL CHECK(row_count>0),index_digest TEXT NOT NULL,origin TEXT NOT NULL CHECK(origin IN ('legacy','created')),created_at TEXT NOT NULL,identity_digest TEXT NOT NULL);
CREATE TABLE observation_rows(dataset_id TEXT NOT NULL REFERENCES observation_identities(dataset_id),date TEXT NOT NULL,asset TEXT NOT NULL,row_sha256 TEXT NOT NULL,PRIMARY KEY(dataset_id,date,asset));
CREATE INDEX observation_reuse ON observation_rows(row_sha256,date,asset,dataset_id);
CREATE TABLE observation_revision_bindings(revision_id TEXT PRIMARY KEY REFERENCES revisions(id),dataset_id TEXT NOT NULL REFERENCES observation_identities(dataset_id),identity_digest TEXT NOT NULL,registered_digest TEXT NOT NULL,task_digest TEXT NOT NULL,created_at TEXT NOT NULL,binding_digest TEXT NOT NULL);
CREATE TABLE observation_unresolved(dataset_id TEXT PRIMARY KEY REFERENCES datasets(id),registered_digest TEXT NOT NULL,market_sha256 TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL);
"""


def _error(message):
    from .service import ServiceError
    return ServiceError(message, 409)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _record_digest(row, field):
    return digest({key: value for key, value in dict(row).items() if key != field})


def _registered(dataset):
    try:
        for name in ('data_path', 'metadata_path'):
            if not 0 < Path(dataset[name]).stat().st_size <= MAX_FILE_BYTES:
                raise ValueError('Registered input exceeds the original loader file limit')
        metadata = json.loads(dataset['metadata'])
        checksum = sha256(dataset['data_path'])
        if (read_json(dataset['metadata_path']) != metadata
                or checksum != metadata['market_sha256']
                or digest({'data': checksum, 'metadata': metadata}) != dataset['sha256']):
            raise ValueError('Registered input differs')
        return metadata
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise _error('Observation identity cannot verify the registered synthetic daily input') from exc


def normalized_rows(dataset):
    """Use exactly the float64 values parsed by the existing execution loader.

    No tolerance, rounding, filling, key aliasing, or source-namespace exemption.
    Asset ordering within an accepted session is canonicalized by its exact key.
    Derived returns are omitted: cropping changes their first-row history even
    when the original OHLCV observation was already exposed.
    """
    metadata = _registered(dataset)
    panels = load_market(dataset['data_path'], dataset['metadata_path'])
    if _registered(dataset) != metadata:
        raise _error('Observation inputs changed while normalization was running')
    assets = sorted(metadata['universe'])
    fields = [panels[field].reindex(columns=assets).to_numpy() for field in FIELDS]
    for day_index, date in enumerate(metadata['calendar_dates']):
        for asset_index, asset in enumerate(assets):
            values = [float(panel[day_index, asset_index]).hex() for panel in fields]
            fingerprint = digest({'version': CANONICAL_VERSION, 'date': date,
                                  'asset': asset, 'fields': list(FIELDS), 'values': values})
            yield {'date': date, 'asset': asset, 'row_sha256': fingerprint}


def _hashers():
    return (hashlib.sha256((CANONICAL_VERSION + ':panel\n').encode()),
            hashlib.sha256((CANONICAL_VERSION + ':index\n').encode()))


def _update(panel, index, row):
    panel.update((row['row_sha256'] + '\n').encode('ascii'))
    index.update((json_text({key: row[key] for key in ('date', 'asset', 'row_sha256')}) + '\n').encode())


def _build(connection, dataset, *, origin):
    metadata = _registered(dataset)
    panel, index = _hashers()
    # DDL foreign keys need the identity first; the encompassing Store/migration
    # transaction prevents any observer from seeing this temporary empty index.
    count = len(metadata['calendar_dates']) * len(metadata['universe'])
    identity = {'dataset_id': dataset['id'], 'canonical_version': CANONICAL_VERSION,
                'market_sha256': metadata['market_sha256'], 'registered_digest': dataset['sha256'],
                'metadata_digest': digest(metadata), 'panel_sha256': 'pending',
                'row_count': count, 'index_digest': 'pending', 'origin': origin,
                'created_at': _now(), 'identity_digest': 'pending'}
    connection.execute('INSERT INTO observation_identities VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(identity.values()))
    actual = 0
    for row in normalized_rows(dataset):
        _update(panel, index, row)
        connection.execute('INSERT INTO observation_rows VALUES (?,?,?,?)',
                           (dataset['id'], row['date'], row['asset'], row['row_sha256']))
        actual += 1
    if actual != count:
        raise _error('Observation normalization did not cover the declared grid')
    identity.update(panel_sha256=panel.hexdigest(), index_digest=index.hexdigest())
    identity['identity_digest'] = _record_digest(identity, 'identity_digest')
    connection.execute('UPDATE observation_identities SET panel_sha256=?,index_digest=?,identity_digest=? WHERE dataset_id=?',
                       (identity['panel_sha256'], identity['index_digest'], identity['identity_digest'], dataset['id']))
    return identity


def _verify_index(connection, dataset):
    row = connection.execute('SELECT * FROM observation_identities WHERE dataset_id=?', (dataset['id'],)).fetchone()
    if row is None:
        raise _error('Historical observation identity is missing for dataset ' + dataset['id'])
    identity = dict(row)
    metadata = _registered(dataset)
    if (identity['canonical_version'] != CANONICAL_VERSION
            or identity['registered_digest'] != dataset['sha256']
            or identity['market_sha256'] != metadata['market_sha256']
            or identity['metadata_digest'] != digest(metadata)
            or _record_digest(identity, 'identity_digest') != identity['identity_digest']):
        raise _error('Observation identity binding changed for dataset ' + dataset['id'])
    panel, index = _hashers()
    count, previous = 0, None
    dates, assets = set(metadata['calendar_dates']), set(metadata['universe'])
    expected_count = len(dates) * len(assets)
    if not 0 < expected_count <= MAX_PANEL_CELLS:
        raise _error('Observation identity exceeds the original 2,000,000 grid-cell limit')
    for row in connection.execute('SELECT date,asset,row_sha256 FROM observation_rows WHERE dataset_id=? ORDER BY date,asset', (dataset['id'],)):
        key = (row['date'], row['asset'])
        if (key == previous or row['date'] not in dates or row['asset'] not in assets
                or not re.fullmatch(r'[0-9a-f]{64}', row['row_sha256'])):
            raise _error('Observation row index is malformed for dataset ' + dataset['id'])
        _update(panel, index, row)
        count += 1
        if count > expected_count:
            raise _error('Observation row index exceeds its registered grid')
        previous = key
    if (count != identity['row_count'] or count != len(dates) * len(assets)
            or panel.hexdigest() != identity['panel_sha256'] or index.hexdigest() != identity['index_digest']):
        raise _error('Observation row index is missing or changed for dataset ' + dataset['id'])
    return identity


def _verify_unanchored_values(connection, dataset):
    """Before the first revision anchor, verify the index against actual input.

    An unused legacy dataset has no independent revision anchor yet. Editing
    both its index and its self-digest cannot grant first-use authorization.
    """
    saved = iter(connection.execute('SELECT date,asset,row_sha256 FROM observation_rows WHERE dataset_id=? ORDER BY date,asset', (dataset['id'],)))
    for actual in normalized_rows(dataset):
        current = next(saved, None)
        if current is None or dict(current) != actual:
            raise _error('Unanchored observation index differs from the actual engine input')
    if next(saved, None) is not None:
        raise _error('Unanchored observation index has extra rows')


def _bind(connection, revision, dataset, identity):
    row = {'revision_id': revision['id'], 'dataset_id': dataset['id'],
           'identity_digest': identity['identity_digest'], 'registered_digest': dataset['sha256'],
           'task_digest': revision['digest'], 'created_at': _now()}
    row['binding_digest'] = _record_digest(row, 'binding_digest')
    connection.execute('INSERT INTO observation_revision_bindings VALUES (?,?,?,?,?,?,?)', tuple(row.values()))


def assert_scope(connection, dataset, *, ignore_revision_id=None, allow_new=False):
    """Verify indexes and independent revision anchors, streaming old authority.

    Missing historical data can conceal reuse. It conservatively blocks the
    scope even when the damaged index cannot demonstrate an actual overlap.
    """
    unresolved = connection.execute('SELECT dataset_id FROM observation_unresolved ORDER BY dataset_id LIMIT 1').fetchone()
    if unresolved:
        raise _error('Historical observation identity is unresolved for dataset ' + unresolved['dataset_id'])
    seen, identities = set(), {}
    rows = connection.execute('''SELECT v.id,v.digest,v.task,r.dataset_id FROM revisions v
        JOIN researches r ON r.id=v.research_id ORDER BY v.created_at,v.id''')
    for revision in rows:
        if revision['id'] == ignore_revision_id:
            continue
        identity = identities.get(revision['dataset_id'])
        if identity is None:
            if len(seen) >= MAX_SCOPE_DATASETS:
                raise _error('Observation history exceeds the 1000-dataset verification bound')
            historical = connection.execute('SELECT * FROM datasets WHERE id=?', (revision['dataset_id'],)).fetchone()
            if historical is None:
                raise _error('Historical observation dataset is missing')
            identity = _verify_index(connection, dict(historical))
            identities[revision['dataset_id']] = identity
            seen.add(revision['dataset_id'])
        binding = connection.execute('SELECT * FROM observation_revision_bindings WHERE revision_id=?', (revision['id'],)).fetchone()
        try:
            valid = (binding is not None and binding['dataset_id'] == revision['dataset_id']
                     and binding['identity_digest'] == identity['identity_digest']
                     and binding['registered_digest'] == identity['registered_digest']
                     and binding['task_digest'] == revision['digest']
                     and digest(json.loads(revision['task'])) == revision['digest']
                     and _record_digest(binding, 'binding_digest') == binding['binding_digest'])
        except (ValueError, TypeError, KeyError, RecursionError):
            valid = False
        if not valid:
            raise _error('Historical observation revision binding is missing or changed for revision ' + revision['id'])
    if dataset['id'] in identities:
        return identities[dataset['id']]
    if len(seen) >= MAX_SCOPE_DATASETS:
        raise _error('Observation history would exceed the 1000-dataset verification bound')
    if allow_new and connection.execute('SELECT 1 FROM observation_identities WHERE dataset_id=?', (dataset['id'],)).fetchone() is None:
        if connection.execute('SELECT 1 FROM observation_rows WHERE dataset_id=? LIMIT 1', (dataset['id'],)).fetchone():
            raise _error('Observation identity is missing while orphan index rows remain')
        return None
    identity = _verify_index(connection, dataset)
    _verify_unanchored_values(connection, dataset)
    return identity


def register_revision(connection, dataset, revision):
    """Create an index only for a genuinely new, never-declared dataset."""
    existing = connection.execute('SELECT dataset_id FROM observation_identities WHERE dataset_id=?', (dataset['id'],)).fetchone()
    if existing is None:
        history = connection.execute('''SELECT v.id FROM revisions v JOIN researches r ON r.id=v.research_id
            WHERE r.dataset_id=? AND v.id<>? LIMIT 1''', (dataset['id'], revision['id'])).fetchone()
        orphan = connection.execute('SELECT 1 FROM observation_rows WHERE dataset_id=? LIMIT 1', (dataset['id'],)).fetchone()
        if history or orphan:
            raise _error('Historical observation identity is missing; no execution-time repair is permitted')
        _build(connection, dataset, origin='created')
    identity = assert_scope(connection, dataset, ignore_revision_id=revision['id'])
    _bind(connection, revision, dataset, identity)


def related_dataset_ids(connection, dataset_id):
    rows = connection.execute('''SELECT DISTINCT old.dataset_id AS dataset_id FROM observation_rows current
        JOIN observation_rows old ON old.row_sha256=current.row_sha256
        AND old.date=current.date AND old.asset=current.asset WHERE current.dataset_id=?
        UNION SELECT ? ORDER BY dataset_id LIMIT ?''', (dataset_id, dataset_id, MAX_SCOPE_DATASETS + 1))
    values = [row['dataset_id'] for row in rows]
    if len(values) > MAX_SCOPE_DATASETS:
        raise _error('Observation reuse exceeds the 1000-dataset verification bound')
    return values


def overlap_reason(connection, dataset, bounds, *, ignore_revision_id=None):
    if connection.execute('SELECT 1 FROM observation_identities WHERE dataset_id=?', (dataset['id'],)).fetchone() is None:
        # Read-only preflight for a never-declared newly registered dataset. Its
        # eventual Store transaction builds/fences the persistent index itself.
        for current in normalized_rows(dataset):
            for interval, start, end, phrase in (
                (bounds['development'], 'test_start', 'test_end', 'Development observations overlap a reserved test interval'),
                (bounds['test'], 'development_start', 'development_end', 'Proposed test observations overlap previously declared development history')):
                if not interval['start'] <= current['date'] <= interval['end']:
                    continue
                row = connection.execute(f'''SELECT b.revision_id,b.{start} AS start,b.{end} AS end
                    FROM observation_rows old JOIN research_guard_boundaries b ON b.dataset_id=old.dataset_id
                    WHERE old.row_sha256=? AND old.date=? AND old.asset=? AND old.date BETWEEN b.{start} AND b.{end}
                    AND (? IS NULL OR b.revision_id<>?) ORDER BY b.created_at,b.revision_id LIMIT 1''',
                    (current['row_sha256'], current['date'], current['asset'], ignore_revision_id, ignore_revision_id)).fetchone()
                if row:
                    return (f"{phrase} {row['start']}..{row['end']} from revision {row['revision_id']} "
                            f"(exact reused row {current['date']} / {current['asset']})")
        return None
    for kind, current_range, old_start, old_end, phrase in (
        ('development', bounds['development'], 'test_start', 'test_end', 'Development observations overlap a reserved test interval'),
        ('test', bounds['test'], 'development_start', 'development_end', 'Proposed test observations overlap previously declared development history')):
        row = connection.execute(f'''SELECT b.revision_id,b.{old_start} AS start,b.{old_end} AS end,
            current.date,current.asset FROM observation_rows current
            JOIN observation_rows old ON old.row_sha256=current.row_sha256 AND old.date=current.date AND old.asset=current.asset
            JOIN research_guard_boundaries b ON b.dataset_id=old.dataset_id
            WHERE current.dataset_id=? AND current.date BETWEEN ? AND ?
            AND current.date BETWEEN b.{old_start} AND b.{old_end}
            AND (? IS NULL OR b.revision_id<>?)
            ORDER BY b.created_at,b.revision_id,current.date,current.asset LIMIT 1''',
            (dataset['id'], current_range['start'], current_range['end'], ignore_revision_id, ignore_revision_id)).fetchone()
        if row:
            return (f"{phrase} {row['start']}..{row['end']} from revision {row['revision_id']} "
                    f"(exact reused row {row['date']} / {row['asset']})")
    return None


def seed_history(connection):
    """One additive migration, with a savepoint per dataset and no file writes."""
    datasets = connection.execute('SELECT * FROM datasets ORDER BY id')
    seeded, bound = 0, 0
    for source in datasets:
        dataset = dict(source)
        if (connection.execute('SELECT 1 FROM observation_identities WHERE dataset_id=?', (dataset['id'],)).fetchone()
                or connection.execute('SELECT 1 FROM observation_unresolved WHERE dataset_id=?', (dataset['id'],)).fetchone()):
            continue
        connection.execute('SAVEPOINT observation_seed')
        dataset_bound = 0
        try:
            identity = _build(connection, dataset, origin='legacy')
            for revision in connection.execute('''SELECT v.* FROM revisions v JOIN researches r ON r.id=v.research_id
                WHERE r.dataset_id=? ORDER BY v.created_at,v.id''', (dataset['id'],)):
                _bind(connection, dict(revision), dataset, identity)
                dataset_bound += 1
            connection.execute('RELEASE observation_seed')
            seeded += 1
            bound += dataset_bound
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            connection.execute('ROLLBACK TO observation_seed')
            connection.execute('RELEASE observation_seed')
            try:
                fingerprint = json.loads(dataset['metadata']).get('market_sha256')
            except (ValueError, TypeError, AttributeError):
                fingerprint = None
            connection.execute('INSERT INTO observation_unresolved VALUES (?,?,?,?,?)',
                (dataset['id'], dataset['sha256'], fingerprint, 'Legacy registered input could not be normalized by the exact synthetic daily loader', _now()))
    return {'seeded_datasets': seeded, 'bound_revisions': bound,
            'unresolved_dataset_ids': [row['dataset_id'] for row in connection.execute('SELECT dataset_id FROM observation_unresolved ORDER BY dataset_id')]}
