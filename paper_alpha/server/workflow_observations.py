"""Immutable, explicitly declared workflow observations, separate from engine time.

Only known protocol files are read. External CLI references remain inert text.
Transactions serialize identity, interval conflicts and idempotent import receipts.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from .db import connect, transaction
from .research_assessments import parse_timestamp
from .service import REPO, TASK_KEYS, ServiceError, loads, now, uid
from .workflow_observations_schema import (
    PHASES, KINDS, WorkflowObservation, ObservationValidation, ObservationDetail,
    ObservationSummary, ObservationTemplate,
)
from ..evidence import sha256
from ..storage import digest, json_text, read_json

PROTOCOL_PATH = REPO / 'evaluation_suites/v07/human_protocol.json'
MAX_BYTES = 256 * 1024
MAX_RECORDS = 500
LIMITATIONS = [
    'Human and participant identity are declarations, not authenticated identities.',
    'Times are user-declared ended intervals, never inferred from engine logs, issue age or wall-clock session duration.',
    'Observed totals cover only recorded intervals; absent time remains null, and six measured phases do not prove continuous observation.',
    'External CLI results, code commit and machine environment are declared, not independently verified by this importer.',
    'A same-condition comparison is one familiar synthetic Alpha101 case, not general efficiency, statistical significance or market evidence.',
    'Practice, repeated exposure, template reuse and help can cause learning effects; raw records and denominators remain visible.',
    'Review timing is included only when explicitly copied into intervals; review totals are never added again.',
]


def _protocol():
    """The registry currently contains one immutable, development-only protocol."""
    try:
        value = read_json(PROTOCOL_PATH)
        for relative, expected in value['inputs'].items():
            # These names come only from the bundled protocol, never request data.
            path = REPO / relative
            if path.is_symlink() or not path.resolve().is_relative_to(REPO.resolve()) or sha256(path) != expected:
                raise ServiceError('Bundled protocol input digest mismatch; create a new reviewed protocol version', 409)
        return value, sha256(PROTOCOL_PATH)
    except (OSError, ValueError, KeyError) as exc:
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError('Bundled observation protocol is unavailable or invalid', 409) from exc


def _parse(value):
    try:
        if len(json_text(value).encode()) > MAX_BYTES:
            raise ServiceError('Observation exceeds 256 KiB', 413)
        return WorkflowObservation.model_validate(value).model_dump()
    except ValidationError as exc:
        details = '; '.join('.'.join(str(x) for x in item['loc']) + ': ' + item['type']
                            for item in exc.errors(include_input=False, include_context=False)[:10])
        raise ServiceError('Invalid workflow observation: ' + details, 422) from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError('Observation must contain only finite JSON values', 422) from exc


def _timing(observation, current_time):
    start = parse_timestamp(observation['started_at'])
    end = parse_timestamp(observation['finished_at']) if observation['finished_at'] else None
    if start > current_time or (end is not None and (end < start or end > current_time)):
        raise ServiceError('Session timestamps are reversed or in the future', 422)
    if end is not None and end - start > timedelta(days=30):
        raise ServiceError('An observation session must fit within 30 days', 422)
    if observation['completion'] == 'completed':
        if end is None or observation['incomplete_reason'] is not None:
            raise ServiceError('Completed observations require finished_at and null incomplete_reason', 422)
        if observation['dimensions'] is None or any(
            item['outcome'] == 'not_assessed' for item in observation['dimensions'].values()
        ):
            raise ServiceError('Completed observations require an actual judgment and reason in all five dimensions', 422)
        outputs = observation['bound_outputs']
        if not outputs['verification_evidence'] or not outputs['report_reference']:
            raise ServiceError('Completed observations require verification and report references', 422)
    elif not observation['incomplete_reason'] or not observation['incomplete_reason'].strip():
        raise ServiceError('Incomplete or abandoned observations require a reason', 422)
    ordered = []
    phases = {phase: {'phase': phase, **{kind + '_seconds': None for kind in KINDS},
                      'ended_intervals': 0, 'interrupted_intervals': 0} for phase in PHASES}
    for interval in observation['intervals']:
        begin = parse_timestamp(interval['started_at'])
        finish = parse_timestamp(interval['ended_at']) if interval['ended_at'] else None
        if begin < start or begin > (end or current_time) or begin - start > timedelta(days=30):
            raise ServiceError('Intervals must fall within the observed session, never in the future', 422)
        if interval['status'] == 'ended':
            if finish is None or finish <= begin or finish > (end or current_time) or finish - start > timedelta(days=30):
                raise ServiceError('Ended intervals require a positive, explicitly observed duration within the session', 422)
            phase = phases[interval['phase']]
            key = interval['kind'] + '_seconds'
            phase[key] = (phase[key] or 0.0) + (finish - begin).total_seconds()
            phase['ended_intervals'] += 1
        else:
            if finish is not None:
                raise ServiceError('Interrupted intervals require null ended_at; do not reconstruct missing time', 422)
            phases[interval['phase']]['interrupted_intervals'] += 1
        # Unknown ends conservatively occupy the remainder of this session.
        ordered.append((begin, finish or end or current_time))
    previous_end = None
    for begin, finish in sorted(ordered):
        if previous_end is not None and begin < previous_end:
            raise ServiceError('Intervals must not overlap across phases or kinds; copied review time counts once', 422)
        previous_end = finish
    measured = [p for p, value in phases.items() if value['ended_intervals']]
    missing_active = [p for p, value in phases.items() if value['active_seconds'] is None]
    interrupted = sum(value['interrupted_intervals'] for value in phases.values())
    def total(kind):
        values = [value[kind + '_seconds'] for value in phases.values() if value[kind + '_seconds'] is not None]
        return sum(values) if values else None
    complete = not missing_active and not interrupted
    return {'phases': list(phases.values()), 'observed_active_seconds': total('active'),
            'observed_waiting_seconds': total('waiting'), 'observed_away_seconds': total('away'),
            'full_active_seconds': total('active') if complete else None,
            'measured_phases': measured, 'unmeasured_phases': [p for p in PHASES if p not in measured],
            'active_unmeasured_phases': missing_active, 'interrupted_intervals': interrupted,
            'complete_active_coverage': complete}


def _interval_spans(observation):
    end = parse_timestamp(observation['finished_at']) if observation['finished_at'] else None
    return [(parse_timestamp(item['started_at']),
             parse_timestamp(item['ended_at']) if item['ended_at'] else end)
            for item in observation['intervals']]


class WorkflowObservations:
    def __init__(self, store):
        self.store = store

    def template(self):
        protocol, fingerprint = _protocol()
        blank = {key: None for key in WorkflowObservation.model_fields}
        blank.update(schema_version=1, protocol_id=protocol['protocol_id'], protocol_digest=fingerprint,
                     record_status='pending_human_observation', intervals=[], errors_and_rework=[],
                     template_reuse=[], help_received=[], allowed_tools=[], limitations=[],
                     bound_outputs={key: None for key in WorkflowObservation.model_fields['bound_outputs'].annotation.model_fields})
        blank['bound_outputs'].update(task_sha256=protocol['inputs']['evaluation_suites/v05/task.json'], review_ids=[])
        result = {'schema_version': 1, 'protocol': {'id': protocol['protocol_id'], 'digest': fingerprint,
                  'inputs': protocol['inputs'], 'phases': list(PHASES), 'conditions': protocol['conditions']},
                  'observation': blank}
        return ObservationTemplate.model_validate(result).model_dump()

    def _binding(self, connection, research_id, observation, protocol):
        research = self.store._one(connection, 'researches', research_id)
        outputs = observation['bound_outputs']
        expected_task = protocol['inputs']['evaluation_suites/v05/task.json']
        if outputs['task_sha256'] != expected_task:
            raise ServiceError('Observation task digest does not match its frozen protocol', 422)
        result = {'protocol_verified': True, 'scientific_inputs_verified': False, 'outputs_verified': False,
                  'code_and_environment_verified': False,
                  'kind': 'external_declared' if observation['condition'] == 'manual_cli' else 'workbench_bound',
                  'revision_id': outputs['revision_id'], 'run_id': outputs['run_id'],
                  'attempt_id': outputs['attempt_id'], 'review_ids': outputs['review_ids'],
                  'result_digests': {}, 'limitations': [LIMITATIONS[3]]}
        if observation['condition'] == 'manual_cli':
            if any(outputs[key] is not None for key in ('revision_id', 'run_id', 'attempt_id')) or outputs['review_ids']:
                raise ServiceError('External CLI observations cannot claim workbench run/review identifiers', 422)
            if observation['completion'] == 'completed' and not outputs['external_run_reference']:
                raise ServiceError('A completed CLI observation requires a declared external run reference', 422)
            if any(item['source_reference'] is not None for item in observation['intervals']):
                raise ServiceError('External CLI intervals cannot claim workbench review timers', 422)
            result['limitations'].append('External references are stored as text and are never opened by the server.')
            return result
        if outputs['external_run_reference'] is not None:
            raise ServiceError('Workbench observations use exact workbench IDs, not an external run reference', 422)
        if outputs['revision_id'] is None:
            raise ServiceError('Workbench observations require their exact immutable revision', 422)
        revision = self.store._one(connection, 'revisions', outputs['revision_id'])
        if revision['research_id'] != research_id:
            raise ServiceError('Observation revision belongs to a different research', 422)
        paper = self.store._one(connection, 'papers', research['paper_id'])
        dataset = self.store._one(connection, 'datasets', research['dataset_id'])
        # The raw task file is frozen in the protocol, but the workbench rewrites
        # file locations and its display title. Compare scientific fields after
        # the same normalization used at revision creation, not raw path bytes.
        expected = read_json(REPO / 'evaluation_suites/v05/task.json')
        expected = self.store._task({key: expected[key] for key in TASK_KEYS}, research['title'], dataset['id'])
        metadata = read_json(REPO / 'examples/alpha101/metadata.json')
        expected_dataset = digest({'data': protocol['inputs']['examples/alpha101/market.csv'], 'metadata': metadata})
        if (loads(revision['task']) != expected or paper['sha256'] != protocol['inputs']['examples/alpha101/paper.pdf']
                or dataset['sha256'] != expected_dataset or loads(dataset['metadata']) != metadata):
            raise ServiceError('Workbench scientific conditions differ from the frozen protocol; use a new protocol', 422)
        try:
            for path in (paper['pdf_path'], dataset['data_path'], dataset['metadata_path']):
                self.store._safe_artifact_path(Path(path), self.store.root)
            if (sha256(paper['pdf_path']) != paper['sha256']
                    or sha256(dataset['data_path']) != protocol['inputs']['examples/alpha101/market.csv']
                    or read_json(dataset['metadata_path']) != metadata):
                raise ServiceError('Registered scientific input snapshot failed integrity verification', 409)
        except (OSError, ValueError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Registered scientific input snapshot is unavailable', 409) from exc
        result['scientific_inputs_verified'] = True
        if outputs['run_id'] is None:
            if outputs['attempt_id'] is not None or outputs['review_ids'] or observation['completion'] == 'completed':
                raise ServiceError('Run-less observations must be incomplete with no attempt or review IDs', 422)
            if any(item['source_reference'] is not None for item in observation['intervals']):
                raise ServiceError('A copied review timer needs a bound review', 422)
            return result
        run = self.store._one(connection, 'runs', outputs['run_id'])
        if run['research_id'] != research_id or run['revision_id'] != revision['id'] or run['mode'] != protocol['engine_mode']:
            raise ServiceError('Observation run does not match research, revision or protocol engine mode', 422)
        if outputs['attempt_id'] is None:
            if outputs['review_ids'] or observation['completion'] == 'completed':
                raise ServiceError('Completed observations and reviews require an exact attempt', 422)
            if any(item['source_reference'] is not None for item in observation['intervals']):
                raise ServiceError('A copied review timer needs a bound attempt', 422)
            return result
        attempt = self.store._one(connection, 'attempts', outputs['attempt_id'])
        if attempt['run_id'] != run['id']:
            raise ServiceError('Observation attempt belongs to a different run', 422)
        if attempt['status'] not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            raise ServiceError('Observe a terminal attempt; a running attempt is not an immutable outcome', 409)
        verification = loads(attempt['verification']) or {}
        state = None
        if verification.get('verified'):
            # Work only with the expected server-owned path, including historical
            # attempts; never trust a client path or alias the latest attempt.
            output = self.store.root / 'runs' / run['id'] / 'attempts' / attempt['id'] / 'output'
            try:
                self.store._safe_artifact_path(output / 'state.json', output)
                state = read_json(output / 'state.json')
                historical_run = {**run, 'attempt_id': attempt['id'], 'state': json_text(state)}
                self.store._check_integrity(connection, historical_run)
            except (OSError, ValueError) as exc:
                if isinstance(exc, ServiceError):
                    raise
                raise ServiceError('Bound experiment snapshot failed integrity verification', 409) from exc
            result['outputs_verified'] = True
        if observation['completion'] == 'completed' and (not result['outputs_verified'] or attempt['status'] != 'completed'):
            raise ServiceError('Completed workbench observations require a completed verified attempt', 422)
        reviews = {}
        for review_id in outputs['review_ids']:
            review = self.store._one(connection, 'reviews', review_id)
            if (review['run_id'], review['revision_id'], review['attempt_id']) != (run['id'], revision['id'], attempt['id']):
                raise ServiceError('Observation review does not belong to the exact run/revision/attempt', 422)
            assessment = loads(review['assessment'])
            if review['source'] != observation['source']:
                raise ServiceError('Observation source differs from its bound review source', 422)
            if assessment is None or assessment['reviewer'] != observation['participant']:
                raise ServiceError('Bound structured review must declare this participant', 422)
            candidate = next((c for c in (state or {}).get('candidates', []) if c['id'] == review['candidate_id']), None)
            if candidate is None or digest(candidate) != review['result_digest']:
                raise ServiceError('Bound review result no longer matches the verified experiment', 409)
            if review['candidate_id'] != protocol['candidate_id']:
                raise ServiceError('Review candidate differs from the frozen protocol', 422)
            if observation['dimensions'] is not None and assessment['dimensions'] != observation['dimensions']:
                raise ServiceError('Observation dimensions differ from its bound structured review', 422)
            reviews[review_id] = assessment
            result['result_digests'][review_id] = review['result_digest']
        if observation['completion'] == 'completed' and not reviews:
            raise ServiceError('Completed workbench observations require a bound structured review', 422)
        for interval in observation['intervals']:
            reference = interval['source_reference']
            if reference is None:
                continue
            if reference not in reviews or interval['phase'] != 'review' or interval['kind'] != 'active' or interval['status'] != 'ended':
                raise ServiceError('Copied review timing requires a bound review and an ended active review interval', 422)
            target = (parse_timestamp(interval['started_at']), parse_timestamp(interval['ended_at']))
            actual = {(parse_timestamp(x['started_at']), parse_timestamp(x['ended_at'])) for x in reviews[reference]['active_intervals']}
            if target not in actual:
                raise ServiceError('Copied review timing differs from the saved review interval', 422)
        return result

    def _validate(self, connection, research_id, observation):
        protocol, fingerprint = _protocol()
        if observation['protocol_id'] != protocol['protocol_id'] or observation['protocol_digest'] != fingerprint:
            raise ServiceError('Unknown or changed observation protocol identity/digest', 422)
        timing = _timing(observation, datetime.now(timezone.utc))
        binding = self._binding(connection, research_id, observation, protocol)
        warnings = list(LIMITATIONS)
        if observation['source'] == 'automation':
            warnings.append('Automation record: excluded from human counts, completion rates and efficiency comparisons.')
        if observation['practice_session']:
            warnings.append('Practice session: excluded from human completion rates and efficiency comparisons.')
        if timing['full_active_seconds'] is None:
            warnings.append('Full active duration is unavailable because phases are unmeasured or interrupted.')
        return ObservationValidation.model_validate({'valid': True, 'observation_digest': digest(observation),
                                                     'timing': timing, 'binding': binding, 'warnings': warnings}).model_dump()

    def validate(self, research_id, observation):
        observation = _parse(observation)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            result = self._validate(connection, research_id, observation)
            self._conflicts(connection, observation)
            return result

    @staticmethod
    def _verified(row):
        try:
            payload = loads(row['payload'])
            if len(row['payload'].encode()) > MAX_BYTES * 2 or digest(payload) != row['payload_digest']:
                raise ValueError('Payload digest mismatch')
            observation = payload['observation']
            if any(payload[key] != row[key] for key in ('id', 'research_id', 'created_at')):
                raise ValueError('Record metadata differs from frozen payload')
            if any(observation[key] != row[key] for key in ('protocol_digest', 'participant', 'session_id', 'condition')):
                raise ValueError('Observation identity differs from frozen payload')
            if digest({'research_id': row['research_id'], 'observation': observation}) != row['request_digest']:
                raise ValueError('Request digest mismatch')
            return ObservationDetail.model_validate({**payload, 'payload_digest': row['payload_digest'], 'integrity': 'verified'}).model_dump()
        except (TypeError, ValueError, KeyError) as exc:
            raise ServiceError('Stored observation failed integrity verification', 409) from exc

    def _conflicts(self, connection, observation):
        rows = connection.execute('SELECT * FROM workflow_observations WHERE participant=? ORDER BY created_at,id LIMIT ?',
                                  (observation['participant'], MAX_RECORDS + 1)).fetchall()
        if len(rows) > MAX_RECORDS:
            raise ServiceError('Participant observation history exceeds the 500-record conflict-check limit', 413)
        current = _interval_spans(observation)
        for row in rows:
            other = self._verified(row)['observation']
            if (other['protocol_digest'], other['session_id'], other['condition']) == (
                    observation['protocol_digest'], observation['session_id'], observation['condition']):
                raise ServiceError('Observation identity already exists; use the original idempotency key to recover it', 409)
            if other['source'] != observation['source']:
                continue
            for begin, finish in current:
                for old_begin, old_finish in _interval_spans(other):
                    # An open-ended interrupted record cannot establish overlap
                    # beyond its last explicit timestamp; retain missingness.
                    if finish is not None and old_finish is not None and begin < old_finish and old_begin < finish:
                        raise ServiceError('Observed intervals overlap another record for this participant/source', 409)

    def import_observation(self, research_id, observation, idempotency_key):
        observation = _parse(observation)
        key = self.store._mutation_key(idempotency_key)
        if key is None:
            raise ServiceError('Observation import requires an idempotency key', 422)
        fingerprint = digest({'research_id': research_id, 'observation': observation})
        with transaction(self.store.db_path) as connection:
            previous = connection.execute('SELECT * FROM workflow_observations WHERE idempotency_key=?', (key,)).fetchone()
            if previous:
                if previous['request_digest'] != fingerprint:
                    raise ServiceError('Observation idempotency key belongs to a different request', 409)
                return self._verified(previous)
            checked = self._validate(connection, research_id, observation)
            self._conflicts(connection, observation)
            count = connection.execute('SELECT COUNT(*) FROM workflow_observations WHERE research_id=?', (research_id,)).fetchone()[0]
            if count >= MAX_RECORDS:
                raise ServiceError('Research observation limit is 500 immutable records', 413)
            payload = {'id': uid(), 'research_id': research_id, 'created_at': now(), 'observation': observation,
                       'timing': checked['timing'], 'binding': checked['binding'], 'warnings': checked['warnings']}
            row = {key: payload[key] for key in ('id', 'research_id', 'created_at')}
            row.update({key: observation[key] for key in ('protocol_digest', 'participant', 'session_id', 'condition')})
            row.update(idempotency_key=key, request_digest=fingerprint, payload=json_text(payload), payload_digest=digest(payload))
            try:
                connection.execute('INSERT INTO workflow_observations VALUES (:id,:research_id,:created_at,:protocol_digest,:participant,:session_id,:condition,:idempotency_key,:request_digest,:payload,:payload_digest)', row)
            except sqlite3.IntegrityError as exc:
                raise ServiceError('Observation identity or idempotency key already exists', 409) from exc
            return self._verified(row)

    def get_observation(self, research_id, observation_id):
        with closing(connect(self.store.db_path)) as connection:
            row = self.store._one(connection, 'workflow_observations', observation_id)
            if row['research_id'] != research_id:
                raise ServiceError('Observation belongs to another research', 404)
            return self._verified(row)

    def list_observations(self, research_id, limit=20, offset=0):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= MAX_RECORDS:
            raise ServiceError('Observation pagination requires limit 1..100 and offset 0..500', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            self.store._one(connection, 'researches', research_id)
            total = connection.execute('SELECT COUNT(*) FROM workflow_observations WHERE research_id=?', (research_id,)).fetchone()[0]
            rows = connection.execute('SELECT * FROM workflow_observations WHERE research_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                                      (research_id, limit, offset)).fetchall()
            return {'items': [self._verified(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def summary(self, research_id):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            self.store._one(connection, 'researches', research_id)
            rows = connection.execute('SELECT * FROM workflow_observations WHERE research_id=? ORDER BY created_at,id LIMIT ?',
                                      (research_id, MAX_RECORDS + 1)).fetchall()
            if len(rows) > MAX_RECORDS or sum(len(row['payload'].encode()) for row in rows) > 16 * 1024 * 1024:
                raise ServiceError('Observation summary exceeds 500 records / 16 MiB; export individual records', 413)
            records = [self._verified(row) for row in rows]
        eligible = [r for r in records if r['observation']['source'] == 'human' and not r['observation']['practice_session']]
        groups = {}
        for record in eligible:
            value = record['observation']
            conditions = {key: deepcopy(value[key]) for key in (
                'protocol_id', 'protocol_digest', 'participant', 'code_commit', 'environment', 'prior_familiarity', 'predeclared_stop_condition')}
            conditions['allowed_tools'] = sorted(value['allowed_tools'])
            conditions['task_sha256'] = value['bound_outputs']['task_sha256']
            groups.setdefault(digest(conditions), []).append(record)
        comparisons = []
        for group_digest, group in sorted(groups.items()):
            manual = [r for r in group if r['observation']['condition'] == 'manual_cli']
            workbench = [r for r in group if r['observation']['condition'] == 'workbench']
            reasons = []
            if len(manual) != 1 or len(workbench) != 1:
                reasons.append('A comparison requires exactly one manual_cli and one workbench record with the same declared protocol/input/participant/code/environment/tools/familiarity/stop conditions; repeated or missing paths are not selected automatically.')
            pair = [manual[0], workbench[0]] if len(manual) == len(workbench) == 1 else []
            if pair and any(r['observation']['completion'] != 'completed' for r in pair):
                reasons.append('Both observations must be completed; incomplete and abandoned attempts remain in the denominator.')
            if pair and any(r['timing']['full_active_seconds'] is None for r in pair):
                reasons.append('Six measured active phases with no interrupted intervals are required for a total active-time delta.')
            if pair and pair[0]['observation']['execution_order'] == pair[1]['observation']['execution_order']:
                reasons.append('Comparison paths require distinct predeclared execution-order positions.')
            comparable = not reasons
            comparisons.append({'group_digest': group_digest, 'observation_ids': [r['id'] for r in group],
                'manual_observation_id': manual[0]['id'] if len(manual) == 1 else None,
                'workbench_observation_id': workbench[0]['id'] if len(workbench) == 1 else None,
                'comparable': comparable, 'reasons': reasons,
                'comparison_basis': 'same_declared_conditions_case_only',
                'active_seconds_delta': pair[1]['timing']['full_active_seconds'] - pair[0]['timing']['full_active_seconds'] if comparable else None,
                'delta_direction': 'workbench_minus_manual_cli'})
        by_condition = []
        for condition in ('manual_cli', 'workbench'):
            items = [r for r in eligible if r['observation']['condition'] == condition]
            completed_count = sum(r['observation']['completion'] == 'completed' for r in items)
            full = sum(r['timing']['complete_active_coverage'] for r in items)
            by_condition.append({'condition': condition, 'observation_ids': [r['id'] for r in items],
                'records': len(items), 'completed': completed_count,
                'incomplete': sum(r['observation']['completion'] == 'incomplete' for r in items),
                'abandoned': sum(r['observation']['completion'] == 'abandoned' for r in items),
                'completion_rate': completed_count / len(items) if items else None,
                'full_active_coverage_records': full, 'partial_or_unmeasured_records': len(items) - full,
                'interrupted_records': sum(r['timing']['interrupted_intervals'] > 0 for r in items),
                'error_and_rework_entries': sum(len(r['observation']['errors_and_rework']) for r in items)})
        completed = sum(r['observation']['completion'] == 'completed' for r in eligible)
        return ObservationSummary.model_validate({'schema_version': 1, 'research_id': research_id, 'generated_at': now(),
            'total': len(records), 'human_nonpractice_records': len(eligible), 'human_completed_records': completed,
            'human_completion_rate': completed / len(eligible) if eligible else None,
            'automation_records': sum(r['observation']['source'] == 'automation' for r in records),
            'practice_records': sum(r['observation']['practice_session'] for r in records),
            'observations': records, 'by_condition': by_condition, 'comparisons': comparisons, 'limitations': LIMITATIONS}).model_dump()

    def export_observation(self, research_id, observation_id):
        return json_text(self.get_observation(research_id, observation_id))
