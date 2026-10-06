"""Read-only comparisons and bounded, immutable research report snapshots.

Financial metrics are read from verified engine results, never recomputed here.
WAL read transactions capture one database view while files are checked without
a writer lock. Reports deliberately freeze that view, not the later commit time.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import math
from pathlib import Path
import re

from pydantic import ValidationError

from .db import connect, transaction
from .regression import recorded_semantics
from .research_insights_schema import ReportCreate, ResearchReport, RunComparison
from .service import ServiceError, loads, now, uid
from ..storage import digest, json_text, read_json

MAX_REVISIONS = 100
MAX_RELATED = 5000
MAX_EVENTS = 10000
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 24 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_FILES = 3000
# Include orchestration and input dependencies which can change time cutoffs,
# alignment, configuration parsing or the result writer, not just arithmetic.
NUMERICAL_CODE = ('paper_alpha/__init__.py', 'paper_alpha/worker.py', 'paper_alpha/workflow.py',
                  'paper_alpha/contracts.py', 'paper_alpha/storage.py', 'paper_alpha/evidence.py',
                  'paper_alpha/market_diagnostics.py', 'paper_alpha/evaluation.py', 'paper_alpha/expressions.py',
                  'paper_alpha/reporting.py', 'paper_alpha/vendor/__init__.py',
                  'paper_alpha/vendor/factors.py', 'paper_alpha/vendor/operators.py')
LIMITATIONS = [
    'Comparable means recorded evaluation conditions agree; it does not establish economic validity or statistical significance.',
    'Metrics are copied from verified saved results; deltas are candidate minus baseline, not fresh backtests.',
    'Means are conditional on recorded coverage and valid-day counts; coverage changes must be inspected.',
    'Synthetic fixture results are software validation evidence, not investment performance.',
    'Tool counts and engine elapsed time are recorded execution measurements, not human research time or agent advantage.',
]


def _bounded_rows(connection, sql, values=(), limit=MAX_RELATED):
    rows = []
    size = 0
    for row in connection.execute(sql, values):
        item = dict(row)
        size += sum(len(value.encode()) for value in item.values() if isinstance(value, str))
        if len(rows) >= limit or size > MAX_RECORD_BYTES:
            raise ServiceError('Report source exceeds its explicit row/byte limit; choose a narrower research scope', 413)
        rows.append(item)
    return rows


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bound_code(manifest, name):
    declared = manifest.get('signature', {}).get('code', {}).get(name)
    return (isinstance(declared, str) and re.fullmatch(r'[0-9a-f]{64}', declared) is not None
            and manifest.get('snapshot_files', {}).get('source/' + name) == declared)


def _environment(signature):
    value = signature.get('environment')
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key].strip()
                                          for key in ('python', 'platform')):
        return None
    packages = value.get('packages')
    if (not isinstance(packages, dict) or not {'numpy', 'pandas', 'pypdf', 'python-dateutil', 'six'} <= set(packages)
            or any(not isinstance(item, str) or not item.strip() for item in packages.values())):
        return None
    return value


def _clean_record(row):
    return {key: value for key, value in row.items()
            if key not in {'idempotency_key', 'request_digest', 'output_dir', 'path', 'pdf_path', 'data_path', 'metadata_path'}}


class ResearchInsights:
    def __init__(self, store):
        self.store = store

    def _source(self, connection, run_id):
        run = self.store._one(connection, 'runs', run_id)
        if run['state'] and len(run['state'].encode()) > MAX_RECORD_BYTES:
            raise ServiceError('Run state exceeds the research-insights byte limit', 413)
        revision = self.store._one(connection, 'revisions', run['revision_id'])
        research = self.store._one(connection, 'researches', run['research_id'])
        paper = self.store._one(connection, 'papers', research['paper_id'])
        dataset = self.store._one(connection, 'datasets', research['dataset_id'])
        task = loads(revision['task'])
        state = loads(run['state']) or {}
        attempt = self.store._one(connection, 'attempts', run['attempt_id']) if run['attempt_id'] else None
        artifacts = _bounded_rows(connection, 'SELECT * FROM artifacts WHERE run_id=? ORDER BY attempt_id,name', (run_id,))
        current = [item for item in artifacts if item['attempt_id'] == run['attempt_id']]
        verified, error, semantics, manifest = False, None, None, None
        if run['status'] not in {'completed', 'failed'} or not (loads(run['verification']) or {}).get('verified'):
            error = 'A terminal source verified by the engine is unavailable'
        else:
            try:
                output = Path(attempt['output_dir'])
                total, count = 0, 0
                for path in output.rglob('*'):
                    if path.is_symlink():
                        raise ServiceError('Source snapshot contains a symlink', 409)
                    if path.is_file():
                        total += path.stat().st_size
                        count += 1
                    if total > MAX_SOURCE_BYTES or count > MAX_SOURCE_FILES:
                        raise ServiceError('Source snapshot exceeds the file-verification bound', 413)
                export_total = 0
                for artifact in current:
                    path = Path(artifact['path'])
                    self.store._safe_artifact_path(path, output.parent / 'exports')
                    if not path.is_file() or path.stat().st_size != artifact['size']:
                        raise ServiceError('Registered export size changed after verification', 409)
                    export_total += artifact['size']
                    if export_total > MAX_SOURCE_BYTES:
                        raise ServiceError('Export snapshot exceeds the file-verification bound', 413)
                self.store._check_integrity(connection, run)
                manifest = read_json(output / 'manifest.json')
                # Verify each source binding once, then interpret historical
                # literals. Missing semantics cannot become today's constants.
                try:
                    if not all(_bound_code(manifest, name) for name in ('paper_alpha/expressions.py', 'paper_alpha/evaluation.py')):
                        raise ValueError('Historical semantic source hashes are not bound to verified snapshot files')
                    semantics = recorded_semantics(output)
                except (ValueError, OSError, KeyError) as exc:
                    error = 'Historical semantic declarations unavailable: ' + self.store._safe_error(exc)
                verified = True
            except ServiceError as exc:
                if exc.status == 413:
                    raise
                error = str(exc)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                error = 'Source integrity verification failed: ' + self.store._safe_error(exc)
        signature = manifest.get('signature', {}) if manifest else {}
        code = signature.get('code')
        numerical_code = ({name: code[name] for name in NUMERICAL_CODE}
                          if isinstance(code, dict) and all(_bound_code(manifest, name) for name in NUMERICAL_CODE) else None)
        metadata = loads(dataset['metadata'])
        summary = {
            'run': self.store._run_public(run), 'source_verified': verified, 'verification_error': error,
            'attempt_id': run['attempt_id'], 'state_digest': attempt['state_digest'] if attempt else None,
            'revision_digest': revision['digest'], 'paper_sha256': paper['sha256'],
            'dataset_sha256': dataset['sha256'], 'data_metadata': metadata,
            'evaluation': task['evaluation'], 'budget': task['budget'], 'recorded_semantics': semantics,
            'code_digest': digest(code) if code and all(_bound_code(manifest, name) for name in code) else None,
            'environment': _environment(signature),
            'tool_calls': state.get('tool_calls') if verified else None,
            'elapsed_seconds': state.get('elapsed_seconds') if verified else None,
        }
        candidates = {item['id']: deepcopy(item) for item in state.get('candidates', [])}
        for candidate in task.get('candidates', []):
            candidates.setdefault(candidate['id'], {**deepcopy(candidate), 'status': 'not_executed', 'attempts': []})
        sources = {}
        for identity, candidate in candidates.items():
            result = candidate.get('result')
            if not verified or not result:
                continue
            matches = [(name, checksum) for name, checksum in candidate.get('artifacts', {}).items() if name.endswith('/result.json')]
            if len(matches) != 1:
                continue
            name, checksum = matches[0]
            matches = [item for item in current if item['name'] == name]
            if len(matches) != 1:
                continue
            artifact = matches[0]
            # The integrity check verifies original results. This binds each
            # displayed metric to its downloadable redacted export as well.
            try:
                matching_export = read_json(artifact['path']) == self.store._public(result)
            except (OSError, ValueError):
                matching_export = False
            if not matching_export:
                continue
            sources[identity] = {'run_id': run_id, 'attempt_id': run['attempt_id'], 'candidate_id': identity,
                'result_digest': digest(result), 'artifact_id': artifact['id'], 'artifact_name': name,
                'artifact_sha256': artifact['sha256'], 'original_sha256': checksum, 'json_pointer': '/metrics'}
        return {'summary': summary, 'run': run, 'state': state, 'task': task, 'attempt': attempt,
                'artifacts': artifacts, 'candidates': candidates, 'sources': sources,
                'numerical_code': numerical_code, 'signature': signature}

    def compare(self, baseline_run_id, candidate_run_id):
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            left = self._source(connection, baseline_run_id)
            right = self._source(connection, candidate_run_id)
            result = self._comparison(left, right)
        return RunComparison.model_validate(self.store._public(result)).model_dump()

    @staticmethod
    def _comparison(left, right):
        a, b = left['summary'], right['summary']
        conditions = []
        def condition(name, x, y, blocking=True):
            status = 'unknown' if x is None or y is None else 'equal' if x == y else 'changed'
            conditions.append({'name': name, 'baseline': x, 'candidate': y, 'status': status, 'blocking': blocking})
        for name in ('paper_sha256', 'dataset_sha256', 'data_metadata', 'evaluation', 'recorded_semantics', 'environment'):
            condition(name, a[name], b[name])
        condition('numerical_code', left['numerical_code'], right['numerical_code'])
        for name in ('revision_digest', 'code_digest', 'budget'):
            condition(name, a[name], b[name], False)
        condition('mode', a['run']['mode'], b['run']['mode'], False)
        comparable = a['source_verified'] and b['source_verified'] and all(
            item['status'] == 'equal' for item in conditions if item['blocking'])
        reasons = [item['name'] + ':' + item['status'] for item in conditions if item['blocking'] and item['status'] != 'equal']
        if not a['source_verified'] or not b['source_verified']:
            reasons.append('source_not_verified')
        candidates = []
        for identity in sorted(set(left['candidates']) | set(right['candidates'])):
            x, y = left['candidates'].get(identity), right['candidates'].get(identity)
            why = list(reasons)
            if x is None or y is None:
                why.append('candidate_missing_on_one_side')
            if not x or not y or x.get('status') != 'evaluated' or y.get('status') != 'evaluated':
                why.append('both_candidates_must_be_evaluated')
            xs, ys = left['sources'].get(identity), right['sources'].get(identity)
            if not xs or not ys:
                why.append('verified_result_artifact_missing')
            xr, yr = (x or {}).get('result') or {}, (y or {}).get('result') or {}
            # Defend against legacy per-candidate settings which differ from
            # the task-level declaration, even with identical task revisions.
            for field in ('config', 'execution', 'split', 'schema_version'):
                if xs and ys and (field not in xr or field not in yr or xr[field] != yr[field]):
                    why.append('result_' + field + '_mismatch_or_unknown')
            allowed = comparable and not why
            xm, ym = xr.get('metrics', {}) if xs else {}, yr.get('metrics', {}) if ys else {}
            metrics = []
            for name in sorted(set(xm) | set(ym)):
                xv, yv = xm.get(name), ym.get(name)
                xv, yv = xv if _number(xv) else None, yv if _number(yv) else None
                difference = yv - xv if allowed and xv is not None and yv is not None else None
                if difference is not None and not _number(difference):
                    difference = None
                def source(value):
                    return {**value, 'json_pointer': '/metrics/' + name.replace('~', '~0').replace('/', '~1')} if value else None
                metrics.append({'name': name, 'baseline': xv, 'candidate': yv, 'delta': difference,
                    'reason': None if difference is not None else ('; '.join(why) if why else 'missing_or_nonfinite_metric'),
                    'baseline_source': source(xs), 'candidate_source': source(ys)})
            candidates.append({'candidate_id': identity,
                'presence': 'both' if x and y else 'baseline_only' if x else 'candidate_only',
                'baseline_status': x.get('status') if x else None, 'candidate_status': y.get('status') if y else None,
                'baseline_expression': x.get('expression') if x else None, 'candidate_expression': y.get('expression') if y else None,
                'expression_changed': x.get('expression') != y.get('expression') if x and y else None,
                'comparable': allowed, 'reasons': why, 'metrics': metrics})
        return {'schema_version': 1, 'compared_at': now(), 'delta_direction': 'candidate_minus_baseline',
                'baseline': a, 'candidate': b, 'comparable': comparable,
                'conditions': conditions, 'candidates': candidates, 'limitations': LIMITATIONS}

    def _report_snapshot(self, connection, research_id, run_ids, identity, created):
        research = self.store._one(connection, 'researches', research_id)
        snapshot_at = now()  # The first SELECT above established this WAL view.
        paper = self.store._one(connection, 'papers', research['paper_id'])
        dataset = self.store._one(connection, 'datasets', research['dataset_id'])
        revisions = _bounded_rows(connection, 'SELECT * FROM revisions WHERE research_id=? ORDER BY number', (research_id,), MAX_REVISIONS)
        for row in revisions:
            row['task'] = loads(row['task'])
        reviews = _bounded_rows(connection, 'SELECT reviews.* FROM reviews JOIN runs ON reviews.run_id=runs.id WHERE runs.research_id=? ORDER BY reviews.created_at,reviews.id', (research_id,))
        reviews = [self.store._review_public(row) for row in reviews]
        issues = _bounded_rows(connection, 'SELECT issues.* FROM issues JOIN reviews ON issues.review_id=reviews.id JOIN runs ON reviews.run_id=runs.id WHERE runs.research_id=? ORDER BY issues.created_at,issues.id', (research_id,))
        events = _bounded_rows(connection, 'SELECT issue_events.* FROM issue_events JOIN issues ON issue_events.issue_id=issues.id JOIN reviews ON issues.review_id=reviews.id JOIN runs ON reviews.run_id=runs.id WHERE runs.research_id=? ORDER BY issue_events.rowid', (research_id,), MAX_EVENTS)
        issue_events = [_clean_record(row) for row in events]
        issues = [{**_clean_record(row), 'events': [item for item in issue_events if item['issue_id'] == row['id']]} for row in issues]
        checks = _bounded_rows(connection, '''SELECT regression_checks.*, runs.research_id AS owning_research_id FROM regression_checks JOIN runs ON regression_checks.run_id=runs.id
            WHERE runs.research_id=? OR regression_checks.id IN (
                SELECT issue_events.check_id FROM issue_events JOIN issues ON issue_events.issue_id=issues.id
                JOIN reviews ON issues.review_id=reviews.id JOIN runs ON reviews.run_id=runs.id WHERE runs.research_id=?)
            ORDER BY regression_checks.created_at,regression_checks.id''', (research_id, research_id))
        checks = [{**row, 'payload': loads(row['payload'])} for row in checks]
        case_ids = sorted({item['case_id'] for row in checks if isinstance(row['payload'], dict)
            for item in row['payload'].get('results', []) if isinstance(item, dict) and isinstance(item.get('case_id'), str)})
        if len(case_ids) > MAX_RELATED:
            raise ServiceError('Referenced regression cases exceed the report row limit', 413)
        placeholders = ','.join('?' for _ in case_ids) or 'NULL'
        cases = _bounded_rows(connection, f'SELECT * FROM regression_cases WHERE research_id=? OR id IN ({placeholders}) ORDER BY created_at,id', (research_id, *case_ids))
        for row in cases:
            row['contract'] = loads(row['contract'])
        owned_reviews = {row['id'] for row in reviews}
        external_review_ids = sorted({row['review_id'] for row in cases} - owned_reviews)
        if external_review_ids:
            placeholders = ','.join('?' for _ in external_review_ids)
            related_reviews = _bounded_rows(connection, f'SELECT * FROM reviews WHERE id IN ({placeholders}) ORDER BY created_at,id', tuple(external_review_ids))
            if len(related_reviews) + len(reviews) > MAX_RELATED:
                raise ServiceError('Referenced reviews exceed the report row limit', 413)
            reviews.extend(self.store._review_public(row) for row in related_reviews)
        runs = []
        candidate_count, candidate_attempt_count, run_attempt_count, run_event_count, unreviewed = 0, 0, 0, 0, 0
        for run_id in run_ids:
            source = self._source(connection, run_id)
            if source['run']['research_id'] != research_id:
                raise ServiceError('Every selected report run must belong to this research', 422)
            attempts = _bounded_rows(connection, 'SELECT * FROM attempts WHERE run_id=? ORDER BY number', (run_id,))
            for row in attempts:
                row['verification'] = loads(row['verification'])
            run_events = _bounded_rows(connection, 'SELECT * FROM events WHERE run_id=? ORDER BY id', (run_id,), MAX_EVENTS)
            for row in run_events:
                row['payload'] = loads(row['payload'])
            candidates = []
            for candidate in source['candidates'].values():
                original_digest = digest(candidate)
                item = deepcopy(candidate)
                metric_source = source['sources'].get(item['id'])
                result = item.get('result')
                daily_count = len(result.get('daily', [])) if isinstance(result, dict) else 0
                if result:
                    if metric_source:
                        result.pop('daily', None)
                    else:
                        item.pop('result', None)
                item.update(metric_source=metric_source, result_daily_count=daily_count,
                            result_projection='verified_summary_daily_referenced' if metric_source else 'unverified_metrics_withheld',
                            candidate_digest=original_digest)
                item['current_review_ids'] = [review['id'] for review in reviews
                    if review['run_id'] == run_id and review['attempt_id'] == source['run']['attempt_id']
                    and review['candidate_id'] == item['id'] and review['result_digest'] == original_digest]
                if not item['current_review_ids']:
                    unreviewed += 1
                candidate_attempt_count += len(item.get('attempts', []))
                candidates.append(item)
            candidate_count += len(candidates)
            run_attempt_count += len(attempts)
            run_event_count += len(run_events)
            runs.append({'summary': source['summary'], 'verification_recorded': loads(source['run']['verification']),
                'manifest_signature': source['signature'], 'candidates': candidates,
                'attempts': [_clean_record(row) for row in attempts], 'events': run_events,
                'artifacts': [_clean_record(row) for row in source['artifacts']],
                'preflight': source['state'].get('preflight') if source['summary']['source_verified'] else None})
        total_runs = connection.execute('SELECT COUNT(*) FROM runs WHERE research_id=?', (research_id,)).fetchone()[0]
        counts = {'revisions': len(revisions), 'selected_runs': len(runs), 'total_research_runs': total_runs,
            'unselected_runs': total_runs - len(runs), 'candidates': candidate_count,
            'candidate_attempts': candidate_attempt_count, 'run_attempts': run_attempt_count, 'run_events': run_event_count,
            'reviews': len(reviews), 'issues': len(issues), 'issue_events': len(issue_events),
            'regression_cases': len(cases), 'regression_checks': len(checks), 'unreviewed_current_candidates': unreviewed,
            'external_referenced_reviews': len(external_review_ids),
            'external_referenced_cases': sum(row['research_id'] != research_id for row in cases),
            'external_referenced_checks': sum(row['owning_research_id'] != research_id for row in checks),
            'source_verified_runs': sum(item['summary']['source_verified'] for item in runs)}
        payload = {
            'schema_version': 1, 'report_id': identity, 'research_id': research_id, 'created_at': created,
            'snapshot_at': snapshot_at, 'selected_run_ids': run_ids, 'counts': counts,
            'scope': {
                'database': 'One WAL read snapshot; later writes are intentionally absent. File verification is performed during capture.',
                'revisions': 'All research revisions, including evidence quotes and explicit claim attribution.',
                'runs': 'Only selected runs. All run-attempt metadata, errors, events and registered artifact references are retained; full candidate attempts and failures are projected from the current run attempt. Older attempt candidate details remain in their referenced state/result artifacts and are not reverified or silently merged with the current attempt.',
                'feedback': 'All reviews, issues, issue events, regression cases and checks owned by this research, including unselected runs; additionally freeze external checks explicitly linked by issue events, cases referenced by included checks, and their reviews. External run execution records are not selected or verified implicitly.',
                'daily_results': 'Daily arrays are not duplicated; their exact row counts and immutable result artifact references are recorded.',
                'integrity': 'The report digest validates this frozen payload, separately from each run source_verified status at capture.',
            },
            'research': research, 'paper': {key: paper[key] for key in ('id', 'title', 'sha256', 'created_at')},
            'dataset': {'id': dataset['id'], 'title': dataset['title'], 'sha256': dataset['sha256'], 'metadata': loads(dataset['metadata'])},
            'revisions': revisions, 'runs': runs,
            'feedback': {'reviews': reviews, 'issues': issues, 'regression_cases': cases, 'regression_checks': checks},
            'limitations': [*LIMITATIONS, 'No narrative is model-generated and no unrecorded human judgment or active research time is inferred.',
                            'Source checks establish stored artifact integrity, not independent numerical or economic correctness; consult saved regression checks and declared review sources.'],
        }
        # Redact while freezing, never at download: relocation must not change
        # this payload, its digest, or exported bytes.
        payload = self.store._public(payload)
        if len(json_text(payload).encode()) > MAX_REPORT_BYTES:
            raise ServiceError('Frozen research report exceeds the 24 MiB limit', 413)
        return payload

    @staticmethod
    def _decode_report(row):
        try:
            if len(row['payload'].encode()) > MAX_REPORT_BYTES:
                raise ValueError('payload exceeds bound')
            payload = loads(row['payload'])
            if digest(payload) != row['payload_digest']:
                raise ValueError('payload digest differs')
            if (payload.get('schema_version') != 1 or payload.get('report_id') != row['id']
                    or payload.get('research_id') != row['research_id'] or payload.get('created_at') != row['created_at']):
                raise ValueError('report identity differs')
            request = {'research_id': row['research_id'], 'run_ids': payload['selected_run_ids']}
            if digest(request) != row['request_digest']:
                raise ValueError('request binding differs')
            result = {'id': row['id'], 'research_id': row['research_id'], 'created_at': row['created_at'],
                'payload_digest': row['payload_digest'], 'selected_run_ids': payload['selected_run_ids'],
                'counts': payload['counts'], 'integrity': 'verified', 'payload': payload}
            return ResearchReport.model_validate(result).model_dump()
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError('Frozen report integrity verification failed', 409) from exc

    def create_report(self, research_id, run_ids, idempotency_key):
        try:
            request = ReportCreate(run_ids=run_ids, idempotency_key=idempotency_key)
        except ValidationError as exc:
            raise ServiceError('Report requires 1..20 distinct runs and a nonblank idempotency key', 422) from exc
        fingerprint = digest({'research_id': research_id, 'run_ids': request.run_ids})
        def replay(connection):
            row = connection.execute('SELECT * FROM research_reports WHERE idempotency_key=?', (request.idempotency_key,)).fetchone()
            if row is None:
                return None
            if row['request_digest'] != fingerprint:
                raise ServiceError('Report idempotency key was already used for another request', 409)
            return self._decode_report(dict(row))
        try:
            with closing(connect(self.store.db_path)) as connection:
                connection.execute('BEGIN')
                previous = replay(connection)
                if previous is not None:
                    return previous
                identity, created = uid(), now()
                payload = self._report_snapshot(connection, research_id, request.run_ids, identity, created)
        except ServiceError:
            # Another same-key request may have committed while this reader
            # checked files. Never describe its successful commit as a new
            # source/size rejection just because our older view lacked it.
            with closing(connect(self.store.db_path)) as connection:
                previous = replay(connection)
                if previous is not None:
                    return previous
            raise
        row = {'id': identity, 'research_id': research_id, 'created_at': created,
            'idempotency_key': request.idempotency_key, 'request_digest': fingerprint,
            'payload': json_text(payload), 'payload_digest': digest(payload)}
        with transaction(self.store.db_path) as connection:
            previous = replay(connection)
            if previous is not None:
                return previous
            connection.execute('INSERT INTO research_reports VALUES (:id,:research_id,:created_at,:idempotency_key,:request_digest,:payload,:payload_digest)', row)
        return self._decode_report(row)

    def get_report(self, report_id):
        return self._decode_report(self.store._fetch('research_reports', report_id))

    def list_reports(self, research_id, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ServiceError('Report list requires limit 1..100 and a nonnegative offset', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            self.store._one(connection, 'researches', research_id)
            total = connection.execute('SELECT COUNT(*) FROM research_reports WHERE research_id=?', (research_id,)).fetchone()[0]
            rows = _bounded_rows(connection, 'SELECT * FROM research_reports WHERE research_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (research_id, limit, offset), limit)
            items = [{key: value for key, value in self._decode_report(dict(row)).items() if key != 'payload'} for row in rows]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def export_report(self, report_id, format):
        if format not in {'json', 'markdown'}:
            raise ServiceError('Report export format must be json or markdown', 422)
        report = self.get_report(report_id)
        if format == 'json':
            return json_text(report)
        payload = report['payload']
        def line(value):
            # Prevent untrusted titles/notes from becoming active Markdown.
            return re.sub(r'([\\`*_\[\]<>#|])', r'\\\1', json.dumps(value, ensure_ascii=False))
        lines = [f"# Research report {report['id']}", '',
                 f"Research: {line(payload['research']['title'])}", '',
                 f"Frozen at: {payload['snapshot_at']}", '',
                 f"Payload SHA-256: `{report['payload_digest']}`", '',
                 'Report integrity: verified. Source integrity is recorded separately per run.', '',
                 '## Scope and counts', '',
                 *[f'- {key}: {value}' for key, value in payload['counts'].items()], '',
                 '## Selected experiments', '']
        for item in payload['runs']:
            summary = item['summary']
            run = summary['run']
            lines += [f"### {run['id']}", '', f"Status: {run['status']}; source verified: {summary['source_verified']}", '']
            for candidate in item['candidates']:
                lines += [f"Candidate: {line(candidate['id'])}; status: {line(candidate['status'])}",
                          f"Expression: {line(candidate.get('expression'))}",
                          f"Metrics: {line(candidate.get('result', {}).get('metrics'))}",
                          f"Result source: {line(candidate['metric_source'])}", '']
        lines += ['## Limitations', '', *['- ' + value for value in payload['limitations']], '',
                  '## Complete frozen snapshot', '',
                  'All evidence, attribution, versions, attempts, failures and feedback are preserved below. Daily arrays are referenced as declared in scope.', '']
        serialized = json_text(payload)
        fence = '`' * max(3, 1 + max((len(part) for part in re.findall(r'`+', serialized)), default=0))
        lines += [fence + 'json', serialized.rstrip(), fence, '']
        return '\n'.join(lines)
