"""Append-only issue decisions, bounded catalog pages and reproducible summaries."""
from __future__ import annotations

from pathlib import Path
import csv
from datetime import datetime
import io
import json
import time

from .db import connect, transaction
from .service import ServiceError, loads, now, uid
from ..storage import digest, json_text, read_json

SOURCES = {'human', 'automation', 'imported', 'legacy_unknown'}
STATES = {'open', 'proposed', 'awaiting_review', 'resolved', 'deferred'}
DISPOSITIONS = {'implementation_fix', 'hypothesis_change', 'data_change', 'accept_limitation'}


class Feedback:
    def __init__(self, store):
        self.store = store

    def get(self, identity):
        connection = connect(self.store.db_path)
        try:
            connection.execute('BEGIN')
            return self._get(connection, identity)
        finally:
            connection.rollback(); connection.close()

    def _get(self, connection, identity):
        issue = self.store._one(connection, 'issues', identity)
        events = [dict(row) for row in connection.execute('SELECT * FROM issue_events WHERE issue_id=? ORDER BY rowid', (identity,))]
        review = self.store._one(connection, 'reviews', issue['review_id'])
        safe = lambda event: {k: v for k, v in event.items() if k not in {'idempotency_key', 'request_digest'}}
        return self.store._public({'id': issue['id'], 'review_id': issue['review_id'], 'created_at': issue['created_at'],
            'research_id': self.store._one(connection, 'runs', review['run_id'])['research_id'], 'candidate_id': review['candidate_id'],
            'source': review['source'], 'state': events[-1]['state'], 'latest_event_id': events[-1]['id'],
            'events': [safe(event) for event in events]})

    @staticmethod
    def _validate(note, source, disposition, key):
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ServiceError('An explicit issue decision note is required (1..4000 characters)', 422)
        if source not in SOURCES or disposition not in DISPOSITIONS:
            raise ServiceError('Invalid declared source or issue disposition', 422)
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ServiceError('An idempotency key of 1..128 characters is required', 422)

    def create(self, review_id, note, source, disposition, idempotency_key):
        self._validate(note, source, disposition, idempotency_key)
        request_digest = digest({'review_id': review_id, 'note': note, 'source': source, 'disposition': disposition})
        with transaction(self.store.db_path) as connection:
            previous = connection.execute('SELECT * FROM issues WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if previous:
                if previous['request_digest'] != request_digest:
                    raise ServiceError('Issue idempotency key was used for another request', 409)
                identity = previous['id']
            else:
                review = self.store._one(connection, 'reviews', review_id)
                identity, created = uid(), now()
                connection.execute('INSERT INTO issues VALUES (?,?,?,?,?)',
                                   (identity, review_id, created, idempotency_key, request_digest))
                connection.execute('INSERT INTO issue_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (uid(), identity, created, 'open', disposition, note, source,
                                    None, None, None, 'initial:' + identity, request_digest, None, None))
        return self.get(identity)

    def update(self, identity, base_event_id, state, disposition, note, source, idempotency_key,
               revision_id=None, target_run_id=None, check_id=None, review_id=None):
        self._validate(note, source, disposition, idempotency_key)
        if state not in STATES:
            raise ServiceError('Invalid issue state', 422)
        request_digest = digest({'id': identity, 'base_event_id': base_event_id, 'state': state,
            'disposition': disposition, 'note': note, 'source': source, 'revision_id': revision_id,
            'target_run_id': target_run_id, 'check_id': check_id})
        with transaction(self.store.db_path) as connection:
            issue = self.store._one(connection, 'issues', identity)
            if review_id is not None and review_id != issue['review_id']:
                raise ServiceError('The original issue review cannot be changed', 409)
            previous = connection.execute('SELECT * FROM issue_events WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if previous:
                if previous['request_digest'] != request_digest:
                    raise ServiceError('Issue decision key was used for another request', 409)
            else:
                latest = connection.execute('SELECT * FROM issue_events WHERE issue_id=? ORDER BY rowid DESC LIMIT 1', (identity,)).fetchone()
                if latest['id'] != base_event_id:
                    raise ServiceError('Issue changed; reload its latest decision', 409)
                review = self.store._one(connection, 'reviews', issue['review_id'])
                original = self.store._one(connection, 'runs', review['run_id'])
                revision = self.store._one(connection, 'revisions', revision_id) if revision_id else None
                target = self.store._one(connection, 'runs', target_run_id) if target_run_id else None
                if target and (not revision or target['revision_id'] != revision_id):
                    raise ServiceError('Target run must belong to the linked revision', 422)
                if revision and disposition == 'implementation_fix' and revision['research_id'] != original['research_id']:
                    raise ServiceError('An implementation fix must retain the original research/data identity', 422)
                check = loads(self.store._one(connection, 'regression_checks', check_id)['payload']) if check_id else None
                if check and (not target or check['run_id'] != target_run_id):
                    raise ServiceError('Regression check must belong to the target run', 422)
                if state == 'resolved':
                    if not target or not revision:
                        raise ServiceError('Resolve requires an explicitly linked revision and verified target experiment', 422)
                    target, target_state = self.store._verified_state(connection, target_run_id)
                    if not any(c['id'] == review['candidate_id'] for c in target_state['candidates']):
                        raise ServiceError('Target does not contain the reviewed candidate', 422)
                    if disposition == 'implementation_fix':
                        if not check or check.get('attempt_id') != target['attempt_id'] or check.get('result_digest') != digest(target_state):
                            raise ServiceError('Resolve requires a regression check bound to this exact target result', 409)
                        matching = []
                        for result in check.get('results', []):
                            case = self.store._one(connection, 'regression_cases', result['case_id'])
                            if case['review_id'] == review['id']:
                                matching.append(result)
                        if not matching or not all(r.get('passed') and r.get('compatible') for r in matching):
                            raise ServiceError('Original approved case must pass under compatible scientific conditions', 409)
                connection.execute('INSERT INTO issue_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (uid(), identity, now(), state, disposition, note, source, revision_id, target_run_id,
                     check_id, idempotency_key, request_digest,
                     target['attempt_id'] if target else None, digest(loads(target['state'])) if target and target['state'] else None))
                self.store._event(connection, original['id'], 'issue_decision', {'issue_id': identity, 'state': state}, review['attempt_id'])
        return self.get(identity)

    def page(self, resource, after=0, limit=50, through=None, research_id=None, status=None, category=None, source=None, dataset_id=None, candidate_id=None):
        tables = {'runs': 'runs', 'researches': 'researches', 'reviews': 'reviews',
                  'regression-cases': 'regression_cases', 'regression-checks': 'regression_checks', 'issues': 'issues'}
        if resource not in tables or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ServiceError('Invalid catalog or page bounds', 422)
        if through is not None and (type(through) is not int or through < 0):
            raise ServiceError('Invalid catalog watermark', 422)
        table = tables[resource]
        conditions, values = [], []
        # SQL fragments come exclusively from this static mapping.
        research_expr = {'runs': 't.research_id', 'researches': 't.id', 'reviews': '(SELECT research_id FROM runs WHERE id=t.run_id)',
            'regression-cases': 't.research_id', 'regression-checks': '(SELECT research_id FROM runs WHERE id=t.run_id)',
            'issues': '(SELECT research_id FROM runs WHERE id=(SELECT run_id FROM reviews WHERE id=t.review_id))'}[resource]
        if research_id:
            conditions.append(research_expr + '=?'); values.append(research_id)
        if dataset_id:
            conditions.append(f'(SELECT dataset_id FROM researches WHERE id={research_expr})=?'); values.append(dataset_id)
        if candidate_id:
            expr = {'reviews': 't.candidate_id', 'regression-cases': 't.candidate_id',
                    'issues': '(SELECT candidate_id FROM reviews WHERE id=t.review_id)'}.get(resource)
            if expr:
                conditions.append(expr + '=?'); values.append(candidate_id)
            elif resource == 'regression-checks':
                conditions.append("EXISTS (SELECT 1 FROM json_each(t.payload,'$.results') j WHERE json_extract(j.value,'$.candidate_id')=?)"); values.append(candidate_id)
            else:
                revision = 't.revision_id' if resource == 'runs' else 't.latest_revision_id'
                conditions.append(f"EXISTS (SELECT 1 FROM json_each((SELECT task FROM revisions WHERE id={revision}),'$.candidates') j WHERE json_extract(j.value,'$.id')=?)"); values.append(candidate_id)
        if status:
            expr = {'runs': 't.status', 'issues': '(SELECT state FROM issue_events WHERE issue_id=t.id ORDER BY rowid DESC LIMIT 1)',
                    'regression-checks': "json_extract(t.payload,'$.outcome')"}.get(resource)
            if not expr:
                raise ServiceError('This catalog does not support status filtering', 422)
            conditions.append(expr + '=?'); values.append(status)
        if category or source:
            if resource not in {'reviews', 'issues'}:
                raise ServiceError('Review classification filters apply to reviews/issues only', 422)
            for name, value in (('category', category), ('source', source)):
                if value:
                    expr = f't.{name}' if resource == 'reviews' else f'(SELECT {name} FROM reviews WHERE id=t.review_id)'
                    conditions.append(expr + '=?'); values.append(value)
        filtered = (' AND ' + ' AND '.join(conditions)) if conditions else ''
        connection = connect(self.store.db_path)
        try:
            connection.execute('BEGIN')
            newest = connection.execute(f'SELECT COALESCE(MAX(rowid),0) FROM {table}').fetchone()[0]
            watermark = newest if through is None else through
            if after > watermark or watermark > newest:
                raise ServiceError('Catalog cursor exceeds current snapshot; restart pagination', 409)
            rows = connection.execute(f'SELECT t.rowid AS cursor,t.* FROM {table} t WHERE t.rowid>? AND t.rowid<=?{filtered} ORDER BY t.rowid LIMIT ?',
                                      [after, watermark, *values, limit + 1]).fetchall()
            total = connection.execute(f'SELECT COUNT(*) FROM {table} t WHERE t.rowid<=?{filtered}', [watermark, *values]).fetchone()[0]
            issue_dtos = {row['id']: self._get(connection, row['id']) for row in rows[:limit]} if resource == 'issues' else {}
        finally:
            connection.rollback(); connection.close()
        def output(row):
            value = dict(row); value.pop('cursor')
            if resource == 'runs':
                return self.store._run_public(value)
            if resource == 'reviews':
                return self.store._review_public(value)
            if resource == 'regression-checks':
                return loads(value['payload'])
            if resource == 'regression-cases':
                return {**value, 'approved': bool(value['approved']), 'contract': loads(value['contract'])}
            if resource == 'issues':
                return issue_dtos[value['id']]
            return value
        selected = rows[:limit]
        return self.store._public({'items': [output(row) for row in selected], 'next_cursor': selected[-1]['cursor'] if selected else after,
            'has_more': len(rows) > limit, 'high_watermark': watermark, 'total_records': total,
            'filters': {'research_id': research_id, 'status': status, 'category': category, 'source': source, 'dataset_id': dataset_id, 'candidate_id': candidate_id},
            'scope': 'Append boundary is fixed; mutable status filters require refresh to see later changes.'})

    def summary(self, research_id=None):
        from ..feedback_metrics import aggregate_feedback
        started = time.monotonic()
        runs = self.store._read('SELECT * FROM runs' + (' WHERE research_id=?' if research_id else ''), (research_id,) if research_id else ())
        if len(runs) > 500:
            raise ServiceError('Summary is bounded to 500 runs; filter by research', 422)
        ids, outputs, skipped, timings = {r['id'] for r in runs}, [], [], []
        # Expensive artifact verification deliberately runs outside a write transaction.
        for run in runs:
            attempts = self.store._read('SELECT * FROM attempts WHERE run_id=?', (run['id'],))
            for attempt in attempts:
                queues = self.store._read("SELECT created_at FROM events WHERE run_id=? AND kind IN ('queued','retry_queued') AND created_at<=? ORDER BY id DESC LIMIT 1", (run['id'], attempt['started_at']))
                seconds = lambda end, start: max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()) if end and start else None
                timings.append({**{k: attempt[k] for k in ('id', 'run_id', 'number', 'status', 'started_at', 'finished_at')},
                    'queue_seconds': seconds(attempt['started_at'], queues[0]['created_at'] if queues else None),
                    'attempt_seconds': seconds(attempt['finished_at'], attempt['started_at']),
                    'scope': 'Attempt wall time includes engine startup, execution and verification; queue time is measured since this attempt was queued.'})
                measured = self.store._read("SELECT kind,payload FROM events WHERE attempt_id=? AND (kind='attempt_timing' OR kind IN ('completed','failed','cancelled','interrupted')) ORDER BY id", (attempt['id'],))
                timings[-1]['observations'] = [{'kind': row['kind'], **loads(row['payload'])} for row in measured]
                if not (loads(attempt['verification']) or {}).get('verified'):
                    skipped.append({'run_id': run['id'], 'attempt_id': attempt['id'], 'reason': 'no_verified_result'}); continue
                connection = connect(self.store.db_path)
                try:
                    state = read_json(Path(attempt['output_dir']) / 'state.json')
                    self.store._check_integrity(connection, {**run, 'attempt_id': attempt['id'], 'state': json_text(state)})
                    outputs.extend({'run_id': run['id'], 'attempt_id': attempt['id'], 'candidate_id': c['id'],
                                    'result_digest': digest(c), 'revision_id':run['revision_id'], 'state_digest':digest(state),
                                    'eligible': True} for c in state['candidates'])
                except (ValueError, OSError, KeyError):
                    skipped.append({'run_id': run['id'], 'attempt_id': attempt['id'], 'reason': 'integrity_failed'})
                finally:
                    connection.close()
        reviews = [self.store._review_public(r) for r in self.store._read('SELECT * FROM reviews') if r['run_id'] in ids]
        review_ids = {r['id'] for r in reviews}
        issues = [self.get(r['id']) for r in self.store._read('SELECT id,review_id FROM issues') if r['review_id'] in review_ids]
        events = [e for issue in issues for e in issue['events']]
        # Cross-data/hypothesis decisions may point outside the filtered research.
        # Preserve that exact historical proof without adding it to review coverage.
        known = {(o['run_id'], o['attempt_id']) for o in outputs}
        for event in events:
            pair = (event.get('target_run_id'), event.get('target_attempt_id'))
            if not all(pair) or pair in known:
                continue
            connection = connect(self.store.db_path)
            try:
                target = self.store._one(connection, 'runs', pair[0])
                attempt = self.store._one(connection, 'attempts', pair[1])
                if attempt['run_id'] != target['id'] or not (loads(attempt['verification']) or {}).get('verified'):
                    continue
                state = read_json(Path(attempt['output_dir']) / 'state.json')
                self.store._check_integrity(connection, {**target, 'attempt_id': attempt['id'], 'state': json_text(state)})
                outputs.extend({'run_id':target['id'], 'attempt_id':attempt['id'], 'candidate_id':c['id'],
                    'result_digest':digest(c), 'revision_id':target['revision_id'], 'state_digest':digest(state),
                    'eligible':False, 'linkage_verified':True} for c in state['candidates'])
                known.add(pair)
            except (ValueError, OSError, KeyError):
                skipped.append({'run_id':pair[0], 'attempt_id':pair[1], 'reason':'linked_target_integrity_failed'})
            finally:
                connection.close()
        cases = [c for c in self.store.list_cases() if c['review_id'] in review_ids]
        checks = [c for c in self.store.list_checks() if c['run_id'] in ids]
        inputs = self.store._public({'outputs': outputs, 'reviews': reviews, 'issues': issues, 'events': events, 'cases': cases, 'checks': checks})
        result = aggregate_feedback(**inputs)
        return self.store._public({'schema_version': 1, 'created_at': now(), 'filters': {'research_id': research_id},
            'metrics': result, 'inputs': inputs, 'input_digest': digest(inputs),
            'skipped_attempts': skipped, 'attempt_timings': timings, 'elapsed_seconds': time.monotonic() - started,
            'limitations': ['Sources are declared, not authenticated identities.', 'Elapsed closure time is not human effort.',
                            'Unverified and modified outputs are reported separately, not counted as verified review opportunities.']})

    def export(self, research_id, format):
        if format not in {'json', 'csv'}:
            raise ServiceError('Export format must be json or csv', 422)
        report = self.summary(research_id)
        if format == 'json':
            return json_text(report), 'application/json'
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(['metric', 'numerator', 'denominator', 'rate', 'eligible_ids', 'passed_ids', 'filters', 'metrics_version', 'input_digest', 'created_at'])
        for name, value in [*report['metrics'].items(), ('human_structured_assessment_coverage', report['metrics']['structured_assessments']['human_output_coverage'])]:
            if isinstance(value, dict) and 'denominator' in value:
                writer.writerow([name, value['numerator'], value['denominator'], value['rate'],
                    json.dumps(value['eligible_ids']), json.dumps(value['passed_ids']), json.dumps(report['filters']),
                    report['metrics']['metrics_version'], report['input_digest'], report['created_at']])
        return stream.getvalue(), 'text/csv'
