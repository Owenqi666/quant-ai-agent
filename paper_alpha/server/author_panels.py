"""Immutable normalized author panels; no MAT reads or network access in HTTP.

Bounded scientific validation runs before the writer lock. Inside that lock a
prepared canonical body is only compared/stored alongside its exact receipt.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime
import json
import math
import re

from .db import connect, transaction
from .service import ServiceError, now
from .author_panel_api_schema import AuthorPanelDetail
from ..author_archive_contract import MAX_PANEL_BYTES
from ..author_panel_schema import AuthorPanel
from ..storage import digest, json_text

SCHEMA = """
CREATE TABLE author_panels(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL);
CREATE INDEX author_panels_created ON author_panels(created_at,id);
CREATE TABLE author_panel_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,panel_id TEXT NOT NULL REFERENCES author_panels(id),record_digest TEXT NOT NULL,record_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
ID_PATTERN = re.compile(r'author_panel_[0-9a-f]{64}\Z')
BODY_FIELDS = {'title', 'note', 'panel', 'result', 'reference', 'verification_scope', 'raw_source_reverified'}
RECEIPT_FIELDS = {'idempotency_key', 'request_digest', 'panel_id', 'record_digest', 'record_metadata_digest', 'created_at'}
MAX_STORED_BYTES = 3 * MAX_PANEL_BYTES
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 100_000


def _core():
    from .. import author_panel
    return author_panel


def _reference(panel, result):
    from ..author_reference import check
    return check(panel, result)


def _text(title, note):
    if not isinstance(title, str) or not 1 <= len(title) <= 200 or not title.strip():
        raise ServiceError('Author panel title must be nonblank and contain 1..200 characters', 422)
    if not isinstance(note, str) or len(note) > 4000:
        raise ServiceError('Author panel note must contain at most 4000 characters', 422)
    try:
        title.encode('utf-8')
        note.encode('utf-8')
    except UnicodeError as exc:
        raise ServiceError('Author panel text must contain valid Unicode characters', 422) from exc
    return title, note


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ServiceError('Idempotency key must be a nonblank string of 1..128 characters', 422)
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ServiceError('Idempotency key must contain valid Unicode characters', 422) from exc
    return value


def _identity(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ServiceError('Author panel identity is invalid', 422)
    return value


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Stored timestamp needs a timezone')
    return value


def _metadata_digest(identity, fingerprint, created_at):
    _timestamp(created_at)
    return digest({'schema_version': 1, 'id': identity, 'digest': fingerprint, 'created_at': created_at})


def _verify_metadata(row):
    fingerprint = _metadata_digest(row['id'], row['digest'], row['created_at'])
    if fingerprint != row['metadata_digest']:
        raise ValueError('Stored author panel metadata digest mismatch')
    return fingerprint


def _parse(payload):
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_STORED_BYTES:
        raise ValueError('Stored author panel exceeds its payload limit')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def reject(value):
        raise ValueError('Nonfinite JSON number')

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('Nonfinite JSON number')
        return result

    return json.loads(payload, object_pairs_hook=unique, parse_constant=reject, parse_float=finite)


def _detail(body, identity, fingerprint, created_at):
    return {**body, 'id': identity, 'digest': fingerprint, 'created_at': created_at}


def _receipt(connection, key, request_fingerprint, identity, fingerprint, metadata_fingerprint):
    row = connection.execute('SELECT * FROM author_panel_receipts WHERE idempotency_key=?', (key,)).fetchone()
    if row is None:
        return None
    try:
        receipt = {field: row[field] for field in RECEIPT_FIELDS}
        _timestamp(receipt['created_at'])
        if (receipt['idempotency_key'] != key or digest(receipt) != row['receipt_digest']
                or receipt['request_digest'] != request_fingerprint
                or receipt['panel_id'] != identity or receipt['record_digest'] != fingerprint
                or receipt['record_metadata_digest'] != metadata_fingerprint):
            raise ValueError('Receipt does not match request or resource')
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ServiceError('Stored author panel receipt conflicts or failed integrity verification; no new record was created', 409) from exc
    return row


class AuthorPanels:
    def __init__(self, store):
        self.store = store

    def _record(self, row):
        try:
            body = _parse(row['payload'])
            if not isinstance(body, dict) or set(body) != BODY_FIELDS:
                raise ValueError('Stored author panel fields differ from supported contract')
            _text(body['title'], body['note'])
            fingerprint = digest(body)
            if row['digest'] != fingerprint or row['id'] != 'author_panel_' + fingerprint:
                raise ValueError('Stored author panel content digest mismatch')
            _verify_metadata(row)
            result = _detail(body, row['id'], fingerprint, row['created_at'])
            modeled = AuthorPanelDetail.model_validate(result).model_dump()
            if json_text(modeled) != json_text(result):
                raise ValueError('Stored author panel is not canonical')
            panel = _core().validate_panel(body['panel'])
            if json_text(panel) != json_text(body['panel']):
                raise ValueError('Stored normalized panel is not canonical')
            checked = _reference(panel, body['result'])
            if checked.get('passed') is not True or json_text(checked) != json_text(body['reference']):
                raise ValueError('Stored author panel independent reference failed')
            return result
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Stored author panel failed integrity verification', 409) from exc

    def create(self, title, note, panel, idempotency_key):
        _text(title, note)
        key = _key(idempotency_key)
        try:
            normalized = AuthorPanel.model_validate(_core().validate_panel(panel)).model_dump()
            result = _core().evaluate(normalized)
            reference = _reference(normalized, result)
            if reference.get('passed') is not True:
                raise ServiceError('Author panel failed independent reference verification', 409)
            body = {'title': title, 'note': note, 'panel': normalized, 'result': result,
                    'reference': reference, 'verification_scope': 'normalized_panel_and_diagnostics',
                    'raw_source_reverified': False}
            payload = json_text(body)
            if len(payload.encode('utf-8')) > MAX_STORED_BYTES:
                raise ServiceError('Author panel stored diagnostics exceed their payload limit', 413)
            fingerprint = digest(body)
            identity = 'author_panel_' + fingerprint
            created_at = now()
            prepared = _detail(body, identity, fingerprint, created_at)
            canonical = AuthorPanelDetail.model_validate(prepared).model_dump()
            if json_text(canonical) != json_text(prepared):
                raise ServiceError('Computed author panel does not match the canonical contract', 409)
            request_fingerprint = digest({'operation': 'author_panel.create', 'schema_version': 1,
                                          'title': title, 'note': note, 'panel': normalized})
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Invalid author panel input: ' + str(exc), 422) from exc
        with transaction(self.store.db_path) as connection:
            row = connection.execute('SELECT * FROM author_panels WHERE id=?', (identity,)).fetchone()
            if row is not None:
                # Comparing with independently verified prepared bytes avoids doing
                # numerical work while holding the SQLite writer lock.
                try:
                    _verify_metadata(row)
                    if row['payload'] != payload or row['digest'] != fingerprint:
                        raise ValueError('Existing content-addressed record mismatch')
                except (ValueError, TypeError) as exc:
                    raise ServiceError('Stored author panel failed integrity verification', 409) from exc
                created_at = row['created_at']
            metadata_fingerprint = _metadata_digest(identity, fingerprint, created_at)
            receipt = _receipt(connection, key, request_fingerprint, identity, fingerprint, metadata_fingerprint)
            if row is None and receipt is not None:
                raise ServiceError('Stored author panel receipt references a missing record', 409)
            if row is None:
                connection.execute('INSERT INTO author_panels(id,payload,digest,created_at,metadata_digest) VALUES (?,?,?,?,?)',
                                   (identity, payload, fingerprint, created_at, metadata_fingerprint))
            if receipt is None:
                frozen = {'idempotency_key': key, 'request_digest': request_fingerprint, 'panel_id': identity,
                          'record_digest': fingerprint, 'record_metadata_digest': metadata_fingerprint, 'created_at': now()}
                connection.execute('INSERT INTO author_panel_receipts VALUES (?,?,?,?,?,?,?)',
                    (key, request_fingerprint, identity, fingerprint, metadata_fingerprint, frozen['created_at'], digest(frozen)))
            return deepcopy(_detail(body, identity, fingerprint, created_at))

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT * FROM author_panels WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ServiceError('Author panel not found', 404)
            return deepcopy(self._record(row))

    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ServiceError('Author panel list limit must be an integer between 1 and 100', 422)
        if type(offset) is not int or not 0 <= offset <= MAX_LIST_OFFSET:
            raise ServiceError('Author panel list offset must be an integer between 0 and 100000', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM author_panels').fetchone()[0]
            rows = connection.execute('SELECT * FROM author_panels ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                                      (limit, offset)).fetchall()
            items = []
            for row in rows:
                value = self._record(row)
                items.append({key: value[key] for key in ('id', 'title', 'note', 'created_at', 'digest',
                             'verification_scope', 'raw_source_reverified')} |
                             {'source': value['panel']['source'], 'selection': value['panel']['selection'],
                              'summary': value['result']['summary']})
            return deepcopy({'items': items, 'total': total, 'limit': limit, 'offset': offset})

    def markdown(self, identity):
        value = self.get(identity)
        # Report contents are generated from verified tool output; never saved
        # user-provided markdown or claimed performance numbers.
        return _core().render_report(value['result'])
