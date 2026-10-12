"""Offline, fenced reconciliation. No retries, process signals, or model calls."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote

from ..storage import digest, json_text
from .db import SCHEMA_VERSION, transaction
from .maintenance import workspace_lease
from .proposal_requests import ProposalRequests, ProposalServiceError, _decode, _error
from .provider_recovery_schema import RecoveryCommand
from .provider_contract import strict_json
from .provider_supervision_journal import termination_proof

MAX_ITEMS = 1000
MAX_RECOVERY_BYTES = 16 * 1024 * 1024


def _fail(code, message):
    raise ProposalServiceError(message, 409, code=code)


def _root(home):
    path = Path(home).absolute()
    if '..' in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        _fail('RECOVERY_PATH_INVALID', 'Recovery requires an existing unlinked workspace.')
    if not path.is_dir() or not (path / 'workbench.sqlite3').is_file() or (path / 'workbench.sqlite3').is_symlink():
        _fail('RECOVERY_PATH_INVALID', 'Workspace database is unavailable.')
    if (path / '.restore-incomplete').exists():
        _fail('RESTORE_INCOMPLETE', 'Workspace restoration is incomplete.')
    return path


def _requests(root):
    # Deliberately no Store(): inspect must not create folders or migrate schema;
    # maintenance already owns an exclusive lease incompatible with Store's SH.
    return ProposalRequests(SimpleNamespace(root=root, db_path=root / 'workbench.sqlite3'), configs={})


def _settings(connection):
    values = dict(connection.execute('SELECT key,value FROM settings'))
    if values.get('schema_version') != str(SCHEMA_VERSION):
        _fail('RECOVERY_SCHEMA_REQUIRED', 'Upgrade a stopped workspace copy before reconciliation.')
    if not re.fullmatch('[0-9a-f]{32}', values.get('provider_workspace_identity', '')):
        _fail('RECOVERY_IDENTITY_INVALID', 'Workspace recovery identity is unavailable.')
    return values


def _checked_action(row):
    raw = row['payload'].encode()
    payload = strict_json(raw, max_bytes=MAX_RECOVERY_BYTES)
    if json_text(payload) != row['payload']:
        _fail('RECOVERY_LEDGER_INTEGRITY', 'Recovery receipt is not canonical.')
    if (digest(payload) != row['digest'] or payload.get('id') != row['id'] or row['id'] != 'provider_recovery_' + row['request_digest'] or
            digest(payload['request']) != row['request_digest'] or
            payload['request']['idempotency_key'] != row['idempotency_key']):
        _fail('RECOVERY_LEDGER_INTEGRITY', 'Recovery receipt failed integrity checks.')
    return payload


def resolved_attempt(connection, value):
    """Validate the overlay before it can suppress an interrupted blocker."""
    attempt = value['attempts'][-1]
    row = connection.execute('SELECT * FROM provider_attempt_resolutions WHERE attempt_id=?', (attempt['id'],)).fetchone()
    if row is None:
        return False
    payload = _decode(row['payload'])
    action = connection.execute('SELECT * FROM provider_recovery_actions WHERE id=?', (row['action_id'],)).fetchone()
    if (digest(payload) != row['digest'] or row['proposal_id'] != value['id'] or
            row['attempt_digest'] != digest(attempt) or payload.get('attempt_digest') != digest(attempt) or
            payload.get('proposal_id') != value['id'] or payload.get('attempt_id') != attempt['id'] or
            payload.get('disposition') != 'abandon_without_replay' or action is None):
        _fail('RECOVERY_LEDGER_INTEGRITY', 'Attempt resolution differs from the original interruption.')
    parent = _checked_action(action)
    if payload not in parent.get('resolutions', []):
        _fail('RECOVERY_LEDGER_INTEGRITY', 'Attempt resolution is not anchored in a recovery receipt.')
    return True


def unresolved_interrupted(connection, requests):
    rows = connection.execute("SELECT id FROM proposal_requests WHERE status='interrupted' ORDER BY id LIMIT ?", (MAX_ITEMS + 1,)).fetchall()
    if len(rows) > MAX_ITEMS:
        _fail('RECOVERY_LIMIT', 'Interrupted inventory exceeds the maintenance bound.')
    return [value for row in rows if not resolved_attempt(connection, value := requests._load(connection, row['id']))]


def _plan(connection, requests):
    settings = _settings(connection)
    rows = connection.execute("SELECT id FROM proposal_requests WHERE status IN ('running','validating','cancelling','interrupted','queued') ORDER BY id LIMIT ?", (MAX_ITEMS + 1,)).fetchall()
    if len(rows) > MAX_ITEMS:
        _fail('RECOVERY_LIMIT', 'Recovery inventory exceeds its bound.')
    unresolved, queued, active = [], [], []
    for row in rows:
        value = requests._load(connection, row['id'])
        if value['status'] == 'interrupted' and resolved_attempt(connection, value):
            continue
        item = {'proposal_id': value['id'], 'status': value['status'],
                'state_digest': digest(requests._state_value(value)), 'deadline_at': value['deadline_at']}
        if value['status'] == 'queued':
            queued.append(item)
            continue
        attempt = value['attempts'][-1]
        item.update(attempt_id=attempt['id'], attempt_digest=digest(attempt))
        origin_row = connection.execute('SELECT * FROM provider_attempt_origins WHERE attempt_id=?', (attempt['id'],)).fetchone()
        origin = _decode(origin_row['payload']) if origin_row else None
        if origin_row is not None and digest(origin) != origin_row['digest']:
            _fail('RECOVERY_LEDGER_INTEGRITY', 'Attempt origin failed integrity checks.')
        item['termination'] = termination_proof(requests.store.root, value, settings['provider_workspace_identity'], origin)
        (unresolved if value['status'] == 'interrupted' else active).append(item)
    value = {'schema_version': 1, 'workspace_identity': settings['provider_workspace_identity'],
             'restored_workspace': settings.get('provider_restored_workspace'),
             'pause': {'reason': settings.get('proposal_dispatch_paused'),
                       'generation': settings.get('provider_pause_generation')},
             'unresolved': unresolved, 'active': active, 'queued': queued,
             'policy': 'abandon_without_replay_then_resume_new_only'}
    return {**value, 'plan_digest': digest(value)}


def inspect_recovery(home):
    root = _root(home)
    uri = 'file:' + quote(str(root / 'workbench.sqlite3'), safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        return _plan(connection, _requests(root))


def apply_recovery(home, command):
    """One atomic maintenance action; plan/receipt never resets a request budget."""
    command = RecoveryCommand.model_validate(command)
    if not command.note.strip() or not command.idempotency_key.strip():
        _fail('RECOVERY_ARGUMENTS_INVALID', 'A nonempty note and idempotency key are required.')
    root = _root(home)
    requests = _requests(root)
    body = command.model_dump()
    with workspace_lease(root, exclusive=True, create=False), requests._lock('worker'), requests._lock('recovery'):
        with transaction(root / 'workbench.sqlite3') as connection:
            _settings(connection)
            existing = connection.execute('SELECT * FROM provider_recovery_actions WHERE idempotency_key=?', (command.idempotency_key,)).fetchone()
            if existing is not None:
                if existing['request_digest'] != digest(body):
                    _fail('IDEMPOTENCY_CONFLICT', 'Recovery key already identifies another command.')
                return _checked_action(existing)
            plan = _plan(connection, requests)
            if plan['plan_digest'] != command.plan_digest:
                _fail('RECOVERY_PLAN_STALE', 'Workspace, pause or recovery evidence changed; inspect again.')
            if not plan['pause']['reason'] and not plan['active']:
                _fail('RECOVERY_NOT_PAUSED', 'No matching paused generation exists.')
            resolutions, cancelled = [], []
            if command.operation == 'reconcile':
                targets = [*plan['active'], *plan['unresolved']]
                if not targets:
                    _fail('RECOVERY_NOT_REQUIRED', 'No unresolved interruption exists; use resume-new for a restored queue.')
                if any(not item['termination']['confirmed'] for item in targets):
                    _fail('STOP_EVIDENCE_UNCONFIRMED', 'A process stop remains unknown; no interruption was resolved.')
                if plan['active']:
                    requests._recover_interrupted(connection)
                    # The original active payloads are retained in the plan;
                    # the overlay binds the actual new interrupted payload.
                    targets = [{**item, 'attempt_digest': digest(requests._load(connection, item['proposal_id'])['attempts'][-1])}
                               for item in targets]
                resolutions = [{'proposal_id': item['proposal_id'], 'attempt_id': item['attempt_id'],
                    'attempt_digest': item['attempt_digest'], 'disposition': 'abandon_without_replay',
                    'termination': item['termination']} for item in targets]
            else:
                if plan['unresolved'] or plan['active']:
                    _fail('RECOVERY_UNRESOLVED', 'Resolve every interrupted attempt before allowing new requests.')
                # Original queue entries are explicitly in the confirmed plan;
                # none can accidentally spend quota after clearing the pause.
                stamp = datetime.now(timezone.utc)
                for item in plan['queued']:
                    value = requests._load(connection, item['proposal_id'])
                    state = requests._state_value(value)
                    expired = stamp >= datetime.fromisoformat(value['deadline_at'])
                    state.update(status='expired' if expired else 'cancelled',
                                 error=_error('TIMEOUT' if expired else 'RECOVERY_QUEUE_CANCELLED',
                                              'Historical queue closed during explicit resume-new maintenance.'))
                    requests._state(connection, value['id'], state)
                    requests._event(connection, value['id'], 'closed_before_recovery_resume')
                    cancelled.append({'proposal_id': value['id'], 'status': state['status']})
                connection.execute("DELETE FROM settings WHERE key='proposal_dispatch_paused'")
            identity = 'provider_recovery_' + digest(body)
            payload = {'schema_version': 1, 'id': identity, 'request': body, 'plan': plan,
                       'created_at': datetime.now(timezone.utc).isoformat(), 'resolutions': resolutions,
                       'closed_queue': cancelled, 'new_requests_allowed': command.operation == 'resume_new'}
            if len(json_text(payload).encode()) > MAX_RECOVERY_BYTES:
                _fail('RECOVERY_LIMIT', 'Recovery receipt exceeds its bounded ledger size.')
            connection.execute('INSERT INTO provider_recovery_actions VALUES (?,?,?,?,?,?)',
                (identity, command.idempotency_key, digest(body), json_text(payload), digest(payload), payload['created_at']))
            for item in resolutions:
                connection.execute('INSERT INTO provider_attempt_resolutions VALUES (?,?,?,?,?,?)',
                    (item['attempt_id'], item['proposal_id'], item['attempt_digest'], identity, json_text(item), digest(item)))
            return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline provider recovery; never retries interrupted calls')
    commands = parser.add_subparsers(dest='operation', required=True)
    for name in ('inspect', 'reconcile', 'resume-new'):
        child = commands.add_parser(name)
        child.add_argument('--home', type=Path, required=True)
        if name != 'inspect':
            child.add_argument('--plan-digest', required=True)
            child.add_argument('--idempotency-key', required=True)
            child.add_argument('--note', required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_recovery(args.home) if args.operation == 'inspect' else apply_recovery(args.home, {
            'operation': args.operation.replace('-', '_'), 'plan_digest': args.plan_digest,
            'idempotency_key': args.idempotency_key, 'note': args.note})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'RECOVERY_FAILED'),
                          'message': 'Recovery did not complete. Inspect the stopped workspace and its recorded evidence.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
