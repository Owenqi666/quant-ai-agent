"""Conservative declared asset/date protection in the existing workspace transaction.

Declarations are durable before the first numeric development read.  A match
across adapters means potential overlap, never authenticated source equivalence.
No loader, database, migration or file access runs at import time.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from ..daily_adapter import content_row_fingerprint, _json_input, _open_regular
from ..daily_contract import CANONICAL_VERSION, FIELDS, MAX_GRID_CELLS, MAX_RAW_BYTES, parse_contract
from ..storage import digest, json_text
from .daily_snapshot_schema import (DATA_KIND, SNAPSHOT_VERSION, declaration_digest,
                                    declaration_payload, development_index_digest,
                                    snapshot_registration_digest)

MAX_HISTORY = 1000
SCHEMA = """
CREATE TABLE daily_snapshot_guard_boundaries(
 registration_id TEXT PRIMARY KEY REFERENCES daily_snapshot_registrations(id),
 declaration_digest TEXT NOT NULL,scope_digest TEXT NOT NULL,
 development_start TEXT NOT NULL,development_end TEXT NOT NULL,test_start TEXT NOT NULL,test_end TEXT NOT NULL,
 declared_key_count INTEGER NOT NULL CHECK(declared_key_count>0),created_at TEXT NOT NULL,boundary_digest TEXT NOT NULL);
CREATE TABLE daily_snapshot_guard_keys(
 registration_id TEXT NOT NULL REFERENCES daily_snapshot_guard_boundaries(registration_id),
 date TEXT NOT NULL,asset TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('development','test')),
 PRIMARY KEY(registration_id,date,asset));
CREATE INDEX daily_snapshot_guard_key_lookup ON daily_snapshot_guard_keys(date,asset,role,registration_id);
CREATE TABLE daily_snapshot_guard_observations(
 registration_id TEXT NOT NULL REFERENCES daily_snapshot_guard_boundaries(registration_id),
 date TEXT NOT NULL,asset TEXT NOT NULL,row_sha256 TEXT NOT NULL,PRIMARY KEY(registration_id,date,asset));
CREATE TABLE daily_snapshot_guard_bindings(
 registration_id TEXT PRIMARY KEY REFERENCES daily_snapshot_guard_boundaries(registration_id),
 dataset_id TEXT NOT NULL REFERENCES datasets(id),registered_digest TEXT NOT NULL,declaration_digest TEXT NOT NULL,
 development_content_digest TEXT NOT NULL,development_index_digest TEXT NOT NULL,
 row_count INTEGER NOT NULL CHECK(row_count>0),created_at TEXT NOT NULL,binding_digest TEXT NOT NULL);
"""


def _error(message):
    from .service import ServiceError
    return ServiceError(message, 409)


def _record_digest(row, field):
    return digest({key: value for key, value in dict(row).items() if key != field})


def _contract(registration):
    value = dict(registration)
    try:
        if declaration_digest(value) != value['declaration_digest']:
            raise ValueError('business declaration digest changed')
        return parse_contract(declaration_payload(value)['contract_json'])
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise _error('Daily snapshot immutable declaration is missing or changed') from exc


def _owner(connection, registration):
    """Caller data is never its own business authority; resolve the saved intent."""
    current = connection.execute('SELECT * FROM daily_snapshot_registrations WHERE id=?',
                                 (registration['id'],)).fetchone()
    if current is None:
        raise _error('Daily snapshot business registration intent is missing')
    current = dict(current)
    _contract(current)
    if (declaration_digest(registration) != current['declaration_digest']
            or registration['declaration_digest'] != current['declaration_digest']):
        raise _error('Daily snapshot caller declaration differs from the saved intent')
    return current


def _keys(contract):
    d = contract.to_dict()
    final = d['evaluation']['splits']['test']
    for day in contract.calendar:
        role = 'development' if day <= contract.cutoff else ('test' if final['start'] <= day <= final['end'] else None)
        if role:
            for asset in contract.assets:
                yield {'date': day, 'asset': asset, 'role': role}


def _scope(contract):
    keys = list(_keys(contract))
    if not 0 < len(keys) <= MAX_GRID_CELLS:
        raise _error('Daily snapshot declaration exceeds its bounded grid')
    return keys, digest({'scope_version': 'daily-declared-asset-date-scope-v1', 'keys': keys})


def _boundary_fields(registration, contract):
    keys, scope_digest = _scope(contract)
    final = contract.to_dict()['evaluation']['splits']['test']
    return keys, {'registration_id': registration['id'], 'declaration_digest': registration['declaration_digest'],
                  'scope_digest': scope_digest, 'development_start': contract.development_calendar[0],
                  'development_end': contract.cutoff, 'test_start': final['start'], 'test_end': final['end'],
                  'declared_key_count': len(keys)}


def _verify_declared(connection, registration):
    contract = _contract(registration)
    keys, expected = _boundary_fields(registration, contract)
    row = connection.execute('SELECT * FROM daily_snapshot_guard_boundaries WHERE registration_id=?',
                             (registration['id'],)).fetchone()
    if row is None:
        raise _error('Daily snapshot historical boundary is missing; no runtime reconstruction is allowed')
    boundary = dict(row)
    if (any(boundary.get(key) != value for key, value in expected.items())
            or _record_digest(boundary, 'boundary_digest') != boundary['boundary_digest']):
        raise _error('Daily snapshot boundary differs from its independent business declaration')
    saved = connection.execute('SELECT date,asset,role FROM daily_snapshot_guard_keys WHERE registration_id=? ORDER BY date,asset',
                               (registration['id'],))
    for expected_key in keys:
        actual = next(saved, None)
        if actual is None or dict(actual) != expected_key:
            raise _error('Daily snapshot declared key index is missing or changed')
    if next(saved, None) is not None:
        raise _error('Daily snapshot declared key index contains extra keys')
    return contract, boundary


def _saved_dataset(connection, dataset):
    saved = connection.execute('SELECT * FROM datasets WHERE id=?', (dataset['id'],)).fetchone()
    if saved is None or any(saved[key] != dataset[key] for key in ('id','sha256','metadata','data_path','metadata_path')):
        raise _error('Daily snapshot caller dataset differs from the saved business dataset')
    return dict(saved)


def _prefix_identity(dataset, contract, registration):
    """Hash saved prefix bytes only.  Never parse CSV/final numeric data here."""
    try:
        metadata = json.loads(dataset['metadata']) if isinstance(dataset['metadata'], str) else dataset['metadata']
        actual_metadata, _ = _json_input(dataset['metadata_path'])
        path, stream, before = _open_regular(dataset['data_path'], MAX_RAW_BYTES)
        with stream:
            hasher = hashlib.sha256()
            size = 0
            while block := stream.read(64 * 1024):
                size += len(block)
                if size > MAX_RAW_BYTES:
                    raise ValueError('prefix exceeds byte limit')
                hasher.update(block)
            after = __import__('os').fstat(stream.fileno())
        observed = path.stat(follow_symlinks=False)
        for stat in (after, observed):
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                raise ValueError('prefix changed during byte verification')
        checksum = hasher.hexdigest()
        from .daily_snapshots import DailySnapshots
        expected_metadata = DailySnapshots._metadata(registration, contract, checksum, registration['development_content_digest'])
        if (digest(actual_metadata) != digest(metadata) or digest(metadata) != digest(expected_metadata) or metadata.get('data_kind') != DATA_KIND
                or metadata.get('version') != SNAPSHOT_VERSION
                or metadata.get('calendar_dates') != list(contract.development_calendar)
                or metadata.get('universe') != list(contract.assets)
                or metadata.get('market_sha256') != checksum
                or digest({'data': checksum, 'metadata': metadata}) != dataset['sha256']):
            raise ValueError('prefix binding differs')
        return metadata
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise _error('Daily snapshot frozen development dataset differs from its registered bytes') from exc


def _saved_observations(connection, registration, contract):
    rows = []
    expected_keys = ((day, asset) for day in contract.development_calendar for asset in contract.assets)
    saved = connection.execute('SELECT date,asset,row_sha256 FROM daily_snapshot_guard_observations WHERE registration_id=? ORDER BY date,asset',
                               (registration['id'],))
    for day, asset in expected_keys:
        row = next(saved, None)
        if row is None or row['date'] != day or row['asset'] != asset or re.fullmatch(r'[0-9a-f]{64}', row['row_sha256']) is None:
            raise _error('Daily snapshot development observation index is missing or changed')
        rows.append(dict(row))
    if next(saved, None) is not None:
        raise _error('Daily snapshot development index includes undeclared or final observations')
    content = digest({'version': CANONICAL_VERSION, 'rows': rows})
    index = digest({'canonical_version': CANONICAL_VERSION, 'observations': rows})
    if content != registration['development_content_digest'] or index != registration['development_index_digest']:
        raise _error('Daily snapshot development index differs from its independent business binding')
    return rows


def _verify_finalized(connection, dataset, registration, contract):
    dataset = _saved_dataset(connection, dataset)
    if (registration['registered_dataset_id'] != dataset['id']
            or snapshot_registration_digest(registration, dataset) != registration['registration_digest']):
        raise _error('Daily snapshot registered dataset identity differs from its business record')
    _prefix_identity(dataset, contract, registration)
    rows = _saved_observations(connection, registration, contract)
    binding = connection.execute('SELECT * FROM daily_snapshot_guard_bindings WHERE registration_id=?',
                                 (registration['id'],)).fetchone()
    if binding is None:
        raise _error('Daily snapshot independent development binding is missing')
    expected = {'registration_id': registration['id'], 'dataset_id': dataset['id'],
                'registered_digest': dataset['sha256'], 'declaration_digest': registration['declaration_digest'],
                'development_content_digest': registration['development_content_digest'],
                'development_index_digest': registration['development_index_digest'], 'row_count': len(rows)}
    if (any(binding[key] != value for key, value in expected.items())
            or _record_digest(binding, 'binding_digest') != binding['binding_digest']):
        raise _error('Daily snapshot development binding is missing or changed')


def _assert_unfinalized(connection, registration):
    if (registration['status'] == 'registered'
            or any(registration[field] is not None for field in ('registration_digest','development_content_digest','development_index_digest'))
            or connection.execute('SELECT 1 FROM daily_snapshot_guard_bindings WHERE registration_id=?', (registration['id'],)).fetchone()
            or connection.execute('SELECT 1 FROM daily_snapshot_guard_observations WHERE registration_id=? LIMIT 1', (registration['id'],)).fetchone()):
        raise _error('Unfinalized daily snapshot has unowned or missing development bindings')


def _history(connection, *, exclude=None):
    registrations = connection.execute('SELECT * FROM daily_snapshot_registrations ORDER BY created_at,id LIMIT ?',
                                       (MAX_HISTORY + 1,)).fetchall()
    if len(registrations) > MAX_HISTORY:
        raise _error('Daily snapshot history exceeds the 1000-registration verification bound')
    result = []
    for raw in registrations:
        registration = dict(raw)
        if registration['id'] == exclude:
            continue
        contract, boundary = _verify_declared(connection, registration)
        if registration['status'] not in {'reserved', 'prechecking', 'registered', 'failed', 'interrupted'}:
            raise _error('Daily snapshot historical registration state is unknown')
        if registration['registered_dataset_id'] is not None:
            raw_dataset = connection.execute('SELECT * FROM datasets WHERE id=?', (registration['registered_dataset_id'],)).fetchone()
            if raw_dataset is None:
                raise _error('Daily snapshot registered business dataset is missing')
            _verify_finalized(connection, dict(raw_dataset), registration, contract)
        else:
            _assert_unfinalized(connection, registration)
        result.append((registration, contract, boundary))
    # A missing independent owner is not hidden by merely dropping business rows.
    for table in ('daily_snapshot_guard_boundaries', 'daily_snapshot_guard_keys',
                  'daily_snapshot_guard_observations', 'daily_snapshot_guard_bindings', 'daily_snapshot_attempts'):
        orphan = connection.execute(f'''SELECT b.registration_id FROM {table} b
            LEFT JOIN daily_snapshot_registrations r ON r.id=b.registration_id WHERE r.id IS NULL LIMIT 1''').fetchone()
        if orphan:
            raise _error('Daily snapshot historical business declaration owner is missing')
    orphan_dataset = connection.execute('''SELECT d.id FROM datasets d
        LEFT JOIN daily_snapshot_registrations r ON r.registered_dataset_id=d.id
        WHERE json_valid(d.metadata) AND json_extract(d.metadata,'$.data_kind')=? AND r.id IS NULL LIMIT 1''', (DATA_KIND,)).fetchone()
    if orphan_dataset:
        raise _error('Daily snapshot typed dataset has no independent business registration')
    return result


def _dates(contract):
    final = contract.to_dict()['evaluation']['splits']['test']
    return {'development': set(contract.development_calendar),
            'test': {day for day in contract.calendar if final['start'] <= day <= final['end']}}


def _conflict(first_dates, first_assets, second_dates, second_assets, identity):
    assets = set(first_assets) & set(second_assets)
    if not assets:
        return None
    for current, previous in (('development', 'test'), ('test', 'development')):
        dates = first_dates[current] & second_dates[previous]
        if dates:
            return (f'Daily snapshot potential asset/date overlap: proposed {current} conflicts with '
                    f'previous {previous} declaration {identity} at {min(dates)} / {min(assets)}; '
                    'source and numeric equivalence are unverified')
    return None


def _legacy_history(connection, *, ignore_revision_id=None):
    """Verify anchored old authority without loading any additional final numbers."""
    rows = connection.execute('''SELECT v.id AS revision_id,v.task,r.dataset_id,d.metadata,d.sha256,d.data_path,d.metadata_path,d.title
        FROM revisions v JOIN researches r ON r.id=v.research_id JOIN datasets d ON d.id=r.dataset_id
        WHERE (? IS NULL OR v.id<>?) ORDER BY v.created_at,v.id LIMIT ?''',
        (ignore_revision_id, ignore_revision_id, MAX_HISTORY + 1)).fetchall()
    if len(rows) > MAX_HISTORY:
        raise _error('Daily snapshot bridge exceeds the 1000-legacy-revision verification bound')
    if not rows:
        return []
    from .daily_revision_bindings import is_typed_task, assert_revision, assert_history
    assert_history(connection)
    legacy = []
    for row in rows:
        if is_typed_task(row['task']):
            # A prefix-only typed revision has its own verified declaration.
            # Do not invent the absent final calendar/numbers for old guards.
            assert_revision(connection, row['revision_id'])
        else:
            if json.loads(row['metadata']).get('data_kind') == DATA_KIND:
                raise _error('Registered daily prefix has no verified typed revision binding')
            legacy.append(row)
    rows = legacy
    if not rows:
        return []
    from . import observation_identity, research_guard
    first = dict(rows[0]); first['id'] = first['dataset_id']
    observation_identity.assert_scope(connection, first, ignore_revision_id=ignore_revision_id)
    result, checked = [], set()
    for row in rows:
        metadata = json.loads(row['metadata'])
        checksum = metadata['market_sha256']
        if checksum not in checked:
            reason = research_guard._registry_reason(connection, checksum, ignore_revision_id=ignore_revision_id)
            unresolved = connection.execute('SELECT 1 FROM research_guard_unresolved WHERE data_sha256=? OR data_sha256 IS NULL LIMIT 1', (checksum,)).fetchone()
            if reason or unresolved:
                raise _error(reason or 'Historical temporal boundary is unresolved')
            checked.add(checksum)
        boundary = connection.execute('SELECT * FROM research_guard_boundaries WHERE revision_id=?', (row['revision_id'],)).fetchone()
        if boundary is None:
            raise _error('Historical legacy temporal boundary is missing')
        dates = {role: {day for day in metadata['calendar_dates'] if boundary[role+'_start'] <= day <= boundary[role+'_end']}
                 for role in ('development','test')}
        result.append((row['revision_id'], dates, metadata['universe']))
    return result


def _check_real_history(connection, dates, assets):
    """Additive literal-key bridge; never reads the new lane's market originals."""
    from ..real_daily_admission import check_real_overlap
    try:
        reason = check_real_overlap(connection, assets=list(assets),
                                    development_sessions=sorted(dates['development']),
                                    test_sessions=sorted(dates['test']))
        if reason:
            return reason
        from .real_daily_workspace_authority import check_main_real_overlap
        return check_main_real_overlap(connection, assets=list(assets),
                                       development_sessions=sorted(dates['development']),
                                       test_sessions=sorted(dates['test']))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, sqlite3.Error):
        raise _error('Real daily development history cannot verify its independent reservations') from None


def metadata_scope_history(connection, *, ignore_revision_id=None):
    """Verify declared old scopes without opening any market or prefix file.

    This is the admission bridge's literal asset/date authority, not a proof of
    numeric equivalence or source readiness. Existing execution guards retain
    their file-backed validation. Missing declarations fail closed even when
    every owner was removed.
    """
    from . import research_guard
    from .daily_revision_bindings import is_typed_task
    from .daily_snapshots import DailySnapshots
    from ..daily_task_v2 import execution_binding, parse_daily_task
    from .provider_catalog import catalog_from_daily_contract

    try:
        registrations = connection.execute(
            'SELECT * FROM daily_snapshot_registrations ORDER BY created_at,id LIMIT ?',
            (MAX_HISTORY + 1,)).fetchall()
        if len(registrations) > MAX_HISTORY:
            raise ValueError('Snapshot metadata scope exceeds its verification bound')
        saved = {}
        result = []
        for raw in registrations:
            row = dict(raw)
            contract, _ = _verify_declared(connection, row)
            if row['status'] not in {'reserved', 'prechecking', 'registered', 'failed', 'interrupted'}:
                raise ValueError('Unknown historical snapshot state')
            if row['registered_dataset_id'] is not None:
                dataset = connection.execute('SELECT * FROM datasets WHERE id=?',
                                             (row['registered_dataset_id'],)).fetchone()
                if dataset is None or row['status'] != 'registered':
                    raise ValueError('Snapshot dataset owner is missing')
                dataset = dict(dataset)
                metadata = json.loads(dataset['metadata'])
                checksum = metadata['market_sha256']
                if (re.fullmatch(r'[0-9a-f]{64}', checksum) is None
                        or snapshot_registration_digest(row, dataset) != row['registration_digest']
                        or digest({'data': checksum, 'metadata': metadata}) != dataset['sha256']
                        or digest(metadata) != digest(DailySnapshots._metadata(row, contract, checksum,
                                                                                row['development_content_digest']))):
                    raise ValueError('Snapshot metadata binding differs')
                observations = _saved_observations(connection, row, contract)
                binding = connection.execute('SELECT * FROM daily_snapshot_guard_bindings WHERE registration_id=?',
                                             (row['id'],)).fetchone()
                expected = {'registration_id': row['id'], 'dataset_id': dataset['id'],
                            'registered_digest': dataset['sha256'], 'declaration_digest': row['declaration_digest'],
                            'development_content_digest': row['development_content_digest'],
                            'development_index_digest': row['development_index_digest'], 'row_count': len(observations)}
                if (binding is None or any(binding[key] != value for key, value in expected.items())
                        or _record_digest(binding, 'binding_digest') != binding['binding_digest']):
                    raise ValueError('Snapshot independent metadata binding differs')
            else:
                _assert_unfinalized(connection, row)
            saved[row['id']] = (row, contract)
            result.append((row['id'], _dates(contract), list(contract.assets)))
        for table in ('daily_snapshot_guard_boundaries', 'daily_snapshot_guard_keys',
                      'daily_snapshot_guard_observations', 'daily_snapshot_guard_bindings', 'daily_snapshot_attempts'):
            if connection.execute(f'''SELECT 1 FROM {table} b LEFT JOIN daily_snapshot_registrations r
                ON r.id=b.registration_id WHERE r.id IS NULL LIMIT 1''').fetchone():
                raise ValueError('Snapshot metadata scope has no declaration owner')
        if connection.execute('''SELECT 1 FROM datasets d LEFT JOIN daily_snapshot_registrations r
            ON r.registered_dataset_id=d.id WHERE json_valid(d.metadata)
            AND json_extract(d.metadata,'$.data_kind')=? AND r.id IS NULL LIMIT 1''', (DATA_KIND,)).fetchone():
            raise ValueError('Typed snapshot dataset has no declaration')

        revisions = connection.execute('''SELECT v.id,v.research_id,v.task,v.digest,r.dataset_id,
            d.metadata,d.sha256 FROM revisions v JOIN researches r ON r.id=v.research_id
            JOIN datasets d ON d.id=r.dataset_id ORDER BY v.created_at,v.id LIMIT ?''',
            (MAX_HISTORY + 1,)).fetchall()
        if len(revisions) > MAX_HISTORY:
            raise ValueError('Revision metadata scope exceeds its verification bound')
        # An inner join must never silently hide a missing original owner.
        if connection.execute('''SELECT 1 FROM revisions v LEFT JOIN researches r ON r.id=v.research_id
            LEFT JOIN datasets d ON d.id=r.dataset_id WHERE r.id IS NULL OR d.id IS NULL LIMIT 1''').fetchone():
            raise ValueError('Revision metadata scope business owner is missing')
        typed_ids = set()
        legacy_ids = set()
        for raw in revisions:
            row = dict(raw)
            task, metadata = json.loads(row['task']), json.loads(row['metadata'])
            if digest(task) != row['digest']:
                raise ValueError('Revision task digest differs')
            if is_typed_task(task):
                typed_ids.add(row['id'])
                declaration = saved.get(task['execution']['registration_id'])
                if declaration is None:
                    raise ValueError('Typed revision declaration is missing')
                registration, contract = declaration
                if (registration['status'] != 'registered'
                        or registration['registered_dataset_id'] != row['dataset_id']):
                    raise ValueError('Typed revision dataset differs')
                expected_execution = execution_binding(registration, row['dataset_id'])
                parse_daily_task(task, expected_binding=expected_execution,
                                 declared_calendar=contract.calendar, catalog=catalog_from_daily_contract(contract))
                expected = {'revision_id': row['id'], 'research_id': row['research_id'], 'dataset_id': row['dataset_id'],
                    **{k: expected_execution[k] for k in ('registration_id', 'registration_digest', 'declaration_digest',
                        'input_digest', 'development_content_digest', 'development_index_digest', 'config_digest',
                        'catalog_digest', 'source_digest')}, 'task_digest': row['digest'],
                    'registered_dataset_digest': row['sha256'], 'metadata_digest': digest(metadata)}
                binding = connection.execute('SELECT * FROM daily_revision_bindings WHERE revision_id=?',
                                             (row['id'],)).fetchone()
                if (binding is None or any(binding[k] != value for k, value in expected.items())
                        or json.loads(binding['payload']) != expected or binding['binding_digest'] != digest(expected)):
                    raise ValueError('Typed revision immutable metadata binding differs')
                continue
            legacy_ids.add(row['id'])
            if metadata.get('data_kind') == DATA_KIND:
                raise ValueError('Daily prefix has no typed revision binding')
            bounds = research_guard._bounds(task, metadata)
            boundary = connection.execute('SELECT * FROM research_guard_boundaries WHERE revision_id=?',
                                          (row['id'],)).fetchone()
            checksum = metadata.get('market_sha256')
            if (not isinstance(checksum, str) or re.fullmatch(r'[0-9a-f]{64}', checksum) is None
                    or digest({'data': checksum, 'metadata': metadata}) != row['sha256']
                    or boundary is None or boundary['research_id'] != row['research_id']
                    or boundary['dataset_id'] != row['dataset_id'] or boundary['data_sha256'] != checksum
                    or boundary['task_digest'] != row['digest'] or boundary['registered_digest'] != row['sha256']
                    or boundary['metadata_digest'] != digest(metadata)
                    or any(boundary[role + '_' + endpoint] != bounds[role][endpoint]
                           for role in ('development', 'test') for endpoint in ('start', 'end'))):
                raise ValueError('Legacy revision declared boundary differs')
            if connection.execute('SELECT 1 FROM research_guard_unresolved WHERE revision_id=? OR data_sha256=? OR data_sha256 IS NULL LIMIT 1',
                                  (row['id'], checksum)).fetchone():
                raise ValueError('Legacy revision declared boundary unresolved')
            if row['id'] != ignore_revision_id:
                dates = {role: {day for day in metadata['calendar_dates']
                                if bounds[role]['start'] <= day <= bounds[role]['end']}
                         for role in ('development', 'test')}
                result.append((row['id'], dates, metadata['universe']))
        if {r[0] for r in connection.execute('SELECT revision_id FROM daily_revision_bindings')} != typed_ids:
            raise ValueError('Typed binding inventory differs from original revisions')
        if any(r[0] not in legacy_ids for r in connection.execute('SELECT revision_id FROM research_guard_boundaries')):
            raise ValueError('Legacy boundary inventory contains an unowned record')
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
        raise _error('Daily admission cannot verify the metadata-only historical declared scopes') from exc


def reserve_declared(connection, registration):
    """Called after immutable intent insertion, before committing numeric permission."""
    registration = _owner(connection, registration)
    contract = _contract(registration)
    if connection.execute('SELECT 1 FROM daily_snapshot_guard_boundaries WHERE registration_id=?', (registration['id'],)).fetchone():
        _verify_declared(connection, registration)
        return status(connection, registration)
    history = _history(connection, exclude=registration['id'])
    proposed_dates = _dates(contract)
    reason = _check_real_history(connection, proposed_dates, contract.assets)
    if reason:
        raise _error(reason)
    for old, previous, _ in history:
        reason = _conflict(proposed_dates, contract.assets, _dates(previous), previous.assets, old['id'])
        if reason:
            raise _error(reason)
    for revision_id, dates, assets in _legacy_history(connection):
        reason = _conflict(proposed_dates, contract.assets, dates, assets, revision_id)
        if reason:
            raise _error(reason)
    keys, boundary = _boundary_fields(registration, contract)
    boundary['created_at'] = datetime.now(timezone.utc).isoformat()
    boundary['boundary_digest'] = _record_digest(boundary, 'boundary_digest')
    connection.execute('INSERT INTO daily_snapshot_guard_boundaries VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(boundary.values()))
    connection.executemany('INSERT INTO daily_snapshot_guard_keys VALUES (?,?,?,?)',
                           ((registration['id'], row['date'], row['asset'], row['role']) for row in keys))
    return status(connection, registration)


def _verify_view(view, registration, contract):
    """Fresh internal tool output must agree with declared keys and actual panels."""
    if registration['origin'] != 'engineering_fixture':
        raise _error('Only engineering fixtures can finalize a daily development snapshot')
    if digest(view.contract.to_dict()) != digest(contract.to_dict()):
        raise _error('Daily snapshot development view changed the frozen contract')
    report, panels = view.report, view.panels
    expected = list((day, asset) for day in contract.development_calendar for asset in contract.assets)
    if (len(view.observations) != len(expected) or len(expected) > MAX_GRID_CELLS
            or type(report['read_scope']['final_numeric_rows_parsed']) is not int
            or report['read_scope']['final_numeric_rows_parsed'] != 0
            or report['read_scope']['full_dataset_validated'] is not False
            or report['identity']['input_digest'] != registration['input_digest']
            or report['identity']['contract_digest'] != registration['contract_digest']
            or report['identity']['canonical_version'] != CANONICAL_VERSION
            or report['identity']['development_content_digest'] != registration['development_content_digest']
            or development_index_digest(view) != registration['development_index_digest']):
        raise _error('Daily snapshot actual development view differs from its frozen tool identity')
    for field in FIELDS:
        if (list(panels[field].index.strftime('%Y-%m-%d')) != list(contract.development_calendar)
                or list(panels[field].columns) != list(contract.assets)):
            raise _error('Daily snapshot actual panels are misaligned')
    for index, ((day, asset), row) in enumerate(zip(expected, view.observations)):
        values = [panels[field].iloc[index//len(contract.assets), index%len(contract.assets)] for field in FIELDS]
        if (row.date != day or row.asset != asset
                or content_row_fingerprint(day, asset, values) != row.row_sha256):
            raise _error('Daily snapshot actual development panels do not match its observation index')
    content = digest({'version': CANONICAL_VERSION, 'rows': [o.__dict__ for o in view.observations]})
    if content != registration['development_content_digest']:
        raise _error('Daily snapshot development content differs from actual observations')


def finalize_snapshot(connection, dataset, registration, view):
    _owner(connection, registration)
    dataset = _saved_dataset(connection, dataset)
    contract, _ = _verify_declared(connection, registration)
    if connection.execute('SELECT 1 FROM daily_snapshot_guard_bindings WHERE registration_id=?', (registration['id'],)).fetchone():
        raise _error('Daily snapshot already finalized; no implicit replacement is permitted')
    if connection.execute('SELECT 1 FROM daily_snapshot_guard_observations WHERE registration_id=? LIMIT 1', (registration['id'],)).fetchone():
        raise _error('Daily snapshot orphan development observations cannot be reconstructed')
    _verify_view(view, registration, contract)
    _prefix_identity(dataset, contract, registration)
    if (registration['registered_dataset_id'] != dataset['id']
            or snapshot_registration_digest(registration, dataset) != registration['registration_digest']):
        raise _error('Daily snapshot finalization is not bound to its actual dataset')
    connection.executemany('INSERT INTO daily_snapshot_guard_observations VALUES (?,?,?,?)',
                           ((registration['id'], o.date, o.asset, o.row_sha256) for o in view.observations))
    binding = {'registration_id': registration['id'], 'dataset_id': dataset['id'],
               'registered_digest': dataset['sha256'], 'declaration_digest': registration['declaration_digest'],
               'development_content_digest': registration['development_content_digest'],
               'development_index_digest': registration['development_index_digest'], 'row_count': len(view.observations),
               'created_at': datetime.now(timezone.utc).isoformat()}
    binding['binding_digest'] = _record_digest(binding, 'binding_digest')
    connection.execute('INSERT INTO daily_snapshot_guard_bindings VALUES (?,?,?,?,?,?,?,?,?)', tuple(binding.values()))
    _verify_finalized(connection, dataset, registration, contract)


def assert_snapshot_safe(connection, dataset, registration, view=None):
    current = _owner(connection, registration)
    contract, _ = _verify_declared(connection, current)
    if current['registered_dataset_id'] != dataset['id']:
        raise _error('Daily snapshot is not finalized for the requested dataset')
    _verify_finalized(connection, dataset, current, contract)
    if view is not None:
        _verify_view(view, current, contract)
    history = _history(connection)
    dates = _dates(contract)
    reason = _check_real_history(connection, dates, contract.assets)
    if reason:
        raise _error(reason)
    for old, previous, _ in history:
        if old['id'] != current['id']:
            reason = _conflict(dates, contract.assets, _dates(previous), previous.assets, old['id'])
            if reason:
                raise _error(reason)
    for revision_id, previous, assets in _legacy_history(connection):
        reason = _conflict(dates, contract.assets, previous, assets, revision_id)
        if reason:
            raise _error(reason)


def check_legacy(connection, dataset, bounds, ignore_revision_id=None):
    """Original hook: no new intent => unchanged old path, no numeric parsing."""
    # Verify external host anchors before any empty-history early return.
    # This also catches deletion of every new table and its SQLite marker.
    reason = _check_real_history(connection, {"development": set(), "test": set()}, [])
    if reason:
        return reason
    # Main-Store reservations exist independently of the legacy rdl/snapshot
    # owner tables. Check the actual proposed keys before that old early exit.
    main_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rdw_reservations'").fetchone()
    if main_table and connection.execute('SELECT 1 FROM rdw_reservations LIMIT 1').fetchone():
        try:
            metadata = json.loads(dataset['metadata']) if isinstance(dataset['metadata'], str) else dataset['metadata']
            dates = {role: {day for day in metadata['calendar_dates'] if bounds[role]['start'] <= day <= bounds[role]['end']}
                     for role in ('development', 'test')}
            reason = _check_real_history(connection, dates, metadata['universe'])
            if reason:
                return reason
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            return 'Daily snapshot bridge cannot verify the proposed declared scope'
    present = connection.execute('SELECT 1 FROM daily_snapshot_registrations LIMIT 1').fetchone()
    if present is None:
        # Deleted owners must not make dangling declarations an empty-history bypass.
        present = any(connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                      for table in ('daily_snapshot_guard_boundaries', 'daily_snapshot_guard_keys',
                                    'daily_snapshot_guard_observations', 'daily_snapshot_guard_bindings', 'daily_snapshot_attempts'))
        present = present or connection.execute(
            "SELECT 1 FROM datasets WHERE json_valid(metadata) AND json_extract(metadata,'$.data_kind')=? LIMIT 1", (DATA_KIND,)).fetchone()
        if not present:
            present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name GLOB 'rdl_*' LIMIT 1").fetchone()
        if not present and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            # The independent host marker survives accidental deletion of every
            # new-lane table; absent tables cannot erase prior test reservations.
            present = connection.execute("SELECT 1 FROM settings WHERE key='real_daily_host_version' LIMIT 1").fetchone()
        if not present:
            return None
    try:
        history = _history(connection)
        metadata = json.loads(dataset['metadata']) if isinstance(dataset['metadata'], str) else dataset['metadata']
        dates = {role: {day for day in metadata['calendar_dates'] if bounds[role]['start'] <= day <= bounds[role]['end']}
                 for role in ('development','test')}
        reason = _check_real_history(connection, dates, metadata['universe'])
        if reason:
            return reason
        for registration, contract, _ in history:
            reason = _conflict(dates, metadata['universe'], _dates(contract), contract.assets, registration['id'])
            if reason:
                return reason
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return 'Daily snapshot bridge cannot verify the independent historical declarations and development bindings'


def status(connection, registration):
    """Public summary contains declarations/counts, never private paths or numbers."""
    current = _owner(connection, registration)
    contract, boundary = _verify_declared(connection, current)
    if current['registered_dataset_id'] is not None:
        raw = connection.execute('SELECT * FROM datasets WHERE id=?', (current['registered_dataset_id'],)).fetchone()
        if raw is None:
            raise _error('Daily snapshot registered dataset is missing')
        _verify_finalized(connection, dict(raw), current, contract)
    else:
        _assert_unfinalized(connection, current)
    # This is also D's post-commit, pre-numeric gate: validate peers and old anchors.
    dates = _dates(contract)
    for old, previous, _ in _history(connection):
        if old['id'] != current['id']:
            reason = _conflict(dates, contract.assets, _dates(previous), previous.assets, old['id'])
            if reason:
                raise _error(reason)
    for revision_id, previous, assets in _legacy_history(connection):
        reason = _conflict(dates, contract.assets, previous, assets, revision_id)
        if reason:
            raise _error(reason)
    return {'guard_version': 'daily-declared-asset-date-guard-v1', 'registration_id': current['id'],
            'declaration_digest': current['declaration_digest'], 'scope_digest': boundary['scope_digest'],
            'boundary_digest': boundary['boundary_digest'], 'scope': 'potential_asset_date_overlap',
            'development': {'start': boundary['development_start'], 'end': boundary['development_end']},
            'test': {'start': boundary['test_start'], 'end': boundary['test_end']},
            'declared_key_count': boundary['declared_key_count'],
            'development_observation_count': len(contract.development_calendar)*len(contract.assets) if current['registered_dataset_id'] else 0,
            'final_numeric_rows_parsed': 0, 'final_observation_fingerprints': 0, 'test_freshness': 'unknown',
            'source_equivalence_verified': False, 'real_source_ready': False, 'scientific_score': None}
