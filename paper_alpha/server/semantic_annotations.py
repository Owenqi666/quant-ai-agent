"""Source-bound immutable material labels, separate from results and claims."""
from contextlib import closing
from copy import deepcopy
import json
import re
import sqlite3

from .. import semantic_materials as materials
from ..storage import digest, json_text
from .author_panels import _metadata_digest, _verify_metadata
from .db import connect, transaction
from .service import ServiceError, now
from .semantic_annotations_schema import (
    SemanticAnnotationCreate, SemanticAnnotationDetail, SemanticAnnotationPreview,
    SemanticAnnotationPreviewRequest, SemanticAnnotationSummary,
    SemanticMaterialCollection, SemanticMaterialDetail,
)

SCHEMA = """
CREATE TABLE semantic_annotations(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,material_sha256 TEXT NOT NULL,case_id TEXT NOT NULL,source TEXT NOT NULL,reviewer TEXT NOT NULL,supersedes_id TEXT REFERENCES semantic_annotations(id));
CREATE UNIQUE INDEX semantic_annotations_root ON semantic_annotations(material_sha256,case_id,source,reviewer) WHERE supersedes_id IS NULL;
CREATE UNIQUE INDEX semantic_annotations_replacement ON semantic_annotations(supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX semantic_annotations_created ON semantic_annotations(created_at,id);
CREATE INDEX semantic_annotations_case ON semantic_annotations(material_sha256,case_id,source,reviewer);
CREATE TABLE semantic_annotation_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,annotation_id TEXT NOT NULL REFERENCES semantic_annotations(id),annotation_digest TEXT NOT NULL,annotation_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
MAX_BYTES = 256 * 1024
MAX_RECORDS = 500
MAX_CHAIN = 100
LIMITATIONS = [
    'Reviewer identity and source=human are local explicit declarations, not authentication.',
    'Hashes and source membership verify material provenance, not semantic truth.',
    'Reference drafts are unverified suggestions and never become human labels automatically.',
    'Material labels never approve an experiment, candidate result, or generated claim.',
    'Automation records are excluded from human coverage. Missing judgments remain unknown.',
    'No model output is evaluated here; semantic quality score remains null.',
    'Momentum anchors refer to a verified bundled source registry; this kit does not reverify its original PDF.',
]
PREVIEW_FIELDS = set(SemanticAnnotationPreview.model_fields)


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'semantic_annotation_[0-9a-f]{64}', value):
        raise ServiceError('Invalid semantic annotation identity', 422)


def _root_scope(value):
    return tuple(value[key] for key in ('material_sha256', 'case_id', 'source', 'reviewer'))


class SemanticAnnotations:
    def __init__(self, store):
        self.store = store

    def _material(self):
        try:
            return materials.load_material()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ServiceError('Bundled semantic material/source integrity verification failed', 409) from exc

    def source_file(self, source_id):
        self._material()
        try:
            return materials.source_file(source_id)
        except (OSError, ValueError, TypeError) as exc:
            raise ServiceError('Unknown or altered bundled semantic source', 422) from exc

    @staticmethod
    def _project(material, case):
        evidence = [deepcopy(item) for item in material['evidence'] if item['id'] in case['evidence_ids']]
        source_ids = {materials.SOURCE_IDS[item['source']] for item in evidence}
        for item in evidence:
            item['source_id'] = materials.SOURCE_IDS[item['source']]
        sources = []
        for source_id in sorted(source_ids):
            relative, expected, media_type = materials.SOURCE_FILES[source_id]
            sources.append({'source_id': source_id, 'path': relative, 'sha256': expected,
                            'media_type': media_type, 'verification': 'bundled_source_digest_verified',
                            'raw_pdf_reverified': source_id == 'alpha101_paper'})
        value = {'schema_version': 1, 'material_sha256': materials.MATERIAL_SHA256,
                 'case_id': case['id'], 'material_case_digest': digest(case),
                 'proposal': deepcopy(case['proposal']), 'evidence': evidence,
                 'review_focus': case['review_focus'], 'reference_draft': case['reference_draft'],
                 'reference_status': 'unverified_reference_draft', 'sources': sources,
                 'original_confirmation': case['human_confirmation'],
                 'scope': material['scope']}
        return SemanticMaterialDetail.model_validate(value).model_dump()

    def materials(self):
        material = self._material()
        return SemanticMaterialCollection.model_validate({
            'schema_version': 1, 'material_sha256': materials.MATERIAL_SHA256,
            'items': [self._project(material, case) for case in material['cases']],
            'total': len(material['cases']), 'limitations': LIMITATIONS,
        }).model_dump()

    def material(self, case_id):
        material = self._material()
        case = next((case for case in material['cases'] if case['id'] == case_id), None)
        if case is None:
            raise ServiceError('Semantic material case not found', 404)
        return self._project(material, case)

    @staticmethod
    def _request(value, create=False):
        try:
            model = SemanticAnnotationCreate if create else SemanticAnnotationPreviewRequest
            parsed = model.model_validate(value).model_dump()
            if len(json_text(parsed).encode()) > MAX_BYTES:
                raise ServiceError('Semantic annotation request exceeds 256 KiB', 413)
            return parsed
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ServiceError('Invalid explicit semantic declaration; values, paths and approval fields are forbidden', 422) from exc

    def _record(self, connection, row, ancestors=(), cache=None):
        try:
            if row['id'] in ancestors or len(ancestors) >= MAX_CHAIN or len(row['payload'].encode()) > MAX_BYTES:
                raise ValueError('Altered or excessive semantic replacement chain')
            if cache is not None and row['id'] in cache:
                detail, chain_length = cache[row['id']]
                if len(ancestors) + chain_length > MAX_CHAIN:
                    raise ValueError('Excessive semantic replacement chain')
                return detail
            body = json.loads(row['payload'])
            if set(body) != PREVIEW_FIELDS or json_text(body) != row['payload'] or digest(body) != row['digest']:
                raise ValueError('Altered/noncanonical semantic annotation')
            if row['id'] != 'semantic_annotation_' + row['digest']:
                raise ValueError('Semantic identity differs')
            _verify_metadata(row)
            if any(body[key] != row[key] for key in ('material_sha256', 'case_id', 'source', 'reviewer', 'supersedes_id')):
                raise ValueError('Semantic searchable metadata differs')
            annotation = {key: body[key] for key in ('case_id', 'reviewer', 'confirmed_at', 'dimensions')}
            checked = materials.validate_annotation(body['material_sha256'], annotation)
            if any(body[key] != checked[key] for key in checked):
                raise ValueError('Semantic declaration/source projection differs')
            if body['limitations'] != LIMITATIONS:
                raise ValueError('Semantic declaration limitations differ')
            if body['supersedes_id'] is not None:
                parent_row = connection.execute('SELECT * FROM semantic_annotations WHERE id=?', (body['supersedes_id'],)).fetchone()
                if parent_row is None:
                    raise ValueError('Semantic replacement parent is missing')
                parent = self._record(connection, parent_row, (*ancestors, row['id']), cache=cache)
                if (_root_scope(body) != _root_scope(parent) or body['supersedes_digest'] != parent['digest']
                        or materials.parse_timestamp(body['confirmed_at']) < materials.parse_timestamp(parent['confirmed_at'])):
                    raise ValueError('Semantic replacement scope/digest/time differs')
            elif body['supersedes_digest'] is not None:
                raise ValueError('Unbound replacement digest')
            detail = {**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}
            if SemanticAnnotationDetail.model_validate(detail).model_dump() != detail:
                raise ValueError('Semantic detail contract differs')
            if cache is not None:
                chain_length = 1 + (cache[body['supersedes_id']][1] if body['supersedes_id'] is not None else 0)
                if chain_length > MAX_CHAIN:
                    raise ValueError('Excessive semantic replacement chain')
                cache[row['id']] = (detail, chain_length)
            return detail
        except ServiceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as exc:
            raise ServiceError('Semantic annotation failed integrity verification', 409) from exc

    def _preview(self, connection, request):
        self._material()
        annotation = {key: request[key] for key in ('case_id', 'reviewer', 'confirmed_at', 'dimensions')}
        try:
            checked = materials.validate_annotation(request['material_sha256'], annotation)
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError('Semantic annotation does not bind the exact frozen material case', 422) from exc
        parent_digest = None
        parent_id = request['supersedes_id']
        if parent_id is not None:
            parent_row = connection.execute('SELECT * FROM semantic_annotations WHERE id=?', (parent_id,)).fetchone()
            if parent_row is None:
                raise ServiceError('Semantic replacement parent not found', 404)
            parent = self._record(connection, parent_row)
            if _root_scope(parent) != _root_scope(request):
                raise ServiceError('Replacement must retain material/case/source/reviewer scope', 422)
            if connection.execute('SELECT id FROM semantic_annotations WHERE supersedes_id=?', (parent_id,)).fetchone():
                raise ServiceError('Replacement parent is no longer the active chain endpoint', 409)
            if materials.parse_timestamp(request['confirmed_at']) < materials.parse_timestamp(parent['confirmed_at']):
                raise ServiceError('Replacement confirmation predates its parent', 422)
            parent_digest = parent['digest']
        elif connection.execute('SELECT id FROM semantic_annotations WHERE material_sha256=? AND case_id=? AND source=? AND reviewer=? AND supersedes_id IS NULL', _root_scope(request)).fetchone():
            raise ServiceError('Existing declaration requires an explicit replacement of its active endpoint', 409)
        value = {'schema_version': 1, 'material_sha256': request['material_sha256'], **checked,
                 'source': request['source'], 'supersedes_id': parent_id, 'supersedes_digest': parent_digest,
                 'claim_approval': False, 'software_verified_semantic_truth': False, 'limitations': LIMITATIONS}
        return SemanticAnnotationPreview.model_validate(value).model_dump()

    def _ledger_integrity(self, connection):
        rows = connection.execute('SELECT * FROM semantic_annotations LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
        receipts = connection.execute('SELECT * FROM semantic_annotation_receipts LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
        if len(rows) > MAX_RECORDS or len(receipts) > MAX_RECORDS:
            raise ServiceError('Semantic ledger exceeds its 500-record bound', 413)
        by_id = {row['id']: row for row in rows}
        covered = set()
        for receipt in receipts:
            body = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
            row = by_id.get(receipt['annotation_id'])
            if (digest(body) != receipt['receipt_digest'] or row is None
                    or (row['digest'], row['metadata_digest']) != (receipt['annotation_digest'], receipt['annotation_metadata_digest'])):
                raise ServiceError('Semantic annotation ledger/receipt binding failed integrity verification', 409)
            covered.add(row['id'])
        if covered != set(by_id):
            raise ServiceError('Semantic annotation has a missing durable creation receipt', 409)
        # Validate searchable columns BEFORE applying caller filters. Otherwise
        # a changed reviewer/source/case column can silently hide an old row.
        cache = {}
        for row in rows:
            self._record(connection, row, cache=cache)
        return cache

    def preview(self, **value):
        request = self._request(value)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            self._ledger_integrity(connection)
            return deepcopy(self._preview(connection, request))

    def create(self, **value):
        request = self._request(value, create=True)
        fingerprint = digest({key: value for key, value in request.items() if key != 'idempotency_key'})
        with transaction(self.store.db_path) as connection:
            cache = self._ledger_integrity(connection)
            receipt = connection.execute('SELECT * FROM semantic_annotation_receipts WHERE idempotency_key=?', (request['idempotency_key'],)).fetchone()
            if receipt is not None:
                body = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
                if digest(body) != receipt['receipt_digest']:
                    raise ServiceError('Semantic annotation receipt failed integrity verification', 409)
                if fingerprint != receipt['request_digest']:
                    raise ServiceError('Idempotency key belongs to a different semantic declaration', 409)
                row = connection.execute('SELECT * FROM semantic_annotations WHERE id=?', (receipt['annotation_id'],)).fetchone()
                if row is None or (row['digest'], row['metadata_digest']) != (receipt['annotation_digest'], receipt['annotation_metadata_digest']):
                    raise ServiceError('Semantic receipt binding differs', 409)
                return deepcopy(self._record(connection, row, cache=cache))
            if connection.execute('SELECT COUNT(*) FROM semantic_annotations').fetchone()[0] >= MAX_RECORDS:
                raise ServiceError('Semantic annotation history exceeds 500 records; preserve/export existing history', 413)
            body = self._preview(connection, request)
            body_digest = digest(body)
            identity, timestamp = 'semantic_annotation_' + body_digest, now()
            row = {'id': identity, 'payload': json_text(body), 'digest': body_digest, 'created_at': timestamp,
                   'metadata_digest': _metadata_digest(identity, body_digest, timestamp),
                   **{key: body[key] for key in ('material_sha256', 'case_id', 'source', 'reviewer', 'supersedes_id')}}
            try:
                connection.execute('INSERT INTO semantic_annotations VALUES (:id,:payload,:digest,:created_at,:metadata_digest,:material_sha256,:case_id,:source,:reviewer,:supersedes_id)', row)
            except sqlite3.IntegrityError as exc:
                raise ServiceError('Semantic declaration scope/replacement was committed concurrently', 409) from exc
            detail = self._record(connection, row)
            receipt_body = {'idempotency_key': request['idempotency_key'], 'request_digest': fingerprint,
                            'annotation_id': identity, 'annotation_digest': body_digest,
                            'annotation_metadata_digest': row['metadata_digest'], 'created_at': now()}
            connection.execute('INSERT INTO semantic_annotation_receipts VALUES (?,?,?,?,?,?,?)', (*receipt_body.values(), digest(receipt_body)))
            # Files and SQLite do not form one atomic domain. Recheck at the
            # last observable pre-commit fence; a changed source rolls back both
            # the new immutable annotation and its creation receipt.
            self._material()
            return deepcopy(detail)

    def get(self, identity):
        _identity(identity)
        self._material()
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            row = connection.execute('SELECT * FROM semantic_annotations WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ServiceError('Semantic annotation not found', 404)
            return deepcopy(self._record(connection, row, cache=cache))

    def list(self, limit=20, offset=0, case_id=None, source=None, reviewer=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ServiceError('Semantic annotation pagination requires limit 1..100 and offset 0..100000', 422)
        self._material()
        where, parameters = [], []
        if case_id is not None:
            self.material(case_id)
        if source is not None and source not in {'human', 'automation'}:
            raise ServiceError('Unknown semantic declaration source', 422)
        if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120):
            raise ServiceError('Reviewer filter must be a nonblank bounded declared identity', 422)
        for key, value in (('case_id', case_id), ('source', source), ('reviewer', reviewer)):
            if value is not None:
                where.append(key + '=?'); parameters.append(value)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            total = connection.execute('SELECT COUNT(*) FROM semantic_annotations' + clause, parameters).fetchone()[0]
            rows = connection.execute('SELECT * FROM semantic_annotations' + clause + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', [*parameters, limit, offset]).fetchall()
            return {'items': [self._record(connection, row, cache=cache) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def summary(self, reviewer=None):
        if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120):
            raise ServiceError('Reviewer filter must be a nonblank bounded declared identity', 422)
        material = self._material()
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            where, parameters = (' WHERE reviewer=?', [reviewer]) if reviewer is not None else ('', [])
            rows = connection.execute('SELECT * FROM semantic_annotations' + where + ' ORDER BY created_at,id LIMIT ?', [*parameters, MAX_RECORDS + 1]).fetchall()
            if len(rows) > MAX_RECORDS or sum(len(row['payload'].encode()) for row in rows) > 16 * 1024 * 1024:
                raise ServiceError('Semantic summary exceeds 500 records / 16 MiB', 413)
            records = [self._record(connection, row, cache=cache) for row in rows]
        replaced = {row['supersedes_id'] for row in records if row['supersedes_id'] is not None}
        active_human = [row for row in records if row['source'] == 'human' and row['id'] not in replaced]
        coverage = []
        for case in material['cases']:
            active = [row for row in active_human if row['case_id'] == case['id']]
            judgments = {tuple(row['dimensions'][name]['outcome'] for name in materials.DIMENSIONS) for row in active}
            if not active:
                status, unknown = 'pending', list(materials.DIMENSIONS)
            elif len(judgments) > 1:
                status = 'conflicting'
                unknown = [name for name in materials.DIMENSIONS if len({row['dimensions'][name]['outcome'] for row in active}) > 1
                           or any(row['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'} for row in active)]
            else:
                status = active[0]['declared_semantic_status']
                unknown = [name for name in materials.DIMENSIONS if active[0]['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'}]
            coverage.append({'case_id': case['id'], 'material_case_digest': digest(case),
                             'active_human_annotation_ids': [row['id'] for row in active],
                             'human_declared_status': status, 'unknown_dimensions': unknown})
        annotated = sum(item['human_declared_status'] != 'pending' for item in coverage)
        fully_assessed = sum(item['human_declared_status'] in {'passed', 'failed'} and not item['unknown_dimensions'] for item in coverage)
        summary_status = ('pending_human_confirmation' if not annotated else
                          'human_declarations_available' if fully_assessed == len(coverage) else 'partial_human_declarations')
        return SemanticAnnotationSummary.model_validate({
            'schema_version': 1, 'material_sha256': materials.MATERIAL_SHA256, 'reviewer': reviewer,
            'material_cases': len(coverage), 'records': len(records),
            'human_records': sum(row['source'] == 'human' for row in records),
            'automation_records': sum(row['source'] == 'automation' for row in records),
            'active_human_records': len(active_human), 'superseded_records': len(replaced),
            'human_annotated_cases': annotated, 'human_fully_assessed_cases': fully_assessed,
            'pending_cases': sum(item['human_declared_status'] == 'pending' for item in coverage),
            'conflicting_cases': sum(item['human_declared_status'] == 'conflicting' for item in coverage),
            'semantic_quality_score': None, 'status': summary_status, 'cases': coverage, 'limitations': LIMITATIONS,
        }).model_dump()

    def export(self, identity):
        return json_text(self.get(identity))
