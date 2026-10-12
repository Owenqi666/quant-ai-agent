"""Frozen final-period capability with permanent exposure before numeric reads.

Only explicit local helpers create permission. The current executor accepts
engineering fixtures; a human receipt alone never upgrades an unqualified real
source. Failed and unknown reads retain exposure and cannot be retried.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time

from ..storage import digest, json_text, atomic_json
from .db import connect, transaction
from .daily_holdout_schema import DailyHoldoutFreezeCreate, DailyHoldoutPermissionCreate
from .daily_revision_bindings import assert_revision
from .provider_supervision_journal import read_metadata
from .service import ServiceError


class HoldoutError(ServiceError):
    def __init__(self, message, code='HOLDOUT_INVALID'):
        super().__init__(message, 409)
        self.code, self.retryable = code, False


def _need(condition, message, code='HOLDOUT_INVALID'):
    if not condition:
        raise HoldoutError(message, code)


def _now():
    return datetime.now(timezone.utc)


def _record(row):
    _need(row is not None, 'Holdout record is missing')
    value = json.loads(row['payload'])
    _need(digest(value) == row['digest'], 'Holdout record digest changed')
    return value


def _evaluated_candidate(folder, candidate, original, verified, evaluation):
    """Bind an evaluable candidate to its exact development tool artifact."""
    _need(candidate.get('status') == 'evaluated' and candidate.get('result', {}).get('status') == 'evaluated',
          'Final candidate requires an evaluated development tool result', 'HOLDOUT_CANDIDATE_REQUIRED')
    identity = original['id']
    attempts = candidate.get('attempts')
    _need(candidate['id'] == identity and candidate['expression'] == original['expression']
          and isinstance(attempts, list) and len(attempts) == 1
          and attempts[0]['number'] == 1 and attempts[0]['status'] == 'evaluated'
          and attempts[0]['expression'] == original['expression']
          and type(attempts[0]['tool_call']) is int and attempts[0]['tool_call'] > 0,
          'Candidate identity, expression or exact development tool attempt changed')
    result = read_metadata(folder / 'candidates' / identity / 'attempt-1' / 'result.json', 8 * 1024 * 1024)
    result_digest = digest(result)
    _need(result_digest == digest(candidate['result']) == candidate['result_digest']
          == verified['candidate_results'].get(identity), 'Candidate differs from its exact verified tool result')
    _need(result['evaluation_scope'] == 'development_prefix_validation_only' and result['split'] == 'validation'
          and result['final_numeric_rows_used'] == 0 and result['expression'] == original['expression']
          and digest(result['config']) == digest(evaluation), 'Candidate development scope or frozen configuration changed')
    metrics = result['metrics']
    counts = ('evaluated_days', 'eligible_days', 'skipped_days', 'rank_ic_days',
              'finite_factor_observations', 'possible_factor_observations')
    _need(all(type(metrics.get(k)) is int and metrics[k] >= 0 for k in counts)
          and metrics['evaluated_days'] > 0
          and metrics['evaluated_days'] + metrics['skipped_days'] == metrics['eligible_days']
          and metrics['rank_ic_days'] <= metrics['evaluated_days']
          and metrics['finite_factor_observations'] <= metrics['possible_factor_observations'],
          'Candidate lacks a consistent nonempty development evaluation', 'HOLDOUT_CANDIDATE_REQUIRED')
    _need(all(type(metrics.get(k)) in (int, float) and math.isfinite(metrics[k])
              for k in ('mean_gross_return', 'sum_gross_return', 'factor_coverage'))
          and 0 <= metrics['factor_coverage'] <= 1,
          'Candidate development metrics are missing or nonfinite', 'HOLDOUT_CANDIDATE_REQUIRED')
    # A finite development measurement is required; its sign and magnitude do
    # not establish research quality or a profitable strategy.
    return {'id': identity, 'expression': original['expression'],
            'candidate_digest': digest(original), 'development_result_digest': result_digest}


def _development(connection, request):
    """Resolve the actual typed development output, never caller measurements."""
    from ..daily_workflow_v2 import verify_daily_run
    binding = assert_revision(connection, request['revision_id'])
    revision = connection.execute('SELECT * FROM revisions WHERE id=?', (request['revision_id'],)).fetchone()
    task = json.loads(revision['task'])
    run = connection.execute('SELECT * FROM runs WHERE id=?', (request['run_id'],)).fetchone()
    attempt = connection.execute('SELECT * FROM attempts WHERE id=?', (request['attempt_id'],)).fetchone()
    _need(run is not None and attempt is not None and run['revision_id'] == request['revision_id']
          and run['attempt_id'] == attempt['id'] and attempt['run_id'] == run['id']
          and run['status'] == attempt['status'] == 'completed', 'Freeze requires the exact completed original development run', 'HOLDOUT_DEVELOPMENT_REQUIRED')
    folder = Path(attempt['output_dir'])
    verified = verify_daily_run(folder, expected_task=task, require_current_code=True)
    _need(verified['valid'] is True and verified['final_numeric_rows_parsed'] == 0
          and verified['evaluation_scope'] == 'development_prefix_validation_only'
          and verified['state_digest'] == request['expected_result_digest'] == attempt['state_digest'],
          'Development proof is incomplete or differs from the original attempt', 'HOLDOUT_DEVELOPMENT_CHANGED')
    state = read_metadata(folder / 'state.json', 8 * 1024 * 1024)
    manifest = read_metadata(folder / 'manifest.json', 1024 * 1024)
    _need(state['status'] == 'completed' and digest(state) == verified['state_digest'], 'Development state changed')
    by_id = {c['id']: c for c in state['candidates']}
    original = {c['id']: c for c in task['candidates']}
    selected = []
    for identity in request['candidate_ids']:
        _need(identity in by_id and identity in original, 'Final candidate has no original development identity', 'HOLDOUT_CANDIDATE_REQUIRED')
        selected.append(_evaluated_candidate(folder, by_id[identity], original[identity], verified, task['evaluation']))
    registration = connection.execute('SELECT * FROM daily_snapshot_registrations WHERE id=?', (binding['registration_id'],)).fetchone()
    from .daily_snapshot_guard import _verify_declared
    contract, boundary = _verify_declared(connection, dict(registration))
    config = contract.to_dict()['evaluation']
    keys = [dict(row) for row in connection.execute('SELECT date,asset FROM daily_snapshot_guard_keys WHERE registration_id=? AND role=\'test\' ORDER BY date,asset', (binding['registration_id'],))]
    _need(keys, 'Final period has no independently declared keys')
    return {'binding': binding, 'binding_digest': digest(binding), 'origin': registration['origin'],
            'development_state_digest': verified['state_digest'], 'development_manifest_sha256': verified['manifest_sha256'],
            'development_code_digest': manifest['code']['digest'], 'candidates': selected, 'candidate_policy_digest': digest(selected),
            'evaluation': config, 'scope_digest': digest(keys), 'final_keys': keys,
            'read_scope': {'development_warmup': 'declared_prefix', 'final_keys_digest': digest(keys),
                           'all_final_candidates_frozen': True, 'extra_numeric_tail_allowed': False},
            'registration_digest': registration['registration_digest'], 'raw_sha256': registration['raw_sha256']}


def freeze_candidates(store, **kwargs):
    request = DailyHoldoutFreezeCreate.model_validate(kwargs).model_dump()
    with transaction(store.db_path) as connection:
        previous = connection.execute('SELECT * FROM daily_holdout_freezes WHERE idempotency_key=?', (request['idempotency_key'],)).fetchone()
        if previous:
            value = _record(previous)
            _need(value['request'] == request, 'Freeze key cannot replace candidate policy', 'IDEMPOTENCY_CONFLICT')
            return value
        actual = _development(connection, request)
        value = {'id': 'daily_holdout_freeze_' + digest(request), 'request': request,
                 'program_id': request['program_id'], 'revision_id': request['revision_id'],
                 'run_id': request['run_id'], 'attempt_id': request['attempt_id'],
                 'registration_id': actual['binding']['registration_id'], 'created_at': _now().isoformat(), **actual}
        connection.execute('INSERT INTO daily_holdout_freezes VALUES(?,?,?,?,?,?,?,?,?)',
            (value['id'], request['idempotency_key'], request['program_id'], request['revision_id'], request['run_id'],
             request['attempt_id'], value['registration_id'], json_text(value), digest(value)))
        return value


def assert_freeze(connection, identity, *, current=True):
    row = connection.execute('SELECT * FROM daily_holdout_freezes WHERE id=?', (identity,)).fetchone()
    value = _record(row)
    _need(value['id'] == row['id'] == 'daily_holdout_freeze_' + digest(value['request'])
          and all(value[k] == row[k] for k in ('program_id','revision_id','run_id','attempt_id','registration_id')), 'Freeze business index differs')
    if current:
        actual = _development(connection, value['request'])
        _need(all(value[k] == v for k, v in actual.items()), 'Frozen source, code, expressions or development result changed', 'HOLDOUT_FREEZE_CHANGED')
    return value


def create_holdout_permission(store, **kwargs):
    """Local helper only; no HTTP path mints a final read permission."""
    request = DailyHoldoutPermissionCreate.model_validate(kwargs).model_dump()
    with transaction(store.db_path) as connection:
        freeze = assert_freeze(connection, request['freeze_id'])
        previous = connection.execute('SELECT * FROM daily_holdout_permissions WHERE freeze_id=?', (freeze['id'],)).fetchone()
        if previous:
            value = _record(previous)
            _need(value['request'] == request, 'Final permission cannot be reset or upgraded', 'IDEMPOTENCY_CONFLICT')
            return value
        # Current registration v1 is engineering-fixture-only. R1 must supply a
        # new verified real qualification adapter before this branch can open.
        _need(request['authorization_kind'] == 'fixture' and freeze['origin'] == 'engineering_fixture',
              'Current real source is not qualified for a final numerical read', 'HOLDOUT_REAL_SOURCE_UNQUALIFIED')
        value = {'id': 'daily_holdout_permission_' + digest(request), 'freeze_id': freeze['id'], 'request': request,
                 'freeze_digest': digest(freeze), 'workspace_id': connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()[0],
                 'created_at': _now().isoformat(), 'deadline_at': (_now() + timedelta(seconds=request['max_seconds'])).isoformat()}
        connection.execute('INSERT INTO daily_holdout_permissions VALUES(?,?,?,?,?)',
            (value['id'], freeze['id'], request['authorization_kind'], json_text(value), digest(value)))
        return value


def _events(connection, exposure):
    result = []
    previous = digest(exposure)
    for row in connection.execute('SELECT * FROM daily_holdout_events WHERE exposure_id=? ORDER BY sequence', (exposure['id'],)):
        value = _record(row)
        _need(value['sequence'] == row['sequence'] == len(result) + 1 and value['previous_digest'] == previous, 'Final exposure event chain changed')
        result.append(value)
        previous = digest(value)
    return result


def reserve_final(connection, freeze_id, permission_id, idempotency_key):
    """Commit this reservation before calling any final numeric parser."""
    _need(connection.in_transaction, 'Final reservation requires the original host transaction')
    freeze = assert_freeze(connection, freeze_id)
    permission = _record(connection.execute('SELECT * FROM daily_holdout_permissions WHERE id=?', (permission_id,)).fetchone())
    _need(permission['freeze_id'] == freeze_id and permission['freeze_digest'] == digest(freeze), 'Final permission names another freeze')
    _need(permission['workspace_id'] == connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()[0], 'Final permission belongs to another workspace', 'HOLDOUT_WORKSPACE_MISMATCH')
    old = connection.execute('SELECT * FROM daily_holdout_exposures WHERE program_id=? OR freeze_id=?', (freeze['program_id'], freeze_id)).fetchone()
    if old:
        exposure = _record(old)
        _need(exposure['freeze_id'] == freeze_id and exposure['permission_id'] == permission_id
              and exposure['idempotency_key'] == idempotency_key, 'Program final exposure was already permanently spent', 'HOLDOUT_EXPOSURE_SPENT')
        return {**exposure, 'replayed': True}
    _need(_now() < datetime.fromisoformat(permission['deadline_at']), 'Final permission expired', 'HOLDOUT_PERMISSION_EXPIRED')
    for prior in connection.execute('SELECT id FROM daily_holdout_exposures'):
        _assert_exposure(connection, prior['id'])
    for key in freeze['final_keys']:
        prior = connection.execute('SELECT exposure_id FROM daily_holdout_exposure_keys WHERE date=? AND asset=? LIMIT 1', (key['date'],key['asset'])).fetchone()
        _need(prior is None, 'Final asset/date was already exposed; source equivalence is unverified', 'HOLDOUT_EXPOSURE_SPENT')
    value = {'id': 'daily_final_exposure_' + digest({'freeze': freeze_id, 'key': idempotency_key}),
             'freeze_id': freeze_id, 'permission_id': permission_id, 'program_id': freeze['program_id'],
             'freeze_digest': digest(freeze), 'scope_digest': freeze['scope_digest'], 'idempotency_key': idempotency_key,
             'created_at': _now().isoformat(), 'exposure_reserved': True}
    connection.execute('INSERT INTO daily_holdout_exposures VALUES(?,?,?,?,?,?)',
        (value['id'], freeze_id, freeze['program_id'], permission_id, json_text(value), digest(value)))
    connection.executemany('INSERT INTO daily_holdout_exposure_keys VALUES(?,?,?)',
        ((value['id'], k['date'], k['asset']) for k in freeze['final_keys']))
    return {**value, 'replayed': False}


def _assert_exposure(connection, identity):
    row = connection.execute('SELECT * FROM daily_holdout_exposures WHERE id=?', (identity,)).fetchone()
    exposure = _record(row)
    freeze = assert_freeze(connection, exposure['freeze_id'], current=False)
    _need(exposure['id'] == row['id'] and exposure['freeze_digest'] == digest(freeze)
          and all(exposure[k] == row[k] for k in ('freeze_id','permission_id','program_id')), 'Exposure business anchor changed')
    keys = [dict(row) for row in connection.execute('SELECT date,asset FROM daily_holdout_exposure_keys WHERE exposure_id=? ORDER BY date,asset', (identity,))]
    _need(keys == freeze['final_keys'] and digest(keys) == exposure['scope_digest'], 'Permanent exposed key inventory changed')
    return exposure, freeze


def _append(connection, exposure, kind, *, numeric_rows=None, result_digest=None, code=None):
    events = _events(connection, exposure)
    event = {'sequence': len(events)+1, 'kind': kind, 'previous_digest': digest(events[-1]) if events else digest(exposure),
             'created_at': _now().isoformat(), 'numeric_rows': numeric_rows, 'result_digest': result_digest, 'code': code}
    connection.execute('INSERT INTO daily_holdout_events VALUES(?,?,?,?)',
                       (exposure['id'], event['sequence'], json_text(event), digest(event)))
    return event


def consume_final_read(connection, exposure_id):
    _need(connection.in_transaction, 'Final read intent requires the original host transaction')
    exposure, freeze = _assert_exposure(connection, exposure_id)
    _need(not _events(connection, exposure), 'Final read intent was already consumed; no retry is permitted', 'HOLDOUT_READ_SPENT')
    permission = _record(connection.execute('SELECT * FROM daily_holdout_permissions WHERE id=?', (exposure['permission_id'],)).fetchone())
    _need(permission['workspace_id'] == connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()[0],
          'Restored final permission belongs to another workspace', 'HOLDOUT_WORKSPACE_MISMATCH')
    _need(_now() < datetime.fromisoformat(permission['deadline_at']), 'Final permission expired before numeric read', 'HOLDOUT_PERMISSION_EXPIRED')
    assert_freeze(connection, freeze['id'])
    return _append(connection, exposure, 'read_started')


def record_final_observation(connection, exposure_id, *, status, numeric_rows=None, result_digest=None, code=None):
    exposure, freeze = _assert_exposure(connection, exposure_id)
    events = _events(connection, exposure)
    _need(events and events[0]['kind'] == 'read_started', 'No durable final read intent exists')
    _need(status in {'completed','failed','interrupted'}, 'Unknown final observation status')
    if status == 'completed':
        _need(type(numeric_rows) is int and numeric_rows == len(freeze['final_keys']) and isinstance(result_digest,str) and len(result_digest)==64,
              'Completed final observation requires exact actual read count and result digest')
    else:
        _need(numeric_rows is None, 'Failed or interrupted final read keeps unknown numeric exposure')
    if len(events) > 1:
        last = events[-1]
        _need((last['kind'],last['numeric_rows'],last['result_digest'],last['code']) == (status,numeric_rows,result_digest,code), 'Final observation cannot be replaced')
        return last
    return _append(connection, exposure, status, numeric_rows=numeric_rows, result_digest=result_digest, code=code)


def holdout_status(store, freeze_id):
    """Counts only; this read never displays or parses final prices/metrics."""
    with closing(connect(store.db_path)) as connection:
        freeze = assert_freeze(connection, freeze_id, current=False)
        row = connection.execute('SELECT * FROM daily_holdout_exposures WHERE freeze_id=?', (freeze_id,)).fetchone()
        if row is None:
            return {'freeze_id':freeze_id,'status':'not_started','exposure_reserved':False,'numeric_rows':0,'real_authorized':False}
        exposure, _ = _assert_exposure(connection, row['id'])
        events = _events(connection, exposure)
        last = events[-1] if events else None
        # Reserved-but-not-started is provably zero; entered without a final
        # stop receipt is unknown and must never be reported as zero/fresh.
        return {'freeze_id':freeze_id,'exposure_id':exposure['id'],'exposure_reserved':True,
                'status':last['kind'] if last else 'reserved','numeric_rows':last['numeric_rows'] if last else 0,
                'result_digest':last['result_digest'] if last else None,'real_authorized':False,'retry_allowed':False}
