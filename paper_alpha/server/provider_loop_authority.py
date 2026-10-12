"""Independent, fixture-only loop authority. Never a Native execution permit.

Hashes below use storage.digest (pretty sorted finite JSON plus a newline),
except externally declared wire/file digests. The ledger records declarations;
it cannot attest that a real host inspected the declared source or artifacts.
"""
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3

from pydantic import ValidationError

from ..expressions import ExpressionError, validate_expression as _engine_validate_expression
from ..storage import digest, json_text
from .provider_contract import strict_json
from .provider_repair_semantics import repair_candidate_expression, _normalized
from .provider_loop_authority_schema import (
    AUTHORITY_ID, OPERATIONS, ExperimentProjection, LoopClaim, LoopGrantPolicy,
    LoopOutcome, SourceAdvance,
)

MAX_EVENT_BYTES = 131072
MAX_EVENTS = 1024
_TABLES = {'loop_metadata', 'loop_sessions', 'loop_events', 'loop_anchors'}


class LoopAuthorityError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, False


def _fail(code, message, status=409):
    raise LoopAuthorityError(code, message, status)


def _model(model, value):
    try:
        return model.model_validate(value).model_dump()
    except ValidationError as exc:
        code = 'UNSUPPORTED_BUDGET' if 'UNSUPPORTED_BUDGET' in str(exc) else 'INVALID_CONTRACT'
        _fail(code, f'{model.__name__} validation failed: {exc}', 422)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
        _fail('INVALID_CONTRACT', 'Identity requires a bounded string identifier', 422)
    return value


def _json(text):
    try:
        return strict_json(text.encode('utf-8'), max_bytes=MAX_EVENT_BYTES)
    except (ValueError, TypeError, AttributeError) as exc:
        _fail('LEDGER_CORRUPT', f'Invalid bounded ledger JSON: {exc}')


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail('INVALID_CLOCK', 'The trusted authority clock must return aware UTC time', 422)
    return value.astimezone(timezone.utc)


def _check_expression(expression, fields):
    # Match the existing bounded repair semantics: aliases are documented,
    # integer window values are not silently coerced during normal validation.
    tree, _ = _normalized(expression, convert_windows=False)
    return _engine_validate_expression(ast.unparse(tree.body), fields)


class FixtureLoopAuthority:
    """One immutable grant per session in a separate SQLite file.

    Public mutators serialize with BEGIN IMMEDIATE. A reserved dispatch is
    permanently charged even if consumption never happens or the result is
    unknown. Replayed records acknowledge history, never authorize dispatch.
    """
    def __init__(self, db_path, *, workspace_id, clock=None):
        self.path = Path(db_path)
        self.workspace_id = _identifier(workspace_id)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if (str(db_path) == ':memory:' or self.path.is_symlink()
                or not self.path.parent.is_dir()
                or (self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1))):
            _fail('INVALID_LEDGER_PATH', 'Use a dedicated regular SQLite file in an existing directory', 422)
        with self._transaction() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names and names != _TABLES:
                _fail('FOREIGN_DATABASE', 'Refusing to initialize or mutate another application database')
            if not names:
                statements = (
                    "CREATE TABLE loop_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), authority_id TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version=1))",
                    "CREATE TABLE loop_sessions (session_id TEXT PRIMARY KEY, grant_id TEXT UNIQUE NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, metadata_json TEXT NOT NULL, metadata_digest TEXT NOT NULL)",
                    "CREATE TABLE loop_events (session_id TEXT NOT NULL REFERENCES loop_sessions(session_id), sequence INTEGER NOT NULL, event_key TEXT NOT NULL, body_json TEXT NOT NULL, event_digest TEXT NOT NULL, PRIMARY KEY(session_id,sequence), UNIQUE(session_id,event_key))",
                    "CREATE TABLE loop_anchors (session_id TEXT PRIMARY KEY REFERENCES loop_sessions(session_id), event_count INTEGER NOT NULL, tail_digest TEXT NOT NULL)",
                    "CREATE TRIGGER policy_no_update BEFORE UPDATE ON loop_sessions BEGIN SELECT RAISE(ABORT,'immutable policy'); END",
                    "CREATE TRIGGER policy_no_delete BEFORE DELETE ON loop_sessions BEGIN SELECT RAISE(ABORT,'immutable policy'); END",
                    "CREATE TRIGGER event_no_update BEFORE UPDATE ON loop_events BEGIN SELECT RAISE(ABORT,'immutable event'); END",
                    "CREATE TRIGGER event_no_delete BEFORE DELETE ON loop_events BEGIN SELECT RAISE(ABORT,'immutable event'); END",
                    "CREATE TRIGGER metadata_no_update BEFORE UPDATE ON loop_metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END",
                    "CREATE TRIGGER metadata_no_delete BEFORE DELETE ON loop_metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END",
                )
                for statement in statements:
                    db.execute(statement)
                db.execute('INSERT INTO loop_metadata VALUES (1,?,1)', (AUTHORITY_ID,))
            rows = list(db.execute('SELECT * FROM loop_metadata'))
            if len(rows) != 1 or tuple(rows[0]) != (1, AUTHORITY_ID, 1):
                _fail('LEDGER_CORRUPT', 'Ledger authority/schema identity differs')

    @contextmanager
    def _transaction(self):
        # Recheck the path for accidental symlink/hardlink replacement on reopen.
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_nlink != 1):
            _fail('INVALID_LEDGER_PATH', 'Ledger path changed')
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def readiness(self):
        return {'authority_id': AUTHORITY_ID, 'authorization_kind': 'fixture',
                'fixture_ready': True, 'native_ready': False,
                'native_status': 'pending', 'code': 'RESEARCH_LOOP_NATIVE_UNQUALIFIED',
                'proof_scope': 'typed_declarations_and_mechanical_ledger_only'}

    def verify_native(self, *args, **kwargs):
        _fail('RESEARCH_LOOP_NATIVE_UNQUALIFIED', 'No Native adapter qualification or real execution permit exists')

    def _now(self):
        return _utc(self.clock()).isoformat()

    def create_session(self, policy):
        policy = _model(LoopGrantPolicy, policy)
        if policy['workspace_id'] != self.workspace_id:
            _fail('WORKSPACE_MISMATCH', 'Policy workspace differs from the authority')
        source, capability = policy['initial_source'], policy['capability']
        if source['kind'] == 'research_revision' and (source['dataset_id'], source['dataset_digest']) != (capability['dataset_id'], capability['dataset_digest']):
            _fail('CAPABILITY_MISMATCH', 'Revision dataset differs from capability declaration')
        with self._transaction() as db:
            old = db.execute('SELECT session_id FROM loop_sessions WHERE idempotency_key=?', (policy['idempotency_key'],)).fetchone()
            if old:
                state = self._load(db, old['session_id'])
                if state['policy'] != policy:
                    _fail('IDEMPOTENCY_CONFLICT', 'Session key already has another immutable policy')
                return self._view(state, replayed=True)
            created = self._now()
            grant_id = 'loop_grant_' + digest(policy)
            session_id = 'loop_session_' + digest({'authority_id': AUTHORITY_ID, 'grant_id': grant_id})
            metadata = {'session_id': session_id, 'grant_id': grant_id, 'policy': policy,
                        'created_at': created,
                        'deadline': (datetime.fromisoformat(created) + timedelta(seconds=policy['budget']['max_seconds'])).isoformat()}
            metadata_digest = digest(metadata)
            if len(json_text(metadata).encode('utf-8')) > MAX_EVENT_BYTES:
                _fail('RECORD_TOO_LARGE', 'Policy exceeds bounded ledger storage', 422)
            db.execute('INSERT INTO loop_sessions VALUES (?,?,?,?,?)',
                       (session_id, grant_id, policy['idempotency_key'], json_text(metadata), metadata_digest))
            db.execute('INSERT INTO loop_anchors VALUES (?,0,?)', (session_id, metadata_digest))
            return self._view(self._load(db, session_id), replayed=False)

    def _load(self, db, session_id):
        _identifier(session_id)
        row = db.execute('SELECT * FROM loop_sessions WHERE session_id=?', (session_id,)).fetchone()
        if not row:
            _fail('SESSION_NOT_FOUND', 'No authority-owned session exists', 404)
        metadata = _json(row['metadata_json'])
        if (not isinstance(metadata, dict) or set(metadata) != {'session_id', 'grant_id', 'policy', 'created_at', 'deadline'}
                or digest(metadata) != row['metadata_digest']):
            _fail('LEDGER_CORRUPT', 'Policy metadata digest/shape differs')
        try:
            policy = _model(LoopGrantPolicy, metadata['policy'])
            created = _utc(datetime.fromisoformat(metadata['created_at']))
            deadline = _utc(datetime.fromisoformat(metadata['deadline']))
        except (ValueError, TypeError):
            _fail('LEDGER_CORRUPT', 'Policy contract/time is invalid')
        grant_id = 'loop_grant_' + digest(policy)
        expected_session = 'loop_session_' + digest({'authority_id': AUTHORITY_ID, 'grant_id': grant_id})
        if (metadata['session_id'] != session_id or session_id != expected_session
                or metadata['grant_id'] != row['grant_id'] or row['grant_id'] != grant_id
                or row['idempotency_key'] != policy['idempotency_key']
                or policy['workspace_id'] != self.workspace_id
                or deadline != created + timedelta(seconds=policy['budget']['max_seconds'])):
            _fail('LEDGER_CORRUPT', 'Immutable session/grant/workspace/deadline binding differs')
        anchor = db.execute('SELECT * FROM loop_anchors WHERE session_id=?', (session_id,)).fetchone()
        if not anchor or type(anchor['event_count']) is not int or not 0 <= anchor['event_count'] <= MAX_EVENTS:
            _fail('LEDGER_CORRUPT', 'Ledger anchor is absent or out of bounds')
        rows = list(db.execute('SELECT * FROM loop_events WHERE session_id=? ORDER BY sequence LIMIT ?', (session_id, MAX_EVENTS + 1)))
        if len(rows) != anchor['event_count']:
            _fail('LEDGER_CORRUPT', 'Event count differs from durable anchor')
        state = dict(metadata, source=policy['initial_source'], used={op: 0 for op in OPERATIONS},
                     claims={}, consumed=set(), outcomes={}, advances=[], experiments={}, events={},
                     failures=0, closed=None, uncertain=False, sequence=0, tail=row['metadata_digest'],
                     last_at=metadata['created_at'])
        kinds = {'claim': LoopClaim, 'outcome': LoopOutcome, 'advance': SourceAdvance, 'experiment': ExperimentProjection}
        for seq, event in enumerate(rows, 1):
            body = _json(event['body_json'])
            if (not isinstance(body, dict) or set(body) != {'sequence', 'event_key', 'kind', 'payload', 'previous', 'at'}
                    or body['sequence'] != seq or type(body['sequence']) is not int
                    or event['sequence'] != seq or body['event_key'] != event['event_key']
                    or body['previous'] != state['tail'] or digest(body) != event['event_digest']):
                _fail('LEDGER_CORRUPT', 'Event chain/sequence/digest differs')
            try:
                at = _utc(datetime.fromisoformat(body['at']))
            except (ValueError, TypeError):
                _fail('LEDGER_CORRUPT', 'Invalid event time')
            if at < datetime.fromisoformat(state['last_at']):
                _fail('LEDGER_CORRUPT', 'Event time moved backwards')
            kind, payload = body['kind'], body['payload']
            if kind in kinds:
                try:
                    payload = _model(kinds[kind], payload)
                except LoopAuthorityError:
                    _fail('LEDGER_CORRUPT', 'Event typed contract differs')
                if payload['session_id'] != session_id:
                    _fail('LEDGER_CORRUPT', 'Event belongs to another session')
            elif kind == 'consume':
                if not isinstance(payload, dict) or set(payload) != {'claim_id', 'request_digest'}:
                    _fail('LEDGER_CORRUPT', 'Consumption shape differs')
            elif kind in {'cancel', 'revoke'}:
                if (not isinstance(payload, dict) or set(payload) != {'reason', 'idempotency_key'}
                        or not isinstance(payload['reason'], str) or not 1 <= len(payload['reason']) <= 256):
                    _fail('LEDGER_CORRUPT', 'Closure shape differs')
            else:
                _fail('LEDGER_CORRUPT', 'Unrecognized event kind')
            if kind == 'claim':
                claim_id = 'claim_' + digest(payload)
                state['claims'][claim_id] = {'claim_id': claim_id, 'request_digest': digest(payload), 'request': payload, 'at': body['at']}
                state['used'][payload['operation']] += 1
            elif kind == 'consume':
                item = state['claims'].get(payload['claim_id'])
                if not item or item['request_digest'] != payload['request_digest'] or payload['claim_id'] in state['consumed']:
                    _fail('LEDGER_CORRUPT', 'Invalid or repeated consumption')
                state['consumed'].add(payload['claim_id'])
            elif kind == 'outcome':
                claim = state['claims'].get(payload['claim_id'])
                if (not claim or payload['request_digest'] != claim['request_digest']
                        or payload['claim_id'] in state['outcomes']):
                    _fail('LEDGER_CORRUPT', 'Outcome has no unique original claim')
                state['outcomes'][payload['claim_id']] = payload
                state['failures'] += int(payload['transport_status'] != 'succeeded' or payload['domain_status'] != 'accepted')
                state['uncertain'] |= payload['transport_status'] == 'unknown'
            elif kind == 'advance':
                state['advances'].append(payload)
                state['source'] = payload['to_source']
            elif kind == 'experiment':
                state['experiments'][digest(payload)] = payload
            elif kind in {'cancel', 'revoke'}:
                state['closed'] = kind
            state['events'][body['event_key']] = {'kind': kind, 'payload': payload, 'event_digest': event['event_digest']}
            state.update(sequence=seq, tail=event['event_digest'], last_at=body['at'])
        if state['tail'] != anchor['tail_digest']:
            _fail('LEDGER_CORRUPT', 'Tail differs from durable anchor')
        return state

    def _state_digest(self, state):
        return digest({'session_id': state['session_id'], 'policy_digest': digest(state['policy']),
                       'source': state['source'], 'sequence': state['sequence'], 'tail': state['tail']})

    def _view(self, state, *, replayed=False):
        return {'authority_id': AUTHORITY_ID, 'authorization_kind': 'fixture',
                'session_id': state['session_id'], 'grant_id': state['grant_id'],
                'policy_digest': digest(state['policy']), 'source': state['source'],
                'state_digest': self._state_digest(state), 'created_at': state['created_at'],
                'deadline': state['deadline'], 'used': state['used'],
                'charged_dispatches': sum(state['used'].values()), 'failures': state['failures'],
                'closed': state['closed'], 'unknown_outcome': state['uncertain'],
                'outstanding_claim_ids': sorted(set(state['claims']) - set(state['outcomes'])),
                'code_digest': digest(state['policy']['code']),
                'capability_digest': digest(state['policy']['capability']),
                'config_digest': state['policy']['provider_config_digest'],
                'event_count': state['sequence'], 'replayed': replayed,
                'dispatch_allowed': False, 'native_ready': False}

    def read_session(self, session_id):
        with self._transaction() as db:
            return self._view(self._load(db, session_id))

    def _active(self, state):
        if state['closed']:
            _fail('SESSION_CLOSED', f"Session is {state['closed']}")
        if state['uncertain']:
            _fail('UNKNOWN_OUTCOME', 'Unknown invocation is permanently charged and blocks fresh work')
        now = self._now()
        if now < state['last_at']:
            _fail('INVALID_CLOCK', 'Authority clock moved backwards')
        if now >= state['deadline']:
            _fail('SESSION_EXPIRED', 'Immutable session deadline has elapsed')
        return now

    def _identity(self, state, value):
        policy = state['policy']
        expected = {'source_digest': state['source']['source_digest'],
                    'config_digest': policy['provider_config_digest'],
                    'code_digest': digest(policy['code']), 'capability_digest': digest(policy['capability'])}
        if any(value.get(key) != item for key, item in expected.items()):
            _fail('IDENTITY_MISMATCH', 'Source/config/code/capability identity differs')

    def _replay(self, state, kind, key, payload):
        old = state['events'].get(kind + ':' + key)
        if old and (old['kind'] != kind or old['payload'] != payload):
            _fail('IDEMPOTENCY_CONFLICT', 'Event key already has another payload')
        return old

    def _append(self, db, state, kind, key, payload):
        if state['sequence'] >= MAX_EVENTS:
            _fail('LEDGER_LIMIT', 'Bounded event ledger is full')
        if self._now() < state['last_at']:
            _fail('INVALID_CLOCK', 'Authority clock moved backwards')
        body = {'sequence': state['sequence'] + 1, 'event_key': kind + ':' + key,
                'kind': kind, 'payload': payload, 'previous': state['tail'], 'at': self._now()}
        raw = json_text(body)
        if len(raw.encode('utf-8')) > MAX_EVENT_BYTES:
            _fail('RECORD_TOO_LARGE', 'Event exceeds bounded ledger storage', 422)
        event_digest = digest(body)
        db.execute('INSERT INTO loop_events VALUES (?,?,?,?,?)',
                   (state['session_id'], body['sequence'], body['event_key'], raw, event_digest))
        db.execute('UPDATE loop_anchors SET event_count=?,tail_digest=? WHERE session_id=?',
                   (body['sequence'], event_digest, state['session_id']))
        return event_digest

    def _parent(self, state, target):
        claim = state['claims'].get(target['parent_claim_id'])
        outcome = state['outcomes'].get(target['parent_claim_id'])
        if (not claim or not outcome or not outcome['candidate'] or outcome['transport_status'] != 'succeeded'
                or outcome['invocation_started'] is not True or outcome['process_stopped'] is not True
                or claim['request']['operation'] not in {'generate_candidates', 'repair_candidate'}):
            _fail('TARGET_PARENT_INVALID', 'Target lacks a stopped successful candidate parent')
        request = claim['request']; candidate = outcome['candidate']
        expected = {'session_id': state['session_id'], 'parent_claim_id': claim['claim_id'],
                    'parent_request_digest': claim['request_digest'], 'parent_source_digest': request['source_digest'],
                    'parent_proposal_id': request['proposal_id'], 'parent_attempt_id': request['attempt_id'],
                    'parent_context_digest': request['context_digest'], 'parent_prepared_digest': request['prepared_digest'],
                    'parent_response_digest': outcome['response_digest'], 'parent_raw_sha256': outcome['raw_sha256'],
                    'parent_receipt_digest': outcome['transport_receipt_digest'], 'parent_validation_digest': outcome['validation_digest'],
                    'candidate_id': candidate['candidate']['id'], 'candidate_digest': candidate['candidate_digest'],
                    'assertions_digest': candidate['assertions_digest']}
        source = state['source']
        if source['kind'] != 'research_revision':
            _fail('TARGET_SOURCE_INVALID', 'Repair/decision requires an explicit research revision')
        expected.update(source_digest=source['source_digest'], research_id=source['research_id'],
                        revision_id=source['revision_id'], revision_digest=source['revision_digest'],
                        config_digest=state['policy']['provider_config_digest'],
                        code_digest=digest(state['policy']['code']), capability_digest=digest(state['policy']['capability']))
        if any(target.get(key) != value for key, value in expected.items()):
            _fail('TARGET_BINDING_MISMATCH', 'Target differs from exact original claim/receipt/candidate/revision')
        return claim, outcome

    def _check_target(self, state, request):
        target = request['target']
        if not target:
            return
        claim, outcome = self._parent(state, target)
        if request['assertions_digest'] != target['assertions_digest']:
            _fail('TARGET_BINDING_MISMATCH', 'Research assertions changed during repair/decision')
        if target['kind'] == 'repair':
            if (claim['request']['source_digest'] != state['source']['source_digest']
                    or outcome['domain_status'] != 'rejected' or outcome['diagnostic_code'] != 'invalid_window'
                    or not outcome['diagnostics_digest'] or target['diagnostics_digest'] != outcome['diagnostics_digest']):
                _fail('TARGET_NOT_REPAIRABLE', 'Repair requires the current source and exact invalid-window diagnostic')
            expression = outcome['candidate']['candidate']['expression']
            try:
                _check_expression(expression, set(state['policy']['capability']['fields']))
            except ExpressionError as exc:
                if exc.code != 'invalid_window':
                    _fail('TARGET_NOT_REPAIRABLE', 'Only an integral-valued written window literal may be normalized')
            else:
                _fail('TARGET_NOT_REPAIRABLE', 'Candidate is already executable')
            repaired = repair_candidate_expression(expression)
            if repaired is None:
                _fail('TARGET_NOT_REPAIRABLE', 'No same-value integral window repair exists')
            try:
                _check_expression(repaired, set(state['policy']['capability']['fields']))
            except ValueError:
                _fail('TARGET_NOT_REPAIRABLE', 'Normalized implementation still requires an unavailable field/operator')
        else:
            experiment = state['experiments'].get(target['experiment_projection_digest'])
            keys = ('source_digest', 'research_id', 'revision_id', 'revision_digest', 'config_digest',
                    'code_digest', 'capability_digest', 'candidate_id', 'candidate_digest',
                    'experiment_id', 'research_job_id', 'run_id', 'execution_attempt_id',
                    'frozen_result_digest', 'state_digest', 'completion_step_id', 'completion_step_digest')
            if (outcome['domain_status'] != 'accepted' or not experiment
                    or experiment['parent_claim_id'] != claim['claim_id']
                    or any(target.get(key) != experiment[key] for key in keys)):
                _fail('TARGET_RESULT_MISMATCH', 'Decision requires the exact owned completed validation result')

    def claim(self, request):
        request = _model(LoopClaim, request)
        with self._transaction() as db:
            state = self._load(db, request['session_id'])
            old = self._replay(state, 'claim', request['idempotency_key'], request)
            claim_id = 'claim_' + digest(request)
            if old:
                return dict(state['claims'][claim_id], replayed=True, dispatch_allowed=False, native_ready=False)
            self._active(state)
            if request['expected_state_digest'] != self._state_digest(state):
                _fail('STATE_CONFLICT', 'Caller must supply the exact current state digest')
            self._identity(state, request)
            if set(state['claims']) - set(state['outcomes']):
                _fail('CLAIM_OUTSTANDING', 'Resolve the original claim; never redispatch it')
            budget = state['policy']['budget']; op = request['operation']
            if (sum(state['used'].values()) >= budget['max_dispatches'] or state['used'][op] >= budget['operations'][op]):
                _fail('BUDGET_EXHAUSTED', 'Shared or operation-specific dispatch allowance exhausted')
            if state['failures'] >= budget['max_failures']:
                _fail('FAILURE_BUDGET_EXHAUSTED', 'Permanent failure allowance exhausted')
            limits = {'context_bytes': 'max_context_bytes', 'output_bytes': 'max_output_bytes',
                      'capture_bytes': 'max_capture_bytes', 'max_seconds': 'max_attempt_seconds'}
            if any(request[key] > budget[limit] for key, limit in limits.items()):
                _fail('REQUEST_LIMIT_EXCEEDED', 'Claim exceeds immutable byte/time allowance', 422)
            source_kind = state['source']['kind']
            if ((op == 'extract_material' and source_kind != 'paper_selection')
                    or (op == 'generate_candidates' and source_kind not in {'accepted_material', 'research_revision'})
                    or (op in {'repair_candidate', 'decide_next_action'} and source_kind != 'research_revision')):
                _fail('OPERATION_SOURCE_MISMATCH', 'Operation is not valid for the current source stage')
            self._check_target(state, request)
            if any(item['request']['proposal_id'] == request['proposal_id'] or item['request']['attempt_id'] == request['attempt_id'] for item in state['claims'].values()):
                _fail('ATTEMPT_ID_REUSED', 'Proposal/attempt identities cannot authorize another dispatch')
            self._append(db, state, 'claim', request['idempotency_key'], request)
            return dict(self._load(db, state['session_id'])['claims'][claim_id],
                        replayed=False, dispatch_allowed=False, fixture_consumption_pending=True, native_ready=False)

    def consume_fixture_claim(self, session_id, claim_id):
        _identifier(claim_id)
        with self._transaction() as db:
            state = self._load(db, session_id)
            item = state['claims'].get(claim_id)
            if not item:
                _fail('CLAIM_NOT_FOUND', 'No session-owned claim exists', 404)
            if claim_id in state['consumed'] or claim_id in state['outcomes']:
                _fail('DISPATCH_SPENT', 'Claim consumption is irreversible; replay is not dispatch authorization')
            now = self._active(state)
            self._identity(state, item['request'])
            attempt_deadline = min(state['deadline'], (datetime.fromisoformat(item['at']) + timedelta(seconds=item['request']['max_seconds'])).isoformat())
            if now >= attempt_deadline:
                _fail('ATTEMPT_EXPIRED', 'Original claim attempt deadline elapsed')
            self._append(db, state, 'consume', claim_id,
                         {'claim_id': claim_id, 'request_digest': item['request_digest']})
            return {'claim_id': claim_id, 'request_digest': item['request_digest'],
                    'fixture_consumed': True, 'dispatch_allowed': False, 'native_ready': False,
                    'attempt_deadline': attempt_deadline}

    def record_outcome(self, outcome):
        outcome = _model(LoopOutcome, outcome)
        with self._transaction() as db:
            state = self._load(db, outcome['session_id']); claim_id = outcome['claim_id']
            old = self._replay(state, 'outcome', claim_id, outcome)
            if old:
                return {'outcome_digest': digest(outcome), 'replayed': True, 'dispatch_allowed': False}
            item = state['claims'].get(claim_id)
            if not item:
                _fail('CLAIM_NOT_FOUND', 'Receipt has no original session-owned claim', 404)
            request = item['request']
            if (outcome['request_digest'] != item['request_digest']
                    or outcome['proposal_id'] != request['proposal_id'] or outcome['attempt_id'] != request['attempt_id']):
                _fail('OUTCOME_BINDING_MISMATCH', 'Receipt must bind the exact original claim/proposal/attempt')
            if outcome['invocation_started'] is True and claim_id not in state['consumed']:
                _fail('OUTCOME_UNCONSUMED', 'Invocation requires a durably consumed fixture claim')
            if any(outcome[key] > request[key] for key in ('context_bytes', 'output_bytes', 'capture_bytes')):
                _fail('OUTCOME_LIMIT_EXCEEDED', 'Captured bytes exceed original claim allowance', 422)
            if outcome['transport_status'] == 'succeeded':
                if not outcome['validation_digest']:
                    _fail('OUTCOME_VALIDATION_REQUIRED', 'Successful transport needs an explicit tool validation identity')
                candidate = outcome['candidate']; op = request['operation']
                if op in {'generate_candidates', 'repair_candidate'} and outcome['domain_status'] in {'accepted', 'rejected'}:
                    if not candidate or candidate['assertions_digest'] != request['assertions_digest']:
                        _fail('OUTCOME_CANDIDATE_REQUIRED', 'Candidate/assertions must bind the original request')
                    if op == 'repair_candidate':
                        parent = state['outcomes'][request['target']['parent_claim_id']]['candidate']['candidate']
                        actual = candidate['candidate']; expected = repair_candidate_expression(parent['expression'])
                        preserved = dict(actual, expression=parent['expression'])
                        if preserved != parent:
                            _fail('REPAIR_SEMANTICS_CHANGED', 'Candidate metadata and research attribution cannot change')
                        if actual['expression'] != parent['expression']:
                            try:
                                checked = _check_expression(actual['expression'], set(state['policy']['capability']['fields']))
                                fixed = _check_expression(expected, set(state['policy']['capability']['fields']))
                            except (ValueError, TypeError):
                                _fail('REPAIR_SEMANTICS_CHANGED', 'Repair is not the declared equivalent implementation')
                            if checked['canonical_expression'] != fixed['canonical_expression']:
                                _fail('REPAIR_SEMANTICS_CHANGED', 'Fields, direction, operator structure and window values are frozen')
                    if outcome['domain_status'] == 'accepted':
                        try:
                            _check_expression(candidate['candidate']['expression'], set(state['policy']['capability']['fields']))
                        except ValueError:
                            _fail('OUTCOME_NOT_EXECUTABLE', 'Accepted candidate failed the deterministic expression tool')
                elif candidate is not None:
                    _fail('OUTCOME_CANDIDATE_UNEXPECTED', 'This operation/domain status cannot publish a candidate')
                if outcome['domain_status'] == 'rejected' and not all((outcome['diagnostic_code'], outcome['diagnostics_digest'])):
                    _fail('OUTCOME_DIAGNOSTIC_REQUIRED', 'Rejection must retain exact tool diagnostic identity')
            # Late receipts remain recordable after cancellation/expiration. They
            # never reopen the session or refund its reservation.
            if self._now() < state['last_at']:
                _fail('INVALID_CLOCK', 'Authority clock moved backwards')
            self._append(db, state, 'outcome', claim_id, outcome)
            return {'outcome_digest': digest(outcome), 'replayed': False, 'dispatch_allowed': False}

    def advance_source(self, advance):
        advance = _model(SourceAdvance, advance)
        with self._transaction() as db:
            state = self._load(db, advance['session_id'])
            if self._replay(state, 'advance', advance['idempotency_key'], advance):
                return {'advance_digest': digest(advance), 'replayed': True, 'dispatch_allowed': False}
            self._active(state)
            if (advance['expected_state_digest'] != self._state_digest(state)
                    or advance['from_source_digest'] != state['source']['source_digest']):
                _fail('STATE_CONFLICT', 'Source advances only from the exact current revision')
            if set(state['claims']) - set(state['outcomes']):
                _fail('CLAIM_OUTSTANDING', 'Source cannot advance while an invocation is unresolved')
            parent = state['claims'].get(advance['parent_claim_id']); outcome = state['outcomes'].get(advance['parent_claim_id'])
            if (not parent or not outcome or outcome['transport_status'] != 'succeeded'
                    or outcome['domain_status'] != 'accepted' or digest(outcome) != advance['parent_outcome_digest']
                    or parent['request']['source_digest'] != advance['from_source_digest']):
                _fail('SOURCE_EDGE_UNOWNED', 'Advance requires an exact accepted claim owned by this source/session')
            old, new = state['source'], advance['to_source']; cap = state['policy']['capability']
            if any(old[key] != new[key] for key in ('paper_id', 'paper_sha256', 'document_digest')) or old['source_digest'] == new['source_digest']:
                _fail('SOURCE_LINEAGE_MISMATCH', 'Paper identity must stay frozen and source digest must advance')
            op = parent['request']['operation']
            if advance['kind'] == 'material_adoption':
                if old['kind'] != 'paper_selection' or op != 'extract_material':
                    _fail('SOURCE_EDGE_INVALID', 'Only accepted paper extraction can adopt material')
            else:
                if (op not in {'generate_candidates', 'repair_candidate'} or not outcome['candidate']
                        or outcome['candidate']['candidate_digest'] != advance['candidate_digest']
                        or (new['dataset_id'], new['dataset_digest']) != (cap['dataset_id'], cap['dataset_digest'])):
                    _fail('SOURCE_EDGE_INVALID', 'Commit requires the owned candidate and frozen dataset capability')
                if advance['kind'] == 'research_commit' and old['kind'] != 'accepted_material':
                    _fail('SOURCE_EDGE_INVALID', 'Initial research commit must start from accepted material')
                if advance['kind'] == 'experiment_commit' and (old['kind'] != 'research_revision'
                        or old['research_id'] != new['research_id'] or old['revision_id'] == new['revision_id']):
                    _fail('SOURCE_EDGE_INVALID', 'Experiment commit must advance the same owned research to a new revision')
                for edge in state['advances']:
                    if any(edge.get(key) == advance[key] for key in ('reservation_token', 'experiment_id', 'research_job_id', 'commit_step_id')):
                        _fail('COMMIT_ID_REUSED', 'Owned commit/experiment identities cannot be reused')
            self._append(db, state, 'advance', advance['idempotency_key'], advance)
            return {'advance_digest': digest(advance), 'replayed': False, 'dispatch_allowed': False}

    def record_experiment_result(self, projection):
        projection = _model(ExperimentProjection, projection)
        with self._transaction() as db:
            state = self._load(db, projection['session_id'])
            if self._replay(state, 'experiment', projection['idempotency_key'], projection):
                return {'experiment_projection_digest': digest(projection), 'replayed': True, 'dispatch_allowed': False}
            self._active(state); self._identity(state, projection)
            source = state['source']
            if source['kind'] != 'research_revision' or any(projection[key] != source[key] for key in ('research_id', 'revision_id', 'revision_digest')):
                _fail('RESULT_REVISION_MISMATCH', 'Result must reference the exact current owned revision')
            keys = ('parent_claim_id', 'reservation_token', 'experiment_id', 'research_job_id',
                    'commit_step_id', 'commit_receipt_digest', 'candidate_digest')
            edges = [edge for edge in state['advances'] if edge['kind'] in {'research_commit', 'experiment_commit'}
                     and edge['to_source'] == source and all(edge[key] == projection[key] for key in keys)]
            outcome = state['outcomes'].get(projection['parent_claim_id'])
            if (len(edges) != 1 or not outcome or outcome['domain_status'] != 'accepted' or not outcome['candidate']
                    or outcome['candidate']['candidate']['id'] != projection['candidate_id']):
                _fail('RESULT_EDGE_UNOWNED', 'Result requires the exact owned commit and original candidate')
            if any(existing['experiment_id'] == projection['experiment_id'] or existing['run_id'] == projection['run_id']
                   or existing['execution_attempt_id'] == projection['execution_attempt_id']
                   or existing['completion_step_id'] == projection['completion_step_id'] for existing in state['experiments'].values()):
                _fail('RESULT_ID_REUSED', 'A completed result identity cannot be reattributed')
            self._append(db, state, 'experiment', projection['idempotency_key'], projection)
            return {'experiment_projection_digest': digest(projection), 'replayed': False, 'dispatch_allowed': False}

    def close_session(self, session_id, *, action, reason, idempotency_key):
        if (action not in {'cancel', 'revoke'} or not isinstance(reason, str) or not 1 <= len(reason) <= 256
                or not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', idempotency_key)):
            _fail('INVALID_CONTRACT', 'Closure requires a bounded reason, key and cancel/revoke action', 422)
        payload = {'reason': reason, 'idempotency_key': idempotency_key}
        with self._transaction() as db:
            state = self._load(db, session_id)
            if self._replay(state, action, idempotency_key, payload):
                return self._view(state, replayed=True)
            if state['closed']:
                _fail('SESSION_CLOSED', 'Session closure is permanent')
            if self._now() < state['last_at']:
                _fail('INVALID_CLOCK', 'Authority clock moved backwards')
            self._append(db, state, action, idempotency_key, payload)
            return self._view(self._load(db, session_id))
