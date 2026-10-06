"""Immutable exact-claim judgments; numeric verification is not semantic approval."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import sqlite3

from ..storage import digest, json_text
from .author_panels import _metadata_digest, _verify_metadata
from .claim_reviews_schema import (
    ClaimReviewCreate, ClaimReviewDetail, ClaimReviewPage, ClaimReviewPreview,
    ClaimReviewPreviewRequest, ClaimReviewStatus, ClaimReviewTarget,
)
from .db import connect, transaction
from .research_assessments import DIMENSIONS, parse_timestamp
from .research_cases import ResearchCases
from .research_claims import ResearchClaims
from .service import ServiceError, now

SCHEMA = """
CREATE TABLE claim_reviews(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,target_digest TEXT NOT NULL,claims_id TEXT NOT NULL REFERENCES research_claims(id),claim_id TEXT NOT NULL,source TEXT NOT NULL,reviewer TEXT NOT NULL,supersedes_id TEXT REFERENCES claim_reviews(id));
CREATE UNIQUE INDEX claim_reviews_root ON claim_reviews(target_digest,source,reviewer) WHERE supersedes_id IS NULL;
CREATE UNIQUE INDEX claim_reviews_replacement ON claim_reviews(supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX claim_reviews_created ON claim_reviews(created_at,id);
CREATE INDEX claim_reviews_target ON claim_reviews(target_digest,claims_id,claim_id,source,reviewer);
CREATE TABLE claim_review_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,review_id TEXT NOT NULL REFERENCES claim_reviews(id),review_digest TEXT NOT NULL,review_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
MAX_BYTES = 512 * 1024
MAX_RECORDS = 500
MAX_CHAIN = 100
MAX_LEDGER_BYTES = 16 * 1024 * 1024
LIMITATIONS = [
    'The reviewer and source=human are explicit local declarations, not authenticated authorship.',
    'This judgment applies only to the exact claim batch, wording, case and original source digests shown here.',
    'Tool-derived numeric values remain separate from narrative text and declared semantic judgments.',
    'Automation judgments never count as human confirmation; original claims remain automation drafts.',
    'Replacing a judgment preserves its history. A new claim batch requires a new judgment; approvals are not inherited.',
    'Human judgments do not change source verification, data or method limitations, or approve the original experiment.',
    'No software verification of semantic truth, model-quality score, portfolio authority or market-performance claim is made.',
]
PREVIEW_FIELDS = set(ClaimReviewPreview.model_fields)
SEARCH_FIELDS = ('target_digest', 'claims_id', 'claim_id', 'source', 'reviewer', 'supersedes_id')


def _identity(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'claim_review_[0-9a-f]{64}', identity):
        raise ServiceError('Invalid claim review identity', 422)


def _scope(value):
    return (value['target']['target_digest'], value['source'], value['reviewer'])


def _semantic_status(dimensions):
    outcomes = [dimensions[name]['outcome'] for name in DIMENSIONS]
    return 'failed' if 'failed' in outcomes else 'passed' if all(value == 'passed' for value in outcomes) else 'incomplete'


def _body_request(body):
    """Reconstruct the exact request fingerprint without a client idempotency key."""
    return {'claims_id': body['target']['claims_id'], 'claim_id': body['target']['claim_id'],
            'expected_target_digest': body['target']['target_digest'],
            **{key: body[key] for key in ('source', 'reviewer', 'confirmed_at', 'dimensions', 'supersedes_id')},
            'expected_supersedes_digest': body['supersedes_digest']}


class ClaimReviews:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _request(value, create=False):
        try:
            model = ClaimReviewCreate if create else ClaimReviewPreviewRequest
            parsed = model.model_validate(value).model_dump()
            if parse_timestamp(parsed['confirmed_at']) > datetime.now(timezone.utc):
                raise ValueError('Confirmation must not be in the future')
            if len(json_text(parsed).encode()) > MAX_BYTES:
                raise ServiceError('Claim review request exceeds 512 KiB', 413)
            return parsed
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ServiceError('Invalid explicit claim judgment; caller values, provenance and approval fields are forbidden', 422) from exc

    def target(self, claims_id, claim_id):
        if not isinstance(claim_id, str) or not claim_id.strip() or len(claim_id) > 120:
            raise ServiceError('Claim identity must be nonblank and bounded', 422)
        claims = ResearchClaims(self.store).get(claims_id)
        # The old draft payload is left untouched. A review additionally needs
        # a durable original creation receipt, rather than accepting an orphan
        # draft after its origin was deleted.
        with closing(connect(self.store.db_path)) as connection:
            original = connection.execute('SELECT * FROM research_claims WHERE id=?', (claims_id,)).fetchone()
            receipts = connection.execute('SELECT * FROM research_claim_receipts WHERE claims_id=? LIMIT ?', (claims_id, MAX_RECORDS + 1)).fetchall()
            if not receipts or len(receipts) > MAX_RECORDS:
                raise ServiceError('Original claim creation receipts are missing or exceed their verification bound', 409)
            expected_request = digest({'case_id': claims['case_id'], 'case_digest': claims['case_digest'], 'claims': claims['submitted_claims']})
            for receipt in receipts:
                body = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
                if (digest(body) != receipt['receipt_digest'] or receipt['request_digest'] != expected_request
                        or (receipt['claims_digest'], receipt['claims_metadata_digest']) != (original['digest'], original['metadata_digest'])):
                    raise ServiceError('Original claim receipt/source binding failed integrity verification', 409)
        claim = next((item for item in claims['claims'] if item['id'] == claim_id), None)
        if claim is None:
            raise ServiceError('Claim is not present in the exact saved batch', 404)
        case = ResearchCases(self.store).get(claims['case_id'])
        if case['digest'] != claims['case_digest']:
            raise ServiceError('Claim case digest differs from its original source', 409)
        context = case['context']
        body = {'schema_version': 1, 'claims_id': claims['id'], 'claims_digest': claims['digest'],
                'claim_id': claim_id, 'claim_digest': digest(claim),
                'case_id': case['id'], 'case_digest': case['digest'],
                **{key: case[key] for key in ('source_kind', 'source_id', 'source_digest')},
                'case_context_digest': digest(context), 'claim': deepcopy(claim),
                'evidence': deepcopy(context['evidence']), 'definitions': deepcopy(context['definitions']),
                'results': [{key: result[key] for key in ('id', 'kind', 'digest', 'summary')} for result in context['results']],
                'provenance': deepcopy(context['provenance']),
                **{key: context[key] for key in ('data_scope', 'method_scope', 'stop_reason')},
                'case_state': context['state'], 'case_limitations': deepcopy(context['limitations']),
                'original_claim_limitations': deepcopy(claims['limitations']), 'limitations': LIMITATIONS}
        value = {**body, 'target_digest': digest(body)}
        try:
            if ClaimReviewTarget.model_validate(value).model_dump() != value or len(json_text(value).encode()) > MAX_BYTES:
                raise ValueError('Target projection exceeds its canonical bound')
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ServiceError('Exact claim target exceeds the 512 KiB projection bound', 413) from exc
        return deepcopy(value)

    def _record(self, connection, row, ancestors=(), cache=None):
        try:
            if row['id'] in ancestors or len(ancestors) >= MAX_CHAIN or len(row['payload'].encode()) > MAX_BYTES:
                raise ValueError('Altered or excessive review replacement chain')
            if cache is not None and row['id'] in cache:
                detail, chain_length = cache[row['id']]
                if len(ancestors) + chain_length > MAX_CHAIN:
                    raise ValueError('Excessive review replacement chain')
                return detail
            body = json.loads(row['payload'])
            if set(body) != PREVIEW_FIELDS or json_text(body) != row['payload'] or digest(body) != row['digest']:
                raise ValueError('Noncanonical or altered claim review')
            if row['id'] != 'claim_review_' + row['digest']:
                raise ValueError('Review identity differs')
            _verify_metadata(row)
            projected = {key: body[key] for key in ('source', 'reviewer', 'supersedes_id')} | {
                key: body['target'][key] for key in ('target_digest', 'claims_id', 'claim_id')}
            if any(projected[key] != row[key] for key in SEARCH_FIELDS):
                raise ValueError('Review searchable metadata differs')
            request = self._request(_body_request(body))
            target = self.target(request['claims_id'], request['claim_id'])
            if target != body['target'] or target['target_digest'] != request['expected_target_digest']:
                raise ValueError('Exact claim/source projection changed')
            if (body['declared_semantic_status'] != _semantic_status(request['dimensions'])
                    or body['limitations'] != LIMITATIONS):
                raise ValueError('Declared semantic state or limitations differs')
            chain_length = 1
            if body['supersedes_id'] is not None:
                parent_row = connection.execute('SELECT * FROM claim_reviews WHERE id=?', (body['supersedes_id'],)).fetchone()
                if parent_row is None:
                    raise ValueError('Review replacement parent is missing')
                parent = self._record(connection, parent_row, (*ancestors, row['id']), cache=cache)
                if (_scope(body) != _scope(parent) or body['supersedes_digest'] != parent['digest']
                        or parse_timestamp(body['confirmed_at']) < parse_timestamp(parent['confirmed_at'])):
                    raise ValueError('Review replacement scope/digest/time differs')
                if cache is not None:
                    chain_length += cache[parent['id']][1]
            elif body['supersedes_digest'] is not None:
                raise ValueError('Unbound replacement digest')
            detail = {**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}
            if ClaimReviewDetail.model_validate(detail).model_dump() != detail or chain_length > MAX_CHAIN:
                raise ValueError('Review detail or chain contract differs')
            if cache is not None:
                cache[row['id']] = (detail, chain_length)
            return detail
        except ServiceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as exc:
            raise ServiceError('Claim review failed integrity verification', 409) from exc

    def _ledger_integrity(self, connection):
        # Bound bytes BEFORE fetching payloads, and verify every searchable
        # column BEFORE a caller filter can hide corrupt history.
        sizes = connection.execute('SELECT typeof(payload) AS storage_type,length(CAST(payload AS BLOB)) AS bytes FROM claim_reviews LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
        if any(row['storage_type'] != 'text' for row in sizes):
            raise ServiceError('Claim review payload storage type failed integrity verification', 409)
        if (len(sizes) > MAX_RECORDS or any(row['bytes'] > MAX_BYTES for row in sizes)
                or sum(row['bytes'] for row in sizes) > MAX_LEDGER_BYTES):
            raise ServiceError('Claim review ledger exceeds 500 records / 16 MiB / 512 KiB per record', 413)
        rows = connection.execute('SELECT * FROM claim_reviews').fetchall()
        receipts = connection.execute('SELECT * FROM claim_review_receipts LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
        if len(receipts) > MAX_RECORDS:
            raise ServiceError('Claim review receipts exceed their 500-record bound', 413)
        by_id, covered, cache = {row['id']: row for row in rows}, set(), {}
        for row in rows:
            self._record(connection, row, cache=cache)
        for receipt in receipts:
            try:
                body = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
                row = by_id.get(receipt['review_id'])
                if (digest(body) != receipt['receipt_digest'] or row is None
                        or (row['digest'], row['metadata_digest']) != (receipt['review_digest'], receipt['review_metadata_digest'])
                        or not isinstance(receipt['idempotency_key'], str) or not receipt['idempotency_key'].strip()
                        or len(receipt['idempotency_key']) > 128
                        or receipt['request_digest'] != digest(_body_request(cache[row['id']][0]))
                        or parse_timestamp(receipt['created_at']) < parse_timestamp(row['created_at'])
                        or parse_timestamp(receipt['created_at']) > datetime.now(timezone.utc)):
                    raise ValueError('Receipt/request binding differs')
                covered.add(row['id'])
            except (ValueError, TypeError, KeyError) as exc:
                raise ServiceError('Claim review durable receipt failed integrity verification', 409) from exc
        if covered != set(by_id):
            raise ServiceError('Claim review has a missing durable creation receipt', 409)
        return cache

    def _preview(self, connection, request, cache):
        target = self.target(request['claims_id'], request['claim_id'])
        if target['target_digest'] != request['expected_target_digest']:
            raise ServiceError('Expected claim target digest is stale or foreign', 409)
        parent_id, parent_digest = request['supersedes_id'], request['expected_supersedes_digest']
        proposed_scope = (target['target_digest'], request['source'], request['reviewer'])
        if parent_id is not None:
            parent_row = connection.execute('SELECT * FROM claim_reviews WHERE id=?', (parent_id,)).fetchone()
            if parent_row is None:
                raise ServiceError('Claim review replacement parent not found', 404)
            parent = self._record(connection, parent_row, cache=cache)
            if parent['digest'] != parent_digest:
                raise ServiceError('Expected replacement digest is stale or foreign', 409)
            if _scope(parent) != proposed_scope:
                raise ServiceError('Replacement must retain exact target/source/reviewer scope', 422)
            if connection.execute('SELECT id FROM claim_reviews WHERE supersedes_id=?', (parent_id,)).fetchone():
                raise ServiceError('Replacement parent is no longer the active endpoint', 409)
            if parse_timestamp(request['confirmed_at']) < parse_timestamp(parent['confirmed_at']):
                raise ServiceError('Replacement confirmation predates its parent', 422)
            if cache[parent_id][1] >= MAX_CHAIN:
                raise ServiceError('Claim review replacement chain exceeds 100 records', 413)
        elif connection.execute('SELECT id FROM claim_reviews WHERE target_digest=? AND source=? AND reviewer=? AND supersedes_id IS NULL', proposed_scope).fetchone():
            raise ServiceError('Existing judgment requires explicit replacement of its active endpoint', 409)
        value = {'schema_version': 1, 'target': target,
                 **{key: request[key] for key in ('source', 'reviewer', 'confirmed_at', 'dimensions', 'supersedes_id')},
                 'supersedes_digest': parent_digest, 'declared_semantic_status': _semantic_status(request['dimensions']),
                 'identity_scope': 'local_declaration_not_authenticated', 'original_result_approval': False,
                 'software_verified_semantic_truth': False, 'semantic_quality_score': None, 'limitations': LIMITATIONS}
        if len(json_text(value).encode()) > MAX_BYTES:
            raise ServiceError('Claim review projection exceeds 512 KiB', 413)
        return ClaimReviewPreview.model_validate(value).model_dump()

    def preview(self, **value):
        request = self._request(value)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            return deepcopy(self._preview(connection, request, cache))

    def create(self, **value):
        request = self._request(value, create=True)
        fingerprint = digest({key: value for key, value in request.items() if key != 'idempotency_key'})
        with transaction(self.store.db_path) as connection:
            cache = self._ledger_integrity(connection)
            receipt = connection.execute('SELECT * FROM claim_review_receipts WHERE idempotency_key=?', (request['idempotency_key'],)).fetchone()
            if receipt is not None:
                if receipt['request_digest'] != fingerprint:
                    raise ServiceError('Idempotency key belongs to a different exact claim judgment', 409)
                detail = cache[receipt['review_id']][0]
                # Repeat original-target verification rather than relying on the
                # ledger cache at the final pre-commit observation fence.
                if self.target(request['claims_id'], request['claim_id']) != detail['target']:
                    raise ServiceError('Original claim source changed during replay', 409)
                return deepcopy(detail)
            if len(cache) >= MAX_RECORDS:
                raise ServiceError('Claim review history exceeds 500 records; preserve/export history', 413)
            body = self._preview(connection, request, cache)
            fingerprint_body, timestamp = digest(body), now()
            identity = 'claim_review_' + fingerprint_body
            row = {'id': identity, 'payload': json_text(body), 'digest': fingerprint_body, 'created_at': timestamp,
                   'metadata_digest': _metadata_digest(identity, fingerprint_body, timestamp),
                   **{key: body[key] for key in ('source', 'reviewer', 'supersedes_id')},
                   **{key: body['target'][key] for key in ('target_digest', 'claims_id', 'claim_id')}}
            total_bytes = connection.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM claim_reviews').fetchone()[0]
            if total_bytes + len(row['payload'].encode()) > MAX_LEDGER_BYTES:
                raise ServiceError('Claim review history exceeds 16 MiB; preserve/export history', 413)
            try:
                connection.execute('INSERT INTO claim_reviews VALUES (:id,:payload,:digest,:created_at,:metadata_digest,:target_digest,:claims_id,:claim_id,:source,:reviewer,:supersedes_id)', row)
            except sqlite3.IntegrityError as exc:
                raise ServiceError('Claim judgment scope/replacement was committed concurrently', 409) from exc
            detail = self._record(connection, row, cache=cache)
            receipt_body = {'idempotency_key': request['idempotency_key'], 'request_digest': fingerprint,
                            'review_id': identity, 'review_digest': fingerprint_body,
                            'review_metadata_digest': row['metadata_digest'], 'created_at': now()}
            connection.execute('INSERT INTO claim_review_receipts VALUES (?,?,?,?,?,?,?)', (*receipt_body.values(), digest(receipt_body)))
            if self.target(request['claims_id'], request['claim_id']) != body['target']:
                raise ServiceError('Original claim source changed before commit', 409)
            return deepcopy(detail)

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            if identity not in cache:
                raise ServiceError('Claim review not found', 404)
            return deepcopy(cache[identity][0])

    def list(self, limit=20, offset=0, claims_id=None, claim_id=None, source=None, reviewer=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ServiceError('Claim review pagination requires limit 1..100 and offset 0..100000', 422)
        if claims_id is not None:
            if claim_id is not None:
                self.target(claims_id, claim_id)
            else:
                ResearchClaims(self.store).get(claims_id)
        elif claim_id is not None:
            raise ServiceError('A claim item filter must specify its exact saved batch', 422)
        if source is not None and source not in {'human', 'automation'}:
            raise ServiceError('Unknown claim review source filter', 422)
        if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120):
            raise ServiceError('Reviewer filter requires a nonblank bounded identity', 422)
        filters = {key: value for key, value in (('claims_id', claims_id), ('claim_id', claim_id), ('source', source), ('reviewer', reviewer)) if value is not None}
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            records = [entry[0] for entry in cache.values()]
            records = [record for record in records if all((record['target'][key] if key in {'claims_id', 'claim_id'} else record[key]) == value for key, value in filters.items())]
            records.sort(key=lambda record: (record['created_at'], record['id']), reverse=True)
            return ClaimReviewPage.model_validate({'items': records[offset:offset + limit], 'total': len(records), 'limit': limit, 'offset': offset}).model_dump()

    def status(self, claims_id, claim_id):
        target = self.target(claims_id, claim_id)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            cache = self._ledger_integrity(connection)
            records = [entry[0] for entry in cache.values() if entry[0]['target']['target_digest'] == target['target_digest']]
        records.sort(key=lambda record: (record['created_at'], record['id']))
        replaced = {record['supersedes_id'] for record in records if record['supersedes_id'] is not None}
        active = [record for record in records if record['id'] not in replaced]
        human = [record for record in active if record['source'] == 'human']
        judgments = {tuple(record['dimensions'][name]['outcome'] for name in DIMENSIONS) for record in human}
        if not human:
            status, unknown = 'pending', list(DIMENSIONS)
        elif len(judgments) > 1:
            status = 'conflicting'
            unknown = [name for name in DIMENSIONS if len({record['dimensions'][name]['outcome'] for record in human}) > 1
                       or any(record['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'} for record in human)]
        else:
            status = human[0]['declared_semantic_status']
            unknown = [name for name in DIMENSIONS if human[0]['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'}]
        return ClaimReviewStatus.model_validate({
            'schema_version': 1, 'target': target, 'records': len(records),
            'human_records': sum(record['source'] == 'human' for record in records),
            'automation_records': sum(record['source'] == 'automation' for record in records),
            'superseded_records': len(replaced), 'active_human_review_ids': [record['id'] for record in human],
            'active_automation_review_ids': [record['id'] for record in active if record['source'] == 'automation'],
            'human_declared_status': status, 'unknown_dimensions': unknown, 'semantic_quality_score': None,
            'original_result_approval': False, 'software_verified_semantic_truth': False, 'limitations': LIMITATIONS,
        }).model_dump()

    def export(self, identity):
        return json_text(self.get(identity))
