"""Independent host authority and durable scopes for bounded OHLC development.

This lane never authenticates a provider, grants final-period access or opens
market originals. Existing fixture declarations keep their original semantics.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .storage import digest, json_text
from .server.db import connect, transaction

VERSION = 'real-daily-local-admission-v1'
FACTS = ('provider', 'rights', 'calendar', 'units', 'universe', 'events', 'vintage', 'raw_identity')
HOST_TABLES = ('rdl_assessments', 'rdl_approvals', 'rdl_revocations', 'rdl_reservations', 'rdl_keys', 'rdl_registrations', 'rdl_normalize_attempts')
MAX_METADATA = 2 * 1024 * 1024
MAX_RAW = 2 * 1024 * 1024
MAX_HISTORY = 1000
CODE_FILES = ('paper_alpha/real_daily_admission.py', 'paper_alpha/real_daily_normalizer.py',
              'paper_alpha/real_daily_method.py', 'paper_alpha/daily_event_review.py',
              'paper_alpha/evaluation.py', 'paper_alpha/expressions.py',
              'paper_alpha/vendor/operators.py', 'paper_alpha/storage.py',
              'paper_alpha/daily_contract.py', 'paper_alpha/market_diagnostics.py',
              'paper_alpha/server/db.py', 'paper_alpha/server/daily_snapshot_schema.py',
              'paper_alpha/server/daily_snapshot_guard.py')
SCHEMA = '''
CREATE TABLE rdl_assessments(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,assessment_digest TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE rdl_approvals(id TEXT PRIMARY KEY,assessment_id TEXT NOT NULL REFERENCES rdl_assessments(id),approval_digest TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE rdl_revocations(approval_id TEXT PRIMARY KEY REFERENCES rdl_approvals(id),payload_json TEXT NOT NULL,revocation_digest TEXT NOT NULL);
CREATE TABLE rdl_reservations(id TEXT PRIMARY KEY,approval_id TEXT NOT NULL REFERENCES rdl_approvals(id),idempotency_key TEXT NOT NULL UNIQUE,reservation_digest TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,registration_id TEXT,owner_token TEXT,error_code TEXT);
CREATE TABLE rdl_keys(reservation_id TEXT NOT NULL REFERENCES rdl_reservations(id),date TEXT NOT NULL,asset TEXT NOT NULL,role TEXT NOT NULL,PRIMARY KEY(reservation_id,date,asset));
CREATE INDEX rdl_scope_lookup ON rdl_keys(date,asset,role);
CREATE TABLE rdl_registrations(id TEXT PRIMARY KEY,reservation_id TEXT NOT NULL UNIQUE REFERENCES rdl_reservations(id),registration_digest TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE rdl_normalize_attempts(id TEXT PRIMARY KEY,reservation_id TEXT NOT NULL UNIQUE REFERENCES rdl_reservations(id),owner_token TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,error_code TEXT);
'''


class RealDailyAdmissionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(condition, code):
    if not condition:
        raise RealDailyAdmissionError(code)


def _closed(value, keys, code='invalid_schema'):
    need(type(value) is dict and set(value) == set(keys), code)


def _sha(value):
    need(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None, 'invalid_pin')
    return value


def _text(value, maximum=4000):
    need(type(value) is str and 0 < len(value) <= maximum and value.strip() == value, 'invalid_text')
    return value


def _utc(value):
    _text(value, 40)
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        need(d.tzinfo is not None and d.utcoffset().total_seconds() == 0, 'invalid_utc')
        return d
    except (ValueError, TypeError, AttributeError):
        raise RealDailyAdmissionError('invalid_utc') from None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(payload):
    def pairs(items):
        value = {}
        for k, v in items:
            need(k not in value, 'duplicate_json_key')
            value[k] = v
        return value
    def invalid(_):
        raise RealDailyAdmissionError('nonfinite_json')
    try:
        value = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid)
        stack = [(value, 0)]; count = 0
        while stack:
            item, level = stack.pop(); count += 1
            need(level <= 64 and count <= 500000, 'bounded_json_required')
            if isinstance(item, dict): stack.extend((v, level + 1) for v in item.values())
            elif isinstance(item, list): stack.extend((v, level + 1) for v in item)
        # json_text rejects overflowed float tokens as well as nonfinite literals.
        json_text(value)
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, RealDailyAdmissionError): raise
        raise RealDailyAdmissionError('invalid_json') from None


def _path(path):
    need((type(path) is str or isinstance(path, Path)) and Path(path).is_absolute(), 'absolute_path_required')
    p = Path(path)
    need('..' not in p.parts and p.resolve() == p and
         not any(q.is_symlink() for q in (p, *p.parents)), 'unsafe_path')
    need(not any(q.casefold() in ('secrets', 'credentials', 'alpha-vantage-claims') or q.casefold().endswith('.key') for q in p.parts), 'secret_path_refused')
    return p


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _read(path, limit=MAX_METADATA):
    p = _path(path)
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit, 'unsafe_file')
        with os.fdopen(fd, 'rb', closefd=False) as f: payload = f.read(limit + 1)
        need(len(payload) == before.st_size and len(payload) <= limit and
             _signature(before) == _signature(os.fstat(fd)) == _signature(p.stat(follow_symlinks=False)), 'input_drift')
        return payload
    finally:
        os.close(fd)


def code_identity():
    root = Path(__file__).resolve().parents[1]
    files = {name: hashlib.sha256(_read(root / name)).hexdigest() for name in CODE_FILES}
    return {'version': VERSION, 'files': files, 'digest': digest(files)}


def _selection(value):
    need(type(value) is dict, 'invalid_selection')
    from .evaluation import validate_config
    required = ('assets', 'sessions', 'development_sessions', 'config', 'partitions')
    need(all(k in value for k in required), 'invalid_selection')
    assets, sessions, parts = value['assets'], value['sessions'], value['partitions']
    need(type(assets) is list and 3 <= len(assets) <= 100 and assets == sorted(set(assets)) and
         all(type(a) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}', a) for a in assets), 'invalid_assets')
    need(type(sessions) is list and 9 <= len(sessions) <= 1024 and sessions == sorted(set(sessions)), 'invalid_sessions')
    for d in sessions:
        need(type(d) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d) is not None, 'invalid_sessions')
        try: date.fromisoformat(d)
        except ValueError: raise RealDailyAdmissionError('invalid_sessions') from None
    _closed(parts, ('train', 'validation', 'test'), 'invalid_partitions')
    need(all(type(parts[k]) is list and len(parts[k]) >= 3 for k in parts), 'invalid_partitions')
    need(parts['train'] + parts['validation'] + parts['test'] == sessions and
         value['development_sessions'] == parts['train'] + parts['validation'], 'invalid_development_prefix')
    config = validate_config(value['config'], sessions)
    need(config == value['config'] and config['min_assets'] <= len(assets) and
         all(config['splits'][k] == {'start': parts[k][0], 'end': parts[k][-1]} for k in parts), 'config_partition_mismatch')
    return deepcopy(value)


def _pins(value, assets):
    _closed(value, ('origin', 'metadata', 'raw', 'code'), 'invalid_input_pins')
    need(value['origin'] in ('engineering_fixture', 'real_source_declaration'), 'invalid_origin')
    need(type(value['metadata']) is list and 1 <= len(value['metadata']) <= 50 and
         type(value['code']) is list and len(value['code']) <= 50 and
         type(value['raw']) is list and len(value['raw']) == len(assets), 'invalid_input_pins')
    seen = set()
    for role in ('metadata', 'code', 'raw'):
        for item in value[role]:
            _closed(item, ('asset', 'path', 'sha256', 'size_bytes') if role == 'raw' else ('path', 'sha256'), 'invalid_input_pin')
            p = str(_path(item['path'])); _sha(item['sha256'])
            need(p not in seen, 'input_role_alias'); seen.add(p)
            if role == 'raw':
                need(item['asset'] in assets and type(item['size_bytes']) is int and 0 < item['size_bytes'] <= MAX_RAW, 'invalid_raw_descriptor')
            else:
                need(not p.casefold().endswith('.raw.json') and 'raw' not in Path(p).stem.casefold(), 'metadata_role_required')
    raw_pins = {r['sha256'] for r in value['raw']}
    need(not any(p['sha256'] in raw_pins for p in value['metadata'] + value['code']), 'copied_raw_role_alias')
    need(sorted(r['asset'] for r in value['raw']) == assets, 'raw_asset_mismatch')
    return deepcopy(value)


def build_assessment(selection, ledger, cash_annotations, source_facts, input_pins):
    """Pure metadata request. Completeness cannot confer execution authority."""
    from .real_daily_method import method_profile, assess_events
    selection = _selection(selection); pins = _pins(input_pins, selection['assets'])
    _closed(source_facts, FACTS, 'source_fact_denominator_required')
    supported_pins = {p['sha256'] for p in pins['metadata'] + pins['raw']}
    blockers = []
    for name, fact in source_facts.items():
        _closed(fact, ('status', 'statement', 'evidence_sha256'), 'invalid_source_fact')
        need(fact['status'] in ('unknown', 'supported', 'rejected'), 'invalid_source_fact')
        if fact['statement'] is not None: _text(fact['statement'])
        if fact['evidence_sha256'] is not None: _sha(fact['evidence_sha256'])
        if fact['status'] == 'supported':
            need(fact['statement'] is not None and fact['evidence_sha256'] in supported_pins, 'unbound_source_evidence')
        else:
            blockers.append({'code': 'source_fact_' + fact['status'], 'fact': name})
    method = method_profile()
    events = assess_events(ledger, selection=selection, cash_annotations=cash_annotations)
    blockers.extend(deepcopy(events['blockers']))
    value = {'schema_version': 1, 'assessment_version': VERSION, 'selection': selection,
             'ledger': deepcopy(ledger), 'cash_annotations': deepcopy(cash_annotations),
             'source_facts': deepcopy(source_facts), 'input_pins': pins,
             'method': method, 'method_digest': digest(method), 'event_review': events,
             'code_identity': code_identity(), 'blockers': blockers,
             'status': 'blocked' if blockers else 'ready_for_host_review',
             'origin': pins['origin'], 'source_authenticated': False, 'real_source_ready': False,
             'point_in_time': 'not_certified', 'local_development_execution_allowed': False,
             'final_numeric_access_allowed': False, 'final_numeric_rows_parsed': 0,
             'limitations': ['OHLC-only raw-price method; volume units are not authenticated.',
                             'Source evidence completeness is a declaration, not provider or rights authentication.',
                             'Static current-vintage universe and historical PIT not certified.',
                             'Cash dividends, costs, slippage and borrow are excluded from returns.']}
    value['assessment_digest'] = digest(value)
    return value


def _assessment(value):
    need(type(value) is dict and type(value.get('assessment_digest')) is str, 'invalid_assessment')
    expected = build_assessment(value['selection'], value['ledger'], value['cash_annotations'],
                                value['source_facts'], value['input_pins'])
    need(value == expected, 'assessment_drift')
    return value


def _record(row, field):
    need(row is not None, 'missing_host_record')
    value = dict(row); payload = _json(value['payload_json'])
    need(digest(payload) == value[field], 'host_record_drift')
    return value, payload


def _all_tables(connection):
    names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = names & set(HOST_TABLES)
    anchor = connection.execute("SELECT value FROM settings WHERE key='real_daily_host_version'").fetchone() if 'settings' in names else None
    databases = connection.execute('PRAGMA database_list').fetchall()
    main_path = next((row[2] for row in databases if row[1] == 'main'), '')
    external_anchor = bool(main_path and (Path(main_path).parent / '.real-daily-host.json').exists())
    need(not ((anchor or external_anchor) and not present), 'real_host_schema_deleted')
    need(not external_anchor or anchor is not None, 'real_host_anchor_deleted')
    need(not present or (present == set(HOST_TABLES) and anchor is not None and anchor[0] == VERSION), 'partial_real_host_schema')
    return bool(present)


def _keys(selection):
    dev = set(selection['development_sessions'])
    return [(d, a, 'development' if d in dev else 'test')
            for d in selection['sessions'] for a in selection['assets']]


def _verify_history(connection):
    """Validate immutable business anchors without opening raw or current inputs."""
    if not _all_tables(connection): return []
    rows = connection.execute('SELECT * FROM rdl_reservations ORDER BY id LIMIT ?', (MAX_HISTORY + 1,)).fetchall()
    need(len(rows) <= MAX_HISTORY, 'history_bound_exceeded')
    for table, foreign, target, key in (
        ('rdl_approvals', 'assessment_id', 'rdl_assessments', 'id'),
        ('rdl_revocations', 'approval_id', 'rdl_approvals', 'id'),
        ('rdl_reservations', 'approval_id', 'rdl_approvals', 'id'),
        ('rdl_keys', 'reservation_id', 'rdl_reservations', 'id'),
        ('rdl_registrations', 'reservation_id', 'rdl_reservations', 'id'),
        ('rdl_normalize_attempts', 'reservation_id', 'rdl_reservations', 'id')):
        orphan = connection.execute(f'SELECT 1 FROM {table} a LEFT JOIN {target} b ON a.{foreign}=b.{key} WHERE b.{key} IS NULL LIMIT 1').fetchone()
        need(orphan is None, 'orphan_real_host_record')
    # Unused approvals and assessments are legitimate, but they too must retain
    # their immutable closed anchors; deleting a reserved row cannot hide drift.
    assessments = {}
    for ar in connection.execute('SELECT * FROM rdl_assessments ORDER BY id LIMIT ?', (MAX_HISTORY + 1,)):
        need(len(assessments) < MAX_HISTORY, 'history_bound_exceeded')
        assessment = _json(ar['payload_json'])
        pin = assessment.pop('assessment_digest', None)
        need(digest(assessment) == pin == ar['assessment_digest'], 'assessment_anchor_drift')
        assessment['assessment_digest'] = pin
        _selection(assessment['selection']); _pins(assessment['input_pins'], assessment['selection']['assets'])
        need(assessment['origin'] == assessment['input_pins']['origin'] and assessment['method_digest'] == digest(assessment['method']), 'assessment_anchor_drift')
        assessments[ar['id']] = assessment
    approvals = {}
    for apr in connection.execute('SELECT * FROM rdl_approvals ORDER BY id LIMIT ?', (MAX_HISTORY + 1,)):
        need(len(approvals) < MAX_HISTORY, 'history_bound_exceeded')
        row, ap = _record(apr, 'approval_digest')
        need(row['id'] == ap['id'] and row['assessment_id'] == ap['assessment_id'] and ap['assessment_id'] in assessments, 'approval_binding_drift')
        ast = assessments[ap['assessment_id']]
        need(ap['assessment_digest'] == ast['assessment_digest'] and ap['method_digest'] == ast['method_digest'] and ap['selection_digest'] == digest(ast['selection']) and not ast['blockers'] and ast['status'] == 'ready_for_host_review', 'approval_scope_drift')
        need(ap['scope'] == 'local_development_only' and ap['numeric_access'] == 'development_ohlc_only' and ap['final_numeric_access_allowed'] is False and ap['source_authenticated'] is False and ap['real_source_ready'] is False and _utc(ap['created_at_utc']) < _utc(ap['expires_at_utc']), 'approval_scope_drift')
        approvals[row['id']] = ap
    for rv in connection.execute('SELECT * FROM rdl_revocations'):
        row, rev = _record(rv, 'revocation_digest')
        need(rev['approval_id'] == row['approval_id'] and rev['approval_id'] in approvals, 'revocation_binding_drift')
        _text(rev['reason'], 1000); _utc(rev['revoked_at_utc'])
    result = []
    for raw in rows:
        row, payload = _record(raw, 'reservation_digest')
        need(payload['id'] == row['id'] and payload['approval_id'] == row['approval_id'], 'reservation_binding_drift')
        approval_row, approval = _record(connection.execute('SELECT * FROM rdl_approvals WHERE id=?', (row['approval_id'],)).fetchone(), 'approval_digest')
        need(approval_row['id'] == approval['id'] and payload['approval_digest'] == approval_row['approval_digest'] and approval_row['assessment_id'] == approval['assessment_id'] and payload['assessment_id'] == approval['assessment_id'] and payload['assessment_digest'] == approval['assessment_digest'], 'approval_binding_drift')
        assessment_row = connection.execute('SELECT * FROM rdl_assessments WHERE id=?', (approval['assessment_id'],)).fetchone()
        need(assessment_row is not None, 'missing_assessment_anchor')
        assessment = _json(assessment_row['payload_json'])
        declared_digest = assessment.pop('assessment_digest', None)
        need(digest(assessment) == declared_digest == assessment_row['assessment_digest'] == approval['assessment_digest'], 'assessment_anchor_drift')
        assessment['assessment_digest'] = declared_digest
        selection = _selection(assessment['selection'])
        need(payload['selection_digest'] == digest(selection) and payload['scope_keys_digest'] == digest(_keys(selection)), 'scope_anchor_drift')
        actual_keys = [tuple(r) for r in connection.execute('SELECT date,asset,role FROM rdl_keys WHERE reservation_id=? ORDER BY date,asset', (row['id'],))]
        need(actual_keys == _keys(selection), 'reservation_keys_drift')
        need(row['status'] in ('reserved', 'normalizing', 'registered', 'failed', 'interrupted'), 'unknown_reservation_state')
        attempts = connection.execute('SELECT * FROM rdl_normalize_attempts WHERE reservation_id=?', (row['id'],)).fetchall()
        if row['status'] == 'reserved': need(not attempts and row['owner_token'] is None, 'normalize_attempt_drift')
        else:
            need(len(attempts) == 1 and attempts[0]['owner_token'] == row['owner_token'] and attempts[0]['status'] in ('running', 'completed', 'failed', 'interrupted'), 'normalize_attempt_drift')
            if row['status'] == 'normalizing': need(attempts[0]['status'] == 'running', 'normalize_attempt_drift')
            if row['status'] == 'registered': need(attempts[0]['status'] == 'completed', 'normalize_attempt_drift')
        registrations = connection.execute('SELECT * FROM rdl_registrations WHERE reservation_id=?', (row['id'],)).fetchall()
        if row['status'] == 'registered':
            need(len(registrations) == 1 and registrations[0]['id'] == row['registration_id'], 'missing_registration_anchor')
            rr, reg = _record(registrations[0], 'registration_digest')
            need(reg['id'] == rr['id'] and reg['reservation_id'] == row['id'] and reg['reservation_digest'] == row['reservation_digest'] and reg['approval_digest'] == payload['approval_digest'], 'registration_anchor_drift')
        else:
            need(not registrations and row['registration_id'] is None, 'unowned_registration_anchor')
        revoked = connection.execute('SELECT * FROM rdl_revocations WHERE approval_id=?', (row['approval_id'],)).fetchone()
        if revoked:
            rv, rev = _record(revoked, 'revocation_digest')
            need(rev['approval_id'] == row['approval_id'], 'revocation_binding_drift')
        result.append((row, payload, selection))
    return result


def check_real_overlap(connection, *, assets, development_sessions, test_sessions):
    """Root legacy hooks call this before numeric access; absent new lane is inert."""
    if not _all_tables(connection): return None
    assets = set(assets); development_sessions = set(development_sessions); test_sessions = set(test_sessions)
    for row, payload, selection in _verify_history(connection):
        if assets & set(selection['assets']):
            if development_sessions & set(selection['partitions']['test']) or test_sessions & set(selection['development_sessions']):
                return 'Real daily potential asset/date overlap with retained reservation ' + row['id']
    return None


class RealDailyHost:
    """Explicit fresh isolated host. No discovery or reuse of the online workspace."""
    def __init__(self, root, *, create=False):
        self._initializing = True
        self.root = _path(root); self.db_path = self.root / 'workbench.sqlite3'
        self.marker = self.root / '.real-daily-host.json'
        need(type(create) is bool, 'explicit_create_required')
        if create:
            need(not self.root.exists() or (self.root.is_dir() and not any(self.root.iterdir())), 'fresh_isolated_host_required')
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            from .server.service import Store
            self.store = Store(self.root)
            with self._transaction() as c:
                for statement in SCHEMA.split(';'):
                    if statement.strip(): c.execute(statement)
                c.execute("INSERT INTO settings VALUES ('real_daily_host_version',?)", (VERSION,))
            marker = {'host_version': VERSION, 'host_root': str(self.root), 'db_path': str(self.db_path), 'scope': 'local_development_only'}
            _immutable(self.marker, json_text(marker).encode())
        else:
            marker = _json(_read(self.marker))
            need(marker == {'host_version': VERSION, 'host_root': str(self.root), 'db_path': str(self.db_path), 'scope': 'local_development_only'}, 'host_identity_drift')
            need(self.db_path.is_file(), 'host_database_missing')
            self.store = None
        self._initializing = False
        with closing(self._connect()) as c:
            need(_all_tables(c), 'real_host_schema_missing')
            need(c.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] in {'27', '28', '29'}, 'base_schema_changed')
            _verify_history(c)

    def _assert_host(self):
        need(_json(_read(self.marker)) == {'host_version': VERSION, 'host_root': str(self.root), 'db_path': str(self.db_path), 'scope': 'local_development_only'}, 'host_identity_drift')

    def _connect(self):
        self._assert_host()
        _path(self.db_path)
        need(self.db_path.is_file() and not self.db_path.is_symlink(), 'host_database_missing')
        return connect(self.db_path)

    @contextmanager
    def _transaction(self):
        if not self._initializing: self._assert_host()
        with transaction(self.db_path) as connection: yield connection

    def _row(self, table, identity, connection=None):
        need(table in HOST_TABLES and type(identity) is str, 'invalid_host_record')
        if connection is None:
            with closing(self._connect()) as c: return self._row(table, identity, c)
        row = connection.execute('SELECT * FROM ' + table + ' WHERE id=?', (identity,)).fetchone()
        need(row is not None, 'missing_host_record')
        return dict(row)

    def submit(self, assessment, *, idempotency_key):
        assessment = _assessment(assessment); _text(idempotency_key, 128)
        with self._transaction() as c:
            found = c.execute('SELECT * FROM rdl_assessments WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if found:
                need(found['assessment_digest'] == assessment['assessment_digest'] and _json(found['payload_json']) == assessment, 'idempotency_conflict')
                identity = found['id']
            else:
                identity = str(uuid.uuid4())
                c.execute('INSERT INTO rdl_assessments VALUES (?,?,?,?,?)', (identity, idempotency_key, assessment['assessment_digest'], json_text(assessment), _now()))
        return {'id': identity, 'assessment_id': identity, 'assessment_digest': assessment['assessment_digest'], 'status': assessment['status']}

    @staticmethod
    def _assert_inputs(assessment):
        pins = assessment['input_pins']; raw_inodes = set()
        for item in pins['raw']:
            p = _path(item['path']); info = p.stat(follow_symlinks=False)
            need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == item['size_bytes'], 'raw_descriptor_drift')
            raw_inodes.add((info.st_dev, info.st_ino))
        for item in pins['metadata'] + pins['code']:
            p = _path(item['path']); info = p.stat(follow_symlinks=False)
            need((info.st_dev, info.st_ino) not in raw_inodes, 'input_role_alias')
            need(hashlib.sha256(_read(p)).hexdigest() == item['sha256'], 'input_pin_drift')
        need(code_identity() == assessment['code_identity'], 'producer_code_drift')

    def approve(self, assessment_id, *, expected_digest, reviewer, expires_at_utc):
        _sha(expected_digest); _text(reviewer, 200)
        expiry = _utc(expires_at_utc); created = _now()
        need(expiry > _utc(created), 'approval_expired')
        with self._transaction() as c:
            row = self._row('rdl_assessments', assessment_id, c); assessment = _assessment(_json(row['payload_json']))
            need(row['assessment_digest'] == expected_digest == assessment['assessment_digest'], 'assessment_pin_mismatch')
            need(assessment['status'] == 'ready_for_host_review' and not assessment['blockers'], 'assessment_blocked')
            self._assert_inputs(assessment)
            identity = str(uuid.uuid4())
            payload = {'schema_version': 1, 'approval_version': VERSION, 'id': identity,
                       'host_root': str(self.root), 'assessment_id': assessment_id, 'assessment_digest': expected_digest,
                       'method_digest': assessment['method_digest'], 'selection_digest': digest(assessment['selection']),
                       'reviewer': reviewer, 'created_at_utc': created, 'expires_at_utc': expires_at_utc,
                       'scope': 'local_development_only', 'numeric_access': 'development_ohlc_only',
                       'whole_byte_integrity_allowed': True, 'final_numeric_access_allowed': False,
                       'source_authenticated': False, 'real_source_ready': False, 'point_in_time': 'not_certified'}
            pin = digest(payload)
            c.execute('INSERT INTO rdl_approvals VALUES (?,?,?,?)', (identity, assessment_id, pin, json_text(payload)))
        return {**payload, 'approval_id': identity, 'approval_digest': pin}

    def assert_approved(self, approval_id, expected_approval_digest):
        _sha(expected_approval_digest)
        with closing(self._connect()) as c:
            c.execute('BEGIN'); _verify_history(c)
            row, approval = _record(self._row('rdl_approvals', approval_id, c), 'approval_digest')
            need(row['approval_digest'] == expected_approval_digest and approval['id'] == approval_id and approval['host_root'] == str(self.root), 'approval_pin_mismatch')
            need(c.execute('SELECT 1 FROM rdl_revocations WHERE approval_id=?', (approval_id,)).fetchone() is None, 'approval_revoked')
            need(_utc(approval['expires_at_utc']) > _utc(_now()), 'approval_expired')
            assessment_row = self._row('rdl_assessments', approval['assessment_id'], c)
            assessment = _assessment(_json(assessment_row['payload_json']))
            need(assessment_row['assessment_digest'] == assessment['assessment_digest'] == approval['assessment_digest'], 'assessment_pin_mismatch')
            need(not assessment['blockers'] and approval['method_digest'] == assessment['method_digest'] and approval['selection_digest'] == digest(assessment['selection']), 'approval_scope_drift')
            self._assert_inputs(assessment)
            return {**approval, 'approval_digest': row['approval_digest'], 'assessment': assessment}

    def revoke(self, approval_id, *, reason):
        _text(reason, 1000)
        with self._transaction() as c:
            self._row('rdl_approvals', approval_id, c)
            existing = c.execute('SELECT * FROM rdl_revocations WHERE approval_id=?', (approval_id,)).fetchone()
            if existing:
                row, value = _record(existing, 'revocation_digest'); need(value['reason'] == reason, 'revocation_conflict')
                return {**value, 'revocation_digest': row['revocation_digest']}
            value = {'schema_version': 1, 'approval_id': approval_id, 'reason': reason, 'revoked_at_utc': _now()}
            pin = digest(value)
            c.execute('INSERT INTO rdl_revocations VALUES (?,?,?)', (approval_id, json_text(value), pin))
        return {**value, 'revocation_digest': pin}

    def reserve(self, approval_id, expected_approval_digest, *, idempotency_key):
        _text(idempotency_key, 128)
        authority = self.assert_approved(approval_id, expected_approval_digest)
        selection = authority['assessment']['selection']
        with self._transaction() as c:
            _verify_history(c)
            need(c.execute('SELECT 1 FROM rdl_revocations WHERE approval_id=?', (approval_id,)).fetchone() is None and _utc(authority['expires_at_utc']) > _utc(_now()), 'approval_no_longer_active')
            existing = c.execute('SELECT * FROM rdl_reservations WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                row, value = _record(existing, 'reservation_digest')
                need(value['approval_id'] == approval_id and value['approval_digest'] == expected_approval_digest, 'idempotency_conflict')
                return {**value, 'reservation_digest': row['reservation_digest'], 'status': row['status'], 'registration_id': row['registration_id']}
            need(check_real_overlap(c, assets=selection['assets'], development_sessions=selection['development_sessions'], test_sessions=selection['partitions']['test']) is None, 'real_scope_overlap')
            from .server.daily_snapshot_guard import _history, _legacy_history, _dates
            proposed_dev, proposed_test, assets = set(selection['development_sessions']), set(selection['partitions']['test']), set(selection['assets'])
            old = [(r['id'], _dates(contract), contract.assets) for r, contract, _ in _history(c)] + _legacy_history(c)
            for old_id, dates, old_assets in old:
                need(not (assets & set(old_assets) and (proposed_dev & dates['test'] or proposed_test & dates['development'])), 'legacy_scope_overlap')
            identity = str(uuid.uuid4())
            value = {'schema_version': 1, 'reservation_version': VERSION, 'id': identity,
                     'approval_id': approval_id, 'approval_digest': expected_approval_digest,
                     'assessment_id': authority['assessment_id'], 'assessment_digest': authority['assessment_digest'],
                     'selection_digest': digest(selection), 'scope_keys_digest': digest(_keys(selection)),
                     'host_root': str(self.root), 'created_at_utc': _now(), 'final_numeric_access_allowed': False}
            pin = digest(value)
            c.execute('INSERT INTO rdl_reservations VALUES (?,?,?,?,?,?,?,?,?)', (identity, approval_id, idempotency_key, pin, json_text(value), 'reserved', None, None, None))
            c.executemany('INSERT INTO rdl_keys VALUES (?,?,?,?)', ((identity, d, a, role) for d, a, role in _keys(selection)))
        return {**value, 'reservation_digest': pin, 'status': 'reserved', 'registration_id': None}

    def get_registration(self, registration_id):
        row, value = _record(self._row('rdl_registrations', registration_id), 'registration_digest')
        return {**value, 'registration_digest': row['registration_digest']}

    def resolve_registration(self, registration_id, expected_digest):
        _sha(expected_digest)
        value = self.get_registration(registration_id)
        need(value['registration_digest'] == expected_digest, 'registration_pin_mismatch')
        authority = self.assert_approved(value['approval_id'], value['approval_digest'])
        with closing(self._connect()) as c:
            history = _verify_history(c)
            found = next((r for r, p, s in history if r['id'] == value['reservation_id']), None)
            need(found is not None and found['status'] == 'registered' and found['registration_id'] == registration_id and found['reservation_digest'] == value['reservation_digest'], 'registration_scope_drift')
        need(authority['assessment_digest'] == value['assessment_digest'] and value['method_digest'] == authority['assessment']['method_digest'] and _json(value['selection_json']) == authority['assessment']['selection'] and _json(value['code_identity_json']) == code_identity(), 'registration_binding_drift')
        prefix = _path(value['prefix_path']); receipt = _path(value['receipt_path'])
        need(prefix.is_relative_to(self.root / 'real_daily_prefixes') and receipt.parent == prefix.parent, 'managed_prefix_required')
        need(hashlib.sha256(_read(prefix)).hexdigest() == value['prefix_sha256'] and hashlib.sha256(_read(receipt)).hexdigest() == value['receipt_sha256'], 'prefix_pin_drift')
        saved = _json(_read(prefix.parent / 'registration.json'))
        need(saved == value, 'producer_registration_drift')
        return {**value, 'selection': deepcopy(authority['assessment']['selection']),
                'assessment': deepcopy(authority['assessment']), 'method': deepcopy(authority['assessment']['method'])}


def _immutable(path, payload):
    p = _path(path); p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as f:
            f.write(payload); f.flush(); os.fsync(fd)
    finally: os.close(fd)
    fd = os.open(p.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
