"""Immutable aggregate study, exact-result review and bounded revision ancestry.

Scientific and relationship checks use one read snapshot. The write transaction
only fences those observed rows and stores content plus its idempotent receipt.
Raw MAT files are never opened and no network is accessed by this service.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import math
import re

from .db import connect, transaction
from .service import ServiceError, now
from .author_panels import AuthorPanels, _metadata_digest, _verify_metadata
from .author_study_api_schema import StudyDetail, StudyReview
from ..storage import digest, json_text

SCHEMA = """
CREATE TABLE author_studies(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,parent_review_id TEXT REFERENCES author_study_reviews(id),parent_study_id TEXT REFERENCES author_studies(id));
CREATE INDEX author_studies_created ON author_studies(created_at,id);
CREATE TABLE author_study_reviews(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,study_id TEXT NOT NULL REFERENCES author_studies(id));
CREATE INDEX author_study_reviews_created ON author_study_reviews(study_id,created_at,id);
CREATE TABLE author_study_receipts(operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,study_id TEXT REFERENCES author_studies(id),review_id TEXT REFERENCES author_study_reviews(id),resource_digest TEXT NOT NULL,resource_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL,PRIMARY KEY(operation,idempotency_key),CHECK((operation='study.create' AND study_id IS NOT NULL AND review_id IS NULL) OR (operation='study.review' AND review_id IS NOT NULL AND study_id IS NULL)));
"""
STUDY_FIELDS = {'title', 'note', 'scan', 'result', 'parent_review_id', 'parent_study_id', 'author_panel_ids',
                'changes', 'verification_scope', 'raw_source_reverified'}
REVIEW_FIELDS = {'study_id', 'study_digest', 'decision', 'note', 'actor'}
RECEIPT_FIELDS = {'operation', 'idempotency_key', 'request_digest', 'study_id', 'review_id',
                  'resource_digest', 'resource_metadata_digest', 'created_at'}
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_ANCESTRY = 32
MAX_REVIEWS = 1000
MAX_VERIFIED_RECORDS = 512
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 100_000
PATTERNS = {kind: re.compile(prefix + r'[0-9a-f]{64}\Z') for kind, prefix in
            [('study', 'author_study_'), ('review', 'author_review_'), ('panel', 'author_panel_')]}
DECISIONS = {'data_insufficient', 'rules_unresolved', 'implementation_error', 'accepted_with_limits'}


def _core():
    from .. import eligibility
    return eligibility


def _id(value, kind):
    if not isinstance(value, str) or not PATTERNS[kind].fullmatch(value):
        raise ServiceError('Author ' + kind + ' identity is invalid', 422)
    return value


def _string(value, label, maximum, *, nonblank=False):
    if (not isinstance(value, str) or len(value) > maximum
            or (nonblank and not value.strip())):
        raise ServiceError(label + ' must be valid text of at most ' + str(maximum) + ' characters', 422)
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ServiceError(label + ' must contain valid Unicode', 422) from exc
    return value


def _panels(value):
    if (not isinstance(value, list) or len(value) > 8 or not all(isinstance(item, str) for item in value)
            or len(set(value)) != len(value)):
        raise ServiceError('Author panel IDs must be a unique list of at most 8 identities', 422)
    return [_id(item, 'panel') for item in value]


def _parse(payload):
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ValueError('Stored study exceeds the payload bound')
    def pairs(items):
        result = {}
        for key, value in items:
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
    return json.loads(payload, object_pairs_hook=pairs, parse_constant=reject, parse_float=finite)


def _changes(parent, current):
    if parent is None:
        return []
    before, after = parent['scan'], current['scan']
    changes = [{'field': 'plan.' + key, 'before': json_text(before['plan'][key]), 'after': json_text(after['plan'][key])}
               for key in sorted(after['plan']) if json_text(before['plan'][key]) != json_text(after['plan'][key])]
    for key, old, new in [('source', before['source'], after['source']),
                          ('input_digest', digest(before), digest(after))]:
        if json_text(old) != json_text(new):
            changes.append({'field': key, 'before': json_text(old), 'after': json_text(new)})
    return changes


def _detail(body, row):
    return {**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}


def _prepared(body, prefix, **columns):
    payload = json_text(body)
    if len(payload.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ServiceError('Study payload exceeds 1 MiB', 413)
    fingerprint = digest(body)
    identity = prefix + fingerprint
    created_at = now()
    row = {'id': identity, 'payload': payload, 'digest': fingerprint, 'created_at': created_at,
           'metadata_digest': _metadata_digest(identity, fingerprint, created_at), **columns}
    model = StudyDetail if prefix == 'author_study_' else StudyReview
    response = _detail(body, row)
    try:
        if json_text(model.model_validate(response).model_dump()) != json_text(response):
            raise ValueError('Prepared resource is not canonical')
    except (ValueError, TypeError, KeyError) as exc:
        raise ServiceError('Prepared study resource failed its response contract; nothing was stored', 409) from exc
    return row


class _Snapshot:
    def __init__(self):
        self.rows = {}
        self.studies = {}
        self.reviews = {}
        self.panels = {}

    def remember(self, table, row):
        if (table, row['id']) not in self.rows and len(self.rows) >= MAX_VERIFIED_RECORDS:
            raise ServiceError('Study relationship verification exceeds its record bound', 413)
        self.rows[(table, row['id'])] = dict(row)

    def fence(self, connection):
        for (table, identity), expected in self.rows.items():
            row = connection.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
            if row is None or dict(row) != expected:
                raise ServiceError('Study dependency changed during validation; retry the unchanged request', 409)


def _read(connection, table, identity, context, *, missing=409):
    row = connection.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise ServiceError('Author study relationship or record not found', missing)
    context.remember(table, row)
    return row


def _review_record(row):
    try:
        body = _parse(row['payload'])
        if not isinstance(body, dict) or set(body) != REVIEW_FIELDS:
            raise ValueError('Stored review fields are invalid')
        _id(body['study_id'], 'study')
        _string(body['note'], 'Review note', 4000, nonblank=True)
        fingerprint = digest(body)
        if row['digest'] != fingerprint or row['id'] != 'author_review_' + fingerprint or row['study_id'] != body['study_id']:
            raise ValueError('Stored review body digest or study relationship differs')
        _verify_metadata(row)
        value = _detail(body, row)
        if json_text(StudyReview.model_validate(value).model_dump()) != json_text(value):
            raise ValueError('Stored review is not canonical')
        return value
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ServiceError('Stored author study review failed integrity verification', 409) from exc


class AuthorStudies:
    def __init__(self, store):
        self.store = store
        self.author_panels = AuthorPanels(store)

    def _study_record(self, row):
        try:
            body = _parse(row['payload'])
            if not isinstance(body, dict) or set(body) != STUDY_FIELDS:
                raise ValueError('Stored study fields are invalid')
            _string(body['title'], 'Study title', 200, nonblank=True)
            _string(body['note'], 'Study note', 4000)
            _panels(body['author_panel_ids'])
            if (body['parent_review_id'] != row['parent_review_id'] or body['parent_study_id'] != row['parent_study_id']
                    or (body['parent_review_id'] is None) != (body['parent_study_id'] is None)):
                raise ValueError('Study parent columns differ from frozen body')
            if body['parent_review_id'] is not None:
                _id(body['parent_review_id'], 'review'); _id(body['parent_study_id'], 'study')
            fingerprint = digest(body)
            if row['digest'] != fingerprint or row['id'] != 'author_study_' + fingerprint:
                raise ValueError('Stored study content digest mismatch')
            _verify_metadata(row)
            normalized = _core().validate_scan(body['scan'])
            result = _core().evaluate(normalized)
            if json_text(normalized) != json_text(body['scan']) or json_text(result) != json_text(body['result']):
                raise ValueError('Stored study input or aggregate result failed verification')
            value = _detail(body, row)
            if json_text(StudyDetail.model_validate(value).model_dump()) != json_text(value):
                raise ValueError('Stored study is not canonical')
            return value
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Stored author study failed integrity verification', 409) from exc

    def _links(self, connection, study, context):
        for identity in study['author_panel_ids']:
            if identity not in context.panels:
                row = _read(connection, 'author_panels', identity, context)
                context.panels[identity] = self.author_panels._record(row)
            panel = context.panels[identity]['panel']
            if (json_text(panel['source']) != json_text(study['scan']['source'])
                    or not study['scan']['plan']['development_start'] <= panel['selection']['target_month'] <= study['scan']['plan']['development_end']):
                raise ServiceError('Linked author panel must use the same pinned source and an in-plan month', 409)

    def _load(self, connection, identity, context, *, missing=404):
        pending, seen = [], set()
        current = identity
        while current is not None and current not in context.studies:
            if current in seen:
                raise ServiceError('Stored author study ancestry contains a cycle', 409)
            if len(pending) >= MAX_ANCESTRY:
                raise ServiceError('Author study ancestry exceeds 32 studies', 413)
            seen.add(current)
            row = _read(connection, 'author_studies', current, context, missing=missing if not pending else 409)
            value = self._study_record(row)
            self._links(connection, value, context)
            pending.append(value)
            review_id = value['parent_review_id']
            if review_id is not None:
                review = self._review(connection, review_id, context)
                if review['study_id'] != value['parent_study_id']:
                    raise ServiceError('Stored parent review and study relationship disagree', 409)
            current = value['parent_study_id']
        for value in reversed(pending):
            parent_entry = context.studies.get(value['parent_study_id'])
            depth = parent_entry[1] + 1 if parent_entry else 1
            if depth > MAX_ANCESTRY:
                raise ServiceError('Author study ancestry exceeds 32 studies', 413)
            parent = parent_entry[0] if parent_entry else None
            if parent is not None:
                review = context.reviews[value['parent_review_id']]
                if review['study_digest'] != parent['digest'] or digest(value['scan']) == digest(parent['scan']):
                    raise ServiceError('Study revision must change scan and bind the exact parent result', 409)
            if json_text(value['changes']) != json_text(_changes(parent, value)):
                raise ServiceError('Stored study changes failed integrity verification', 409)
            context.studies[value['id']] = (value, depth)
        return context.studies[identity][0]

    @staticmethod
    def _review(connection, identity, context, *, missing=409):
        if identity not in context.reviews:
            context.reviews[identity] = _review_record(_read(connection, 'author_study_reviews', identity, context, missing=missing))
        return context.reviews[identity]

    @staticmethod
    def _save(connection, table, prepared):
        row = connection.execute(f'SELECT * FROM {table} WHERE id=?', (prepared['id'],)).fetchone()
        if row is not None:
            try:
                _verify_metadata(row)
                if any(row[key] != value for key, value in prepared.items() if key not in {'created_at', 'metadata_digest'}):
                    raise ValueError('Stored content differs from independently prepared resource')
            except (ValueError, TypeError, KeyError) as exc:
                raise ServiceError('Stored author study resource failed integrity verification', 409) from exc
            return dict(row), False
        return prepared, True

    @staticmethod
    def _receipt(connection, operation, key, request_fingerprint, row):
        saved = connection.execute('SELECT * FROM author_study_receipts WHERE operation=? AND idempotency_key=?', (operation, key)).fetchone()
        if saved is None:
            return False
        try:
            receipt = {field: saved[field] for field in RECEIPT_FIELDS}
            from .author_panels import _timestamp
            _timestamp(receipt['created_at'])
            expected_study = row['id'] if operation == 'study.create' else None
            expected_review = row['id'] if operation == 'study.review' else None
            if (digest(receipt) != saved['receipt_digest'] or receipt['request_digest'] != request_fingerprint
                    or receipt['study_id'] != expected_study or receipt['review_id'] != expected_review
                    or receipt['resource_digest'] != row['digest'] or receipt['resource_metadata_digest'] != row['metadata_digest']):
                raise ValueError('Receipt does not match exact request and response')
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Stored author study receipt conflicts or failed verification', 409) from exc
        return True

    @staticmethod
    def _insert(connection, table, row):
        keys = list(row)
        connection.execute(f'INSERT INTO {table}({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})', tuple(row[key] for key in keys))

    @staticmethod
    def _write_receipt(connection, operation, key, fingerprint, row):
        receipt = {'operation': operation, 'idempotency_key': key, 'request_digest': fingerprint,
                   'study_id': row['id'] if operation == 'study.create' else None,
                   'review_id': row['id'] if operation == 'study.review' else None,
                   'resource_digest': row['digest'], 'resource_metadata_digest': row['metadata_digest'], 'created_at': now()}
        AuthorStudies._insert(connection, 'author_study_receipts', {**receipt, 'receipt_digest': digest(receipt)})

    def create(self, title, note, scan, parent_review_id, author_panel_ids, idempotency_key):
        _string(title, 'Study title', 200, nonblank=True); _string(note, 'Study note', 4000)
        _string(idempotency_key, 'Idempotency key', 128, nonblank=True)
        panels = _panels(author_panel_ids)
        if parent_review_id is not None:
            _id(parent_review_id, 'review')
        try:
            scan = _core().validate_scan(scan)
            result = _core().evaluate(scan)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Invalid author eligibility scan: ' + str(exc), 422) from exc
        context = _Snapshot()
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            parent = None
            if parent_review_id is not None:
                review = self._review(connection, parent_review_id, context, missing=404)
                parent = self._load(connection, review['study_id'], context, missing=409)
                if review['study_digest'] != parent['digest']:
                    raise ServiceError('Parent review does not bind the exact study result', 409)
                if context.studies[parent['id']][1] >= MAX_ANCESTRY:
                    raise ServiceError('Author study ancestry exceeds 32 studies', 413)
                if digest(scan) == digest(parent['scan']):
                    raise ServiceError('A revised study must change the scan', 422)
            body = {'title': title, 'note': note, 'scan': scan, 'result': result,
                    'parent_review_id': parent_review_id, 'parent_study_id': parent['id'] if parent else None,
                    'author_panel_ids': panels, 'changes': [], 'verification_scope': 'aggregate_consistency_only',
                    'raw_source_reverified': False}
            self._links(connection, body, context)
            body['changes'] = _changes(parent, body)
        prepared = _prepared(body, 'author_study_', parent_review_id=parent_review_id, parent_study_id=body['parent_study_id'])
        fingerprint = digest({'operation': 'study.create', 'title': title, 'note': note, 'scan': scan,
                              'parent_review_id': parent_review_id, 'author_panel_ids': panels})
        with transaction(self.store.db_path) as connection:
            context.fence(connection)
            saved, fresh = self._save(connection, 'author_studies', prepared)
            replayed = self._receipt(connection, 'study.create', idempotency_key, fingerprint, saved)
            if fresh and replayed:
                raise ServiceError('Study receipt references a missing resource', 409)
            if fresh:
                self._insert(connection, 'author_studies', saved)
            if not replayed:
                self._write_receipt(connection, 'study.create', idempotency_key, fingerprint, saved)
        return deepcopy(_detail(body, saved))

    def get(self, identity):
        _id(identity, 'study')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return deepcopy(self._load(connection, identity, _Snapshot()))

    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT or type(offset) is not int or not 0 <= offset <= MAX_LIST_OFFSET:
            raise ServiceError('Study pagination requires limit 1..100 and offset 0..100000', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM author_studies').fetchone()[0]
            rows = connection.execute('SELECT id FROM author_studies ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            context = _Snapshot(); items = []
            for row in rows:
                value = self._load(connection, row['id'], context)
                items.append({key: value[key] for key in ('id', 'digest', 'created_at', 'title', 'note', 'parent_review_id',
                             'parent_study_id', 'verification_scope', 'raw_source_reverified')} |
                             {'plan': value['scan']['plan'], 'summary': value['result']['summary']})
            return deepcopy({'items': items, 'total': total, 'limit': limit, 'offset': offset})

    def markdown(self, identity):
        return _core().render_report(self.get(identity)['result'])

    def review(self, identity, study_digest, decision, note, actor, idempotency_key):
        _id(identity, 'study')
        if not isinstance(study_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', study_digest):
            raise ServiceError('Study digest is invalid', 422)
        if not isinstance(decision, str) or decision not in DECISIONS or actor not in ('human', 'automation'):
            raise ServiceError('Review decision or actor is invalid', 422)
        _string(note, 'Review note', 4000, nonblank=True); _string(idempotency_key, 'Idempotency key', 128, nonblank=True)
        context = _Snapshot()
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            study = self._load(connection, identity, context)
            if study['digest'] != study_digest:
                raise ServiceError('Review must bind the exact current study digest', 409)
        body = {'study_id': identity, 'study_digest': study_digest, 'decision': decision, 'note': note, 'actor': actor}
        prepared = _prepared(body, 'author_review_', study_id=identity)
        fingerprint = digest({'operation': 'study.review', **body})
        with transaction(self.store.db_path) as connection:
            context.fence(connection)
            saved, fresh = self._save(connection, 'author_study_reviews', prepared)
            replayed = self._receipt(connection, 'study.review', idempotency_key, fingerprint, saved)
            if fresh and replayed:
                raise ServiceError('Review receipt references a missing resource', 409)
            count = connection.execute('SELECT COUNT(*) FROM author_study_reviews WHERE study_id=?', (identity,)).fetchone()[0]
            if count > MAX_REVIEWS or fresh and count >= MAX_REVIEWS:
                raise ServiceError('Study reviews exceed the 1000-review bound', 413)
            if fresh:
                self._insert(connection, 'author_study_reviews', saved)
            if not replayed:
                self._write_receipt(connection, 'study.review', idempotency_key, fingerprint, saved)
        return deepcopy(_detail(body, saved))

    def reviews(self, identity):
        _id(identity, 'study')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN'); context = _Snapshot()
            study = self._load(connection, identity, context)
            count = connection.execute('SELECT COUNT(*) FROM author_study_reviews WHERE study_id=?', (identity,)).fetchone()[0]
            if count > MAX_REVIEWS:
                raise ServiceError('Study reviews exceed the 1000-review bound', 413)
            rows = connection.execute('SELECT * FROM author_study_reviews WHERE study_id=? ORDER BY created_at,id', (identity,)).fetchall()
            items = [_review_record(row) for row in rows]
            if any(item['study_digest'] != study['digest'] for item in items):
                raise ServiceError('Stored review is bound to a different study digest', 409)
            return deepcopy({'items': items})
