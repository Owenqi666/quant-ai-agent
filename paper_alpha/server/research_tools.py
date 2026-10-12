"""Bounded local diagnostic tools; no provider, result import or human approval.

Each call is durably charged before execution. A process-held session lock
serializes callers without holding SQLite while verifying source artifacts.
An unfinished call conservatively consumes its reserved remaining time on
recovery. Read tools cannot be forcibly interrupted inside a source verifier;
their deadline is checked before and after execution, and late values discarded.
"""
from contextlib import closing, contextmanager
import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import time

from pydantic import ValidationError
from .db import connect, transaction
from .service import ServiceError, now
from .research_tools_schema import ToolBudget, ToolSession, ToolCall, ToolProposal
from ..storage import digest, json_text

SCHEMA = """
CREATE TABLE research_tool_sessions(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES research_cases(id),charged_count INTEGER NOT NULL,ledger_digest TEXT NOT NULL);
CREATE INDEX research_tool_sessions_created ON research_tool_sessions(created_at,id);
CREATE TABLE research_tool_calls(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES research_tool_sessions(id),sequence INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(session_id,sequence),UNIQUE(session_id,idempotency_key));
"""
TOOLS = {
    'read_case': ('Read the verified frozen research context and its limitations.', 'read_only'),
    'read_evidence': ('Read the case evidence with its exact verification scope.', 'read_only'),
    'read_result': ('Read deterministic source results, their IDs and digests.', 'read_only'),
    'propose_next_action': ('Save an unverified automation draft for an allowed action.', 'automation_draft'),
}
FATAL_CODES = {'SOURCE_INTEGRITY', 'SOURCE_UNAVAILABLE', 'RESPONSE_TOO_LARGE'}
MAX_PAYLOAD = 16 * 1024 * 1024
MAX_SESSION_BYTES = 16 * 1024 * 1024
# Preserve room for every remaining bounded request and its failure receipt.
MAX_CALL_RECEIPT_BYTES = 96 * 1024


class ToolServiceError(ServiceError):
    def __init__(self, message, status=409, *, code, retryable=False):
        super().__init__(message, status)
        self.code, self.retryable = code, retryable


def _error(code, message, retryable=False):
    return {'ok': False, 'error': {'code': code, 'message': message, 'retryable': retryable},
            'case_json': None, 'evidence_json': None, 'result_json': None, 'proposal': None}


def _success(**value):
    return {'ok': True, 'error': None, 'case_json': None, 'evidence_json': None,
            'result_json': None, 'proposal': None, **value}


def _text(value, label, maximum, *, nonblank=True):
    if not isinstance(value, str) or len(value) > maximum or (nonblank and not value.strip()):
        raise ToolServiceError(label + ' is invalid', 422, code='ARGUMENTS_INVALID')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ToolServiceError(label + ' must contain valid Unicode', 422, code='ARGUMENTS_INVALID') from exc
    return value


def _identity(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + '[0-9a-f]{64}', value):
        raise ToolServiceError('Identity is invalid', 422, code='ARGUMENTS_INVALID')
    return value


def _parse(payload):
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_PAYLOAD:
        raise ValueError('Invalid stored payload size')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate stored key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('Nonfinite stored value')
    value = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    if json_text(value) != payload:
        raise ValueError('Stored payload is not canonical')
    return value


def _canonical_arguments(arguments):
    # Reject unsafe/unbounded serialization before creating any ledger entry.
    # Unknown serializable fields remain in the entry and fail tool validation.
    if not isinstance(arguments, dict):
        raise ToolServiceError('Arguments must be an object', 422, code='ARGUMENTS_INVALID')
    try:
        def visit(value, depth=0):
            if depth > 8:
                raise ValueError('Arguments nesting exceeds the bound')
            if isinstance(value, str):
                value.encode('utf-8')
            elif value is None or type(value) in (bool, int):
                pass
            elif type(value) is float:
                if not math.isfinite(value):
                    raise ValueError('Nonfinite number')
            elif isinstance(value, list):
                if len(value) > 100:
                    raise ValueError('Array exceeds bound')
                for item in value:
                    visit(item, depth + 1)
            elif isinstance(value, dict):
                if len(value) > 20 or not all(isinstance(key, str) for key in value):
                    raise ValueError('Object exceeds bound')
                for key, item in value.items():
                    visit(key, depth + 1); visit(item, depth + 1)
            else:
                raise ValueError('Not a JSON value')
        visit(arguments)
        # Pydantic emits explicit null defaults; they have no tool meaning.
        value = {key: item for key, item in arguments.items()
                 if not (key in {'action', 'rationale', 'evidence_ids'} and item is None)}
        text = json_text(value)
        if len(text.encode('utf-8')) > 32768:
            raise ValueError('Arguments exceed 32 KiB')
        return value
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ToolServiceError('Arguments must be bounded finite UTF-8 JSON', 422, code='ARGUMENTS_INVALID') from exc


class ResearchTools:
    def __init__(self, store):
        self.store = store

    def capabilities(self):
        return {'provider_connected': False,
                'tools': [{'name': name, 'description': info[0], 'authority': info[1]} for name, info in TOOLS.items()],
                'forbidden': ['import_counts_or_metrics', 'write_human_review', 'modify_thresholds',
                              'execute_author_portfolio', 'access_final_test', 'execute_code', 'read_arbitrary_files'],
                'budget_scope': 'One user-created immutable session; the tool caller cannot create or replenish sessions. Persisted UTF-8 call payloads are limited to 16 MiB, with room reserved for bounded error receipts.',
                'time_policy': 'Cumulative active-call time, not idle time; checked before/after reads. An interrupted call consumes all its reserved remaining time. Late values are discarded.',
                'review_policy': 'Proposals are unverified automation drafts. They do not create approval, change research results or execute actions.'}

    def _case(self, session):
        from .research_cases import ResearchCases
        case = ResearchCases(self.store).get(session['case_id'])
        if case['digest'] != session['case_digest']:
            raise ServiceError('Case digest changed', 409)
        return case

    def _session_row(self, row):
        try:
            body = _parse(row['payload'])
            if set(body) != {'case_id', 'case_digest', 'budget', 'idempotency_key'}:
                raise ValueError('Unexpected session fields')
            _identity(body['case_id'], 'research_case_'); _identity(body['case_digest'], '')
            _text(body['idempotency_key'], 'Idempotency key', 128)
            ToolBudget.model_validate(body['budget'])
            fingerprint = digest(body)
            if (row['digest'] != fingerprint or row['id'] != 'tool_session_' + fingerprint
                    or row['case_id'] != body['case_id'] or row['idempotency_key'] != body['idempotency_key']
                    or row['request_digest'] != fingerprint
                    or row['metadata_digest'] != digest({'id': row['id'], 'digest': fingerprint, 'created_at': row['created_at']})):
                raise ValueError('Session metadata differs')
            return {'id': row['id'], 'digest': fingerprint, 'created_at': row['created_at'],
                    'case_id': body['case_id'], 'case_digest': body['case_digest'], 'budget': body['budget']}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ToolServiceError('Stored tool session failed integrity verification', code='LEDGER_INTEGRITY') from exc

    def _call_row(self, row):
        try:
            body = _parse(row['payload'])
            if json_text(ToolCall.model_validate(body).model_dump()) != row['payload']:
                raise ValueError('Call is not canonical')
            request = {'tool': body['tool'], 'arguments': _parse(body['arguments_json'])}
            expected_id = 'tool_call_' + digest({'session_id': row['session_id'], 'idempotency_key': row['idempotency_key']})
            if (row['digest'] != digest(body) or row['id'] != expected_id or body['id'] != expected_id
                    or row['request_digest'] != digest(request) or body['request_digest'] != digest(request)
                    or row['sequence'] != body['sequence'] or body['elapsed_seconds'] < 0
                    or (body['status'] == 'running') != (body['response'] is None)
                    or (body['status'] == 'running') != (body['finished_at'] is None)):
                raise ValueError('Call metadata differs')
            response = body['response']
            if response is not None:
                if response['ok'] != (body['status'] == 'completed') or response['ok'] == (response['error'] is not None):
                    raise ValueError('Call response status differs')
            return body
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ToolServiceError('Stored tool call failed integrity verification', code='LEDGER_INTEGRITY') from exc

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM research_tool_sessions WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ToolServiceError('Tool session not found', 404, code='SESSION_NOT_FOUND')
        session = self._session_row(row)
        rows = connection.execute('SELECT * FROM research_tool_calls WHERE session_id=? ORDER BY sequence', (identity,)).fetchall()
        if sum(len(item['payload'].encode('utf-8')) for item in rows) > MAX_SESSION_BYTES:
            raise ToolServiceError('Tool ledger exceeds its persistent byte budget', code='LEDGER_INTEGRITY')
        calls = [self._call_row(item) for item in rows]
        if (len(calls) != row['charged_count'] or row['ledger_digest'] != digest([item['digest'] for item in rows])
                or len(calls) > session['budget']['max_calls']
                or [item['sequence'] for item in calls] != list(range(1, len(calls) + 1))):
            raise ToolServiceError('Tool ledger order or call bound changed', code='LEDGER_INTEGRITY')
        usage = {'calls': len(calls), 'errors': sum(item['status'] in {'failed', 'interrupted'} for item in calls),
                 'elapsed_seconds': sum((item['elapsed_seconds'] for item in calls), 0.0)}
        fatal = any(item['response'] and item['response']['error'] and item['response']['error']['code'] in FATAL_CODES for item in calls)
        exhausted = (usage['calls'] >= session['budget']['max_calls'] or usage['errors'] >= session['budget']['max_errors']
                     or usage['elapsed_seconds'] >= session['budget']['max_seconds'])
        status = 'blocked' if fatal else 'running' if any(item['status'] == 'running' for item in calls) else 'exhausted' if exhausted else 'active'
        return {**session, 'usage': usage, 'status': status, 'calls': calls}

    def create(self, case_id, case_digest, budget, idempotency_key):
        _identity(case_id, 'research_case_'); _identity(case_digest, '')
        _text(idempotency_key, 'Idempotency key', 128)
        try:
            budget = ToolBudget.model_validate(budget).model_dump()
        except ValidationError as exc:
            raise ToolServiceError('Invalid tool budget', 422, code='ARGUMENTS_INVALID') from exc
        body = {'case_id': case_id, 'case_digest': case_digest, 'budget': budget, 'idempotency_key': idempotency_key}
        fingerprint = digest(body)
        # A replay returns the historical immutable session without refreshing budgets.
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT * FROM research_tool_sessions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if row is not None:
                self._session_row(row)
                if row['request_digest'] != fingerprint:
                    raise ToolServiceError('Session idempotency key has another request', code='IDEMPOTENCY_CONFLICT')
                return self._load(connection, row['id'])
        try:
            self._case(body)
        except ServiceError as exc:
            raise ToolServiceError('Research case cannot be verified', exc.status, code='SOURCE_INTEGRITY') from exc
        identity, created_at = 'tool_session_' + fingerprint, now()
        with transaction(self.store.db_path) as connection:
            row = connection.execute('SELECT * FROM research_tool_sessions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if row is not None:
                self._session_row(row)
                if row['request_digest'] != fingerprint:
                    raise ToolServiceError('Session idempotency key has another request', code='IDEMPOTENCY_CONFLICT')
                return self._load(connection, row['id'])
            connection.execute('INSERT INTO research_tool_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (identity, json_text(body), fingerprint, created_at,
                                digest({'id': identity, 'digest': fingerprint, 'created_at': created_at}),
                                idempotency_key, fingerprint, case_id, 0, digest([])))
            return self._load(connection, identity)

    def get(self, identity):
        _identity(identity, 'tool_session_')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._load(connection, identity)

    def list(self, limit=20, offset=0, case_id=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ToolServiceError('Invalid tool-session page', 422, code='ARGUMENTS_INVALID')
        if case_id is not None:
            _identity(case_id, 'research_case_')
        where, values = (' WHERE case_id=?', (case_id,)) if case_id else ('', ())
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM research_tool_sessions' + where, values).fetchone()[0]
            rows = connection.execute('SELECT id FROM research_tool_sessions' + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
            items = []
            for row in rows:
                session = self._load(connection, row['id'])
                session.pop('calls')
                items.append(session)
            return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    @contextmanager
    def _lock(self, identity):
        path = self.store.root / ('.' + identity + '.lock')
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ToolServiceError('Session lock is not a regular file', code='LEDGER_INTEGRITY')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ToolServiceError('Session has an active call; retry the same key', code='SESSION_BUSY', retryable=True) from exc
            yield
        finally:
            os.close(descriptor)

    def _dispatch(self, session, tool, arguments):
        if tool not in TOOLS:
            return _error('TOOL_DENIED', 'This capability is not exposed to the tool caller.')
        if tool != 'propose_next_action' and arguments:
            return _error('ARGUMENTS_INVALID', 'Read tools accept no arguments.')
        if tool == 'propose_next_action':
            if set(arguments) != {'action', 'rationale', 'evidence_ids'}:
                return _error('ARGUMENTS_INVALID', 'A proposal accepts only action, rationale and evidence_ids.')
            try:
                proposal = ToolProposal.model_validate({**arguments, 'actor': 'automation', 'status': 'draft', 'semantic_fidelity': 'unverified'}).model_dump()
                if not proposal['rationale'].strip() or len(set(proposal['evidence_ids'])) != len(proposal['evidence_ids']):
                    raise ValueError('Blank or duplicate proposal fields')
                if any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in proposal['evidence_ids']):
                    raise ValueError('Invalid evidence identity')
            except (ValueError, TypeError) as exc:
                return _error('ARGUMENTS_INVALID', 'Proposal fields are invalid or outside the declared action vocabulary.')
        case = self._case(session)
        if tool == 'read_case':
            return _success(case_json=json_text(case))
        if tool == 'read_evidence':
            return _success(evidence_json=json_text(case['context']['evidence']))
        if tool == 'read_result':
            return _success(result_json=json_text(case['context']['results']))
        if proposal['action'] not in case['context']['allowed_actions']:
            return _error('ACTION_NOT_ALLOWED', 'Action is not allowed for the frozen research state.')
        if not set(proposal['evidence_ids']) <= {item['id'] for item in case['context']['evidence']}:
            return _error('EVIDENCE_NOT_FOUND', 'Proposal references evidence outside this research case.')
        return _success(proposal=proposal)

    def _save_call(self, connection, identity, body):
        connection.execute('UPDATE research_tool_calls SET payload=?,digest=? WHERE id=?', (json_text(body), digest(body), identity))
        session_id = connection.execute('SELECT session_id FROM research_tool_calls WHERE id=?', (identity,)).fetchone()[0]
        self._anchor(connection, session_id)

    def _anchor(self, connection, session_id):
        values = [row[0] for row in connection.execute('SELECT digest FROM research_tool_calls WHERE session_id=? ORDER BY sequence', (session_id,))]
        connection.execute('UPDATE research_tool_sessions SET charged_count=?,ledger_digest=? WHERE id=?',
                           (len(values), digest(values), session_id))

    def call(self, identity, tool, arguments, idempotency_key):
        _identity(identity, 'tool_session_')
        _text(tool, 'Tool name', 80); _text(idempotency_key, 'Idempotency key', 128)
        arguments = _canonical_arguments(arguments)
        request_digest = digest({'tool': tool, 'arguments': arguments})
        with self._lock(identity):
            with transaction(self.store.db_path) as connection:
                session = self._load(connection, identity)
                for item in session['calls']:
                    if item['status'] == 'running':
                        item = {**item, 'status': 'interrupted', 'finished_at': now(),
                                'response': _error('CALL_INTERRUPTED', 'An unfinished call consumed its reserved remaining time; budget is not restored.')}
                        self._save_call(connection, item['id'], item)
            # Recovery must commit even if a different subsequent request is
            # rejected by the now-exhausted budget or a conflicting key.
            with transaction(self.store.db_path) as connection:
                session = self._load(connection, identity)
                prior = connection.execute('SELECT * FROM research_tool_calls WHERE session_id=? AND idempotency_key=?', (identity, idempotency_key)).fetchone()
                if prior is not None:
                    record = self._call_row(prior)
                    if prior['request_digest'] != request_digest:
                        raise ToolServiceError('Call idempotency key has another request', code='IDEMPOTENCY_CONFLICT')
                    return record
                if session['status'] == 'blocked':
                    raise ToolServiceError('Source verification failed; this session is closed', code='SESSION_BLOCKED')
                if session['status'] == 'exhausted':
                    raise ToolServiceError('Tool session budget is exhausted', 429, code='BUDGET_EXHAUSTED')
                remaining = session['budget']['max_seconds'] - session['usage']['elapsed_seconds']
                call_id = 'tool_call_' + digest({'session_id': identity, 'idempotency_key': idempotency_key})
                record = {'id': call_id, 'sequence': len(session['calls']) + 1, 'tool': tool,
                          'arguments_json': json_text(arguments), 'request_digest': request_digest,
                          'status': 'running', 'started_at': now(), 'finished_at': None,
                          'elapsed_seconds': float(remaining), 'response': None}
                connection.execute('INSERT INTO research_tool_calls VALUES(?,?,?,?,?,?,?)',
                                   (call_id, identity, record['sequence'], idempotency_key, request_digest, json_text(record), digest(record)))
                self._anchor(connection, identity)
            started = time.monotonic()
            try:
                response = self._dispatch(session, tool, arguments)
            except ServiceError as exc:
                response = _error('SOURCE_UNAVAILABLE' if exc.status == 404 else 'SOURCE_INTEGRITY', 'The bound research source could not be verified; no result is trusted.')
            except sqlite3.OperationalError:
                response = _error('STORE_BUSY', 'The source store is temporarily unavailable.', True)
            except Exception:
                response = _error('INTERNAL_ERROR', 'Tool execution failed; inspect local diagnostics before starting another session.')
            elapsed = max(0.0, time.monotonic() - started)
            if elapsed >= remaining:
                response = _error('TIME_BUDGET_EXHAUSTED', 'The call exceeded the remaining active-time budget; its output was discarded.')
            record = {**record, 'status': 'completed' if response['ok'] else 'failed', 'finished_at': now(),
                      'elapsed_seconds': elapsed, 'response': response}
            # If completion persistence fails, the original charged running entry
            # remains. A subsequent owner conservatively finalizes interruption.
            with transaction(self.store.db_path) as connection:
                self._load(connection, identity)
                previous_bytes = connection.execute('SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM research_tool_calls WHERE session_id=? AND id<>?', (identity, call_id)).fetchone()[0]
                reserved_receipts = (session['budget']['max_calls'] - record['sequence']) * MAX_CALL_RECEIPT_BYTES
                if previous_bytes + len(json_text(record).encode('utf-8')) + reserved_receipts > MAX_SESSION_BYTES:
                    record = {**record, 'status': 'failed', 'response': _error(
                        'RESPONSE_TOO_LARGE', 'The verified output exceeds this session storage budget; output was discarded and the session is closed.')}
                self._save_call(connection, call_id, record)
            return record
