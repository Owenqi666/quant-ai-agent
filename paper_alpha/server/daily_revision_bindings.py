"""Immutable business anchors for prefix-only typed daily revisions.

These anchors contain no current code SHA, private path or final observation.
Run manifests own current implementation identity. Importing this module does
not initialize a database or read a dataset.
Schema27 migration/backup allow only these exact protection triggers. The only
binding mutator is an idempotent insert; reads independently resolve original
revision, registration, declared keys, saved prefix bytes and hashes.
"""
import json
from ..storage import digest, json_text
from .service import ServiceError

KIND = 'registered_daily_development_v2'
SCHEMA = """
CREATE TABLE daily_revision_bindings(revision_id TEXT PRIMARY KEY REFERENCES revisions(id),research_id TEXT NOT NULL REFERENCES researches(id),dataset_id TEXT NOT NULL REFERENCES datasets(id),registration_id TEXT NOT NULL REFERENCES daily_snapshot_registrations(id),registration_digest TEXT NOT NULL,declaration_digest TEXT NOT NULL,input_digest TEXT NOT NULL,development_content_digest TEXT NOT NULL,development_index_digest TEXT NOT NULL,config_digest TEXT NOT NULL,catalog_digest TEXT NOT NULL,source_digest TEXT NOT NULL,task_digest TEXT NOT NULL,registered_dataset_digest TEXT NOT NULL,metadata_digest TEXT NOT NULL,payload TEXT NOT NULL,binding_digest TEXT NOT NULL);
CREATE TABLE daily_revision_events(revision_id TEXT NOT NULL REFERENCES daily_revision_bindings(revision_id),run_id TEXT NOT NULL REFERENCES runs(id),kind TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(revision_id,run_id,kind));
CREATE TRIGGER daily_revision_bindings_no_update BEFORE UPDATE ON daily_revision_bindings BEGIN SELECT RAISE(ABORT,'immutable daily revision binding'); END;
CREATE TRIGGER daily_revision_bindings_no_delete BEFORE DELETE ON daily_revision_bindings BEGIN SELECT RAISE(ABORT,'permanent daily revision binding'); END;
"""
REQUIRED_COLUMNS = {'daily_revision_bindings': {
    'revision_id', 'research_id', 'dataset_id', 'registration_id', 'registration_digest',
    'declaration_digest', 'input_digest', 'development_content_digest', 'development_index_digest',
    'config_digest', 'catalog_digest', 'source_digest', 'task_digest', 'registered_dataset_digest',
    'metadata_digest', 'payload', 'binding_digest'},
    'daily_revision_events': {'revision_id', 'run_id', 'kind', 'created_at'}}


def _need(condition, message):
    if not condition:
        raise ServiceError(message, 409)


def is_typed_task(task):
    if isinstance(task, str):
        task = json.loads(task)
    return (isinstance(task, dict) and type(task.get('schema_version')) is int and task.get('schema_version') == 2
            and isinstance(task.get('execution'), dict) and task['execution'].get('kind') == KIND)


def _expected(connection, dataset, revision):
    from . import daily_snapshot_guard as guard
    from ..daily_task_v2 import execution_binding, parse_daily_task
    from .provider_catalog import catalog_from_daily_contract
    identity = revision if isinstance(revision, str) else revision['id']
    saved = connection.execute('SELECT * FROM revisions WHERE id=?', (identity,)).fetchone()
    _need(saved is not None, 'Typed daily revision has no saved business owner')
    saved = dict(saved)
    if not isinstance(revision, str):
        _need(all(saved[k] == revision[k] for k in ('id', 'research_id', 'task', 'digest')), 'Caller daily revision differs from saved revision')
    task = json.loads(saved['task'])
    _need(is_typed_task(task) and digest(task) == saved['digest'], 'Typed daily task identity differs')
    research = connection.execute('SELECT * FROM researches WHERE id=?', (saved['research_id'],)).fetchone()
    _need(research is not None and research['dataset_id'] == task['execution']['dataset_id'], 'Typed daily research dataset differs')
    current_dataset = connection.execute('SELECT * FROM datasets WHERE id=?', (research['dataset_id'],)).fetchone()
    _need(current_dataset is not None, 'Typed daily dataset business owner is missing')
    current_dataset = dict(current_dataset)
    if dataset is not None:
        _need(all(dataset[k] == current_dataset[k] for k in ('id', 'sha256', 'metadata', 'data_path', 'metadata_path')), 'Caller daily dataset differs from saved dataset')
    row = connection.execute('SELECT * FROM daily_snapshot_registrations WHERE id=?', (task['execution']['registration_id'],)).fetchone()
    _need(row is not None, 'Typed daily registration business anchor is missing')
    row = dict(row)
    _need(row['status'] == 'registered' and row['registered_dataset_id'] == current_dataset['id'], 'Typed daily registration is not finalized for this dataset')
    contract, _ = guard._verify_declared(connection, row)
    guard._verify_finalized(connection, current_dataset, row, contract)
    expected = execution_binding(row, current_dataset['id'])
    _need(task['execution'] == expected, 'Typed daily execution binding differs from independent business declaration')
    parse_daily_task(task, expected_binding=expected, declared_calendar=contract.calendar,
                     catalog=catalog_from_daily_contract(contract))
    return {'revision_id': saved['id'], 'research_id': saved['research_id'], 'dataset_id': current_dataset['id'],
        **{k: expected[k] for k in ('registration_id', 'registration_digest', 'declaration_digest', 'input_digest',
                                  'development_content_digest', 'development_index_digest', 'config_digest', 'catalog_digest', 'source_digest')},
        'task_digest': saved['digest'], 'registered_dataset_digest': current_dataset['sha256'],
        'metadata_digest': digest(json.loads(current_dataset['metadata']))}


def bind_revision(connection, dataset, revision):
    """Insert after the original revision insert, inside the SAME transaction."""
    expected = _expected(connection, dataset, revision)
    existing = connection.execute('SELECT * FROM daily_revision_bindings WHERE revision_id=?', (expected['revision_id'],)).fetchone()
    if existing is not None:
        return assert_revision(connection, revision, dataset=dataset)
    columns = tuple(expected) + ('payload', 'binding_digest')
    connection.execute('INSERT INTO daily_revision_bindings(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')',
                       (*expected.values(), json_text(expected), digest(expected)))
    return expected


def assert_revision(connection, revision, *, dataset=None):
    """Verify saved typed anchors; a missing anchor never creates one on reads."""
    expected = _expected(connection, dataset, revision)
    row = connection.execute('SELECT * FROM daily_revision_bindings WHERE revision_id=?', (expected['revision_id'],)).fetchone()
    _need(row is not None, 'Typed daily revision immutable binding is missing')
    _need(all(row[k] == v for k, v in expected.items()) and json.loads(row['payload']) == expected
          and row['binding_digest'] == digest(expected), 'Typed daily revision immutable binding is missing or changed')
    return expected


def assert_history(connection):
    """Bounded read-only typed history, including missing and orphan anchors."""
    rows = connection.execute('SELECT id,task FROM revisions ORDER BY created_at,id LIMIT 1001').fetchall()
    _need(len(rows) <= 1000, 'Typed daily history exceeds the 1000-revision verification bound')
    typed = [r['id'] for r in rows if is_typed_task(r['task'])]
    table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_revision_bindings'").fetchone()
    if table is None:
        _need(not typed, 'Typed daily history immutable binding table is missing')
        return {'typed_revision_count': 0}
    bound = {r[0] for r in connection.execute('SELECT revision_id FROM daily_revision_bindings')}
    _need(bound == set(typed), 'Typed daily historical binding inventory is missing or unowned')
    for identity in typed:
        assert_revision(connection, identity)
    return {'typed_revision_count': len(typed)}


def record_event(connection, revision_id, run_id, kind):
    from datetime import datetime, timezone
    binding = assert_revision(connection, revision_id)
    _need(kind in {'submitted', 'retried', 'reserved', 'started', 'completed', 'failed', 'cancelled', 'interrupted'}, 'Unknown daily revision event kind')
    run = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    _need(run is not None and run['revision_id'] == revision_id and run['research_id'] == binding['research_id'], 'Daily event names another original run')
    connection.execute('INSERT OR IGNORE INTO daily_revision_events VALUES(?,?,?,?)',
                       (revision_id, run_id, kind, datetime.now(timezone.utc).isoformat()))
