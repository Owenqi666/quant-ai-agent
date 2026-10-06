"""Atomic receipts for explicitly identified user mutations.

The caller holds the same immediate transaction for the business row and receipt.
A replay acknowledges the original commit; it does not revalidate its artifacts.
"""
from __future__ import annotations

import json
import re

from ..storage import digest, json_text

LEGACY_RECEIPT_SCHEMA = """
CREATE TABLE mutation_receipts(
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    review_id TEXT REFERENCES reviews(id),
    case_id TEXT REFERENCES regression_cases(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation,idempotency_key),
    CHECK((operation='review.create' AND review_id IS NOT NULL AND case_id IS NULL)
       OR (operation='case.approve' AND case_id IS NOT NULL AND review_id IS NULL))
)
"""

RECEIPT_SCHEMA = """
CREATE TABLE mutation_receipts(
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    review_id TEXT REFERENCES reviews(id),
    case_id TEXT REFERENCES regression_cases(id),
    research_id TEXT REFERENCES researches(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation,idempotency_key),
    CHECK((operation='review.create' AND review_id IS NOT NULL AND case_id IS NULL AND research_id IS NULL)
       OR (operation='case.approve' AND case_id IS NOT NULL AND review_id IS NULL AND research_id IS NULL)
       OR (operation='research.create' AND research_id IS NOT NULL AND review_id IS NULL AND case_id IS NULL))
)
"""

RESOURCE_COLUMNS = {'review.create': 'review_id', 'case.approve': 'case_id', 'research.create': 'research_id'}

# Separate extension preserves every historical receipt column and byte. Schema
# seven readers must not open an upgraded workspace, but migration does not
# rebuild or reinterpret the already issued receipts.
WORKFLOW_RECEIPT_SCHEMA = """
CREATE TABLE workflow_mutation_receipts(
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    revision_id TEXT REFERENCES revisions(id),
    check_id TEXT REFERENCES regression_checks(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation,idempotency_key),
    CHECK((operation='revision.create' AND revision_id IS NOT NULL AND check_id IS NULL)
       OR (operation='regression.check' AND check_id IS NOT NULL AND revision_id IS NULL))
)
"""
WORKFLOW_RESOURCE_COLUMNS = {'revision.create': 'revision_id', 'regression.check': 'check_id'}


class ReceiptError(ValueError):
    """A reused key conflicts, or a stored receipt cannot be safely replayed."""


def validate_key(value):
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
        raise ValueError('Idempotency key must be a nonblank string of 1..128 characters')
    return value


def validate_review_target(attempt_id, result_digest, assessment=None):
    """Optional exact target for both generic and structured review requests."""
    if attempt_id is None and result_digest is None:
        return None
    if (not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 64
            or not isinstance(result_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', result_digest)):
        raise ValueError('Provide both expected_attempt_id and expected_result_digest with valid values')
    target = {'expected_attempt_id': attempt_id, 'expected_result_digest': result_digest}
    if assessment is not None and any(assessment[name] != value for name, value in target.items()):
        raise ValueError('Top-level review target must match the structured assessment target')
    return target


def request_digest(operation, payload):
    # Named, default-filled arguments and canonical JSON make object ordering
    # irrelevant. User text and interval representations remain exact values.
    return digest({'schema': 'mutation-request-v1', 'operation': operation, 'payload': payload})


def replay(connection, operation, key, fingerprint):
    if key is None:
        return None
    workflow = operation in WORKFLOW_RESOURCE_COLUMNS
    table = 'workflow_mutation_receipts' if workflow else 'mutation_receipts'
    row = connection.execute(
        f'SELECT * FROM {table} WHERE operation=? AND idempotency_key=?',
        (operation, key)).fetchone()
    if row is None:
        return None
    if row['request_digest'] != fingerprint:
        raise ReceiptError('Idempotency key was already used for a different request')
    try:
        response = json.loads(row['response'])
        resource_id = row[(WORKFLOW_RESOURCE_COLUMNS if workflow else RESOURCE_COLUMNS)[operation]]
        if (not isinstance(response, dict) or response.get('id') != resource_id
                or digest(response) != row['response_digest']):
            raise ValueError('Receipt mismatch')
    except (TypeError, ValueError) as exc:
        raise ReceiptError('Stored mutation receipt is invalid; no new record was created') from exc
    return response


def record(connection, operation, key, fingerprint, response):
    if key is None:
        return
    if operation in WORKFLOW_RESOURCE_COLUMNS:
        connection.execute(
            'INSERT INTO workflow_mutation_receipts VALUES (?,?,?,?,?,?,?,?)',
            (operation, key, fingerprint, json_text(response), digest(response),
             response['id'] if operation == 'revision.create' else None,
             response['id'] if operation == 'regression.check' else None, response['created_at']))
        return
    connection.execute(
        'INSERT INTO mutation_receipts(operation,idempotency_key,request_digest,response,response_digest,review_id,case_id,research_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (operation, key, fingerprint, json_text(response), digest(response),
         response['id'] if operation == 'review.create' else None,
         response['id'] if operation == 'case.approve' else None,
         response['id'] if operation == 'research.create' else None, response['created_at']))
