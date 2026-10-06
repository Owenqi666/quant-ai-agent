"""Immutable automation claim drafts with server-owned numeric displays.

Reference membership is a structural check, never a semantic judgment. No
claim operation creates a review, changes a result or executes an experiment.
"""
from contextlib import closing
from copy import deepcopy
import json
import math
import re

from .author_panels import _metadata_digest, _verify_metadata
from .db import connect, transaction
from .research_cases import ResearchCases
from .research_claims_schema import ClaimCreate, ClaimDetail, ClaimPreview, ClaimPreviewRequest
from .service import ServiceError, now
from ..storage import digest, json_text

SCHEMA = """
CREATE TABLE research_claims(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES research_cases(id));
CREATE INDEX research_claims_created ON research_claims(created_at,id);
CREATE INDEX research_claims_case ON research_claims(case_id,created_at,id);
CREATE TABLE research_claim_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,claims_id TEXT NOT NULL REFERENCES research_claims(id),claims_digest TEXT NOT NULL,claims_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
MAX_BYTES = 256 * 1024
FIELDS = {'case_id', 'case_digest', 'submitted_claims', 'claims', 'limitations'}
LIMITATIONS = [
    'This is an automation draft, not a human assessment or approval.',
    'Evidence membership, source attribution checks and literal matching do not prove that narrative text is true or faithful.',
    'Narrative text is untrusted and semantically unverified, including any numbers written in it. Only authoritative_display is server-derived.',
    'Numeric references identify the exact frozen computed result and its JSON pointer; result data and evaluation limitations still apply.',
    'Source assessments remain bound to their original exact result and do not automatically assess this new claim draft.',
]
DAILY_METRICS = {'mean_rank_ic', 'rank_ic_days', 'mean_gross_return', 'sum_gross_return',
                 'evaluated_days', 'eligible_days', 'skipped_days', 'purged_days',
                 'finite_factor_observations', 'possible_factor_observations', 'factor_coverage'}
MONTHLY_METRICS = {'months_total', 'months_evaluated', 'gross_months', 'turnover_months',
                   'mean_gross_return', 'mean_net_return_proxy', 'volatility_annualized_proxy',
                   'sharpe_annualized_proxy', 'max_drawdown_proxy', 'terminal_nav_proxy', 'mean_turnover_proxy'}
MONTHLY_SERIES = {'gross_return', 'net_return_proxy', 'turnover_proxy', 'cost_proxy', 'nav_proxy', 'drawdown_proxy'}
AUTHOR_COUNTS = {'assets', 'momentum_ready', 'momentum_missing', 'momentum_invalid', 'momentum_unrepresentable',
                 'momentum_dgw_ready', 'momentum_mv_ready', 'momentum_dgw_mv_ready', 'selected_ready'}
INDUSTRY_MOM_METRICS = {'months_total', 'months_evaluated', 'mean_gross_return',
                        'annualized_sample_volatility', 'annualized_mean_over_volatility_zero_rf',
                        'terminal_gross_return_index', 'max_gross_drawdown'}
INDUSTRY_MOM_SERIES = {'gross_return', 'gross_return_index', 'gross_drawdown', 'gross_exposure', 'net_exposure'}


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'research_claims_[0-9a-f]{64}', value):
        raise ServiceError('Invalid research claims identity', 422)


def _parts(pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 500:
        raise ServiceError('Metric reference requires a bounded JSON pointer', 422)
    raw = pointer[1:].split('/')
    if not 1 <= len(raw) <= 16 or any(re.search(r'~(?![01])', token) for token in raw):
        raise ServiceError('Metric JSON pointer escapes or depth are invalid', 422)
    parts = [token.replace('~1', '/').replace('~0', '~') for token in raw]
    if any(not token or token in {'.', '..', '__proto__', 'prototype', 'constructor'}
           or '\\' in token or '\x00' in token for token in parts):
        raise ServiceError('Unsafe or empty metric JSON pointer token', 422)
    return parts


def _index(value):
    return bool(re.fullmatch(r'0|[1-9][0-9]{0,7}', value))


def _metric_path(kind, parts):
    if kind == 'daily_candidates':
        return len(parts) == 4 and _index(parts[0]) and parts[1:3] == ['result', 'metrics'] and parts[3] in DAILY_METRICS
    if kind == 'monthly_portfolio':
        return (len(parts) == 3 and parts[0] == 'summary' and _index(parts[1]) and parts[2] in MONTHLY_METRICS
                or len(parts) == 5 and parts[0] == 'months' and _index(parts[1])
                and parts[2] == 'strategies' and _index(parts[3]) and parts[4] in MONTHLY_SERIES)
    if kind == 'industry_mom_portfolio':
        return (len(parts) == 3 and parts[0] == 'summary' and _index(parts[1]) and parts[2] in INDUSTRY_MOM_METRICS
                or len(parts) == 5 and parts[0] == 'months' and _index(parts[1])
                and parts[2] == 'strategies' and _index(parts[3]) and parts[4] in INDUSTRY_MOM_SERIES
                or len(parts) == 3 and parts[0] == 'months' and _index(parts[1])
                and parts[2] == 'difference_gross_return'
                or len(parts) == 2 and parts[0] == 'paired_comparison'
                and parts[1] in {'months_total', 'months_evaluated', 'mean_gross_difference'})
    return (kind == 'author_eligibility' and (
        len(parts) == 2 and parts[0] == 'summary' and parts[1] in {'months', 'months_meeting_threshold', 'min_selected', 'max_selected'}
        or len(parts) == 3 and parts[0] == 'months' and _index(parts[1]) and parts[2] in AUTHOR_COUNTS))


def _resolve(result, reference):
    parts = _parts(reference['pointer'])
    if not _metric_path(result['kind'], parts):
        raise ServiceError('JSON pointer does not select a supported computed metric or count', 422)
    value = json.loads(result['payload_json'])
    if digest(value) != result['digest']:
        raise ServiceError('Frozen metric payload failed integrity verification', 409)
    try:
        for part in parts:
            if isinstance(value, list):
                if not _index(part):
                    raise ValueError('Noncanonical array index')
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise ValueError('Cannot traverse a scalar')
    except (IndexError, KeyError, ValueError, TypeError) as exc:
        raise ServiceError('Metric JSON pointer does not resolve in the exact result', 422) from exc
    if type(value) not in (int, float) or isinstance(value, float) and not math.isfinite(value):
        raise ServiceError('Metric pointer must resolve to a finite numeric result; null and booleans are unsupported', 422)
    return {**reference, 'value': value,
            'display': reference['result_id'] + ':' + reference['pointer'] + ' = ' + json_text(value).strip(),
            'verification': 'verified_result_value'}


class ResearchClaims:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _request(case_id, case_digest, claims, idempotency_key=None):
        request = {'case_id': case_id, 'case_digest': case_digest, 'claims': claims}
        if idempotency_key is not None:
            request['idempotency_key'] = idempotency_key
        model = ClaimPreviewRequest if idempotency_key is None else ClaimCreate
        try:
            value = model.model_validate(request).model_dump()
            if len(json_text(value).encode('utf-8')) > MAX_BYTES:
                raise ServiceError('Claim request exceeds 256 KiB', 413)
            return value
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ServiceError('Invalid structured claim request; caller metric values and approval fields are forbidden', 422) from exc

    def _preview(self, request):
        case = ResearchCases(self.store).get(request['case_id'])
        if case['digest'] != request['case_digest']:
            raise ServiceError('Claim case digest is stale or foreign', 409)
        evidence = {item['id']: item for item in case['context']['evidence']}
        results = {item['id']: item for item in case['context']['results']}
        resolved = []
        for claim in request['claims']:
            if not set(claim['evidence_ids']) <= set(evidence):
                raise ServiceError('Claim cites evidence outside its exact case', 422)
            origins = {evidence[identity]['origin'] for identity in claim['evidence_ids']}
            kind, attribution = claim['kind'], claim['attribution']
            if kind == 'evidence_statement':
                origin = {'paper_original': 'paper', 'author_code': 'author_code'}.get(attribution)
                if origin is None or not origins or origins != {origin}:
                    raise ServiceError('Evidence statement attribution differs from its cited source origins', 422)
            elif kind == 'interpretation' and attribution not in {'model_conjecture', 'user_modification', 'project_convention', 'unresolved'}:
                raise ServiceError('Interpretation must remain an explicit conjecture, modification or convention', 422)
            elif kind == 'project_rule' and (attribution not in {'project_convention', 'user_modification'}
                                             or attribution == 'project_convention' and origins - {'project'}):
                raise ServiceError('Project rule cannot be promoted to a paper or author conclusion', 422)
            elif kind == 'metric' and attribution != 'project_convention':
                raise ServiceError('Computed project metrics cannot be attributed to original-paper conclusions', 422)
            metrics = []
            for reference in claim['metric_references']:
                if reference['case_id'] != case['id'] or reference['case_digest'] != case['digest']:
                    raise ServiceError('Metric reference belongs to a different case', 409)
                result = results.get(reference['result_id'])
                if result is None:
                    raise ServiceError('Metric result identity is outside the exact case', 422)
                if reference['result_digest'] != result['digest']:
                    raise ServiceError('Metric result digest is stale or foreign', 409)
                metrics.append(_resolve(result, reference))
            resolved.append({key: claim[key] for key in ('id', 'kind', 'attribution', 'evidence_ids')} |
                            {'narrative_text': claim['text'],
                             'evidence_verifications': [evidence[identity]['verification'] for identity in claim['evidence_ids']],
                             'metrics': metrics, 'authoritative_display': '\n'.join(item['display'] for item in metrics) if metrics else None,
                             'citation_integrity': 'verified' if claim['evidence_ids'] else 'not_applicable',
                             'semantic_fidelity': 'unverified', 'status': 'draft', 'actor': 'automation'})
        value = {'case_id': case['id'], 'case_digest': case['digest'], 'claims': resolved, 'limitations': LIMITATIONS}
        try:
            normalized = ClaimPreview.model_validate(value).model_dump()
            if normalized != value or len(json_text(value).encode()) > MAX_BYTES:
                raise ValueError('Claim projection exceeds contract')
        except (ValueError, TypeError) as exc:
            raise ServiceError('Resolved claim projection exceeds contract', 413) from exc
        return deepcopy(value)

    def preview(self, case_id, case_digest, claims):
        return self._preview(self._request(case_id, case_digest, claims))

    def _record(self, row):
        try:
            if len(row['payload'].encode()) > MAX_BYTES:
                raise ValueError('Claim record too large')
            body = json.loads(row['payload'])
            if set(body) != FIELDS or json_text(body) != row['payload'] or digest(body) != row['digest']:
                raise ValueError('Noncanonical or altered claim record')
            if row['id'] != 'research_claims_' + row['digest'] or row['case_id'] != body['case_id']:
                raise ValueError('Claim identity differs')
            _verify_metadata(row)
            preview = self.preview(body['case_id'], body['case_digest'], body['submitted_claims'])
            if any(preview[key] != body[key] for key in preview):
                raise ValueError('Claim reference resolution changed')
            detail = {**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}
            if ClaimDetail.model_validate(detail).model_dump() != detail:
                raise ValueError('Claim detail is not canonical')
            return detail
        except ServiceError:
            raise
        except (ValueError, KeyError, TypeError, RecursionError, OverflowError) as exc:
            raise ServiceError('Research claim record failed integrity verification', 409) from exc

    def create(self, case_id, case_digest, claims, idempotency_key):
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ServiceError('Claim creation requires a nonblank idempotency key of 1..128 characters', 422)
        request = self._request(case_id, case_digest, claims, idempotency_key)
        request_digest = digest({key: value for key, value in request.items() if key != 'idempotency_key'})
        with transaction(self.store.db_path) as connection:
            receipt = connection.execute('SELECT * FROM research_claim_receipts WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if receipt is not None:
                fields = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
                if digest(fields) != receipt['receipt_digest']:
                    raise ServiceError('Claim receipt failed integrity verification', 409)
                if receipt['request_digest'] != request_digest:
                    raise ServiceError('Idempotency key belongs to a different claim request', 409)
                row = connection.execute('SELECT * FROM research_claims WHERE id=?', (receipt['claims_id'],)).fetchone()
                if row is None or row['digest'] != receipt['claims_digest'] or row['metadata_digest'] != receipt['claims_metadata_digest']:
                    raise ServiceError('Claim receipt binding differs', 409)
                return self._record(row)
            preview = self._preview(request)
            body = {**preview, 'submitted_claims': request['claims']}
            fingerprint = digest(body); identity = 'research_claims_' + fingerprint
            row = connection.execute('SELECT * FROM research_claims WHERE id=?', (identity,)).fetchone()
            if row is None:
                timestamp = now()
                row = {'id': identity, 'payload': json_text(body), 'digest': fingerprint, 'created_at': timestamp,
                       'metadata_digest': _metadata_digest(identity, fingerprint, timestamp), 'case_id': case_id}
                connection.execute('INSERT INTO research_claims VALUES (:id,:payload,:digest,:created_at,:metadata_digest,:case_id)', row)
            detail = self._record(row)
            receipt_body = {'idempotency_key': idempotency_key, 'request_digest': request_digest, 'claims_id': identity,
                            'claims_digest': fingerprint, 'claims_metadata_digest': row['metadata_digest'], 'created_at': now()}
            connection.execute('INSERT INTO research_claim_receipts VALUES (?,?,?,?,?,?,?)', (*receipt_body.values(), digest(receipt_body)))
            return deepcopy(detail)

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT * FROM research_claims WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ServiceError('Research claims not found', 404)
            return self._record(row)

    def list(self, limit=20, offset=0, case_id=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ServiceError('Claim pagination requires limit 1..100 and offset 0..100000', 422)
        where, parameters = '', []
        if case_id is not None:
            if not isinstance(case_id, str) or not re.fullmatch(r'research_case_[0-9a-f]{64}', case_id):
                raise ServiceError('Invalid claim case filter', 422)
            where, parameters = ' WHERE case_id=?', [case_id]
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM research_claims' + where, parameters).fetchone()[0]
            rows = connection.execute('SELECT * FROM research_claims' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                                      [*parameters, limit, offset]).fetchall()
            items = []
            for row in rows:
                detail = self._record(row)
                items.append({key: detail[key] for key in ('id', 'digest', 'case_id', 'case_digest', 'created_at')} |
                             {'count': len(detail['claims']), 'semantic_fidelity': 'unverified'})
            return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def markdown(self, identity):
        detail = self.get(identity)
        lines = ['# Structured research claim draft', '', 'Identity: ' + identity, 'Digest: ' + detail['digest'],
                 'Case: ' + detail['case_id'], 'Case digest: ' + detail['case_digest'], '',
                 'Status: automation draft; narrative semantics unverified; no human approval.', '']
        for claim in detail['claims']:
            lines += ['## ' + claim['id'], '', 'Kind: ' + claim['kind'] + '; attribution: ' + claim['attribution'],
                      'Evidence: ' + ', '.join(claim['evidence_ids']), '', 'Unverified narrative:', '',
                      *['> ' + line for line in claim['narrative_text'].splitlines()], '']
            if claim['authoritative_display'] is not None:
                lines += ['Server-derived numeric display:', '', '```text', claim['authoritative_display'], '```', '']
        lines += ['## Limitations', ''] + ['- ' + item for item in detail['limitations']] + ['']
        return '\n'.join(lines)
