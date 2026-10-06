"""Immutable development reference versions and exact declaration comparisons."""
from contextlib import closing
from copy import deepcopy
import json
import re

from .. import semantic_evaluation as core
from .. import semantic_materials as materials
from ..storage import digest, json_text
from .author_panels import _metadata_digest, _verify_metadata
from .db import connect, transaction
from .semantic_annotations import SemanticAnnotations
from .semantic_evaluation_sets_schema import (
    SemanticEvaluationBundle, SemanticEvaluationComparisonCreate,
    SemanticEvaluationComparisonDetail, SemanticEvaluationSetCreate,
    SemanticEvaluationSetDetail, SemanticEvaluationSetPreviewRequest,
)
from .service import ServiceError, now

SCHEMA = """
CREATE TABLE semantic_evaluation_sets(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL);
CREATE INDEX semantic_evaluation_sets_created ON semantic_evaluation_sets(created_at,id);
CREATE TABLE semantic_evaluation_set_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,set_id TEXT NOT NULL REFERENCES semantic_evaluation_sets(id),set_digest TEXT NOT NULL,set_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
CREATE TABLE semantic_evaluation_comparisons(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,set_id TEXT NOT NULL REFERENCES semantic_evaluation_sets(id));
CREATE INDEX semantic_evaluation_comparisons_created ON semantic_evaluation_comparisons(set_id,created_at,id);
CREATE TABLE semantic_evaluation_comparison_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,comparison_id TEXT NOT NULL REFERENCES semantic_evaluation_comparisons(id),comparison_digest TEXT NOT NULL,comparison_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
MAX_RECORDS = 500
REQUEST_BYTES = 256 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024


def _identity(value, kind):
    if not isinstance(value, str) or not re.fullmatch('semantic_evaluation_' + kind + r'_[0-9a-f]{64}', value):
        raise ServiceError('Invalid semantic evaluation identity', 422)


def _parsed(model, value):
    try:
        return core.bounded(model.model_validate(value).model_dump(), REQUEST_BYTES)
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ServiceError('Invalid closed semantic evaluation request; all cases and dimensions must be explicit', 422) from exc


class SemanticEvaluationSets:
    def __init__(self, store):
        self.store = store
        self.annotations = SemanticAnnotations(store)

    @staticmethod
    def _budget(connection, additional=0):
        total = 0
        for table, maximum in (('semantic_evaluation_sets', core.MAX_BYTES),
                               ('semantic_evaluation_comparisons', REQUEST_BYTES)):
            # SQLite byte lengths are checked before fetching payload strings;
            # bounded row counts alone could otherwise allocate many GiB.
            invalid = connection.execute('SELECT 1 FROM ' + table +
                " WHERE typeof(payload)!='text' OR length(CAST(payload AS BLOB))>? LIMIT 1", (maximum,)).fetchone()
            if invalid:
                raise ServiceError('Semantic evaluation payload type/byte bound differs', 409)
            total += connection.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM ' + table).fetchone()[0]
        if total + additional > MAX_LEDGER_BYTES:
            raise ServiceError('Semantic evaluation total payload ledger exceeds 16 MiB', 413)

    @staticmethod
    def _rows(connection, table):
        rows = connection.execute('SELECT * FROM ' + table + ' LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
        if len(rows) > MAX_RECORDS:
            raise ServiceError('Semantic evaluation ledger exceeds 500 entries', 413)
        return rows

    @staticmethod
    def _receipts(connection, table, rows, name):
        receipts = SemanticEvaluationSets._rows(connection, table)
        by_id, covered = {row['id']: row for row in rows}, set()
        for receipt in receipts:
            body = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
            row = by_id.get(receipt[name + '_id'])
            try:
                materials.parse_timestamp(receipt['created_at'])
                if (not isinstance(receipt['idempotency_key'], str) or not receipt['idempotency_key'].strip()
                        or len(receipt['idempotency_key']) > 128
                        or not re.fullmatch(r'[0-9a-f]{64}', receipt['request_digest'])):
                    raise ValueError('Invalid semantic receipt fields')
            except (ValueError, TypeError, KeyError) as exc:
                raise ServiceError('Semantic evaluation durable receipt contract differs', 409) from exc
            if (digest(body) != receipt['receipt_digest'] or row is None
                    or (row['digest'], row['metadata_digest']) != (receipt[name + '_digest'], receipt[name + '_metadata_digest'])):
                raise ServiceError('Semantic evaluation durable receipt binding differs', 409)
            # Self-hashing a receipt is not enough: its request must identify
            # the exact original immutable resource, not another valid request.
            if name == 'set':
                original = SemanticEvaluationSets._payload(row, core.SET_FIELDS, core.MAX_BYTES)
                original_request = {'material_sha256': original['material_sha256'], 'case_ids': original['case_ids'],
                                    'expected_active_annotations': original['active_human_annotations']}
            else:
                original = SemanticEvaluationSets._payload(row, core.COMPARISON_FIELDS, REQUEST_BYTES)
                original_request = {key: original[key] for key in SemanticEvaluationComparisonCreate.model_fields if key != 'idempotency_key'}
            if receipt['request_digest'] != digest(original_request):
                raise ServiceError('Semantic receipt does not bind the original exact creation request', 409)
            covered.add(row['id'])
        if covered != set(by_id):
            raise ServiceError('Semantic evaluation resource has no durable creation receipt', 409)

    @staticmethod
    def _payload(row, fields, maximum):
        try:
            if not isinstance(row['payload'], str) or len(row['payload'].encode('utf-8')) > maximum:
                raise ValueError('Semantic evaluation row exceeds its bound')
            body = json.loads(row['payload'])
            if not isinstance(body, dict) or set(body) != fields or json_text(body) != row['payload'] or digest(body) != row['digest']:
                raise ValueError('Semantic evaluation canonical payload differs')
            _verify_metadata(row)
            return body
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ServiceError('Semantic evaluation row failed integrity verification', 409) from exc

    def _record(self, connection, row, annotation_cache):
        body = self._payload(row, core.SET_FIELDS, core.MAX_BYTES)
        try:
            value = core.verify_detail({**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}, root=materials.ROOT)
            for frozen in body['annotation_history']:
                current = annotation_cache.get(frozen['id'])
                if current is None or current[0] != frozen:
                    raise ValueError('Original frozen annotation record is missing or altered')
            return value
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
            raise ServiceError('Frozen semantic reference failed source/annotation integrity verification', 409) from exc

    def _comparison_record(self, row, sets):
        body = self._payload(row, core.COMPARISON_FIELDS, REQUEST_BYTES)
        try:
            if row['set_id'] != body['set_id'] or body['set_id'] not in sets:
                raise ValueError('Comparison searchable reference metadata differs')
            return core.verify_comparison({**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}, sets[body['set_id']])
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ServiceError('Semantic comparison failed exact-reference integrity verification', 409) from exc

    def _ledger(self, connection):
        self._budget(connection)
        annotation_cache = self.annotations._ledger_integrity(connection)
        rows = self._rows(connection, 'semantic_evaluation_sets')
        comparison_rows = self._rows(connection, 'semantic_evaluation_comparisons')
        self._receipts(connection, 'semantic_evaluation_set_receipts', rows, 'set')
        self._receipts(connection, 'semantic_evaluation_comparison_receipts', comparison_rows, 'comparison')
        sets = {row['id']: self._record(connection, row, annotation_cache) for row in rows}
        comparisons = {row['id']: self._comparison_record(row, sets) for row in comparison_rows}
        return annotation_cache, sets, comparisons

    def _preview(self, request, annotation_cache):
        material = self.annotations._material()
        if request['material_sha256'] != materials.MATERIAL_SHA256:
            raise ServiceError('Reference must name the exact frozen material SHA256', 422)
        try:
            return core.build_snapshot(request['case_ids'], material,
                                       [item[0] for item in annotation_cache.values()], core.sources_snapshot())
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
            raise ServiceError('Cannot freeze requested semantic material cases or sources', 422) from exc

    def preview(self, **value):
        request = _parsed(SemanticEvaluationSetPreviewRequest, value)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            annotations, _, _ = self._ledger(connection)
            return deepcopy(self._preview(request, annotations))

    @staticmethod
    def _receipt(connection, table, name, request, fingerprint, rows):
        receipt = connection.execute('SELECT * FROM ' + table + ' WHERE idempotency_key=?', (request['idempotency_key'],)).fetchone()
        if receipt is None:
            if connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] >= MAX_RECORDS:
                raise ServiceError('Semantic evaluation durable receipt history exceeds 500 entries', 413)
            return None
        if fingerprint != receipt['request_digest']:
            raise ServiceError('Idempotency key belongs to a different semantic evaluation request', 409)
        value = rows.get(receipt[name + '_id'])
        if value is None:
            raise ServiceError('Semantic evaluation receipt original resource is missing', 409)
        return deepcopy(value)

    @staticmethod
    def _insert_receipt(connection, table, name, request, fingerprint, row):
        body = {'idempotency_key': request['idempotency_key'], 'request_digest': fingerprint,
                name + '_id': row['id'], name + '_digest': row['digest'],
                name + '_metadata_digest': row['metadata_digest'], 'created_at': now()}
        connection.execute('INSERT INTO ' + table + ' VALUES (?,?,?,?,?,?,?)', (*body.values(), digest(body)))

    @staticmethod
    def _insert(connection, table, prefix, body, rows, *, set_id=None):
        fingerprint = digest(body)
        identity = prefix + fingerprint
        if identity in rows:
            row = connection.execute('SELECT * FROM ' + table + ' WHERE id=?', (identity,)).fetchone()
            return row, rows[identity]
        if len(rows) >= MAX_RECORDS:
            raise ServiceError('Semantic evaluation history exceeds 500 entries', 413)
        payload = json_text(body)
        SemanticEvaluationSets._budget(connection, len(payload.encode('utf-8')))
        timestamp = now()
        row = {'id': identity, 'payload': payload, 'digest': fingerprint, 'created_at': timestamp,
               'metadata_digest': _metadata_digest(identity, fingerprint, timestamp)}
        if set_id is not None:
            row['set_id'] = set_id
        keys = ','.join(row)
        connection.execute('INSERT INTO ' + table + '(' + keys + ') VALUES (' + ','.join(':' + key for key in row) + ')', row)
        return row, {**body, 'id': identity, 'digest': fingerprint, 'created_at': timestamp}

    def _final_fence(self, connection, reference):
        # SQLite serializes annotation writes. Files are a separate domain;
        # verify every pinned source and original record at the last precommit
        # fence, rolling back resource and receipt together on observed change.
        annotations = self.annotations._ledger_integrity(connection)
        self.annotations._material()
        core.verify_detail(reference, root=materials.ROOT)
        if any(annotations.get(item['id'], (None,))[0] != item for item in reference['annotation_history']):
            raise ServiceError('Original frozen annotation changed before commit', 409)

    def create(self, **value):
        request = _parsed(SemanticEvaluationSetCreate, value)
        fingerprint = digest({key: item for key, item in request.items() if key != 'idempotency_key'})
        try:
            with transaction(self.store.db_path) as connection:
                annotations, sets, _ = self._ledger(connection)
                replay = self._receipt(connection, 'semantic_evaluation_set_receipts', 'set', request, fingerprint, sets)
                if replay is not None:
                    self._final_fence(connection, replay)
                    return replay
                body = self._preview(request, annotations)
                if request['expected_active_annotations'] != body['active_human_annotations']:
                    raise ServiceError('Human reference selection changed; preview all active declarations again', 409)
                row, detail = self._insert(connection, 'semantic_evaluation_sets', 'semantic_evaluation_set_', body, sets)
                self._insert_receipt(connection, 'semantic_evaluation_set_receipts', 'set', request, fingerprint, row)
                self._final_fence(connection, detail)
                return deepcopy(SemanticEvaluationSetDetail.model_validate(detail).model_dump())
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
            raise ServiceError('Semantic reference failed final source verification', 409) from exc

    def get(self, identity):
        _identity(identity, 'set')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            _, sets, _ = self._ledger(connection)
            if identity not in sets:
                raise ServiceError('Semantic reference set not found', 404)
            return deepcopy(sets[identity])

    @staticmethod
    def _page(values, limit, offset):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ServiceError('Invalid bounded semantic evaluation pagination', 422)
        ordered = sorted(values, key=lambda item: (item['created_at'], item['id']), reverse=True)
        return {'items': deepcopy(ordered[offset:offset + limit]), 'total': len(ordered), 'limit': limit, 'offset': offset}

    def list(self, *, limit=20, offset=0):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            _, sets, _ = self._ledger(connection)
            return self._page(sets.values(), limit, offset)

    def compare(self, **value):
        request = _parsed(SemanticEvaluationComparisonCreate, value)
        fingerprint = digest({key: item for key, item in request.items() if key != 'idempotency_key'})
        try:
            with transaction(self.store.db_path) as connection:
                _, sets, comparisons = self._ledger(connection)
                replay = self._receipt(connection, 'semantic_evaluation_comparison_receipts', 'comparison', request, fingerprint, comparisons)
                if replay is not None:
                    self._final_fence(connection, sets[replay['set_id']])
                    return replay
                reference = sets.get(request['set_id'])
                if reference is None:
                    raise ServiceError('Semantic comparison reference set not found', 404)
                if request['set_digest'] != reference['digest']:
                    raise ServiceError('Semantic comparison binds a different reference digest', 409)
                try:
                    body = core.build_comparison(reference, request)
                except ValueError as exc:
                    raise ServiceError('Comparison requires all selected cases and all five dimensions exactly once', 422) from exc
                row, detail = self._insert(connection, 'semantic_evaluation_comparisons', 'semantic_evaluation_comparison_', body, comparisons, set_id=reference['id'])
                self._insert_receipt(connection, 'semantic_evaluation_comparison_receipts', 'comparison', request, fingerprint, row)
                self._final_fence(connection, reference)
                return deepcopy(SemanticEvaluationComparisonDetail.model_validate(detail).model_dump())
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
            raise ServiceError('Semantic comparison failed final source verification', 409) from exc

    def comparison(self, identity):
        _identity(identity, 'comparison')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            _, _, comparisons = self._ledger(connection)
            if identity not in comparisons:
                raise ServiceError('Semantic comparison not found', 404)
            return deepcopy(comparisons[identity])

    def comparisons(self, set_id, *, limit=20, offset=0):
        _identity(set_id, 'set')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            _, sets, comparisons = self._ledger(connection)
            if set_id not in sets:
                raise ServiceError('Semantic reference set not found', 404)
            # Filter only after checking every immutable row/searchable column.
            return self._page([value for value in comparisons.values() if value['set_id'] == set_id], limit, offset)

    def export(self, identity):
        _identity(identity, 'set')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            _, sets, comparisons = self._ledger(connection)
            if identity not in sets:
                raise ServiceError('Semantic reference set not found', 404)
            reference = sets[identity]
            selected = sorted((item for item in comparisons.values() if item['set_id'] == identity), key=lambda item: item['id'])
            body = {'schema_version': 1, 'kind': 'semantic_evaluation_set_portable_integrity_bundle',
                    'reference': reference,
                    'reference_metadata_digest': core.metadata_digest(identity, reference['digest'], reference['created_at']),
                    'comparisons': selected,
                    'comparison_metadata_digests': {item['id']: core.metadata_digest(item['id'], item['digest'], item['created_at']) for item in selected},
                    'binary_sources_included': False,
                    'scope': 'frozen_declared_reference_and_agreement_integrity_not_authenticated_authorship',
                    'limitations': core.LIMITATIONS}
            try:
                bundle = SemanticEvaluationBundle.model_validate(core.bounded({**body, 'manifest_digest': digest(body)})).model_dump()
                core.verify_bundle(bundle, root=materials.ROOT)
                return deepcopy(bundle)
            except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
                raise ServiceError('Semantic export exceeds bounds or failed offline integrity verification', 409) from exc
