"""Main OHLC task/revision bindings; no numeric loader in history or guards."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from ..real_daily_main_task import CORE_KEYS, is_main_task, parse_main_task, inspect_main_task
from ..real_daily_main_metadata import snapshot
from ..storage import digest, json_text
from .real_daily_workspace_authority import _rows, _history, _utc
from .real_daily_prefix_registration import (RealDailyPrefixes, main_execution_binding, assert_prefix_history,
    _payload, REGISTRATION_KEYS, MAX_REGISTRATION_BYTES, RealDailyPrefixError)

BINDING_VERSION = 'real-daily-main-revision-v1'
BINDING_KEYS = frozenset({'schema_version', 'binding_version', 'revision_id', 'research_id', 'paper_id', 'dataset_id',
    'registration_id', 'registration_digest', 'assessment_id', 'assessment_digest', 'selection_digest', 'config_digest',
    'task_digest', 'execution_digest', 'execution', 'catalog_digest', 'source_digest', 'dataset_sha256', 'metadata_digest',
    'reservation_id', 'reservation_digest', 'authority_id', 'epoch_id', 'created_at_utc'})


def need(condition, code):
    if not condition:
        raise RealDailyPrefixError(code)


def source_for_dataset(store, dataset_id, registration_id=None, connection=None, *, require_current_permission=True):
    source = RealDailyPrefixes(store).source_for_dataset(dataset_id, require_current_permission=require_current_permission,
                                                        read_prefix=False, connection=connection)
    need(registration_id is None or source['registration']['id'] == registration_id, 'main_registration_dataset_mismatch')
    return source


def freeze_task(store, title, dataset_id, task, registration_id=None, connection=None):
    source = source_for_dataset(store, dataset_id, registration_id, connection)
    value = snapshot(task, maximum=512 * 1024)
    if is_main_task(value):
        need(value['title'] == title, 'main_task_title_mismatch')
    else:
        need(type(value) is dict and not set(value) - CORE_KEYS - {'schema_version', 'title'} and
             type(value.get('schema_version', 1)) is int and value.get('schema_version', 1) == 1,
             'invalid_main_task_core')
        value = {'schema_version': 3, 'title': title, **{key: deepcopy(value[key]) for key in CORE_KEYS},
                 'execution': source['execution']}
    return parse_main_task(value, source['execution'], source['registration']['selection'], source['catalog'])


def assert_main_task(store, task, dataset_id=None, connection=None, *, require_current_permission=True):
    need(is_main_task(task), 'main_typed_task_required')
    source = source_for_dataset(store, dataset_id or task['execution']['dataset_id'], task['execution']['registration_id'],
                                connection, require_current_permission=require_current_permission)
    parsed = parse_main_task(task, source['execution'], source['registration']['selection'], source['catalog'])
    need(json_text(parsed) == json_text(task), 'main_task_not_canonical')
    return source


def _registrations(connection):
    rows = _rows(connection, 'SELECT * FROM rdw_prefix_registrations LIMIT 1001')
    need(len(rows) <= 1000, 'prefix_history_bound_exceeded')
    return {row['id']: {**_payload(row, 'registration_digest', REGISTRATION_KEYS, MAX_REGISTRATION_BYTES),
                       'registration_digest': row['registration_digest']} for row in rows}


def _one(connection, table, key, value):
    rows = _rows(connection, 'SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,))
    need(len(rows) == 1, 'main_revision_owner_missing')
    return rows[0]


def _expected_revision(connection, revision, registrations, dataset=None):
    revision_id = revision if isinstance(revision, str) else revision['id']
    saved = _one(connection, 'revisions', 'id', revision_id)
    if not isinstance(revision, str):
        need(all(saved[key] == revision[key] for key in ('id', 'research_id', 'task', 'digest')), 'main_revision_caller_drift')
    task = json.loads(saved['task'])
    need(is_main_task(task) and digest(task) == saved['digest'], 'main_revision_task_drift')
    research = _one(connection, 'researches', 'id', saved['research_id'])
    need(research['dataset_id'] == task['execution']['dataset_id'], 'main_revision_dataset_drift')
    current_dataset = _one(connection, 'datasets', 'id', research['dataset_id'])
    if dataset is not None:
        need(all(current_dataset[key] == dataset[key] for key in ('id', 'sha256', 'metadata')), 'main_revision_dataset_drift')
    registration_id = task['execution']['registration_id']
    need(registration_id in registrations, 'main_revision_registration_missing')
    reg = registrations[registration_id]
    need(reg['dataset_id'] == current_dataset['id'] and reg['dataset_sha256'] == current_dataset['sha256'] and
         reg['metadata_digest'] == digest(json.loads(current_dataset['metadata'])), 'main_revision_dataset_drift')
    execution = main_execution_binding(reg)
    parsed = parse_main_task(task, execution, reg['selection'], reg['catalog'])
    need(json_text(parsed) == json_text(task), 'main_revision_task_drift')
    paper = _one(connection, 'papers', 'id', research['paper_id'])
    _utc(saved['created_at'])
    value = {'schema_version': 1, 'binding_version': BINDING_VERSION, 'revision_id': saved['id'],
        'research_id': research['id'], 'paper_id': paper['id'], 'dataset_id': current_dataset['id'],
        'registration_id': reg['id'], **{key: reg[key] for key in ('registration_digest', 'assessment_id', 'assessment_digest',
            'selection_digest', 'catalog_digest', 'reservation_id', 'reservation_digest', 'authority_id', 'epoch_id')},
        'config_digest': execution['config_digest'], 'task_digest': saved['digest'], 'execution_digest': digest(execution),
        'execution': execution, 'source_digest': execution['source_digest'], 'dataset_sha256': current_dataset['sha256'],
        'metadata_digest': reg['metadata_digest'], 'created_at_utc': saved['created_at']}
    snapshot(value, maximum=128 * 1024)
    return value


def _assert_current_registration(connection, registration):
    databases = _rows(connection, 'PRAGMA database_list')
    main = next(row['file'] for row in databases if row['name'] == 'main')
    root = Path(main).parent
    service = RealDailyPrefixes(SimpleNamespace(root=root, db_path=Path(main)))
    _, authority_state = _history(connection)
    command = {key: registration[key] for key in
        ('handle_id', 'handle_digest', 'approval_id', 'approval_digest', 'reservation_id', 'reservation_digest')}
    service._current(connection, authority_state, command)


def register_main_revision(connection, dataset, revision):
    """Insert after saved revision, in Root's original revision transaction."""
    registrations = _registrations(connection)
    expected = _expected_revision(connection, revision, registrations, dataset)
    existing = _rows(connection, 'SELECT * FROM rdw_revision_bindings WHERE revision_id=?', (expected['revision_id'],))
    if existing:
        return assert_main_revision(connection, dataset, revision)
    _assert_current_registration(connection, registrations[expected['registration_id']])
    connection.execute('INSERT INTO rdw_revision_bindings VALUES (?,?,?,?,?,?,?,?,?,?)',
        (expected['revision_id'], expected['registration_id'], expected['assessment_id'], expected['selection_digest'],
         expected['config_digest'], expected['task_digest'], expected['execution_digest'], digest(expected),
         json_text(expected), expected['created_at_utc']))
    # Validate after inserting the one pending new binding. Missing any other
    # typed binding still fails, and capacity overflow rolls back the caller tx.
    assert_prefix_history(connection)
    return expected


def assert_main_revision(connection, dataset, revision, *, require_current_permission=False):
    state = assert_prefix_history(connection)
    expected = _expected_revision(connection, revision, state['registrations'], dataset)
    rows = _rows(connection, 'SELECT * FROM rdw_revision_bindings WHERE revision_id=?', (expected['revision_id'],))
    need(len(rows) == 1, 'main_revision_binding_missing')
    _validate_binding_row(rows[0], expected)
    if require_current_permission:
        _assert_current_registration(connection, state['registrations'][expected['registration_id']])
    return expected


def _validate_binding_row(row, expected):
    value = json.loads(row['payload_json'])
    need(type(value) is dict and set(value) == BINDING_KEYS and json_text(value) == json_text(expected) and
         row['binding_digest'] == digest(value), 'main_revision_binding_drift')
    for key in ('revision_id', 'registration_id', 'assessment_id', 'selection_digest', 'config_digest', 'task_digest', 'execution_digest'):
        need(row[key] == value[key], 'main_revision_binding_drift')
    need(row['created_at'] == value['created_at_utc'], 'main_revision_binding_drift')


def assert_revision_binding_history(connection, rows, registrations):
    """Exact typed inventory and closed saved binds, without recursive resolver."""
    revisions = _rows(connection, 'SELECT id,task FROM revisions LIMIT 1001')
    need(len(revisions) <= 1000, 'main_revision_history_bound_exceeded')
    typed = {row['id'] for row in revisions if is_main_task(json.loads(row['task']))}
    need({row['revision_id'] for row in rows} == typed, 'main_revision_binding_inventory_drift')
    for row in rows:
        expected = _expected_revision(connection, row['revision_id'], registrations)
        _validate_binding_row(row, expected)
    return {'typed_revision_count': len(typed)}


def assert_result_binding_history(connection, rows, registrations):
    # Root is the sole writer/parser owner for main producer publication.
    from .real_daily_main_execution import assert_result_binding_history as assert_results
    return assert_results(connection, rows, registrations)
