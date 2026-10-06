"""Verified local industry sources and a fenced, development-only queue.

HTTP never supplies file paths, methods, market values or computation code.
Registration copies a verified completed CLI artifact; execution always creates
a new attempt and uses the installed engine, never saved source/MATLAB files.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from .db import connect, transaction
from .service import ServiceError, now, uid
from .industry_mom_schema import (IndustryMomSourceDetail, IndustryMomExperimentSummary,
                                 IndustryMomExperimentDetail, IndustryMomVerification)
from .. import mom_only, mom_only_workflow as workflow
from ..storage import atomic_json, digest, json_text

MAX_SECONDS = 60
MAX_ATTEMPTS = 3
MAX_MUTATION_RECEIPTS = 32
MAX_JSON = 2 * 1024 * 1024
MAX_BYTES = 64 * 1024 * 1024
PHASES = ('preparing', 'executing', 'verifying', 'publishing')
ACTIVE = {'running', 'cancelling'}
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


def _text(value, label, maximum, *, blank=False):
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        raise ServiceError(label + ' is invalid', 422)
    try:
        value.encode('utf8')
    except UnicodeError as exc:
        raise ServiceError(label + ' is invalid UTF-8', 422) from exc
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ServiceError('Invalid industry experiment/attempt identity', 422)
    return value


def _source_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'industry_mom_source_[0-9a-f]{64}', value):
        raise ServiceError('Invalid industry source identity', 422)
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ServiceError('Invalid SHA256', 422)
    return value


def _parse(text):
    if not isinstance(text, str) or len(text.encode('utf8')) > MAX_JSON:
        raise ServiceError('Stored industry record exceeds its bound', 409)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate stored key')
            result[key] = value
        return result
    def reject(_):
        raise ValueError('Nonfinite JSON')
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject)
        if json_text(value) != text:
            raise ValueError('Noncanonical stored JSON')
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ServiceError('Stored industry JSON failed integrity verification', 409) from exc


def _safe_path(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ServiceError('Industry path traverses a symbolic link', 409)
    return path


def _bytes(path, limit=MAX_BYTES):
    path = _safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('File must be bounded, regular and independently owned')
        value = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(value) > limit or before.st_size != len(value)
                or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
                != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink)):
            raise ValueError('File changed during its bounded read')
        return value


def _sha(path, limit=MAX_BYTES):
    return hashlib.sha256(_bytes(path, limit)).hexdigest()


def _json(path):
    # Parse the exact bounded bytes already read, never reopen a path between
    # the size/regular-file check and JSON decoding.
    text = _bytes(path, MAX_JSON).decode('utf8')
    def reject(value):
        raise ValueError('Invalid JSON numeric constant: ' + value)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('Nonfinite JSON number')
        return parsed
    return json.loads(text, parse_constant=reject, object_pairs_hook=pairs, parse_float=finite_float)


def _inventory(folder):
    folder = _safe_path(folder)
    if not folder.is_dir():
        raise ValueError('Artifact directory is missing')
    result, size = {}, 0
    for path in sorted(folder.rglob('*')):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Artifact contains a symlink, hardlink or nonregular file')
        size += info.st_size
        if size > MAX_BYTES or len(result) >= 65:
            raise ValueError('Industry artifact exceeds its file/byte budget')
        result[str(path.relative_to(folder))] = _sha(path)
    return result


def _copy_file(source, destination):
    value = _bytes(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    actual = hashlib.sha256(value).hexdigest()
    if _sha(source) != actual or _sha(destination) != actual:
        raise ValueError('File changed during snapshotting')


def _copy_tree(source, destination, inventory):
    destination.mkdir(parents=True, exist_ok=False)
    for name, expected in inventory.items():
        _copy_file(source / name, destination / name)
        if _sha(destination / name) != expected:
            raise ValueError('Copied artifact differs from frozen inventory')
    if _inventory(source) != inventory or _inventory(destination) != inventory:
        raise ValueError('Original or copied artifact changed during registration')


def _pagination(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ServiceError('Pagination requires limit 1..100 and offset 0..100000', 422)


def _receipt(connection, operation, key, arguments=None):
    row = connection.execute('SELECT * FROM industry_mom_receipts WHERE operation=? AND idempotency_key=?', (operation, key)).fetchone()
    if row is None:
        return None
    row = dict(row)
    try:
        request, response = _parse(row['request']), _parse(row['response'])
        if not isinstance(request, dict) or set(request) != {'arguments', 'input_digest'} or not isinstance(request['arguments'], dict):
            raise ValueError('Receipt request fields differ')
        wanted = {'source_id'} if operation == 'register' else {'experiment_id'}
        if (not isinstance(response, dict) or set(response) != wanted or next(iter(response.values())) != row['resource_id']
                or digest(request) != row['request_digest'] or digest(response) != row['response_digest']
                or digest({k: row[k] for k in ('operation', 'idempotency_key', 'request_digest', 'response_digest', 'resource_id', 'created_at')}) != row['receipt_digest']):
            raise ValueError('Receipt identity or digests differ')
        if operation == 'create':
            _hash(request['input_digest'])
        elif request['input_digest'] is not None:
            raise ValueError('Unexpected receipt input digest')
        if arguments is not None and request['arguments'] != arguments:
            raise ServiceError('Industry idempotency key belongs to another request', 409)
        return row, request, response
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError('Industry receipt failed integrity verification', 409) from exc


def _put_receipt(connection, operation, key, arguments, identity, input_digest=None):
    request = {'arguments': arguments, 'input_digest': input_digest}
    response = {'source_id' if operation == 'register' else 'experiment_id': identity}
    row = {'operation': operation, 'idempotency_key': key, 'request_digest': digest(request),
           'response_digest': digest(response), 'resource_id': identity, 'created_at': now()}
    connection.execute('INSERT INTO industry_mom_receipts VALUES (?,?,?,?,?,?,?,?,?)',
        (operation, key, json_text(request), row['request_digest'], json_text(response), row['response_digest'], identity,
         row['created_at'], digest(row)))
    return row['request_digest'], response


def _projection(folder, checked):
    if (checked.get('verified') is not True or checked.get('status') != 'completed'
            or checked.get('calculation_verified') is not True or checked.get('reference_passed') is not True
            or checked.get('reserved_evaluated') is not False):
        raise ValueError('Only completed, numerically verified development artifacts may be registered')
    request, result, manifest = (_json(folder / name) for name in ('input.json', 'result.json', 'manifest.json'))
    panel = _json(folder / 'panel.json')
    return {
        'data_kind': 'market_derived_portfolio_returns', 'asset_kind': 'industry_portfolio',
        'market_source_id': mom_only.SOURCE_ID, 'research_scope': 'project_modification',
        'archive_sha256': request['source_sha256'], 'method_digest': request['method']['contract_digest'],
        'config_digest': digest(request['config']), 'input_digest': checked['input_digest'],
        'panel_digest': digest(panel), 'result_digest': checked['result_digest'],
        'manifest_digest': digest(manifest), 'code_digest': digest(request['code_sha256']),
        'verification_scope': 'local_bytes_and_independent_numeric_reference',
        'config_json': json_text(request['config']), 'method_json': json_text(request['method']),
        'source_json': json_text(result['source']), 'verification_json': json_text(checked),
        'human_judgment': None, 'reserved_evaluated': False,
    }


class IndustryMomSources:
    def __init__(self, store):
        self.store = store

    def _path(self, identity):
        return _safe_path(self.store.root / 'industry-mom' / 'sources' / _source_id(identity) / 'artifact')

    @contextmanager
    def _registration_lock(self):
        path = self.store.root / '.industry-mom-registration.lock'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ServiceError('Unsafe source registration lock', 409)
            end = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise ServiceError('Source registration is busy; replay the original key', 409)
                    time.sleep(.02)
            yield
        finally:
            os.close(descriptor)

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM industry_mom_sources WHERE id=?', (_source_id(identity),)).fetchone()
        if row is None:
            raise ServiceError('Industry source not found', 404)
        row = dict(row)
        try:
            body = _parse(row['payload'])
            if (not isinstance(body, dict) or set(body) != {'arguments', 'registration_key', 'projection', 'files'}
                    or digest(body) != row['digest'] or row['id'] != 'industry_mom_source_' + digest(body)
                    or body['registration_key'] != row['idempotency_key']
                    or digest({k: row[k] for k in ('id', 'digest', 'created_at', 'idempotency_key', 'request_digest')}) != row['metadata_digest']):
                raise ValueError('Source record identity differs')
            receipts = connection.execute('SELECT operation,idempotency_key FROM industry_mom_receipts WHERE resource_id=?', (identity,)).fetchall()
            if len(receipts) != 1 or receipts[0]['operation'] != 'register' or receipts[0]['idempotency_key'] != row['idempotency_key']:
                raise ValueError('Original registration receipt is missing or duplicated')
            receipt, request, response = _receipt(connection, 'register', row['idempotency_key'], body['arguments'])
            if receipt['request_digest'] != row['request_digest'] or response != {'source_id': identity}:
                raise ValueError('Registration receipt differs from its source')
            value = {'id': identity, 'digest': row['digest'], 'title': body['arguments']['title'],
                     'note': body['arguments']['note'], 'created_at': row['created_at'], **body['projection']}
            IndustryMomSourceDetail.model_validate(value)
            return value, body
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Industry source record failed integrity verification', 409) from exc

    def _verified(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            value, body = self._load(connection, identity)
        path = self._path(identity)
        try:
            inventory = _inventory(path)
            if inventory != body['files']:
                raise ValueError('Registered file inventory differs')
            checked = workflow.verify(path)
            if _projection(path, checked) != body['projection'] or _inventory(path) != inventory:
                raise ValueError('Registered source changed or is no longer verified')
            with closing(connect(self.store.db_path)) as connection:
                current, current_body = self._load(connection, identity)
                if current != value or current_body != body:
                    raise ValueError('Source record changed during verification')
            if _inventory(path) != inventory:
                raise ValueError('Registered source changed at its final read fence')
            return value, inventory
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise ServiceError('Registered industry artifact failed integrity verification', 409) from exc

    def register(self, artifact_dir, title, note, idempotency_key):
        _text(title, 'Source title', 200)
        _text(note, 'Source note', 4000, blank=True)
        _text(idempotency_key, 'Idempotency key', 128)
        artifact = _safe_path(artifact_dir)
        arguments = {'artifact_path': str(artifact), 'title': title, 'note': note}
        with self._registration_lock():
            with closing(connect(self.store.db_path)) as connection:
                previous = _receipt(connection, 'register', idempotency_key, arguments)
                anchored = connection.execute('SELECT id FROM industry_mom_sources WHERE idempotency_key=?', (idempotency_key,)).fetchone()
                if previous is None and anchored is not None:
                    self._load(connection, anchored['id'])  # A missing receipt cannot be recreated.
            if previous:
                return self.get(previous[2]['source_id'])
            try:
                inventory = _inventory(artifact)
                checked = workflow.verify(artifact)
                projection = _projection(artifact, checked)
                if _inventory(artifact) != inventory:
                    raise ValueError('Original artifact changed during verification')
                body = {'arguments': arguments, 'registration_key': idempotency_key,
                        'projection': projection, 'files': inventory}
                identity = 'industry_mom_source_' + digest(body)
                target = self._path(identity)
                if target.exists():
                    # A crash after copy and before the DB transaction leaves a
                    # verifiable immutable orphan; adopt only identical bytes.
                    if _inventory(target) != inventory or _projection(target, workflow.verify(target)) != projection:
                        raise ValueError('Orphan registration differs from the exact original request')
                else:
                    stage = _safe_path(self.store.root / 'industry-mom' / 'registration-staging' / uid())
                    _copy_tree(artifact, stage, inventory)
                    if _projection(stage, workflow.verify(stage)) != projection:
                        raise ValueError('Registered copy differs from verified original')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(stage, target)
                if _inventory(artifact) != inventory or _inventory(target) != inventory:
                    raise ValueError('Source bytes changed before registration commit')
                with transaction(self.store.db_path) as connection:
                    if _receipt(connection, 'register', idempotency_key, arguments):
                        raise ValueError('Registration key unexpectedly changed while locked')
                    request_digest, _ = _put_receipt(connection, 'register', idempotency_key, arguments, identity)
                    created = now()
                    metadata = {'id': identity, 'digest': digest(body), 'created_at': created,
                                'idempotency_key': idempotency_key, 'request_digest': request_digest}
                    connection.execute('INSERT INTO industry_mom_sources VALUES (?,?,?,?,?,?,?)',
                        (identity, json_text(body), digest(body), created, digest(metadata), idempotency_key, request_digest))
                    # Last filesystem + record fence after every durable row is
                    # inserted, before commit. A lost response only replays.
                    if _inventory(target) != inventory or _inventory(artifact) != inventory:
                        raise ValueError('Source bytes changed at final registration fence')
                    self._load(connection, identity)
                return self.get(identity)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                if isinstance(exc, ServiceError):
                    raise
                raise ServiceError('Industry registration rejected: ' + str(exc)[:512], 409) from exc

    def get(self, identity):
        return self._verified(identity)[0]

    def list(self, limit=20, offset=0):
        _pagination(limit, offset)
        with closing(connect(self.store.db_path)) as connection:
            rows = connection.execute('SELECT id FROM industry_mom_sources ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            total = connection.execute('SELECT COUNT(*) FROM industry_mom_sources').fetchone()[0]
        from .industry_mom_schema import IndustryMomSourceSummary
        items = []
        for row in rows:
            value = self.get(row['id'])
            items.append(IndustryMomSourceSummary.model_validate({k: value[k] for k in IndustryMomSourceSummary.model_fields}).model_dump())
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def _row_digest(row):
    return digest({k: v for k, v in row.items() if k not in {'heartbeat', 'record_digest'}})


class IndustryMomExperiments:
    def __init__(self, store):
        self.store = store
        self.sources = IndustryMomSources(store)

    def _path(self, identity, attempt_id):
        return _safe_path(self.store.root / 'industry-mom' / 'experiments' / _id(identity) / 'attempts' / _id(attempt_id))

    def _seal(self, connection, identity):
        row = dict(connection.execute('SELECT * FROM industry_mom_experiments WHERE id=?', (identity,)).fetchone())
        connection.execute('UPDATE industry_mom_experiments SET record_digest=? WHERE id=?', (_row_digest(row), identity))

    def _seal_attempt(self, connection, identity):
        row = dict(connection.execute('SELECT * FROM industry_mom_attempts WHERE id=?', (identity,)).fetchone())
        connection.execute('UPDATE industry_mom_attempts SET record_digest=? WHERE id=?', (_row_digest(row), identity))

    def _event(self, connection, identity, kind, payload=None, attempt_id=None):
        stamp, encoded = now(), json_text(payload or {})
        cursor = connection.execute('INSERT INTO industry_mom_events(experiment_id,attempt_id,kind,created_at,payload,digest) VALUES (?,?,?,?,?,?)',
                                    (identity, attempt_id, kind, stamp, encoded, 'pending'))
        body = {'id': cursor.lastrowid, 'experiment_id': identity, 'attempt_id': attempt_id,
                'kind': kind, 'created_at': stamp, 'payload': encoded}
        connection.execute('UPDATE industry_mom_events SET digest=? WHERE id=?', (digest(body), cursor.lastrowid))
        values = [row[0] for row in connection.execute('SELECT digest FROM industry_mom_events WHERE experiment_id=? ORDER BY id', (identity,))]
        connection.execute('UPDATE industry_mom_experiments SET event_count=?,event_digest=? WHERE id=?', (len(values), digest(values), identity))
        self._seal(connection, identity)

    def _row(self, connection, identity):
        saved = connection.execute('SELECT * FROM industry_mom_experiments WHERE id=?', (_id(identity),)).fetchone()
        if saved is None:
            raise ServiceError('Industry experiment not found', 404)
        row = dict(saved)
        try:
            if _row_digest(row) != row['record_digest']:
                raise ValueError('Experiment record changed')
            request = _parse(row['input'])
            if (not isinstance(request, dict) or digest(request) != row['input_digest']
                    or digest(request['config']) != row['config_digest']):
                raise ValueError('Frozen experiment input differs')
            mom_only.validate_config(request['config'])
            workflow._validate_method(request['method'], request['config'], request['source_sha256'])
            if set(request) != {'schema_version', 'config', 'method', 'source_sha256', 'source_receipt', 'source_receipt_scope', 'code_sha256'}:
                raise ValueError('Frozen workflow input fields differ')
            if set(request['code_sha256']) != set(workflow.SOURCE_FILES):
                raise ValueError('Calculation code inventory differs')
            for value in request['code_sha256'].values():
                _hash(value)
            receipts = connection.execute("SELECT idempotency_key FROM industry_mom_receipts WHERE resource_id=? AND operation='create'", (identity,)).fetchall()
            if len(receipts) != 1 or receipts[0]['idempotency_key'] != row['idempotency_key']:
                raise ValueError('Original create receipt is missing or repeated')
            receipt, original, response = _receipt(connection, 'create', row['idempotency_key'],
                                                   {'source_id': row['source_id'], 'source_digest': row['source_digest']})
            if receipt['request_digest'] != row['request_digest'] or original['input_digest'] != row['input_digest'] or response != {'experiment_id': identity}:
                raise ValueError('Original create receipt changed')
            for item in connection.execute('SELECT operation,idempotency_key FROM industry_mom_receipts WHERE resource_id=?', (identity,)):
                if item['operation'] not in {'create', 'cancel', 'retry'}:
                    raise ValueError('Unknown experiment receipt operation')
                _receipt(connection, item['operation'], item['idempotency_key'])
            events = [dict(item) for item in connection.execute('SELECT * FROM industry_mom_events WHERE experiment_id=? ORDER BY id', (identity,))]
            if len(events) != row['event_count'] or digest([e['digest'] for e in events]) != row['event_digest']:
                raise ValueError('Event ledger coverage differs')
            for event in events:
                if digest({k: v for k, v in event.items() if k != 'digest'}) != event['digest']:
                    raise ValueError('Event digest differs')
                _parse(event['payload'])
            mutation_keys = []
            for event in events:
                if event['kind'] in {'cancel_requested', 'retry_queued'}:
                    payload = _parse(event['payload'])
                    if not isinstance(payload, dict) or set(payload) != {'operation', 'idempotency_key', 'arguments', 'request_digest'}:
                        raise ValueError('Mutation event binding differs')
                    operation = 'cancel' if event['kind'] == 'cancel_requested' else 'retry'
                    if payload['operation'] != operation:
                        raise ValueError('Mutation event operation differs')
                    receipt, _, response = _receipt(connection, operation, payload['idempotency_key'], payload['arguments'])
                    if receipt['request_digest'] != payload['request_digest'] or response != {'experiment_id': identity}:
                        raise ValueError('Mutation receipt differs from its original event')
                    mutation_keys.append((operation, payload['idempotency_key']))
            saved_keys = [(item['operation'], item['idempotency_key']) for item in connection.execute(
                "SELECT operation,idempotency_key FROM industry_mom_receipts WHERE resource_id=? AND operation!='create'", (identity,))]
            if (len(saved_keys) > MAX_MUTATION_RECEIPTS or len(set(mutation_keys)) != len(mutation_keys)
                    or set(saved_keys) != set(mutation_keys)):
                raise ValueError('Mutation receipt/event coverage differs')
            attempts = [dict(item) for item in connection.execute('SELECT * FROM industry_mom_attempts WHERE experiment_id=? ORDER BY number', (identity,))]
            if (len(attempts) != row['attempt_count'] or not 0 <= len(attempts) <= MAX_ATTEMPTS
                    or [a['number'] for a in attempts] != list(range(1, len(attempts) + 1))
                    or (attempts and (attempts[-1]['id'] != row['attempt_id'] or attempts[-1]['worker_id'] != row['worker_id']))):
                raise ValueError('Attempt history differs')
            for attempt in attempts:
                if _row_digest(attempt) != attempt['record_digest']:
                    raise ValueError('Attempt metadata differs')
            if row['status'] in ACTIVE and (not attempts or attempts[-1]['status'] != 'running'):
                raise ValueError('Active attempt differs')
            if row['status'] == 'completed' and (not attempts or attempts[-1]['status'] != 'completed'):
                raise ValueError('Published attempt status differs')
            return row
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Industry experiment failed ledger integrity verification', 409) from exc

    def _fetch(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._row(connection, identity)

    def _source(self, row):
        source, files = self.sources._verified(row['source_id'])
        if source['digest'] != row['source_digest']:
            raise ServiceError('Selected industry source digest changed', 412)
        request = _parse(row['input'])
        original = _json(self.sources._path(source['id']) / 'input.json')
        if {k: v for k, v in request.items() if k != 'code_sha256'} != {k: v for k, v in original.items() if k != 'code_sha256'}:
            raise ServiceError('Frozen input differs from its exact registered source', 409)
        return source, files

    @staticmethod
    def _code():
        files = {name: _sha(workflow.ROOT / name, MAX_JSON) for name in workflow.SOURCE_FILES}
        if any(_sha(workflow.ROOT / name, MAX_JSON) != value for name, value in files.items()):
            raise ServiceError('Calculation code changed during freezing', 409)
        return files

    @staticmethod
    def _summary(row):
        value = {k: row[k] for k in ('id', 'source_id', 'source_digest', 'created_at', 'updated_at', 'status',
                                    'attempt_count', 'attempt_id', 'worker_id', 'error', 'phase', 'input_digest', 'config_digest')}
        value.update(max_seconds=MAX_SECONDS, max_attempts=MAX_ATTEMPTS, research_scope='project_modification')
        return IndustryMomExperimentSummary.model_validate(value).model_dump()

    def create(self, source_id, source_digest, idempotency_key):
        _source_id(source_id); _hash(source_digest); _text(idempotency_key, 'Idempotency key', 128)
        arguments = {'source_id': source_id, 'source_digest': source_digest}
        with closing(connect(self.store.db_path)) as connection:
            previous = _receipt(connection, 'create', idempotency_key, arguments)
            anchored = connection.execute('SELECT id FROM industry_mom_experiments WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if previous is None and anchored is not None:
                self._row(connection, anchored['id'])  # Lost receipts never reset the original effect.
        if previous:
            self.get(previous[2]['experiment_id'])
            return previous[2]
        source, files = self.sources._verified(source_id)
        if source['digest'] != source_digest:
            raise ServiceError('Selected source changed', 412)
        request = _json(self.sources._path(source_id) / 'input.json')
        request['code_sha256'] = self._code()
        payload, input_digest = json_text(request), digest(request)
        if len(payload.encode('utf8')) > MAX_JSON:
            raise ServiceError('Frozen industry input exceeds its bound', 413)
        with transaction(self.store.db_path) as connection:
            previous = _receipt(connection, 'create', idempotency_key, arguments)
            if previous:
                self._row(connection, previous[2]['experiment_id'])
                return previous[2]
            current, _ = self.sources._load(connection, source_id)
            if current != source or _inventory(self.sources._path(source_id)) != files:
                raise ServiceError('Source changed before queue insertion', 409)
            identity, stamp = uid(), now()
            receipt_digest, response = _put_receipt(connection, 'create', idempotency_key, arguments, identity, input_digest)
            connection.execute('INSERT INTO industry_mom_experiments(id,source_id,source_digest,input,input_digest,config_digest,created_at,updated_at,status,idempotency_key,request_digest,record_digest,event_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, source_id, source_digest, payload, input_digest, digest(request['config']), stamp, stamp, 'queued',
                 idempotency_key, receipt_digest, 'pending', digest([])))
            self._event(connection, identity, 'queued', {'source_id': source_id, 'source_digest': source_digest, 'input_digest': input_digest})
            if _inventory(self.sources._path(source_id)) != files:
                raise ServiceError('Source changed at final queue fence', 409)
            self._row(connection, identity)
        return response

    def list(self, limit=20, offset=0):
        _pagination(limit, offset)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            ids = connection.execute('SELECT id FROM industry_mom_experiments ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            rows = [self._row(connection, item['id']) for item in ids]
            total = connection.execute('SELECT COUNT(*) FROM industry_mom_experiments').fetchone()[0]
        for row in rows:
            self._source(row)
        return {'items': [self._summary(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def status(self, identity):
        row = self._fetch(identity)
        return {**{k: row[k] for k in ('id', 'status', 'attempt_id', 'attempt_count', 'phase')},
                'change_token': digest({k: row[k] for k in ('record_digest', 'event_digest')}), 'integrity_checked': False}

    def _owned(self, connection, identity, worker_id, attempt_id):
        row = self._row(connection, identity)
        attempt = connection.execute('SELECT * FROM industry_mom_attempts WHERE id=?', (_id(attempt_id),)).fetchone()
        if (row['worker_id'] != worker_id or row['attempt_id'] != attempt_id or row['status'] not in ACTIVE
                or attempt is None or attempt['worker_id'] != worker_id or attempt['experiment_id'] != identity or attempt['status'] != 'running'):
            raise ServiceError('Worker no longer owns this industry attempt', 409)
        return row

    def remaining_seconds(self, identity, worker_id, attempt_id):
        with closing(connect(self.store.db_path)) as connection:
            self._owned(connection, identity, worker_id, attempt_id)
            started = connection.execute('SELECT started_at FROM industry_mom_attempts WHERE id=?', (attempt_id,)).fetchone()[0]
        return max(0., MAX_SECONDS - (time.time() - datetime.fromisoformat(started).timestamp()))

    def heartbeat(self, worker_id, identity, attempt_id):
        with transaction(self.store.db_path) as connection:
            stamp = time.time()
            changed = connection.execute("UPDATE industry_mom_experiments SET heartbeat=? WHERE id=? AND worker_id=? AND attempt_id=? AND status IN ('running','cancelling')", (stamp, identity, worker_id, attempt_id)).rowcount
            if changed:
                connection.execute('INSERT INTO workers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen', (worker_id, stamp))
            return bool(changed)

    @contextmanager
    def heartbeat_scope(self, identity, worker_id, attempt_id):
        stopped = threading.Event()
        def pulse():
            while not stopped.wait(.5):
                try:
                    if not self.heartbeat(worker_id, identity, attempt_id):
                        return
                except Exception:
                    import logging
                    logging.getLogger('paper_alpha.industry_mom').exception('industry_heartbeat_failed')
        self.heartbeat(worker_id, identity, attempt_id)
        thread = threading.Thread(target=pulse, daemon=True, name='industry-mom-heartbeat')
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(16)

    def record_phase(self, identity, worker_id, attempt_id, phase):
        if phase not in PHASES:
            raise ServiceError('Unknown industry phase', 422)
        with transaction(self.store.db_path) as connection:
            row = self._owned(connection, identity, worker_id, attempt_id)
            if row['phase'] != phase:
                connection.execute('UPDATE industry_mom_experiments SET phase=?,updated_at=? WHERE id=?', (phase, now(), identity))
                self._event(connection, identity, 'execution_phase', {'phase': phase}, attempt_id)

    def claim(self, worker_id):
        _text(worker_id, 'Worker identity', 200)
        with transaction(self.store.db_path) as connection:
            selected = connection.execute("SELECT id FROM industry_mom_experiments WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if selected is None:
                return None
            row = self._row(connection, selected['id'])
            number, attempt_id, stamp = row['attempt_count'] + 1, uid(), now()
            if number > MAX_ATTEMPTS:
                raise ServiceError('Industry attempt limit exceeded', 409)
            connection.execute("INSERT INTO industry_mom_attempts(id,experiment_id,number,worker_id,status,started_at,record_digest) VALUES(?,?,?,?,'running',?,?)", (attempt_id, row['id'], number, worker_id, stamp, 'pending'))
            self._seal_attempt(connection, attempt_id)
            connection.execute("UPDATE industry_mom_experiments SET status='running',attempt_count=?,attempt_id=?,worker_id=?,heartbeat=?,updated_at=?,error=NULL,phase='preparing',result_digest=NULL,verification=NULL WHERE id=?",
                               (number, attempt_id, worker_id, time.time(), stamp, row['id']))
            self._event(connection, row['id'], 'execution_phase', {'phase': 'preparing'}, attempt_id)
        with self.heartbeat_scope(row['id'], worker_id, attempt_id):
            try:
                source, files = self._source(row)
                request = _parse(row['input'])
                if self._code() != request['code_sha256']:
                    raise ValueError('Calculation code differs from the queued snapshot; create a new versioned experiment')
                folder = self._path(row['id'], attempt_id)
                folder.mkdir(parents=True, exist_ok=False)
                original = self.sources._path(source['id'])
                for name in sorted(files):
                    if name.startswith('inputs/'):
                        _copy_file(original / name, folder / name)
                atomic_json(folder / 'queued-input.json', request)
                if _inventory(original) != files or digest(_json(folder / 'queued-input.json')) != row['input_digest']:
                    raise ValueError('Source or frozen input changed during materialization')
                if self.cancel_requested(row['id']):
                    self.finish(row['id'], worker_id, attempt_id, 'cancelled')
                    return None
                if self.remaining_seconds(row['id'], worker_id, attempt_id) <= 0:
                    raise ValueError('Industry attempt deadline elapsed during preparation')
                with closing(connect(self.store.db_path)) as connection:
                    self._owned(connection, row['id'], worker_id, attempt_id)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.finish(row['id'], worker_id, attempt_id, 'failed', 'Input preparation failed: ' + str(exc))
                return None
        return {'kind': 'industry_mom', 'id': row['id'], 'attempt_id': attempt_id,
                'source_id': source['id'], 'source_digest': source['digest'], 'source_sha256': source['archive_sha256'],
                'source_path': str(folder / 'inputs/source.zip'), 'config_path': str(folder / 'inputs/config.json'),
                'method_path': str(folder / 'inputs/method.json'), 'evidence_dir': str(folder / 'inputs'),
                'source_receipt_path': str(folder / 'inputs/source_receipt.json') if (folder / 'inputs/source_receipt.json').exists() else None,
                'output_dir': str(folder / 'output'), 'input_digest': row['input_digest'], 'max_seconds': MAX_SECONDS}

    def cancel_requested(self, identity):
        return self._fetch(identity)['status'] in {'cancelling', 'cancelled'}

    def _verified(self, row):
        source, source_files = self._source(row)
        output = self._path(row['id'], row['attempt_id']) / 'output'
        try:
            files = _inventory(output)
            checked = workflow.verify(output)
            projection = _projection(output, checked)
            request = _json(output / 'input.json')
            if (checked['input_digest'] != row['input_digest'] or request != _parse(row['input'])
                    or projection['archive_sha256'] != source['archive_sha256']
                    or projection['config_digest'] != source['config_digest']
                    or projection['method_digest'] != source['method_digest']
                    or projection['panel_digest'] != source['panel_digest'] or _inventory(output) != files):
                raise ValueError('Published output differs from its exact queued input/source')
            verification = {
                'verified': True, 'calculation_verified': True, 'reference_passed': True,
                'source_id': source['id'], 'source_digest': source['digest'], 'source_input_digest': source['input_digest'],
                'source_manifest_digest': source['manifest_digest'], 'attempt_id': row['attempt_id'],
                'input_digest': checked['input_digest'], 'result_digest': checked['result_digest'],
                'panel_digest': projection['panel_digest'], 'manifest_digest': projection['manifest_digest'],
                'code_digest': projection['code_digest'], 'environment_digest': _json(output / 'manifest.json')['files']['environment.json'],
                'config_digest': projection['config_digest'], 'method_digest': projection['method_digest'],
                'archive_sha256': projection['archive_sha256'], 'reserved_evaluated': False, 'human_review': 'pending'}
            IndustryMomVerification.model_validate(verification)
            if row['status'] == 'completed':
                with closing(connect(self.store.db_path)) as connection:
                    current = self._row(connection, row['id'])
                    attempt = connection.execute('SELECT * FROM industry_mom_attempts WHERE id=?', (row['attempt_id'],)).fetchone()
                    if (current != row or row['result_digest'] != checked['result_digest']
                            or _parse(row['verification']) != verification or attempt['status'] != 'completed'
                            or attempt['result_digest'] != row['result_digest'] or _parse(attempt['verification']) != verification):
                        raise ValueError('Published metadata differs from verified output')
            return verification, files, source, source_files
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise ServiceError('Industry output failed integrity verification', 409) from exc

    def get(self, identity):
        row = self._fetch(identity)
        source, source_files = self._source(row)
        result_json = reference_json = report = verification = target = None
        if row['status'] == 'completed':
            verification, files, source, source_files = self._verified(row)
            output = self._path(identity, row['attempt_id']) / 'output'
            result_json = json_text(_json(output / 'result.json'))
            reference_json = json_text(_json(output / 'reference.json'))
            report = _bytes(output / 'report.md', MAX_JSON).decode('utf8')
            if _inventory(output) != files:
                raise ServiceError('Industry output changed during projection', 409)
            target = {'attempt_id': row['attempt_id'], 'result_digest': verification['result_digest']}
        with closing(connect(self.store.db_path)) as connection:
            current = self._row(connection, identity)
            if _row_digest(current) != _row_digest(row):
                raise ServiceError('Industry experiment changed during projection; refresh', 409)
            current_source, _ = self.sources._load(connection, row['source_id'])
            if current_source != source:
                raise ServiceError('Industry source record changed during projection', 409)
            attempts = [{**{k: item[k] for k in ('id', 'number', 'worker_id', 'status', 'started_at', 'finished_at', 'error', 'result_digest')},
                         'verification_json': item['verification']}
                        for item in connection.execute('SELECT * FROM industry_mom_attempts WHERE experiment_id=? ORDER BY number', (identity,))]
        try:
            if _inventory(self.sources._path(source['id'])) != source_files:
                raise ValueError('Industry source bytes changed during projection')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ServiceError('Industry source changed at final projection fence', 409) from exc
        value = {'experiment': self._summary(row), 'source': source, 'attempts': attempts,
                 'result_json': result_json, 'reference_json': reference_json, 'report_markdown': report,
                 'verification': verification, 'review_target': target}
        return IndustryMomExperimentDetail.model_validate(value).model_dump()

    def get_report(self, identity):
        value = self.get(identity)
        if value['report_markdown'] is None or value['verification'] is None:
            raise ServiceError('Industry report requires a completed verified attempt', 409)
        return value['report_markdown']

    def finish(self, identity, worker_id, attempt_id, status, error=None):
        if status not in TERMINAL:
            raise ServiceError('Invalid industry terminal state', 422)
        with closing(connect(self.store.db_path)) as connection:
            row = self._owned(connection, identity, worker_id, attempt_id)
        verification = files = source = source_files = None
        with self.heartbeat_scope(identity, worker_id, attempt_id):
            if status == 'completed' and row['status'] != 'cancelling':
                try:
                    self.record_phase(identity, worker_id, attempt_id, 'verifying')
                    row = self._fetch(identity)
                    verification, files, source, source_files = self._verified(row)
                    self.record_phase(identity, worker_id, attempt_id, 'publishing')
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    status, error, verification = 'failed', 'Output verification failed: ' + str(exc), None
            with transaction(self.store.db_path) as connection:
                current = self._owned(connection, identity, worker_id, attempt_id)
                if verification:
                    try:
                        started = connection.execute('SELECT started_at FROM industry_mom_attempts WHERE id=?', (attempt_id,)).fetchone()[0]
                        if time.time() - datetime.fromisoformat(started).timestamp() >= MAX_SECONDS:
                            raise ValueError('Industry attempt deadline elapsed before publication')
                        selected, _ = self.sources._load(connection, current['source_id'])
                        if (current['input'] != row['input'] or current['input_digest'] != row['input_digest']
                                or selected != source or _inventory(self.sources._path(source['id'])) != source_files
                                or _inventory(self._path(identity, attempt_id) / 'output') != files):
                            raise ValueError('Source/input/output changed at publication fence')
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        status, error, verification = 'failed', 'Publication verification failed: ' + str(exc), None
                if current['status'] == 'cancelling':
                    status, error, verification = 'cancelled', 'Cancellation requested', None
                stamp, error = now(), self.store._safe_error(error)
                result_digest = verification['result_digest'] if verification else None
                encoded = json_text(verification) if verification else None
                connection.execute('UPDATE industry_mom_attempts SET status=?,finished_at=?,error=?,result_digest=?,verification=? WHERE id=?',
                                   (status, stamp, error, result_digest, encoded, attempt_id))
                self._seal_attempt(connection, attempt_id)
                connection.execute('UPDATE industry_mom_experiments SET status=?,updated_at=?,error=?,phase=NULL,result_digest=?,verification=? WHERE id=?',
                                   (status, stamp, error, result_digest, encoded, identity))
                self._event(connection, identity, status, {'verified': verification is not None, 'error': error}, attempt_id)
        return {'experiment_id': identity}

    def _mutation(self, identity, expected_attempt_id, idempotency_key, operation):
        _id(identity)
        if expected_attempt_id is not None:
            _id(expected_attempt_id)
        if operation == 'retry' and expected_attempt_id is None:
            raise ServiceError('Retry requires an exact previous attempt', 422)
        _text(idempotency_key, 'Idempotency key', 128)
        arguments = {'experiment_id': identity, 'expected_attempt_id': expected_attempt_id}
        row = self._fetch(identity)
        self._source(row)
        with transaction(self.store.db_path) as connection:
            previous = _receipt(connection, operation, idempotency_key, arguments)
            if previous:
                self._row(connection, identity)
                return previous[2]
            row = self._row(connection, identity)
            mutation_count = connection.execute(
                "SELECT COUNT(*) FROM industry_mom_receipts WHERE resource_id=? AND operation IN ('cancel','retry')",
                (identity,)).fetchone()[0]
            if mutation_count >= MAX_MUTATION_RECEIPTS:
                raise ServiceError('Industry operation ledger reached its 32 cancel/retry request limit; existing keys can still replay', 409)
            if row['attempt_id'] != expected_attempt_id:
                raise ServiceError('Industry mutation targets another attempt', 412)
            if operation == 'retry':
                if row['status'] not in {'failed', 'cancelled', 'interrupted'} or row['attempt_count'] >= MAX_ATTEMPTS:
                    raise ServiceError('Industry experiment cannot retry or reached its three-attempt limit', 409)
                connection.execute("UPDATE industry_mom_experiments SET status='queued',heartbeat=NULL,error=NULL,phase=NULL,result_digest=NULL,verification=NULL,updated_at=? WHERE id=?", (now(), identity))
                kind = 'retry_queued'
            else:
                status = 'cancelled' if row['status'] == 'queued' else 'cancelling' if row['status'] == 'running' else row['status']
                connection.execute('UPDATE industry_mom_experiments SET status=?,updated_at=? WHERE id=?', (status, now(), identity))
                kind = 'cancel_requested'
            request_digest, response = _put_receipt(connection, operation, idempotency_key, arguments, identity)
            self._event(connection, identity, kind, {'operation': operation, 'idempotency_key': idempotency_key,
                        'arguments': arguments, 'request_digest': request_digest}, expected_attempt_id)
        return response

    def cancel(self, identity, expected_attempt_id, idempotency_key):
        return self._mutation(identity, expected_attempt_id, idempotency_key, 'cancel')

    def retry(self, identity, expected_attempt_id, idempotency_key):
        return self._mutation(identity, expected_attempt_id, idempotency_key, 'retry')

    def recover_stale(self, stale_seconds=0):
        """Caller must already hold the inherited workspace worker flock."""
        if type(stale_seconds) not in (int, float) or not 0 <= stale_seconds <= 3600:
            raise ServiceError('Invalid recovery interval', 422)
        recovered = []
        with transaction(self.store.db_path) as connection:
            ids = connection.execute("SELECT id FROM industry_mom_experiments WHERE status IN ('running','cancelling') AND (heartbeat IS NULL OR heartbeat<?)", (time.time() - stale_seconds,)).fetchall()
            for item in ids:
                row = self._row(connection, item['id'])
                status = 'cancelled' if row['status'] == 'cancelling' else 'interrupted'
                stamp, error = now(), 'Worker lost; partial files retained. Explicit retry required.'
                connection.execute('UPDATE industry_mom_attempts SET status=?,finished_at=?,error=? WHERE id=?', (status, stamp, error, row['attempt_id']))
                self._seal_attempt(connection, row['attempt_id'])
                connection.execute('UPDATE industry_mom_experiments SET status=?,updated_at=?,error=?,phase=NULL,result_digest=NULL,verification=NULL WHERE id=?', (status, stamp, error, row['id']))
                self._event(connection, row['id'], 'worker_lost', {'status': status}, row['attempt_id'])
                recovered.append(row['id'])
        return recovered
