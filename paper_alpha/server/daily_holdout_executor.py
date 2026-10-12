"""One original, fixture-only final observation; permanent H0 authority owns reads.

New code leaves development identities and schema27 unchanged. A parent owns
the host lease and program lock, commits both exposure and read intent, then
starts one bounded child. Recovery never starts a child or reloads prices.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..daily_adapter import (_open_regular, _stable, _number, MAX_LINE_BYTES,
                             content_row_fingerprint)
from ..daily_contract import COLUMNS, FIELDS, CANONICAL_VERSION, MAX_RAW_BYTES, parse_contract
from ..evaluation import (_calendar, _evaluate_validation, validate_config,
                          MAX_PANEL_CELLS, MAX_EVALUATION_CELLS)
from ..expressions import compute_expression, validate_expression
from ..storage import digest, json_text
from .db import connect, transaction, audit_trigger_definitions
from .daily_holdout import (HoldoutError, _need, _record, _events, _assert_exposure,
                           assert_freeze, reserve_final, consume_final_read,
                           record_final_observation, holdout_status)
from .daily_snapshots import DailySnapshots, _strict_json
from .maintenance import workspace_lease

VERSION = 'daily-fixture-final-executor-v1'
BASELINE = {'id': 'fixed_close_return_v1', 'expression': 'returns',
            'semantics': 'close[t] / close[t-1] - 1, available after session t close',
            'selection': 'fixed before final read; no repair or tuning'}
NEW_CODE_FILES = ('paper_alpha/server/daily_holdout_executor.py',
                  'scripts/execute_daily_holdout_fixture.py')
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc)


def _safe(path):
    path = Path(path).absolute()
    _need('..' not in path.parts and not any(p.is_symlink() for p in (path, *path.parents))
          and path.resolve() == path, 'Canonical local paths without symbolic links required')
    return path


def _read(path, limit=MAX_ARTIFACT_BYTES):
    path = _safe(path)
    _, stream, before = _open_regular(path, limit)
    with stream:
        raw = stream.read(limit + 1)
        _need(len(raw) == before.st_size and len(raw) <= limit, 'Bounded file changed')
        _stable(path, stream, before)
    return raw


def _json(path):
    return _strict_json(_read(path))


def _sha(path):
    return hashlib.sha256(_read(path)).hexdigest()


def _immutable(path, raw):
    path = _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path, value):
    _immutable(path, json_text(value).encode())


def _store(store):
    root = _safe(store.root)
    db_path = _safe(store.db_path)
    _need(db_path == root / 'workbench.sqlite3' and db_path.is_file()
          and not (root / '.restore-incomplete').exists(), 'Existing initialized workspace required')
    with closing(connect(db_path)) as connection:
        version = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        _need(version is not None and version[0] == '27', 'Original schema27 workspace required')
        triggers = {row['name']: row['sql'] for row in connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger'")}
        expected = audit_trigger_definitions()
        normalize = lambda value: re.sub(r'\s+', ' ', value.strip().rstrip(';')).lower()
        _need(set(triggers) == set(expected) and all(normalize(triggers[k]) == normalize(expected[k])
              for k in expected), 'Original protection triggers changed')
    return SimpleNamespace(root=root, db_path=db_path)


@contextmanager
def _owner(store, program_id):
    folder = _safe(store.root / '.daily-holdout-locks')
    folder.mkdir(exist_ok=True)
    path = _safe(folder / (digest(program_id) + '.lock'))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe final ownership lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HoldoutError('Original final attempt still owns this program', 'HOLDOUT_BUSY') from exc
        yield fd
    finally:
        os.close(fd)


def _folder(store, exposure_id):
    _need(type(exposure_id) is str and re.fullmatch(r'daily_final_exposure_[a-f0-9]{64}', exposure_id),
          'Invalid original exposure identity')
    return _safe(store.root / 'daily_holdouts' / exposure_id / 'attempt-1')


def _identity():
    from ..daily_workflow_v2 import CODE_FILES
    root = Path(__file__).resolve().parents[2]
    files = {name: _sha(root / name) for name in (*CODE_FILES, *NEW_CODE_FILES)}
    value = {'version': VERSION, 'files': files, 'python': platform.python_version(),
             'numpy': np.__version__, 'pandas': pd.__version__, 'baseline': deepcopy(BASELINE)}
    return {**value, 'digest': digest(value)}


def _snapshot_code(folder, code):
    root = Path(__file__).resolve().parents[2]
    for name, expected in code['files'].items():
        raw = _read(root / name)
        _need(hashlib.sha256(raw).hexdigest() == expected, 'Final source changed while sealing')
        _immutable(folder / 'source' / name, raw)


def _preflight(connection, freeze, permission):
    _need(freeze['origin'] == 'engineering_fixture'
          and permission['request']['authorization_kind'] == 'fixture',
          'H1 accepts engineering fixtures and fixture permissions only', 'HOLDOUT_REAL_SOURCE_UNQUALIFIED')
    _need(permission['freeze_id'] == freeze['id'] and permission['freeze_digest'] == digest(freeze),
          'Permission differs from original freeze')
    _need(permission['workspace_id'] == connection.execute(
        "SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()[0],
        'Restored workspace cannot spend original permission', 'HOLDOUT_WORKSPACE_MISMATCH')
    registration = dict(connection.execute('SELECT * FROM daily_snapshot_registrations WHERE id=?',
                                         (freeze['registration_id'],)).fetchone())
    contract = parse_contract(json.loads(registration['contract_json']))
    dates = list(contract.calendar)
    config = freeze['evaluation']
    _need(config == contract.to_dict()['evaluation'], 'Frozen final configuration changed')
    first = dates.index(config['splits']['test']['start'])
    last = dates.index(config['splits']['test']['end'])
    _need(first == dates.index(contract.cutoff) + 1,
          'Final start must immediately follow the declared development prefix; numeric gaps are forbidden',
          'HOLDOUT_NUMERIC_GAP')
    expected = [{'date': day, 'asset': asset} for day in dates[first:last + 1] for asset in contract.assets]
    _need(expected == freeze['final_keys'] and digest(expected) == freeze['scope_digest'], 'Final scope changed')
    _need((last + 1) * len(contract.assets) <= MAX_PANEL_CELLS
          and (last - first - 1) * len(contract.assets) <= MAX_EVALUATION_CELLS,
          'Final panels/evaluation exceed engine bounds')
    _need(not any(event['date'] <= dates[last] for event in contract.to_dict()['known_events']),
          'Unsupported event in final or historical read scope', 'HOLDOUT_UNSUPPORTED_EVENT')
    task = json.loads(connection.execute('SELECT task FROM revisions WHERE id=?',
                                        (freeze['revision_id'],)).fetchone()[0])
    candidates = freeze['candidates']
    _need(1 <= len(candidates) <= 2 and len(candidates) <= task['budget']['max_candidates'],
          'Frozen candidate budget exceeded')
    _need(BASELINE['id'] not in {c['id'] for c in candidates},
          'A frozen candidate cannot replace the fixed baseline identity', 'HOLDOUT_BASELINE_COLLISION')
    expressions = [c['expression'] for c in candidates] + [BASELINE['expression']]
    for expression in expressions:
        validate_expression(expression, set(FIELDS) | {'returns'})
    calls = len(expressions)
    _need(calls <= task['budget']['max_tool_calls'], 'Candidate and baseline cumulative tool budget exceeded',
          'HOLDOUT_BUDGET_EXHAUSTED')
    remaining = (datetime.fromisoformat(permission['deadline_at']) - _now()).total_seconds()
    seconds = min(task['budget']['max_seconds'], permission['request']['max_seconds'], remaining)
    _need(seconds > 0, 'Original final permission expired', 'HOLDOUT_PERMISSION_EXPIRED')
    return registration, contract, {'max_seconds': float(seconds), 'max_tool_calls': calls,
                                   'max_candidates': len(candidates), 'baseline_calls': 1,
                                   'max_attempts_per_expression': 1, 'max_repairs': 0}


def _recover_locked(store, freeze_id):
    status = holdout_status(store, freeze_id)
    if status['status'] == 'completed':
        return verify_fixture_holdout(store, freeze_id)
    if status['status'] == 'read_started':
        with transaction(store.db_path) as connection:
            record_final_observation(connection, status['exposure_id'], status='interrupted', code='OUTCOME_UNKNOWN')
        status = holdout_status(store, freeze_id)
    return {**status, 'numeric_rows_read_by_this_call': 0, 'numeric_parser_calls': 0,
            'computation_calls': 0, 'retry_allowed': False}


def recover_fixture_holdout(store, freeze_id):
    store = _store(store)
    with workspace_lease(store.root, create=False):
        with closing(connect(store.db_path)) as connection:
            freeze = assert_freeze(connection, freeze_id, current=False)
        with _owner(store, freeze['program_id']):
            return _recover_locked(store, freeze_id)


def execute_fixture_holdout(store, freeze_id, permission_id, idempotency_key):
    store = _store(store)
    _need(type(idempotency_key) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', idempotency_key),
          'Bounded final idempotency key required')
    request = {'freeze_id': freeze_id, 'permission_id': permission_id, 'idempotency_key': idempotency_key}
    with workspace_lease(store.root, create=False) as lease_fd:
        with closing(connect(store.db_path)) as connection:
            freeze = assert_freeze(connection, freeze_id, current=False)
        with _owner(store, freeze['program_id']) as owner_fd:
            with closing(connect(store.db_path)) as connection:
                old = connection.execute('SELECT id FROM daily_holdout_exposures WHERE freeze_id=? OR program_id=?',
                                         (freeze_id, freeze['program_id'])).fetchone()
                if old:
                    exposure, old_freeze = _assert_exposure(connection, old['id'])
                    _need(exposure['freeze_id'] == freeze_id and exposure['permission_id'] == permission_id
                          and exposure['idempotency_key'] == idempotency_key,
                          'Program exposure is permanently spent', 'HOLDOUT_EXPOSURE_SPENT')
                    return {**_recover_locked(store, old_freeze['id']), 'replayed': True}
                freeze = assert_freeze(connection, freeze_id)
                permission = _record(connection.execute('SELECT * FROM daily_holdout_permissions WHERE id=?',
                                                        (permission_id,)).fetchone())
                registration, contract, budget = _preflight(connection, freeze, permission)
            # Bytes only: source drift is denied before exposure/read intent.
            paths = DailySnapshots(store)._check_inputs(registration)
            code = _identity()
            with transaction(store.db_path) as connection:
                exposure = reserve_final(connection, freeze_id, permission_id, idempotency_key)
            _need(not exposure['replayed'], 'Original reservation already exists; recover metadata only')
            folder = _folder(store, exposure['id'])
            try:
                folder.mkdir(parents=True, exist_ok=False)
                descriptor = {'schema_version': 1, 'version': VERSION, 'request': request,
                              'exposure_id': exposure['id'], 'freeze': freeze, 'freeze_digest': digest(freeze),
                              'permission_digest': digest(permission), 'workspace_id': permission['workspace_id'],
                              'baseline': deepcopy(BASELINE), 'baseline_policy_digest': digest(BASELINE),
                              'budget': budget, 'code': code, 'parent_pid': os.getpid(),
                              'deadline_at': min(_now() + timedelta(seconds=budget['max_seconds']),
                                                 datetime.fromisoformat(permission['deadline_at'])).isoformat()}
                _write_json(folder / 'descriptor.json', descriptor)
                descriptor_sha = _sha(folder / 'descriptor.json')
                _snapshot_code(folder, code)
                # Both transactions are complete before the child can open numeric rows.
                with transaction(store.db_path) as connection:
                    consume_final_read(connection, exposure['id'])
                _run_child(store, folder, descriptor, descriptor_sha, lease_fd, owner_fd)
                _need(_identity() == code, 'Final implementation changed during execution')
                report = _verify_artifacts(store, exposure, freeze, expected_descriptor_sha=descriptor_sha)
                with transaction(store.db_path) as connection:
                    record_final_observation(connection, exposure['id'], status='completed',
                                             numeric_rows=report['read_scope']['final_numeric_rows_parsed'],
                                             result_digest=digest(report))
                return {**verify_fixture_holdout(store, freeze_id), 'replayed': False,
                        'numeric_rows_read_by_this_call': report['read_scope']['final_numeric_rows_parsed'],
                        'numeric_parser_calls': 1, 'computation_calls': budget['max_tool_calls']}
            except BaseException as exc:
                status = holdout_status(store, freeze_id)
                if status['status'] == 'read_started':
                    interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit, TimeoutError, subprocess.TimeoutExpired))
                    code_name = 'HOLDOUT_TIMEOUT' if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else getattr(exc, 'code', 'HOLDOUT_EXECUTION_FAILED')
                    with transaction(store.db_path) as connection:
                        record_final_observation(connection, exposure['id'],
                                                 status='interrupted' if interrupted else 'failed', code=code_name)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise HoldoutError('Final attempt stopped; exposure remains spent. Inspect/recover the original attempt.',
                                   getattr(exc, 'code', 'HOLDOUT_EXECUTION_FAILED')) from exc


def _run_child(store, folder, descriptor, descriptor_sha, lease_fd, owner_fd):
    remaining = (datetime.fromisoformat(descriptor['deadline_at']) - _now()).total_seconds()
    if remaining <= 0:
        raise TimeoutError('Cumulative final wall budget exhausted before child')
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, '-m', 'paper_alpha.server.daily_holdout_executor', '--worker',
               str(store.root), descriptor['exposure_id'], descriptor_sha, str(owner_fd)]
    with (folder / 'worker.log').open('xb') as log:
        log.write(b'Original fixture final child; permanent read intent already committed.\n')
        log.flush()
        process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                   pass_fds=(lease_fd, owner_fd), start_new_session=True)
        try:
            returncode = process.wait(timeout=remaining)
            if returncode == 124:
                raise TimeoutError('Original final child exhausted the cumulative deadline')
            _need(returncode == 0, 'Final child failed; inspect original worker.log')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            log.flush()
            os.fsync(log.fileno())


def _read_final_panels(store, descriptor):
    """Only worker authority enters this parser; one prefix+final read, no tail decode."""
    with closing(connect(store.db_path)) as connection:
        exposure, freeze = _assert_exposure(connection, descriptor['exposure_id'])
        events = _events(connection, exposure)
        _need(len(events) == 1 and events[0]['kind'] == 'read_started', 'Numeric read intent is not fresh')
        _need(digest(freeze) == descriptor['freeze_digest'] and freeze == descriptor['freeze'], 'Descriptor freeze changed')
        permission = _record(connection.execute('SELECT * FROM daily_holdout_permissions WHERE id=?',
                                                (exposure['permission_id'],)).fetchone())
        _need(digest(permission) == descriptor['permission_digest'], 'Descriptor permission changed')
        registration, contract, _budget = _preflight(connection, freeze, permission)
        assert_freeze(connection, freeze['id'])
    paths = DailySnapshots(store)._check_inputs(registration)
    dates = list(contract.calendar)
    last = dates.index(freeze['evaluation']['splits']['test']['end'])
    allowed_dates = dates[:last + 1]
    final_start = freeze['evaluation']['splits']['test']['start']
    final_keys, fingerprints, values = [], [], []
    path, stream, before = _open_regular(paths['csv'], MAX_RAW_BYTES)
    with stream:
        # Whole-file hash and physical-line count do not decode the opaque post-test tail.
        hasher, size, lines, ending = hashlib.sha256(), 0, 0, b''
        while block := stream.read(64 * 1024):
            hasher.update(block); size += len(block); lines += block.count(b'\n'); ending = block[-1:]
        _need(size == registration['raw_bytes'] and hasher.hexdigest() == freeze['raw_sha256'], 'Final raw bytes changed')
        _need(lines == 1 + len(dates) * len(contract.assets) and ending == b'\n', 'Final physical grid count changed')
        stream.seek(0)
        _need(stream.readline(MAX_LINE_BYTES + 1).rstrip(b'\r\n') == ','.join(COLUMNS).encode(), 'Final header changed')
        for index, (day, asset) in enumerate((d, a) for d in allowed_dates for a in contract.assets):
            raw = stream.readline(MAX_LINE_BYTES + 1)
            _need(bool(raw) and len(raw) <= MAX_LINE_BYTES and raw.endswith(b'\n'), 'Invalid bounded final line')
            text = raw.rstrip(b'\r\n').decode('ascii')
            _need('"' not in text and '\x00' not in text and '\r' not in text, 'Unsupported final CSV layout')
            cells = text.split(',')
            _need(len(cells) == len(COLUMNS) and cells[:2] == [day, asset], 'Final key/grid order changed')
            row = [_number(token, field, index + 2, day, asset) for field, token in zip(FIELDS, cells[2:])]
            opening, high, low, close, _volume = row
            _need(low <= min(opening, close) <= max(opening, close) <= high, 'Invalid final OHLC bounds')
            values.append(row)
            fingerprints.append({'date': day, 'asset': asset, 'row_sha256': content_row_fingerprint(day, asset, row)})
            if day >= final_start:
                final_keys.append({'date': day, 'asset': asset})
        _stable(path, stream, before)
    _need(final_keys == freeze['final_keys'] and digest(final_keys) == exposure['scope_digest'], 'Parsed final scope differs from permanent exposure')
    prefix_rows = len(contract.development_calendar) * len(contract.assets)
    _need(digest({'version': CANONICAL_VERSION, 'rows': fingerprints[:prefix_rows]})
          == freeze['binding']['development_content_digest'], 'Parsed lookback differs from frozen development observations')
    grid = np.asarray(values, dtype=np.float64).reshape(len(allowed_dates), len(contract.assets), len(FIELDS))
    panels = {field: pd.DataFrame(grid[:, :, i], index=pd.DatetimeIndex(allowed_dates, name='date'),
                                 columns=contract.assets) for i, field in enumerate(FIELDS)}
    with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
        panels['returns'] = panels['close'].pct_change(fill_method=None)
    _need(np.isfinite(panels['returns'].iloc[1:].to_numpy()).all(), 'Nonfinite final derived return')
    scope = {'numeric_parser_calls': 1, 'development_numeric_rows_parsed': len(contract.development_calendar) * len(contract.assets),
             'final_numeric_rows_parsed': len(final_keys), 'final_keys_digest': digest(final_keys),
             'read_start': allowed_dates[0], 'read_end': allowed_dates[-1],
             'post_test_numeric_rows_parsed': 0, 'post_test_tail': 'opaque bytes only',
             'exposure_and_read_started_committed_before_parse': True,
             'content_digest': digest(fingerprints)}
    return panels, contract, scope


def _compute_attempt(store, folder, descriptor):
    started = time.monotonic()
    _need(descriptor['version'] == VERSION and descriptor['baseline'] == BASELINE
          and descriptor['baseline_policy_digest'] == digest(BASELINE), 'Final baseline/version changed')
    _need(_identity() == descriptor['code'], 'Final code differs from sealed descriptor')
    panels, contract, scope = _read_final_panels(store, descriptor)
    config = validate_config(descriptor['freeze']['evaluation'], contract.calendar)
    dates = _calendar(panels['open'].index)
    expected = list(contract.calendar)[:list(contract.calendar).index(config['splits']['test']['end']) + 1]
    _need(dates == expected and list(panels['open'].columns) == list(contract.assets), 'Final axes changed')
    metadata = {'data_kind': 'engineering_fixture', 'scope': 'original_once_final_test',
                'registration_id': descriptor['freeze']['registration_id'],
                'scope_digest': descriptor['freeze']['scope_digest'], 'real_source_ready': False}
    for panel in panels.values():
        panel.attrs['market_metadata'] = deepcopy(metadata)
    computations = [dict(c, role='candidate') for c in descriptor['freeze']['candidates']]
    computations.append({**deepcopy(BASELINE), 'role': 'fixed_baseline'})
    results = {}
    for number, item in enumerate(computations, 1):
        _need(number <= descriptor['budget']['max_tool_calls'], 'Cumulative final tool budget exceeded')
        _need(_now() < datetime.fromisoformat(descriptor['deadline_at']), 'Cumulative final wall budget exhausted')
        factor = compute_expression(item['expression'], panels)
        result = _evaluate_validation(factor, panels, config, 'test', dates)
        result.update(expression=item['expression'], candidate_id=item['id'], role=item['role'],
                      evaluation_scope='fixture_once_final_test', final_numeric_rows_used=scope['final_numeric_rows_parsed'],
                      final_keys_digest=scope['final_keys_digest'])
        result['limitations'] = ['Engineering fixture results test software behavior only; scientific_score is null.',
                                'Gross rank portfolio excludes costs, fills, borrow, liquidity and capacity.',
                                'Trailing development history is allowed; signal, entry and exit remain within test.']
        name = f'results/computation-{number}.json'
        _write_json(folder / name, result)
        results[item['id']] = {'role': item['role'], 'path': name, 'digest': digest(result),
                              'expression': item['expression'], 'metrics': result['metrics']}
    _need(_identity() == descriptor['code'], 'Source changed during final computations')
    _write_json(folder / 'read-scope.json', scope)
    report = {'schema_version': 1, 'version': VERSION, 'status': 'completed',
              'exposure_id': descriptor['exposure_id'], 'freeze_digest': descriptor['freeze_digest'],
              'descriptor_digest': digest(descriptor), 'code_digest': descriptor['code']['digest'],
              'candidate_policy_digest': descriptor['freeze']['candidate_policy_digest'],
              'baseline_policy_digest': descriptor['baseline_policy_digest'],
              'config_digest': digest(config), 'read_scope': scope, 'results': results,
              'computation_calls': len(computations), 'budget': descriptor['budget'],
              'elapsed_seconds': float(time.monotonic() - started),
              'scientific_score': None, 'real_source_ready': False}
    _write_json(folder / 'report.json', report)


def _inventory(folder):
    files = {}
    for path in sorted(folder.rglob('*')):
        _safe(path)
        _need(path.is_file() or path.is_dir(), 'Unsupported final artifact')
        if path.is_file() and path != folder / 'manifest.json':
            files[path.relative_to(folder).as_posix()] = _sha(path)
    return files


def _verify_artifacts(store, exposure, freeze, *, expected_descriptor_sha=None):
    folder = _folder(store, exposure['id'])
    descriptor = _json(folder / 'descriptor.json')
    descriptor_sha = _sha(folder / 'descriptor.json')
    if expected_descriptor_sha is not None:
        _need(descriptor_sha == expected_descriptor_sha, 'Original final descriptor changed')
    _need(descriptor['freeze'] == freeze and descriptor['freeze_digest'] == digest(freeze)
          and descriptor['exposure_id'] == exposure['id'], 'Original final artifact identity changed')
    _need(descriptor['request'] == {k: exposure[k] for k in ('freeze_id', 'permission_id', 'idempotency_key')},
          'Final attempt request changed')
    _need(descriptor['baseline'] == BASELINE and descriptor['baseline_policy_digest'] == digest(BASELINE), 'Baseline changed')
    code = descriptor['code']
    _need(digest({k: v for k, v in code.items() if k != 'digest'}) == code['digest'], 'Final code digest changed')
    manifest_path = folder / 'manifest.json'
    files = _inventory(folder)
    if manifest_path.exists():
        manifest = _json(manifest_path)
        _need(manifest == {'schema_version': 1, 'version': VERSION, 'descriptor_sha256': descriptor_sha,
                           'files': files}, 'Final artifact inventory/bytes changed')
    else:
        _need(expected_descriptor_sha is not None, 'Completed final manifest is missing')
    expected_files = {'descriptor.json', 'worker.log', 'read-scope.json', 'report.json'} | {
        'source/' + name for name in code['files']}
    expected_computations = [dict(c, role='candidate') for c in freeze['candidates']] + [{**BASELINE, 'role': 'fixed_baseline'}]
    expected_files |= {f'results/computation-{i}.json' for i in range(1, len(expected_computations) + 1)}
    _need(set(files) == expected_files, 'Closed final artifact inventory changed')
    for name, sha in code['files'].items():
        _need(files['source/' + name] == sha, 'Recorded final source snapshot changed')
    report, scope = _json(folder / 'report.json'), _json(folder / 'read-scope.json')
    _need(report['status'] == 'completed' and report['version'] == VERSION
          and report['exposure_id'] == exposure['id'] and report['freeze_digest'] == digest(freeze)
          and report['descriptor_digest'] == digest(descriptor) and report['code_digest'] == code['digest']
          and report['baseline_policy_digest'] == digest(BASELINE) and report['config_digest'] == digest(freeze['evaluation'])
          and report['candidate_policy_digest'] == freeze['candidate_policy_digest'], 'Final report bindings changed')
    _need(report['read_scope'] == scope and scope['numeric_parser_calls'] == 1
          and scope['final_numeric_rows_parsed'] == len(freeze['final_keys'])
          and scope['final_keys_digest'] == freeze['scope_digest'] and scope['post_test_numeric_rows_parsed'] == 0
          and scope['exposure_and_read_started_committed_before_parse'] is True,
          'Actual final parser receipt differs from permanent scope')
    _need(report['budget'] == descriptor['budget'] and report['computation_calls'] == len(expected_computations)
          and report['scientific_score'] is None and report['real_source_ready'] is False, 'Final budget/scope changed')
    # Historical witnessed v1 reports may predate elapsed measurement. Their
    # original report digest still owns readback; absence is never filled in.
    if 'elapsed_seconds' in report:
        _need(type(report['elapsed_seconds']) is float and math.isfinite(report['elapsed_seconds'])
              and 0 <= report['elapsed_seconds'] <= descriptor['budget']['max_seconds'], 'Actual final elapsed time is invalid')
    results = {}
    for i, item in enumerate(expected_computations, 1):
        name = f'results/computation-{i}.json'
        result = _json(folder / name)
        _need(result['expression'] == item['expression'] and result['candidate_id'] == item['id']
              and result['role'] == item['role'] and result['config'] == freeze['evaluation']
              and result['split'] == 'test' and result['evaluation_scope'] == 'fixture_once_final_test'
              and result['final_numeric_rows_used'] == len(freeze['final_keys'])
              and result['final_keys_digest'] == freeze['scope_digest'], 'Actual final computation binding changed')
        results[item['id']] = {'role': item['role'], 'path': name, 'digest': digest(result),
                              'expression': item['expression'], 'metrics': result['metrics']}
    _need(report['results'] == results, 'Reported final results differ from exact tool artifacts')
    if not manifest_path.exists():
        _write_json(manifest_path, {'schema_version': 1, 'version': VERSION,
                                   'descriptor_sha256': descriptor_sha, 'files': files})
    return report


def verify_fixture_holdout(store, freeze_id):
    """Historical readback; prices and computations are never re-opened."""
    store = _store(store)
    with workspace_lease(store.root, create=False):
        with closing(connect(store.db_path)) as connection:
            row = connection.execute('SELECT id FROM daily_holdout_exposures WHERE freeze_id=?', (freeze_id,)).fetchone()
            _need(row is not None, 'Original final exposure missing')
            exposure, freeze = _assert_exposure(connection, row['id'])
            events = _events(connection, exposure)
            _need(len(events) == 2 and events[-1]['kind'] == 'completed', 'No original completed final receipt')
            terminal = events[-1]
        report = _verify_artifacts(store, exposure, freeze)
        _need(digest(report) == terminal['result_digest']
              and terminal['numeric_rows'] == report['read_scope']['final_numeric_rows_parsed'],
              'Final report differs from permanent observation digest')
        return {'status': 'completed', 'verified': True, 'freeze_id': freeze_id,
                'exposure_id': exposure['id'], 'result_digest': digest(report),
                'numeric_rows': terminal['numeric_rows'], 'numeric_rows_read_by_this_call': 0,
                'numeric_parser_calls': 0, 'computation_calls': 0, 'retry_allowed': False,
                'report': report, 'artifact_dir': str(_folder(store, exposure['id']))}


def _worker(argv):
    _need(len(argv) == 4, 'Closed original final child arguments required')
    root, exposure_id, expected_sha, owner_value = argv
    store = _store(SimpleNamespace(root=Path(root), db_path=Path(root) / 'workbench.sqlite3'))
    folder = _folder(store, exposure_id)
    descriptor = _json(folder / 'descriptor.json')
    _need(_sha(folder / 'descriptor.json') == expected_sha and descriptor['parent_pid'] == os.getppid(),
          'Original parent/descriptor required')
    owner_fd = int(owner_value)
    expected_lock = store.root / '.daily-holdout-locks' / (digest(descriptor['freeze']['program_id']) + '.lock')
    info, lock_info = os.fstat(owner_fd), _safe(expected_lock).stat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
          and (info.st_dev, info.st_ino) == (lock_info.st_dev, lock_info.st_ino), 'Original inherited final owner required')
    remaining = (datetime.fromisoformat(descriptor['deadline_at']) - _now()).total_seconds()
    _need(remaining > 0, 'Original final deadline exhausted')
    def deadline(_signum, _frame):
        raise TimeoutError('Final cumulative wall budget exhausted')
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        _compute_attempt(store, folder, descriptor)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    _need(len(sys.argv) >= 2 and sys.argv[1] == '--worker', 'Internal inherited child only; use the fixture CLI')
    try:
        _worker(sys.argv[2:])
    except TimeoutError:
        raise SystemExit(124)
