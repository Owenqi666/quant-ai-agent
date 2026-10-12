"""Durable proposal requests, separate from the deterministic execution queue.

Each invocation is charged before dispatch. Only its current fenced attempt can
accept a response. Completed drafts do not expire while waiting for handoff.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .db import connect, transaction
from .maintenance import workspace_lease
from .provider_schema import ProposalCreate, ProposalHandoff, ProviderConfig
from .provider_contract import digest as provider_digest
from .provider_contract_registry import request_handler
from .service import ServiceError, now
from ..storage import atomic_json, digest, json_text

SCHEMA = """
CREATE TABLE proposal_requests(id TEXT PRIMARY KEY,research_id TEXT NOT NULL REFERENCES researches(id),base_revision_id TEXT NOT NULL REFERENCES revisions(id),idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,payload TEXT NOT NULL,payload_digest TEXT NOT NULL,created_at TEXT NOT NULL,deadline_at TEXT NOT NULL,status TEXT NOT NULL,state TEXT NOT NULL,state_digest TEXT NOT NULL,ledger_digest TEXT NOT NULL);
CREATE INDEX proposal_requests_queue ON proposal_requests(status,created_at,id);
CREATE TABLE proposal_attempts(id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL REFERENCES proposal_requests(id),sequence INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(proposal_id,sequence));
CREATE TABLE proposal_events(id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL REFERENCES proposal_requests(id),sequence INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(proposal_id,sequence));
"""
MAX_RECORD_BYTES = 512 * 1024
TERMINAL = {'handed_off', 'abstained', 'rejected', 'failed', 'cancelled', 'expired', 'stale', 'interrupted', 'exhausted'}


class ProposalServiceError(ServiceError):
    def __init__(self, message, status=409, *, code, retryable=False):
        super().__init__(message, status)
        self.code, self.retryable = code, retryable


def _error(code, message, retryable=False):
    return {'code': code, 'message': message, 'retryable': retryable}


def _decode(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate record key')
            value[key] = item
        return value
    def invalid(value):
        raise ValueError('Nonfinite record value')
    if len(text.encode('utf-8')) > MAX_RECORD_BYTES:
        raise ValueError('Record is too large')
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    if json_text(value) != text:
        raise ValueError('Record is not canonical')
    return value


def _identity(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'proposal_[0-9a-f]{64}', identity):
        raise ProposalServiceError('Invalid proposal identity', 422, code='ARGUMENTS_INVALID')


def validate_proposal_output(store, request, context, output):
    # Keep the existing validation seam for acceptance deadline checks.
    return request_handler(request).validate_output(store, request, context, output)


class ProposalRequests:
    def __init__(self, store, *, configs: dict[str, ProviderConfig], research_authority=None,
                 enable_material_span_references=False, enable_grounded_material=False):
        if type(enable_material_span_references) is not bool:
            raise ValueError('Material span references require an explicit host boolean')
        self.store = store
        self.enable_material_span_references = enable_material_span_references
        if type(enable_grounded_material) is not bool:
            raise ValueError('Grounded material requires an explicit host boolean')
        self.enable_grounded_material = enable_grounded_material
        # An isolated host runner may inject a verified evaluation authority.
        # Configuration booleans and ordinary HTTP requests never create one.
        self.research_authority = research_authority
        self.configs = {key: ProviderConfig.model_validate(value) for key, value in configs.items()}
        if any(key != value.config_id for key, value in self.configs.items()):
            raise ValueError('Provider configuration key differs from config_id')

    @contextmanager
    def _lock(self, name):
        path = self.store.root.parent / f'.{self.store.root.name}.proposal-{name}.lock'
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProposalServiceError('Proposal lock is not a regular file', code='LEDGER_INTEGRITY')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProposalServiceError('Proposal operation is busy', 503, code='PROPOSAL_BUSY', retryable=True) from exc
            yield descriptor
        finally:
            os.close(descriptor)

    def _folder(self, identity):
        _identity(identity)
        path = self.store.root / 'proposals' / identity
        for item in (path.parent, path):
            if item.is_symlink():
                raise ProposalServiceError('Proposal paths cannot be symlinks', code='ARTIFACT_INTEGRITY')
        return path

    def _read_path(self, path, limit=MAX_RECORD_BYTES):
        """Read a bounded regular artifact without blocking on substituted FIFOs."""
        path = Path(path)
        relative = path.relative_to(self.store.root)
        if '..' in relative.parts:
            raise ValueError('Artifact escaped its workspace')
        for item in (path, *path.parents):
            if item == self.store.root:
                break
            if item.is_symlink():
                raise ValueError('Artifact path contains a symlink')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise ValueError('Artifact must be a bounded private regular file')
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if (len(data) != before.st_size or len(data) > limit
                or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError('Artifact changed while reading')
        return data

    def _json_path(self, path):
        return _decode(self._read_path(path).decode('utf-8'))

    def _publish(self, identity, relative, value):
        path = self._folder(identity) / relative
        for item in (path, *path.parents):
            if item == self.store.root:
                break
            if item.is_symlink():
                raise ProposalServiceError('Proposal artifact path is a symlink', code='ARTIFACT_INTEGRITY')
        content = json_text(value)
        if len(content.encode('utf-8')) > MAX_RECORD_BYTES:
            raise ProposalServiceError('Proposal artifact exceeds its bound', 413, code='OUTPUT_TOO_LARGE')
        if path.exists():
            if not path.is_file() or self._read_path(path).decode('utf-8') != content:
                raise ProposalServiceError('Existing proposal artifact differs', code='RESPONSE_CONFLICT')
            return
        atomic_json(path, value)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _event(self, connection, identity, kind, *, attempt=None, **details):
        sequence = connection.execute('SELECT COUNT(*) FROM proposal_events WHERE proposal_id=?', (identity,)).fetchone()[0] + 1
        if sequence > 32:
            raise ProposalServiceError('Proposal event budget exceeded', 429, code='LEDGER_TOO_LARGE')
        payload = {'sequence': sequence, 'kind': kind, 'created_at': now(), **details}
        if attempt is not None:
            payload.update(attempt_id=attempt['id'], attempt_digest=digest(attempt))
        text = json_text(payload)
        connection.execute('INSERT INTO proposal_events VALUES(?,?,?,?,?)',
                           ('proposal_event_' + digest({'proposal': identity, 'sequence': sequence}), identity, sequence, text, digest(payload)))
        hashes = [row[0] for row in connection.execute('SELECT digest FROM proposal_events WHERE proposal_id=? ORDER BY sequence', (identity,))]
        connection.execute('UPDATE proposal_requests SET ledger_digest=? WHERE id=?', (digest(hashes), identity))

    def _state(self, connection, identity, state):
        connection.execute('UPDATE proposal_requests SET status=?,state=?,state_digest=? WHERE id=?',
                           (state['status'], json_text(state), digest(state), identity))

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM proposal_requests WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ProposalServiceError('Proposal not found', 404, code='PROPOSAL_NOT_FOUND')
        try:
            body, state = _decode(row['payload']), _decode(row['state'])
            source_request = request_handler(body['request']).parse_request(body['request'])
            if source_request.schema_version == 2:
                from .paper_material_spans import validate_context_mode
                validate_context_mode(body['context'], operation=source_request.operation)
                from .provider_grounded_contract import mode_for_context
                mode_for_context(body['context'], source_request.operation)
                if provider_digest(body['context']) != body['context_digest']:
                    raise ValueError('Frozen research context digest differs')
            if (digest(body) != row['payload_digest'] or digest(body['request']) != row['request_digest']
                    or identity != 'proposal_' + row['request_digest'] or digest(state) != row['state_digest']
                    or state['status'] != row['status'] or source_request.research_id != row['research_id']
                    or source_request.base_revision_id != row['base_revision_id']
                    or body['request']['idempotency_key'] != row['idempotency_key']
                    or body['created_at'] != row['created_at'] or body['deadline_at'] != row['deadline_at']):
                raise ValueError('Proposal anchor differs')
            records = connection.execute('SELECT * FROM proposal_events WHERE proposal_id=? ORDER BY sequence', (identity,)).fetchall()
            if digest([record['digest'] for record in records]) != row['ledger_digest']:
                raise ValueError('Proposal event anchor differs')
            events, attempt_anchors = [], {}
            for sequence, record in enumerate(records, 1):
                event = _decode(record['payload'])
                if event['sequence'] != sequence or record['sequence'] != sequence or digest(event) != record['digest']:
                    raise ValueError('Proposal event differs')
                events.append(event)
                if 'attempt_id' in event:
                    attempt_anchors[event['attempt_id']] = event['attempt_digest']
            attempts = []
            for record in connection.execute('SELECT * FROM proposal_attempts WHERE proposal_id=? ORDER BY sequence', (identity,)):
                attempt = _decode(record['payload'])
                if (attempt['id'] != record['id'] or digest(attempt) != record['digest']
                        or attempt_anchors.get(record['id']) != record['digest']
                        or attempt['sequence'] != record['sequence']):
                    raise ValueError('Attempt anchor differs')
                if attempt.get('prepared_digest') is not None:
                    prepared_path = self._folder(identity) / 'attempts' / attempt['id'] / 'prepared.json'
                    if (prepared_path.is_symlink() or prepared_path.stat().st_size > MAX_RECORD_BYTES
                            or digest(self._json_path(prepared_path)) != attempt['prepared_digest']):
                        raise ValueError('Prepared invocation artifact differs')
                if attempt['status'] != 'running' and attempt['receipt'] is not None:
                    folder = self._folder(identity) / 'attempts' / attempt['id']
                    receipt_path = folder / 'receipt.json'
                    if (receipt_path.is_symlink() or receipt_path.stat().st_size > MAX_RECORD_BYTES
                            or self._json_path(receipt_path) != attempt['receipt']):
                        raise ValueError('Provider receipt artifact differs')
                    raw_digest = attempt.get('raw_sha256')
                    if raw_digest is not None:
                        raw_path = folder / 'response.raw.json'
                        if (raw_path.is_symlink() or not raw_path.is_file()
                                or raw_path.stat().st_size > body['request']['budget']['max_output_bytes']
                                or hashlib.sha256(self._read_path(raw_path, body['request']['budget']['max_output_bytes'])).hexdigest() != raw_digest):
                            raise ValueError('Raw provider response artifact differs')
                    if attempt['response_digest'] is not None:
                        parsed_path = folder / 'parsed.json'
                        if (parsed_path.is_symlink() or parsed_path.stat().st_size > MAX_RECORD_BYTES
                                or provider_digest(self._json_path(parsed_path)) != attempt['response_digest']):
                            raise ValueError('Parsed provider response artifact differs')
                    if attempt.get('validation') is not None:
                        validation_path = folder / 'validation.json'
                        if (validation_path.is_symlink() or validation_path.stat().st_size > MAX_RECORD_BYTES
                                or self._json_path(validation_path) != attempt['validation']):
                            raise ValueError('Validation artifact differs')
                attempts.append(attempt)
            if len(attempts) != state['provider_invocations']:
                raise ValueError('Charged invocation count differs')
            for relative, value in [('request.json', body['request']), ('context.json', body['context']), ('provider-config.json', body['config'])]:
                path = self._folder(identity) / relative
                if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES or self._json_path(path) != value:
                    raise ValueError('Frozen input artifact differs')
            if state['accepted_response_digest'] is not None:
                path = self._folder(identity) / 'accepted/response.json'
                if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES or provider_digest(self._json_path(path)) != state['accepted_response_digest']:
                    raise ValueError('Accepted response artifact differs')
            return {'id': identity, **body, **state, 'attempts': attempts, 'events': events}
        except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
            if isinstance(exc, ProposalServiceError):
                raise
            raise ProposalServiceError('Proposal record or artifacts failed integrity checks', code='LEDGER_INTEGRITY') from exc

    def replay_create(self, body: ProposalCreate):
        """Recover an existing create before checking current dispatch policy.

        Reading the old effect neither authorizes another invocation nor expands
        its budget. New requests still go through the caller's runtime gate.
        """
        body = request_handler(body).parse_request(body)
        fingerprint = digest(body.model_dump())
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            prior = connection.execute('SELECT id,request_digest FROM proposal_requests WHERE idempotency_key=?',
                                       (body.idempotency_key,)).fetchone()
            if prior is None:
                return None
            if prior['request_digest'] != fingerprint:
                raise ProposalServiceError('Proposal key has another request', code='IDEMPOTENCY_CONFLICT')
            return self._load(connection, prior['id'])

    def create(self, body: ProposalCreate):
        handler = request_handler(body)
        body = handler.parse_request(body)
        request = body.model_dump()
        fingerprint, identity = digest(request), 'proposal_' + digest(request)
        with workspace_lease(self.store.root), self._lock('create'):
            with closing(connect(self.store.db_path)) as connection:
                prior = connection.execute('SELECT id,request_digest FROM proposal_requests WHERE idempotency_key=?', (body.idempotency_key,)).fetchone()
                if prior:
                    if prior['request_digest'] != fingerprint:
                        raise ProposalServiceError('Proposal key has another request', code='IDEMPOTENCY_CONFLICT')
                    return self._load(connection, prior['id'])
            if handler.schema_version == 1 and body.purpose != 'generate_from_evidence':
                raise ProposalServiceError('Initial provider supports generation only', 422, code='PURPOSE_UNSUPPORTED')
            if any(getattr(body.budget, name) is not None for name in ('hard_token_limit', 'hard_model_call_limit', 'hard_cost_limit')):
                raise ProposalServiceError('Hard supplier budgets are not supported', 422, code='UNSUPPORTED_BUDGET')
            config = self.configs.get(body.provider_config_id)
            if config is None or not config.enabled:
                raise ProposalServiceError('Provider is not configured and enabled', 503, code='PROVIDER_DISABLED')
            context = handler.build_context(self.store, body, enable_material_span_references=self.enable_material_span_references,
                                            enable_grounded_material=self.enable_grounded_material and config.mode == 'codex_cli')
            if context['source_digest'] != body.expected_source_digest:
                raise ProposalServiceError('Proposal source changed', code='SOURCE_CHANGED')
            if len(json_text(context).encode('utf-8')) > body.budget.max_context_bytes:
                raise ProposalServiceError('Provider context exceeds its bound', 413, code='CONTEXT_TOO_LARGE')
            created = now()
            deadline = (datetime.fromisoformat(created) + timedelta(seconds=body.budget.max_seconds)).isoformat()
            if handler.schema_version == 2:
                from .research_session_execution import SessionExecution
                session_execution = SessionExecution(self.store, self)
                with closing(connect(self.store.db_path)) as connection:
                    connection.execute('BEGIN')
                    cap = session_execution.session_deadline(connection, body)
                    deadline = min(datetime.fromisoformat(deadline), datetime.fromisoformat(cap)).isoformat()
                    session_execution.authorize_proposal_create(connection, body, config, deadline_at=deadline, context=context)
                # Include framing overhead in the transport limit before writing
                # a queued effect. IDs have their final fixed lengths already.
                handler.build_prompt(body, context, identity, 'proposal_attempt_' + '0' * 64)
            frozen = {'request': request, 'config': config.model_dump(), 'context': context,
                      'context_digest': provider_digest(context), 'created_at': created, 'deadline_at': deadline}
            if len(json_text(frozen).encode('utf-8')) > MAX_RECORD_BYTES:
                raise ProposalServiceError('Frozen request exceeds its bound', 413, code='CONTEXT_TOO_LARGE')
            for relative, value in [('request.json', request), ('context.json', context), ('provider-config.json', config.model_dump())]:
                self._publish(identity, relative, value)
            state = {'status': 'queued', 'provider_invocations': 0, 'current_attempt_id': None,
                     'accepted_attempt_id': None, 'accepted_response_digest': None,
                     'research_job_id': None, 'error': None, 'handoff': None}
            with transaction(self.store.db_path) as connection:
                if handler.schema_version == 2:
                    session_execution.authorize_proposal_create(connection, body, config, deadline_at=deadline, context=context)
                connection.execute('INSERT INTO proposal_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity, body.research_id, body.base_revision_id, body.idempotency_key, fingerprint,
                     json_text(frozen), digest(frozen), created, deadline, 'queued', json_text(state), digest(state), digest([])))
                self._event(connection, identity, 'created')
                if handler.schema_version == 2:
                    session_execution.record_proposal_created(connection, self._load(connection, identity))
            return self.get(identity)

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._load(connection, identity)

    def list(self, *, limit=20, offset=0, research_id=None, schema_version=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ProposalServiceError('Invalid pagination', 422, code='ARGUMENTS_INVALID')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            clauses, values = [], []
            if research_id:
                clauses.append('research_id=?'); values.append(research_id)
            if schema_version is not None:
                if type(schema_version) is not int or schema_version not in (1, 2):
                    raise ProposalServiceError('Invalid contract version', 422, code='ARGUMENTS_INVALID')
                clauses.append("COALESCE(json_extract(payload,'$.request.schema_version'),1)=?")
                values.append(schema_version)
            clause = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            total = connection.execute('SELECT COUNT(*) FROM proposal_requests' + clause, values).fetchone()[0]
            ids = connection.execute('SELECT id FROM proposal_requests' + clause + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
            return {'items': [self._load(connection, row['id']) for row in ids], 'total': total, 'limit': limit, 'offset': offset}

    def cancel(self, identity, *, session_id=None):
        _identity(identity)
        with workspace_lease(self.store.root), self._lock(identity), transaction(self.store.db_path) as connection:
            value = self._load(connection, identity)
            if session_id is not None:
                # Parent cancellation must not rewrite a child which was already
                # reconciled. Read that authority under the same write lock as
                # this mutation so a concurrent reconcile cannot race the stop.
                from .research_sessions import ResearchSessions
                sessions = ResearchSessions(self.store)
                loaded = sessions._load(connection, session_id)
                owned = [item for item in loaded['reservations']
                         if (item['children'] or {}).get('proposal_id') == identity]
                if len(owned) != 1:
                    raise ProposalServiceError('Parent cancellation has no exact child relation', code='CHILD_IDENTITY_MISMATCH')
                reservation = owned[0]
                sessions._children(connection, loaded, reservation, reservation['children'])
                if reservation['outcome'] is not None:
                    sessions._verify_outcome(connection, reservation, reservation['outcome'])
                    if value['status'] in TERMINAL | {'ready'}:
                        return value
                    # An interrupted parent outcome is not evidence that a
                    # still-running child stopped. Propagate its safety cancel.
            if value['status'] == 'handed_off' or (value['status'] == 'handing_off' and value['handoff'] is not None):
                raise ProposalServiceError('Reconcile the existing handoff, then cancel its research job', code='HANDOFF_PENDING')
            if value['status'] in TERMINAL:
                return value
            state = self._state_value(value)
            state.update(status='cancelling' if value['status'] in {'running', 'cancelling', 'validating'} else 'cancelled',
                         error=_error('CANCELLED', 'Application cancellation recorded.'))
            self._state(connection, identity, state)
            self._event(connection, identity, 'cancel_requested')
        return self.get(identity)

    @staticmethod
    def _state_value(value):
        return {key: value[key] for key in ('status', 'provider_invocations', 'current_attempt_id',
            'accepted_attempt_id', 'accepted_response_digest', 'research_job_id', 'error', 'handoff')}

    def claim(self, worker_id, *, config_digests=None):
        """Short transaction. Caller owns the process worker fence before entry."""
        if config_digests is not None and (not isinstance(config_digests, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       or not re.fullmatch(r'[0-9a-f]{64}', value) for key, value in config_digests.items())):
            raise ValueError('Invalid registered provider configuration identities')
        with transaction(self.store.db_path) as connection:
            if connection.execute("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'").fetchone():
                return None
            # Uncertain calls never authorize a replacement after restart.
            if connection.execute("SELECT 1 FROM proposal_requests WHERE status IN ('running','validating','cancelling')").fetchone():
                return None
            from .provider_recovery import unresolved_interrupted
            if unresolved_interrupted(connection, self):
                return None
            rows = connection.execute("SELECT id FROM proposal_requests WHERE status='queued' ORDER BY created_at,id").fetchall()
            for row in rows:
                value = self._load(connection, row['id'])
                state = self._state_value(value)
                if datetime.now(timezone.utc) >= datetime.fromisoformat(value['deadline_at']):
                    state.update(status='expired', error=_error('TIMEOUT', 'Generation deadline elapsed before dispatch.'))
                    self._state(connection, value['id'], state)
                    self._event(connection, value['id'], 'expired_before_dispatch')
                    continue
                if config_digests is not None:
                    configured = config_digests.get(value['config']['config_id'])
                    if configured is None:
                        continue
                    if configured != provider_digest(value['config']):
                        state.update(status='failed', error=_error('CONFIG_CHANGED', 'Registered provider differs from the frozen request.'))
                        self._state(connection, value['id'], state)
                        self._event(connection, value['id'], 'configuration_changed_before_dispatch')
                        continue
                if value['provider_invocations'] >= 1:
                    state.update(status='exhausted', error=_error('INVOCATION_LIMIT', 'Initial provider permits one dispatch.'))
                    self._state(connection, value['id'], state)
                    self._event(connection, value['id'], 'invocation_limit')
                    continue
                if value['request'].get('schema_version', 1) == 2:
                    from .research_session_execution import SessionExecution
                    try:
                        remaining = SessionExecution(self.store, self).authorize_proposal_dispatch(connection, value)
                        if remaining <= 0:
                            raise ProposalServiceError('Session deadline elapsed', code='SESSION_EXPIRED')
                    except (ServiceError, ValueError, OSError) as exc:
                        code = getattr(exc, 'code', 'SESSION_INVALID')
                        status = 'expired' if code == 'SESSION_EXPIRED' else 'cancelled' if code == 'SESSION_CLOSED' else 'stale'
                        state.update(status=status, error=_error(code, self.store._safe_error(exc)))
                        self._state(connection, value['id'], state)
                        self._event(connection, value['id'], 'session_rejected_before_dispatch', error=state['error'])
                        continue
                sequence = value['provider_invocations'] + 1
                attempt_id = 'proposal_attempt_' + digest({'proposal': value['id'], 'sequence': sequence})
                attempt = {'id': attempt_id, 'sequence': sequence, 'worker_id': worker_id,
                           'fencing_token': digest({'attempt': attempt_id, 'worker': worker_id}),
                           'status': 'running', 'started_at': now(), 'finished_at': None,
                           'prepared_digest': None, 'response_digest': None, 'raw_sha256': None,
                           'receipt': None, 'validation': None, 'error': None}
                if value['request'].get('schema_version', 1) == 2 and (value['config']['mode'] != 'fixture' or
                    self.research_authority is not None and hasattr(self.research_authority, 'owns_session') and
                    self.research_authority.owns_session(connection, value['request']['session_id'])):
                    # Atomic with the original attempt charge. A failed write
                    # rolls back both ledgers, and no subprocess has started.
                    self.research_authority.reserve_attempt(connection, value, attempt_id)
                    parent_deadline = SessionExecution(self.store, self).session_deadline(connection, value['request'])
                    if datetime.now(timezone.utc) >= min(datetime.fromisoformat(value['deadline_at']),
                                                         datetime.fromisoformat(parent_deadline)):
                        raise ProposalServiceError('Session deadline elapsed while reserving the evaluation slot',
                                                   code='SESSION_EXPIRED')
                connection.execute('INSERT INTO proposal_attempts VALUES(?,?,?,?,?)',
                                   (attempt_id, value['id'], sequence, json_text(attempt), digest(attempt)))
                # Atomic with charging: even death before prepared/journal has a
                # same-host boot boundary without rewriting the frozen attempt.
                from .provider_supervision_journal import host_boot_identity
                workspace_identity = connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()[0]
                origin = {'schema_version': 1, 'proposal_id': value['id'], 'attempt_id': attempt_id,
                          'attempt_charged_digest': digest(attempt), 'fencing_token': attempt['fencing_token'],
                          'workspace_identity': workspace_identity, 'host_boot': host_boot_identity()}
                connection.execute('INSERT INTO provider_attempt_origins VALUES(?,?,?)',
                                   (attempt_id, json_text(origin), digest(origin)))
                state.update(status='running', provider_invocations=sequence, current_attempt_id=attempt_id)
                self._state(connection, value['id'], state)
                self._event(connection, value['id'], 'attempt_charged', attempt=attempt, origin_digest=digest(origin))
                return {'proposal': {**value, **state}, 'attempt': attempt}
        return None

    def register_prepared(self, identity, attempt_id, fencing_token, prepared):
        """Anchor the exact stdin/schema/config before provider entry."""
        with workspace_lease(self.store.root), self._lock(identity):
            value = self.get(identity)
            attempt = next((item for item in value['attempts'] if item['id'] == attempt_id), None)
            if (attempt is None or attempt['status'] != 'running' or attempt['fencing_token'] != fencing_token
                    or value['current_attempt_id'] != attempt_id):
                raise ProposalServiceError('Attempt ownership changed', code='ATTEMPT_STALE')
            prepared_digest = digest(prepared)
            if attempt['prepared_digest'] is not None:
                if prepared_digest != attempt['prepared_digest']:
                    raise ProposalServiceError('Prepared invocation differs', code='RESPONSE_CONFLICT')
                return value
            if (prepared['proposal_id'] != identity or prepared['attempt_id'] != attempt_id
                    or prepared['context_digest'] != value['context_digest']
                    or prepared['provider_config'] != value['config']
                    or prepared['budget'] != value['request']['budget']):
                raise ProposalServiceError('Prepared invocation does not match request', code='REQUEST_MISMATCH')
            if value['request'].get('schema_version', 1) == 2:
                from .paper_material_spans import validate_context_mode
                checked_request = request_handler(value['request']).parse_request(value['request'])
                if validate_context_mode(value['context'], operation=checked_request.operation):
                    handler = request_handler(checked_request)
                    if (prepared.get('output_schema') != handler.output_schema(value['context'])
                            or prepared.get('input_text') != handler.build_prompt(checked_request, value['context'], identity, attempt_id)):
                        raise ProposalServiceError('Prepared span input differs from frozen prompt and schema', code='REQUEST_MISMATCH')
            self._publish(identity, f'attempts/{attempt_id}/prepared.json', prepared)
            attempt = {**attempt, 'prepared_digest': prepared_digest}
            with transaction(self.store.db_path) as connection:
                self._load(connection, identity)
                connection.execute('UPDATE proposal_attempts SET payload=?,digest=? WHERE id=?', (json_text(attempt), digest(attempt), attempt_id))
                self._event(connection, identity, 'invocation_prepared', attempt=attempt)
            return self.get(identity)

    def finish(self, identity, attempt_id, fencing_token, *, response=None, receipt=None, error=None):
        """Accept once, retaining the host receipt even when the output is rejected."""
        with workspace_lease(self.store.root), self._lock(identity):
            value = self.get(identity)
            attempt = next((item for item in value['attempts'] if item['id'] == attempt_id), None)
            if attempt is None or attempt['fencing_token'] != fencing_token or value['current_attempt_id'] != attempt_id:
                raise ProposalServiceError('Attempt ownership changed', code='ATTEMPT_STALE')
            response_digest = provider_digest(response) if response is not None else None
            if attempt['status'] != 'running':
                if attempt['response_digest'] != response_digest:
                    raise ProposalServiceError('Attempt already has another response', code='RESPONSE_CONFLICT')
                return value
            receipt = receipt.model_dump() if hasattr(receipt, 'model_dump') else receipt
            if receipt is not None:
                self._publish(identity, f'attempts/{attempt_id}/receipt.json', receipt)
            if response is not None:
                self._publish(identity, f'attempts/{attempt_id}/parsed.json', response)
            state = self._state_value(value)
            status, diagnostic = 'failed', None
            if (receipt is None and error is not None and error.get('code') == 'OUTCOME_UNKNOWN') or (receipt is not None and not receipt.get('process_stopped', False)):
                status, error = 'interrupted', _error('OUTCOME_UNKNOWN', 'Provider process termination is unconfirmed.')
            elif value['status'] == 'cancelling':
                status, error = 'cancelled', _error('CANCELLED', 'Provider stopped after cancellation.')
            elif datetime.now(timezone.utc) >= datetime.fromisoformat(value['deadline_at']):
                status, error = 'expired', _error('TIMEOUT', 'Late response is retained but cannot be accepted.')
            elif error is None and response is not None:
                try:
                    from .provider_schema import ProviderReceipt
                    checked_receipt = ProviderReceipt.model_validate(receipt)
                    if checked_receipt.proposal_id != identity or checked_receipt.attempt_id != attempt_id:
                        raise ProposalServiceError('Provider receipt identity differs', code='REQUEST_MISMATCH')
                    raw_path = self._folder(identity) / 'attempts' / attempt_id / 'response.raw.json'
                    if attempt['prepared_digest'] is None or raw_path.is_symlink() or not raw_path.is_file():
                        raise ProposalServiceError('Accepted response requires frozen input and exact raw bytes', code='ARTIFACT_INTEGRITY')
                    if raw_path.stat().st_size > value['request']['budget']['max_output_bytes']:
                        raise ProposalServiceError('Raw response exceeds its byte bound', code='OUTPUT_TOO_LARGE')
                    raw = self._read_path(raw_path, value['request']['budget']['max_output_bytes'])
                    if hashlib.sha256(raw).hexdigest() != checked_receipt.response_sha256:
                        raise ProposalServiceError('Raw response differs from its receipt', code='ARTIFACT_INTEGRITY')
                    handler = request_handler(value['request'])
                    parsed = handler.parse_output(raw, request=value['request'], context=value['context'], proposal_id=identity, attempt_id=attempt_id,
                        context_digest=value['context_digest'], candidate_limit=value['request']['candidate_limit'],
                        max_bytes=value['request']['budget']['max_output_bytes']).model_dump()
                    if parsed != response:
                        raise ProposalServiceError('Parsed response differs from raw bytes', code='ARTIFACT_INTEGRITY')
                    from .paper_material_spans import validate_context_mode
                    span_mode = (validate_context_mode(value['context'], operation=value['request'].get('operation'))
                                 if handler.schema_version == 2 else False)
                    from .provider_grounded_contract import mode_for_context
                    grounded_mode = (mode_for_context(value['context'], value['request'].get('operation')) == 'grounded_extract_material'
                                     if handler.schema_version == 2 else False)
                    current = handler.build_context(self.store, handler.parse_request(value['request']),
                                                    enable_material_span_references=span_mode,
                                                    enable_grounded_material=grounded_mode)
                    if current['source_digest'] != value['context']['source_digest']:
                        raise ProposalServiceError('Frozen source changed', code='SOURCE_CHANGED')
                    diagnostic = validate_proposal_output(self.store, value['request'], value['context'], response)
                    if not diagnostic['valid']:
                        status, error = 'rejected', _error('CANDIDATES_BLOCKED', 'Provider response failed domain validation.')
                    elif response['outcome'] == 'abstain':
                        status = 'abstained'
                    else:
                        status = 'ready'
                except (ServiceError, ValueError, OSError) as exc:
                    status, error = 'stale' if getattr(exc, 'code', '') in {'SOURCE_CHANGED', 'BASE_REVISION_CHANGED'} else 'rejected', _error(getattr(exc, 'code', 'DRAFT_INVALID'), self.store._safe_error(exc))
            elif error is None:
                error = _error('OUTPUT_MISSING', 'Provider returned no usable output.')
            if diagnostic is not None:
                self._publish(identity, f'attempts/{attempt_id}/validation.json', diagnostic)
            # Source/PDF/guard checks can consume the remaining generation time.
            # A late validation is preserved, never promoted to an accepted draft.
            if status in {'ready', 'abstained'} and datetime.now(timezone.utc) >= datetime.fromisoformat(value['deadline_at']):
                status, error = 'expired', _error('TIMEOUT', 'Generation deadline elapsed during domain validation.')
            if status == 'ready':
                self._publish(identity, 'accepted/response.json', response)
                state.update(accepted_attempt_id=attempt_id, accepted_response_digest=response_digest)
            raw_path = self._folder(identity) / 'attempts' / attempt_id / 'response.raw.json'
            raw_sha256 = hashlib.sha256(self._read_path(raw_path, value['request']['budget']['max_output_bytes'])).hexdigest() if raw_path.is_file() and not raw_path.is_symlink() else None
            attempt = {**attempt, 'status': status, 'finished_at': now(), 'response_digest': response_digest,
                       'raw_sha256': raw_sha256, 'receipt': receipt, 'validation': diagnostic, 'error': error}
            state.update(status=status, error=error)
            with transaction(self.store.db_path) as connection:
                self._load(connection, identity)
                if status in {'ready', 'abstained'} and datetime.now(timezone.utc) >= datetime.fromisoformat(value['deadline_at']):
                    status, error = 'expired', _error('TIMEOUT', 'Generation deadline elapsed before acceptance commit.')
                    attempt.update(status=status, error=error)
                    state.update(status=status, error=error, accepted_attempt_id=None, accepted_response_digest=None)
                if status in {'ready', 'abstained'} and value['request'].get('schema_version', 1) == 2:
                    from .research_session_execution import SessionExecution
                    try:
                        if value['config']['mode'] != 'fixture':
                            if self.research_authority is None:
                                raise ProposalServiceError('Research provider v2 has no explicit host authority',
                                                           code='RESEARCH_V2_REAL_UNQUALIFIED')
                            self.research_authority.verify_result(value, attempt,
                                response=response, receipt=receipt, error=error)
                        if SessionExecution(self.store, self).authorize_proposal_dispatch(connection, value, phase='finish') <= 0:
                            raise ProposalServiceError('Session deadline elapsed', code='SESSION_EXPIRED')
                    except (ServiceError, ValueError, OSError) as exc:
                        code = getattr(exc, 'code', 'SESSION_INVALID')
                        status = ('expired' if code == 'SESSION_EXPIRED' else
                                  'cancelled' if code == 'SESSION_CLOSED' else
                                  'rejected' if code == 'EVALUATION_OUTPUT_POLICY_INVALID' else 'stale')
                        error = _error(code, self.store._safe_error(exc))
                        attempt.update(status=status, error=error)
                        state.update(status=status, error=error, accepted_attempt_id=None, accepted_response_digest=None)
                connection.execute('UPDATE proposal_attempts SET payload=?,digest=? WHERE id=?', (json_text(attempt), digest(attempt), attempt_id))
                self._state(connection, identity, state)
                self._event(connection, identity, 'attempt_finished', attempt=attempt, status=status)
                # Record transport evidence even when domain validation rejects
                # the output. This observer cannot promote an output to ready.
                if value['request'].get('schema_version', 1) == 2:
                    from .provider_authority_router import ResearchAuthorityRouter
                    from .provider_loop_native_authority import LoopNativeResearchAuthority
                    if type(self.research_authority) in {ResearchAuthorityRouter, LoopNativeResearchAuthority}:
                        self.research_authority.observe_transport({**value, **state}, attempt, receipt=receipt,
                                                                  error=error, connection=connection)
                if status == 'interrupted':
                    from .provider_recovery_schema import pause_dispatch
                    pause_dispatch(connection, 'interrupted_invocation')
            return self.get(identity)

    def recover_interrupted(self):
        """Requires the old CLI's inherited worker fence to have been released."""
        with transaction(self.store.db_path) as connection:
            return self._recover_interrupted(connection)

    def _recover_interrupted(self, connection):
        """Shared transaction variant; caller owns the inherited worker fence."""
        rows = connection.execute("SELECT id FROM proposal_requests WHERE status IN ('running','validating','cancelling')").fetchall()
        for row in rows:
            value = self._load(connection, row['id'])
            state = self._state_value(value)
            attempt = value['attempts'][-1]
            error = _error('OUTCOME_UNKNOWN', 'Worker stopped without a confirmed invocation receipt. Explicit reconciliation is required.')
            attempt = {**attempt, 'status': 'interrupted', 'finished_at': now(), 'error': error}
            state.update(status='interrupted', error=error)
            connection.execute('UPDATE proposal_attempts SET payload=?,digest=? WHERE id=?', (json_text(attempt), digest(attempt), attempt['id']))
            self._state(connection, value['id'], state)
            self._event(connection, value['id'], 'interrupted_after_restart', attempt=attempt)
            if value['request'].get('schema_version', 1) == 2:
                from .provider_authority_router import ResearchAuthorityRouter
                from .provider_loop_native_authority import LoopNativeResearchAuthority
                if type(self.research_authority) in {ResearchAuthorityRouter, LoopNativeResearchAuthority}:
                    self.research_authority.observe_transport({**value, **state}, attempt,
                        receipt=attempt.get('receipt'), error=error, connection=connection)
        if rows:
            from .provider_recovery_schema import pause_dispatch
            pause_dispatch(connection, 'interrupted_invocation')
        return len(rows)

    def handoff(self, identity, body: ProposalHandoff):
        body = ProposalHandoff.model_validate(body)
        if not body.idempotency_key.strip():
            raise ProposalServiceError('Handoff key is blank', 422, code='ARGUMENTS_INVALID')
        request, fingerprint = body.model_dump(), digest(body.model_dump())
        from .research_jobs import ResearchJobs
        with workspace_lease(self.store.root), self._lock(identity):
            value = self.get(identity)
            if value['request'].get('schema_version', 1) != 1:
                raise ProposalServiceError('Research v2 experiment handoff is not enabled in G2', 422, code='HANDOFF_NOT_ENABLED')
            existing = value['handoff']
            if existing is not None:
                if existing['request_digest'] != fingerprint:
                    raise ProposalServiceError('Handoff already has another immutable request', code='IDEMPOTENCY_CONFLICT')
                if value['research_job_id'] is not None:
                    return value
            else:
                if value['status'] != 'ready':
                    raise ProposalServiceError('Proposal is not ready for handoff', code='ACTION_NOT_ALLOWED')
                if body.accepted_response_digest != value['accepted_response_digest']:
                    raise ProposalServiceError('Accepted response digest changed', code='RESPONSE_CONFLICT')
                existing = {'request': request, 'request_digest': fingerprint,
                            'effect_key': 'proposal-handoff-' + digest({'proposal': identity})}
                state = self._state_value(value)
                state.update(status='handing_off', handoff=existing)
                with transaction(self.store.db_path) as connection:
                    self._load(connection, identity)
                    self._state(connection, identity, state)
                    self._event(connection, identity, 'handoff_intent', request_digest=fingerprint, effect_key=existing['effect_key'])
            effect = self.store._read('SELECT id FROM research_jobs WHERE idempotency_key=?', (existing['effect_key'],))
            if not effect:
                # A durable intent alone does not prove creation took place.
                # Recheck the original exposure before authorizing a new effect.
                try:
                    handler = request_handler(value['request'])
                    from .paper_material_spans import validate_context_mode
                    from .provider_grounded_contract import mode_for_context
                    span_mode = (validate_context_mode(value['context'], value['request'].get('operation'))
                                 if handler.schema_version == 2 else False)
                    grounded_mode = (mode_for_context(value['context'], value['request'].get('operation')) == 'grounded_extract_material'
                                     if handler.schema_version == 2 else False)
                    current = handler.build_context(self.store, handler.parse_request(value['request']),
                        enable_material_span_references=span_mode, enable_grounded_material=grounded_mode)
                    if current != value['context']:
                        raise ProposalServiceError('Proposal context changed before handoff', code='SOURCE_CHANGED')
                except (ServiceError, ValueError, OSError) as exc:
                    with transaction(self.store.db_path) as connection:
                        latest = self._load(connection, identity)
                        state = self._state_value(latest)
                        state.update(status='stale', error=_error(getattr(exc, 'code', 'SOURCE_CHANGED'), self.store._safe_error(exc)))
                        self._state(connection, identity, state)
                        self._event(connection, identity, 'handoff_stale', error=state['error'])
                    raise
            # ResearchJobs.create replays an existing receipt before checking
            # source/current revision; an acknowledgement loss cannot duplicate it.
            response = self._json_path(self._folder(identity) / 'accepted/response.json')
            if provider_digest(response) != value['accepted_response_digest']:
                raise ProposalServiceError('Accepted response changed before job creation', code='RESPONSE_CONFLICT')
            args = {key: request[key] for key in ('budget', 'note', 'allow_partial_execution', 'method_status', 'unresolved_rules')}
            job = ResearchJobs(self.store).create(value['request']['research_id'], value['request']['base_revision_id'],
                response['draft'], idempotency_key=existing['effect_key'], **args)
            saved = {**existing, 'research_job_id': job['id']}
            self._publish(identity, 'handoff.json', saved)
            with transaction(self.store.db_path) as connection:
                value = self._load(connection, identity)
                state = self._state_value(value)
                state.update(status='handed_off', research_job_id=job['id'])
                self._state(connection, identity, state)
                self._event(connection, identity, 'handed_off', research_job_id=job['id'])
            return self.get(identity)
