"""Atomic, cumulative research budgets. This is a ledger, not an execution queue.

Reservation happens before a host calls an existing service. Reserved work is
never refunded, including failed and uncertain dispatches. Replays acknowledge
the same effect only. No model or experimental operation is performed here.
"""
from __future__ import annotations

from contextlib import closing, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from pydantic import TypeAdapter

from ..storage import digest, json_text
from .db import connect, transaction
from .service import ServiceError
from .research_sessions_schema import (SessionCreate, SessionSource, SessionChildren,
                                      SessionReserve, SessionOutcome, SessionReservation,
                                      SessionLedgerState, SessionEvent)

SCHEMA = """
CREATE TABLE research_sessions(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,payload TEXT NOT NULL,payload_digest TEXT NOT NULL,created_at TEXT NOT NULL,deadline_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,state TEXT NOT NULL,state_digest TEXT NOT NULL,event_count INTEGER NOT NULL,ledger_digest TEXT NOT NULL);
CREATE TABLE research_session_events(session_id TEXT NOT NULL REFERENCES research_sessions(id),sequence INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(session_id,sequence));
CREATE TABLE research_session_reservations(token TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES research_sessions(id),effect_key TEXT NOT NULL,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(session_id,effect_key));
CREATE INDEX research_sessions_created ON research_sessions(created_at,id);
"""
MAX_BYTES = 128 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
TERMINAL = frozenset({'completed', 'stopped', 'cancelled', 'exhausted', 'interrupted'})
ZERO = {'provider_dispatches': 0, 'tool_actions': 0, 'experiments': 0, 'failures': 0}
SOURCE = TypeAdapter(SessionSource)
PROPOSAL_OPERATIONS = {'extract': {'extract_material'},
                       'generate': {'generate_from_evidence', 'generate_candidates'},
                       'repair': {'repair_candidate'}, 'decide': {'decide_next_action'}}


class SessionError(ServiceError):
    def __init__(self, message, status=409, *, code):
        super().__init__(message, status)
        self.code, self.retryable = code, False


def _parse(text, maximum=MAX_BYTES):
    if not isinstance(text, str) or len(text.encode()) > maximum:
        raise ValueError('Session payload exceeds the bound')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def reject(_):
        raise ValueError('Nonfinite JSON number')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def _text(value):
    result = json_text(value)
    if len(result.encode()) > MAX_BYTES:
        raise SessionError('Session payload exceeds the byte budget', 413, code='PAYLOAD_TOO_LARGE')
    return result


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Timestamps must have an explicit timezone')
    return parsed.astimezone(timezone.utc)


class ResearchSessions:
    def __init__(self, store, *, source_resolver=None, clock=None):
        self.store, self.source_resolver = store, source_resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError('Session clock must return an aware datetime')
        return value.astimezone(timezone.utc)

    def _resolve(self, connection, source):
        source = SOURCE.validate_python(source).model_dump()
        if source['kind'] == 'research_revision':
            research = connection.execute('SELECT latest_revision_id FROM researches WHERE id=?',
                                          (source['research_id'],)).fetchone()
            if research is None or research['latest_revision_id'] != source['revision_id']:
                raise SessionError('Research revision is no longer current', code='SOURCE_STALE')
            from .provider_context import _provider_source
            actual = _provider_source(self.store, source['research_id'], source['revision_id'])
            if actual['revision']['digest'] != source['revision_digest'] or digest(actual) != source['source_digest']:
                raise SessionError('Research source identity differs', code='SOURCE_INTEGRITY')
        else:
            if self.source_resolver is None:
                from .provider_research_context import resolve_research_source
                actual = resolve_research_source(self.store, source['source_request'], connection=connection)
            else:
                actual = self.source_resolver(connection, deepcopy(source))
            if not isinstance(actual, dict) or digest(actual) != source['source_digest']:
                raise SessionError('Material source identity differs', code='SOURCE_INTEGRITY')
            identities = ('paper_id', 'selection_digest') if source['kind'] == 'paper_selection' else ('material_id', 'version_id', 'material_digest')
            if any(actual.get(name) != source[name] for name in identities):
                raise SessionError('Material source identifiers differ', code='SOURCE_INTEGRITY')
        return source

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM research_sessions WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise SessionError('Research session not found', 404, code='SESSION_NOT_FOUND')
        try:
            request = SessionCreate.model_validate(_parse(row['payload'])).model_dump()
            if (digest(request) != row['payload_digest'] or row['request_digest'] != digest(request)
                    or row['id'] != 'research_session_' + digest(request)
                    or row['idempotency_key'] != request['idempotency_key']
                    or _timestamp(row['deadline_at']) != _timestamp(row['created_at']) + timedelta(seconds=request['budget']['max_seconds'])
                    or row['metadata_digest'] != digest({key: row[key] for key in
                       ('id', 'request_digest', 'payload_digest', 'created_at', 'deadline_at')})):
                raise ValueError('Session metadata differs')
            state = SessionLedgerState.model_validate(_parse(row['state'])).model_dump()
            if digest(state) != row['state_digest']:
                raise ValueError('Session state digest differs')
            event_rows = connection.execute('SELECT * FROM research_session_events WHERE session_id=? ORDER BY sequence', (identity,)).fetchall()
            reservation_rows = connection.execute('SELECT * FROM research_session_reservations WHERE session_id=? ORDER BY token', (identity,)).fetchall()
            if (not event_rows or len(event_rows) != row['event_count'] or len(event_rows) > 3000
                    or digest([item['digest'] for item in event_rows]) != row['ledger_digest']
                    or sum(len(item['payload'].encode()) for item in event_rows) > MAX_LEDGER_BYTES):
                raise ValueError('Session event anchor differs')
            events, anchors, previous = [], {}, None
            for index, item in enumerate(event_rows, 1):
                event = SessionEvent.model_validate(_parse(item['payload'])).model_dump()
                if (event['sequence'] != index or item['sequence'] != index or event['state_after']['version'] != index
                        or event['previous_digest'] != previous
                        or digest(event) != item['digest']):
                    raise ValueError('Session event chain differs')
                _timestamp(event['created_at'])
                if index == 1 and (event['kind'] != 'created' or event['state_after']['current_source'] != request['source']):
                    raise ValueError('Session root source differs')
                if event['reservation'] is not None:
                    anchors[event['reservation']['token']] = digest(event['reservation'])
                events.append(event)
                previous = item['digest']
            if events[-1]['state_after'] != state:
                raise ValueError('State is not anchored by the last event')
            reservations, usage = [], deepcopy(ZERO)
            for item in reservation_rows:
                reservation = SessionReservation.model_validate(_parse(item['payload'])).model_dump()
                fingerprint = digest({key: reservation[key] for key in ('action', 'effect_key', 'action_digest')})
                charges = {**ZERO, 'tool_actions': 1,
                           'provider_dispatches': int(reservation['action'] in {'extract', 'generate', 'repair', 'decide'}),
                           'experiments': int(reservation['action'] == 'experiment')}
                if (item['token'] != reservation['token'] or item['effect_key'] != reservation['effect_key']
                        or item['request_digest'] != fingerprint or reservation['request_digest'] != fingerprint
                        or item['digest'] != digest(reservation) or anchors.get(item['token']) != item['digest']
                        or reservation['token'] != 'session_action_' + digest({'session_id': identity, 'effect_key': reservation['effect_key']})
                        or reservation['deadline_at'] != row['deadline_at'] or reservation['charges'] != charges
                        or reservation['source_digest'] != reservation['source']['source_digest']
                        or (reservation['status'] == 'finished') != (reservation['outcome'] is not None)
                        or (reservation['status'] == 'effect_recorded' and reservation['children'] is None)):
                    raise ValueError('Reservation identity or event anchor differs')
                for key in usage:
                    usage[key] += reservation['charges'][key]
                if reservation['outcome'] and reservation['outcome']['status'] in {'failed', 'interrupted'}:
                    usage['failures'] += 1
                reservations.append(reservation)
            reservations.sort(key=lambda item: item['sequence'])
            if (len(anchors) != len(reservations) or [r['sequence'] for r in reservations] != list(range(1, len(reservations) + 1))
                    or usage != state['usage'] or any(usage[key] > request['budget']['max_' + key] for key in usage)
                    or sum(r['status'] != 'finished' for r in reservations) > 1):
                raise ValueError('Session cumulative accounting differs')
            return {'row': dict(row), 'request': request, 'state': state, 'events': events, 'reservations': reservations}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise SessionError('Research session ledger failed integrity verification', code='LEDGER_INTEGRITY') from exc

    def _view(self, loaded):
        row, request, state = loaded['row'], loaded['request'], loaded['state']
        remaining_seconds = max(0.0, (_timestamp(row['deadline_at']) - self._now()).total_seconds())
        effective_state = state['state']
        reason = state['stop_reason']
        if effective_state not in TERMINAL and remaining_seconds == 0:
            effective_state, reason = 'exhausted', 'ABSOLUTE_DEADLINE_EXCEEDED'
        return {'schema_version': 1, 'id': row['id'], 'request_digest': row['request_digest'],
                'version': state['version'],
                'state_digest': row['state_digest'], 'ledger_digest': row['ledger_digest'],
                'created_at': row['created_at'], 'deadline_at': row['deadline_at'],
                'root_source': deepcopy(request['source']), 'root_source_digest': request['source']['source_digest'],
                'current_source': deepcopy(state['current_source']), 'purpose': request['purpose'],
                'config': deepcopy(request['config']), 'budget': deepcopy(request['budget']),
                'usage': deepcopy(state['usage']), 'state': state['state'],
                'effective_state': effective_state, 'stop_reason': reason,
                'remaining_seconds': remaining_seconds, 'reservations': deepcopy(loaded['reservations']),
                'semantic_fidelity': 'unverified',
                'execution_scope': (('provider_and_experiment_gated' if request['budget']['max_experiments'] > 0 else 'provider_queue_gated')
                                    if request['config']['contract_id'] == 'research_provider_v2' else 'ledger_only')}

    def _event(self, connection, loaded, kind, key, fingerprint, *, reservation=None, receipt_digest=None):
        row, state = loaded['row'], loaded['state']
        state['version'] = len(loaded['events']) + 1
        event = SessionEvent.model_validate({'sequence': len(loaded['events']) + 1,
                  'kind': kind, 'created_at': self._now().isoformat(),
                  'previous_digest': digest(loaded['events'][-1]) if loaded['events'] else None,
                  'operation_key': key, 'operation_digest': fingerprint, 'state_after': deepcopy(state),
                  'reservation': deepcopy(reservation), 'commit_receipt_digest': receipt_digest}).model_dump()
        encoded, event_digest = _text(event), digest(event)
        if len(loaded['events']) >= 3000 or sum(len(json_text(e).encode()) for e in loaded['events']) + len(encoded.encode()) > MAX_LEDGER_BYTES:
            raise SessionError('Session event ledger is full', code='LEDGER_BUDGET_EXCEEDED')
        connection.execute('INSERT INTO research_session_events VALUES (?,?,?,?)', (row['id'], event['sequence'], encoded, event_digest))
        loaded['events'].append(event)
        row.update(state=_text(state), state_digest=digest(state), event_count=len(loaded['events']),
                   ledger_digest=digest([digest(e) for e in loaded['events']]))
        connection.execute('UPDATE research_sessions SET state=?,state_digest=?,event_count=?,ledger_digest=? WHERE id=?',
                           (row['state'], row['state_digest'], row['event_count'], row['ledger_digest'], row['id']))

    @staticmethod
    def _expected(loaded, expected_digest):
        if loaded['row']['state_digest'] != expected_digest:
            raise SessionError('Session changed; refresh its state before another action', code='SESSION_CONFLICT')

    def _open(self, loaded):
        if loaded['state']['state'] in TERMINAL:
            raise SessionError('Session is closed; no new effect is permitted', code='SESSION_CLOSED')
        if self._now() >= _timestamp(loaded['row']['deadline_at']):
            raise SessionError('The absolute deadline, including waiting, has passed', code='SESSION_EXPIRED')

    def create(self, source, purpose, config, budget, key, *, _connection=None):
        request = SessionCreate.model_validate({'source': source, 'purpose': purpose, 'config': config,
                                                'budget': budget, 'idempotency_key': key}).model_dump()
        fingerprint, encoded = digest(request), _text(request)
        identity = 'research_session_' + fingerprint
        with (transaction(self.store.db_path) if _connection is None else nullcontext(_connection)) as connection:
            old = connection.execute('SELECT id,request_digest FROM research_sessions WHERE idempotency_key=?', (key,)).fetchone()
            if old:
                if old['request_digest'] != fingerprint:
                    raise SessionError('Session key was used for a different plan', code='IDEMPOTENCY_CONFLICT')
                return self._view(self._load(connection, old['id']))
            self._resolve(connection, request['source'])
            created = self._now()
            row = {'id': identity, 'idempotency_key': key, 'request_digest': fingerprint,
                   'payload': encoded, 'payload_digest': fingerprint, 'created_at': created.isoformat(),
                   'deadline_at': (created + timedelta(seconds=request['budget']['max_seconds'])).isoformat()}
            row['metadata_digest'] = digest({name: row[name] for name in
                                             ('id', 'request_digest', 'payload_digest', 'created_at', 'deadline_at')})
            state = SessionLedgerState.model_validate({'state': 'created', 'current_source': request['source'], 'usage': ZERO}).model_dump()
            row.update(state=_text(state), state_digest=digest(state), event_count=0, ledger_digest=digest([]))
            connection.execute('INSERT INTO research_sessions VALUES (:id,:idempotency_key,:request_digest,:payload,:payload_digest,:created_at,:deadline_at,:metadata_digest,:state,:state_digest,:event_count,:ledger_digest)', row)
            loaded = {'row': row, 'request': request, 'state': state, 'events': [], 'reservations': []}
            self._event(connection, loaded, 'created', key, fingerprint)
            return self._view(loaded)

    def get(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            return self._view(self._load(connection, identity))

    def reserve(self, identity, action, effect_key, expected_digest, *, action_digest, _connection=None):
        request = SessionReserve.model_validate({'action': action, 'effect_key': effect_key,
                                                'expected_digest': expected_digest, 'action_digest': action_digest}).model_dump()
        fingerprint = digest({name: request[name] for name in ('action', 'effect_key', 'action_digest')})
        with (transaction(self.store.db_path) if _connection is None else nullcontext(_connection)) as connection:
            loaded = self._load(connection, identity)
            old = next((r for r in loaded['reservations'] if r['effect_key'] == effect_key), None)
            if old:
                if old['request_digest'] != fingerprint:
                    raise SessionError('Effect key was already used for another action', code='IDEMPOTENCY_CONFLICT')
                return {'reservation': deepcopy(old), 'session': self._view(loaded), 'replayed': True}
            if (loaded['request']['config']['contract_id'] == 'research_provider_v2'
                    and action in PROPOSAL_OPERATIONS
                    and connection.execute("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'").fetchone()):
                raise SessionError('Proposal dispatch is paused; inspect its recovery state', 503, code='DISPATCH_PAUSED')
            self._expected(loaded, expected_digest)
            self._open(loaded)
            if any(r['status'] != 'finished' for r in loaded['reservations']):
                raise SessionError('An earlier reservation is unresolved; reconcile it first', code='EFFECT_UNRESOLVED')
            compatible = {'paper_selection': {'extract', 'read'}, 'accepted_material': {'generate', 'experiment', 'read'},
                          'research_revision': {'generate', 'repair', 'decide', 'experiment', 'read'}}
            if action not in compatible[loaded['state']['current_source']['kind']]:
                raise SessionError('Action is incompatible with the frozen source branch', code='ACTION_SOURCE_MISMATCH')
            self._resolve(connection, loaded['state']['current_source'])
            charges = {**ZERO, 'tool_actions': 1, 'provider_dispatches': int(action in {'extract', 'generate', 'repair', 'decide'}),
                       'experiments': int(action == 'experiment')}
            usage, budget = loaded['state']['usage'], loaded['request']['budget']
            if usage['failures'] >= budget['max_failures'] or any(usage[k] + charges[k] > budget['max_' + k] for k in charges):
                raise SessionError('Cumulative session budget is exhausted', code='SESSION_BUDGET_EXCEEDED')
            reservation = SessionReservation.model_validate({'token': 'session_action_' + digest({'session_id': identity, 'effect_key': effect_key}),
                            'sequence': len(loaded['reservations']) + 1, 'action': action, 'effect_key': effect_key,
                            'action_digest': action_digest, 'request_digest': fingerprint, 'created_at': self._now().isoformat(),
                            'deadline_at': loaded['row']['deadline_at'], 'source': loaded['state']['current_source'],
                            'source_digest': loaded['state']['current_source']['source_digest'], 'charges': charges, 'status': 'reserved'}).model_dump()
            # Recheck immediately before committing a new side-effect permit.
            self._open(loaded)
            loaded['state'].update(state='active', stop_reason=None)
            for name in usage:
                usage[name] += charges[name]
            loaded['reservations'].append(reservation)
            connection.execute('INSERT INTO research_session_reservations VALUES (?,?,?,?,?,?)',
                               (reservation['token'], identity, effect_key, fingerprint, _text(reservation), digest(reservation)))
            self._event(connection, loaded, 'reserved', effect_key, fingerprint, reservation=reservation)
            return {'reservation': deepcopy(reservation), 'session': self._view(loaded), 'replayed': False}

    @staticmethod
    def _reservation(loaded, token):
        reservation = next((r for r in loaded['reservations'] if r['token'] == token), None)
        if reservation is None:
            raise SessionError('Reservation does not belong to this session', code='RESERVATION_NOT_FOUND')
        return reservation

    @staticmethod
    def _save_reservation(connection, reservation):
        connection.execute('UPDATE research_session_reservations SET payload=?,digest=? WHERE token=?',
                           (_text(reservation), digest(reservation), reservation['token']))

    def _children(self, connection, loaded, reservation, children):
        if reservation['action'] == 'experiment':
            from .research_session_experiments import verify_experiment_child
            if verify_experiment_child(self.store, connection, loaded, reservation, children):
                return
        source = reservation['source']
        if source['kind'] != 'research_revision' and not children['proposal_id']:
            raise SessionError('Material child ownership awaits the G2 adapter', code='CHILD_SOURCE_UNSUPPORTED')
        if reservation['action'] in {'extract', 'generate', 'repair', 'decide'} and (not children['proposal_id'] or children['research_job_id'] or children['run_id']):
            raise SessionError('A provider action may record only its proposal', code='CHILD_ACTION_MISMATCH')
        if reservation['action'] == 'read':
            raise SessionError('Read actions create no child resource', code='CHILD_ACTION_MISMATCH')
        if reservation['action'] == 'experiment' and (not children['research_job_id'] or children['proposal_id'] or children['run_id']):
            raise SessionError('An experiment records only its job; the job owns its associated run', code='CHILD_ACTION_MISMATCH')
        for name, table in [('proposal_id', 'proposal_requests'), ('research_job_id', 'research_jobs'), ('run_id', 'runs')]:
            identity = children[name]
            if identity is None:
                continue
            row = connection.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
            if row is None or row['idempotency_key'] != reservation['effect_key']:
                raise SessionError('Child is not the reserved effect of this session', code='CHILD_IDENTITY_MISMATCH')
            if source['kind'] == 'research_revision' and row['research_id'] != source['research_id']:
                raise SessionError('Child has a different research owner', code='CHILD_IDENTITY_MISMATCH')
            if row['request_digest'] != reservation['action_digest']:
                raise SessionError('Actual child request differs from the reserved request digest', code='CHILD_REQUEST_MISMATCH')
            if name == 'proposal_id' and source['kind'] == 'research_revision' and row['base_revision_id'] != source['revision_id']:
                raise SessionError('Proposal has a different research source', code='CHILD_SOURCE_MISMATCH')
            if name == 'proposal_id':
                from .proposal_requests import ProposalRequests
                from .provider_contract import digest as wire_digest
                proposal = ProposalRequests(self.store, configs={})._load(connection, identity)
                config = loaded['request']['config']
                operation = proposal['request'].get('operation', proposal['request'].get('purpose'))
                if (operation not in PROPOSAL_OPERATIONS[reservation['action']]
                        or proposal['request'].get('contract_id', 'codex_cli_provider_v1') != config['contract_id']):
                    raise SessionError('Proposal operation or contract differs from the reserved action', code='CHILD_ACTION_MISMATCH')
                if proposal['request'].get('schema_version') == 2:
                    if proposal['request']['session_id'] != loaded['row']['id']:
                        raise SessionError('Proposal names another session', code='CHILD_IDENTITY_MISMATCH')
                    source_request = proposal['request']['source']
                    expected_request = (source['source_request'] if source['kind'] != 'research_revision' else
                                        {'kind': 'research_revision', 'research_id': source['research_id'], 'base_revision_id': source['revision_id']})
                    if source_request != expected_request:
                        raise SessionError('Proposal source descriptor differs', code='CHILD_SOURCE_MISMATCH')
                if (proposal['request']['expected_source_digest'] != source['source_digest']
                        or proposal['request']['provider_config_id'] != config['provider_config_id']
                        or wire_digest(proposal['config']) != config['provider_config_digest']
                        or proposal['request']['budget']['max_provider_invocations'] > reservation['charges']['provider_dispatches']
                        or _timestamp(proposal['deadline_at']) > _timestamp(reservation['deadline_at'])):
                    raise SessionError('Proposal exceeds or differs from its session permit', code='CHILD_BUDGET_MISMATCH')
            if name == 'research_job_id':
                from .research_jobs import ResearchJobs
                job = ResearchJobs(self.store)._load(connection, identity)
                if job['base_revision_id'] != source['revision_id']:
                    raise SessionError('Research job has a different base revision', code='CHILD_SOURCE_MISMATCH')
                if _timestamp(job['created_at']) + timedelta(seconds=job['budget']['max_seconds']) > _timestamp(reservation['deadline_at']):
                    raise SessionError('Research job exceeds the absolute session deadline', code='CHILD_BUDGET_MISMATCH')
            if name == 'run_id' and row['revision_id'] != source['revision_id']:
                raise SessionError('Run has a different source revision; register its committed job instead', code='CHILD_SOURCE_MISMATCH')

    def record_effect(self, identity, token, children):
        children = SessionChildren.model_validate(children).model_dump()
        with transaction(self.store.db_path) as connection:
            return self.record_effect_in_transaction(connection, identity, token, children)

    def record_effect_in_transaction(self, connection, identity, token, children):
        """Register a real child in the same transaction that creates it."""
        children = SessionChildren.model_validate(children).model_dump()
        loaded = self._load(connection, identity)
        reservation = self._reservation(loaded, token)
        if reservation['children'] is not None:
            if reservation['children'] != children:
                raise SessionError('Reservation already refers to another effect', code='IDEMPOTENCY_CONFLICT')
            return {'reservation': deepcopy(reservation), 'session': self._view(loaded), 'replayed': True}
        uncertain_finished = (reservation['status'] == 'finished'
                              and reservation['outcome']['status'] == 'interrupted')
        if reservation['status'] == 'finished' and not uncertain_finished:
            raise SessionError('Finished reservation cannot acquire a new effect', code='RESERVATION_FINISHED')
        self._children(connection, loaded, reservation, children)
        reservation.update(status='finished' if uncertain_finished else 'effect_recorded', children=children)
        self._save_reservation(connection, reservation)
        self._event(connection, loaded, 'effect_recorded', token, digest(children), reservation=reservation)
        return {'reservation': deepcopy(reservation), 'session': self._view(loaded), 'replayed': False}

    def _verify_outcome(self, connection, reservation, outcome):
        """Only actual terminal children can justify success/failure completion.

        Unknown is a conservative stop, never a claim that a child terminated.
        The host still owns selecting the correct semantic next action.
        """
        if self._correction_outcome(connection, reservation, outcome):
            return
        children = reservation['children']
        if children is None:
            if reservation['action'] != 'read' and outcome['status'] not in {'failed', 'interrupted'}:
                raise SessionError('Successful provider/experiment action needs its actual child effect', code='CHILD_REQUIRED')
            return
        if children['proposal_id']:
            from .proposal_requests import ProposalRequests
            child = ProposalRequests(self.store, configs={})._load(connection, children['proposal_id'])
            actual_digest = child['accepted_response_digest'] or (child['attempts'][-1]['response_digest'] if child['attempts'] else None)
            positive = {'ready', 'handed_off', 'abstained'}
            negative = {'rejected', 'failed', 'cancelled', 'expired', 'stale', 'exhausted'}
            if (outcome['status'] in {'completed', 'awaiting_input', 'awaiting_handoff'} and child['status'] not in positive
                    or outcome['status'] == 'failed' and child['status'] not in negative
                    or outcome['status'] == 'awaiting_handoff' and child['status'] == 'abstained'):
                raise SessionError('Recorded outcome contradicts the actual proposal state', code='CHILD_OUTCOME_MISMATCH')
        else:
            from .research_jobs import ResearchJobs
            child = ResearchJobs(self.store)._load(connection, children['research_job_id'])
            actual_digest = digest(_parse(child['result_json'], 512 * 1024)) if child['result_json'] is not None else None
            if (outcome['status'] in {'completed', 'awaiting_input', 'awaiting_handoff'} and child['state'] != 'completed'
                    or outcome['status'] == 'failed' and child['state'] not in {'blocked', 'failed', 'cancelled', 'exhausted'}):
                raise SessionError('Recorded outcome contradicts the actual experiment job state', code='CHILD_OUTCOME_MISMATCH')
        if outcome['result_digest'] != actual_digest:
            raise SessionError('Recorded result digest differs from the actual child artifact', code='CHILD_RESULT_MISMATCH')

    def _correction_outcome(self, connection, reservation, outcome):
        if reservation['action'] != 'read':
            return False
        row = connection.execute('SELECT session_id FROM research_session_reservations WHERE token=?',
                                 (reservation['token'],)).fetchone()
        if row is None:
            raise SessionError('Read reservation is missing', code='LEDGER_INTEGRITY')
        from .research_loop_correction import guard_correction_outcome
        guarded = guard_correction_outcome(self.store, connection, session_id=row['session_id'],
                                           reservation=reservation, outcome=outcome)
        if reservation['effect_key'].startswith('loop-correction-read-') and not guarded:
            raise SessionError('Host correction read lost its durable owner', code='CORRECTION_OWNER_MISSING')
        return guarded

    def finish(self, identity, token, outcome, *, _connection=None):
        outcome = SessionOutcome.model_validate(outcome).model_dump()
        with (transaction(self.store.db_path) if _connection is None else nullcontext(_connection)) as connection:
            loaded = self._load(connection, identity)
            reservation = self._reservation(loaded, token)
            if reservation['outcome'] is not None:
                self._correction_outcome(connection, reservation, outcome)
                if reservation['outcome'] != outcome:
                    raise SessionError('Reservation already has another recorded outcome', code='IDEMPOTENCY_CONFLICT')
                return {'reservation': deepcopy(reservation), 'session': self._view(loaded), 'replayed': True}
            self._verify_outcome(connection, reservation, outcome)
            reservation.update(status='finished', outcome=outcome)
            state, budget = loaded['state'], loaded['request']['budget']
            if outcome['status'] in {'failed', 'interrupted'}:
                state['usage']['failures'] += 1
            if outcome['status'] == 'interrupted':
                state.update(state='interrupted', stop_reason=outcome['code'])
            elif state['state'] not in TERMINAL:
                next_state = {'completed': 'active', 'failed': 'active', 'awaiting_input': 'awaiting_input',
                              'awaiting_handoff': 'awaiting_handoff', 'interrupted': 'interrupted'}[outcome['status']]
                reason = outcome['code'] if next_state in {'interrupted', 'awaiting_input', 'awaiting_handoff'} else None
                if next_state != 'interrupted' and self._now() >= _timestamp(loaded['row']['deadline_at']):
                    next_state, reason = 'exhausted', 'ABSOLUTE_DEADLINE_EXCEEDED'
                elif next_state != 'interrupted' and state['usage']['failures'] >= budget['max_failures']:
                    next_state, reason = 'exhausted', 'FAILURE_BUDGET_EXCEEDED'
                state.update(state=next_state, stop_reason=reason)
            self._save_reservation(connection, reservation)
            self._event(connection, loaded, 'action_finished', token, digest(outcome), reservation=reservation)
            return {'reservation': deepcopy(reservation), 'session': self._view(loaded), 'replayed': False}

    def _close(self, identity, expected_digest, key, status, reason):
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not isinstance(reason, str) or not 1 <= len(reason) <= 128:
            raise SessionError('Stop key and reason must be bounded nonempty strings', 422, code='INVALID_STOP')
        fingerprint = digest({'status': status, 'reason': reason})
        with transaction(self.store.db_path) as connection:
            loaded = self._load(connection, identity)
            old = next((e for e in loaded['events'] if e['operation_key'] == key and e['kind'] in {'session_finished', 'session_cancelled'}), None)
            if old:
                if old['operation_digest'] != fingerprint:
                    raise SessionError('Stop key was used for another decision', code='IDEMPOTENCY_CONFLICT')
                return self._view(loaded)
            self._expected(loaded, expected_digest)
            if status == 'completed' and self._now() >= _timestamp(loaded['row']['deadline_at']):
                raise SessionError('Deadline expired before session completion was recorded', code='SESSION_EXPIRED')
            if status == 'completed' and any(r['status'] != 'finished' for r in loaded['reservations']):
                raise SessionError('Unresolved effects prevent completion', code='EFFECT_UNRESOLVED')
            if loaded['state']['state'] == 'interrupted':
                if status != 'cancelled':
                    raise SessionError('Unknown execution remains interrupted', code='OUTCOME_UNKNOWN')
                # A safety stop request is always available, but it is not proof
                # that an unknown child process stopped and cannot reopen work.
                loaded['state']['stop_reason'] = 'USER_CANCEL_REQUESTED_OUTCOME_UNKNOWN'
                self._event(connection, loaded, 'session_cancelled', key, fingerprint)
                return self._view(loaded)
            if status == 'cancelled' and loaded['state']['state'] in TERMINAL:
                loaded['state']['stop_reason'] = 'USER_CANCEL_REQUESTED_AFTER_' + loaded['state']['state'].upper()
                self._event(connection, loaded, 'session_cancelled', key, fingerprint)
                return self._view(loaded)
            if loaded['state']['state'] in TERMINAL and loaded['state']['state'] != status:
                raise SessionError('Session is already closed', code='SESSION_CLOSED')
            loaded['state'].update(state=status, stop_reason=reason)
            self._event(connection, loaded, 'session_cancelled' if status == 'cancelled' else 'session_finished', key, fingerprint)
            return self._view(loaded)

    def cancel(self, identity, expected_digest, key):
        """Ledger stop is not a child-process stop receipt; the host must cancel children."""
        return self._close(identity, expected_digest, key, 'cancelled', 'USER_CANCELLED')

    def outcome(self, identity, expected_digest, key, *, status, reason):
        if status not in {'completed', 'stopped'}:
            raise SessionError('Session outcome is completed or stopped', 422, code='INVALID_OUTCOME')
        return self._close(identity, expected_digest, key, status, reason)

    def advance_from_job_commit(self, identity, token, job_id, expected_digest):
        """Read an actual session-owned commit receipt; never follow latest blindly."""
        from .research_jobs import ResearchJobs
        with transaction(self.store.db_path) as connection:
            loaded = self._load(connection, identity)
            reservation = self._reservation(loaded, token)
            if not reservation['children'] or reservation['children']['research_job_id'] != job_id:
                raise SessionError('Only this session reservation\'s registered job may advance its source', code='COMMIT_NOT_OWNED')
            job = ResearchJobs(self.store)._load(connection, job_id)
            step = next((s for s in job['steps'] if s['action'] == 'commit' and s['status'] == 'completed'), None)
            if step is None:
                raise SessionError('The child job has no completed commit', code='COMMIT_NOT_FOUND')
            output = _parse(step['output_json'])
            receipt = connection.execute('SELECT * FROM workflow_mutation_receipts WHERE operation=? AND idempotency_key=?',
                                         ('revision.create', step['effect_key'])).fetchone()
            if receipt is None:
                raise SessionError('Authoritative revision receipt is missing', code='COMMIT_INTEGRITY')
            response = _parse(receipt['response'])
            revision = connection.execute('SELECT * FROM revisions WHERE id=?', (output['revision_id'],)).fetchone()
            base = reservation['source']
            if (base['kind'] != 'research_revision' or job['base_revision_id'] != base['revision_id']
                    or response.get('id') != output['revision_id'] or receipt['revision_id'] != output['revision_id']
                    or digest(response) != receipt['response_digest'] or response.get('digest') != output['revision_digest']
                    or revision is None or revision['research_id'] != base['research_id']
                    or revision['digest'] != output['revision_digest'] or digest(_parse(revision['task'])) != revision['digest']
                    or response.get('task') != _parse(revision['task'])):
                raise SessionError('Child commit and revision receipt identities differ', code='COMMIT_INTEGRITY')
            fingerprint = digest({'token': token, 'job_id': job_id, 'receipt_digest': receipt['response_digest']})
            key = 'commit:' + token
            old = next((e for e in loaded['events'] if e['kind'] == 'source_advanced' and e['operation_key'] == key), None)
            if old:
                if old['operation_digest'] != fingerprint:
                    raise SessionError('Commit replay identity changed', code='COMMIT_INTEGRITY')
                return self._view(loaded)
            self._expected(loaded, expected_digest)
            if loaded['state']['current_source'] != base:
                raise SessionError('A different source progression has already happened', code='SOURCE_STALE')
            from .provider_context import _provider_source
            actual = _provider_source(self.store, base['research_id'], revision['id'])
            payload = connection.execute('SELECT payload FROM research_jobs WHERE id=?', (job_id,)).fetchone()
            original = _parse(payload['payload'], 512 * 1024)['source']
            if {k: v for k, v in actual.items() if k != 'revision'} != {k: v for k, v in original.items() if k != 'revision'}:
                raise SessionError('Paper, data or development source changed outside the child commit', code='SOURCE_STALE')
            source = {'kind': 'research_revision', 'research_id': base['research_id'], 'revision_id': revision['id'],
                      'revision_digest': revision['digest'], 'source_digest': digest(actual)}
            self._resolve(connection, source)
            loaded['state']['current_source'] = source
            self._event(connection, loaded, 'source_advanced', key, fingerprint, receipt_digest=receipt['response_digest'])
            return self._view(loaded)
