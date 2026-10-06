"""Read-only chart data bound to the exact, verified review target.

No evaluator or historical Python is executed. Saved daily values are copied;
the only new numeric projection is the explicitly labelled arithmetic running
sum and coverage ratio. SQLite uses a read snapshot, never a writer lock.
"""
from contextlib import closing
import math
from pathlib import Path
import re

from pydantic import ValidationError

from .candidate_series_schema import CandidateSeriesResponse, CandidateSeriesPoint, MAX_POINTS
from .db import connect
from .observation_context import (_bounded_tree, _safe_file, _safe_json,
                                  _safe_snapshot_references, MAX_TREE_FILES)
from .regression import recorded_semantics
from .service import ServiceError, loads
from ..evaluation import EXECUTION
from ..evidence import sha256
from ..storage import digest, json_text


MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SUMMARY_FIELDS = ('sum_gross_return', 'mean_gross_return', 'mean_rank_ic',
                  'evaluated_days', 'rank_ic_days', 'skipped_days', 'purged_days', 'factor_coverage')
LIMITATIONS = [
    '收益按 exit_date 表示完成持有期；Rank IC 和覆盖按 signal_date 展示，对应其后的 entry_date 至 exit_date。',
    'gross_return 和 cumulative_gross_return 均为比例（1 = 100%）；累计为有效日毛收益的算术和，不是复利净值或 WorldQuant BRAIN PnL。',
    '跳过及边界剔除日保留空值，不能补零或跨空缺连线；后续有效日的累计接续此前有效日。',
    'coverage 为有限信号资产数除以冻结股票池数量；边界剔除日未评估，available_assets 和 coverage 均为 null。',
    '收益未扣手续费、滑点及借券成本，不提供资金净值、Sharpe、回撤或自动投资评级。',
    '合成数据结果仅用于软件验证，不能作为真实投资表现或 Alpha 盈利证据。',
    '最多返回 4000 个信号日且响应不超过 2 MiB；超限拒绝，不截断。完整结果仍保留于冻结产物。',
]


def _require(condition, message):
    if not condition:
        raise ServiceError(message, 409)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _equal_number(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    return _finite(actual) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12)


def _project_points(result, metadata):
    """Validate saved row meanings and derive a bounded lightweight projection."""
    _require(result.get('schema_version') == 1 and result.get('split') == 'validation',
             'Candidate series supports only recorded schema-one validation results')
    _require(result.get('execution') == EXECUTION,
             'Recorded execution semantics are not supported by this series projection')
    daily = result['daily']
    _require(isinstance(daily, list), 'Saved daily results are invalid')
    if len(daily) > MAX_POINTS:
        raise ServiceError(f'Candidate series exceeds the {MAX_POINTS}-point limit; download the complete saved result', 413)
    bounds = result['config']['splits']['validation']
    calendar = metadata['calendar_dates']
    dates = calendar[calendar.index(bounds['start']):calendar.index(bounds['end']) + 1]
    _require(len(dates) >= 3 and len(daily) == len(dates), 'Saved daily dates differ from the frozen validation interval')
    universe = metadata['universe']
    _require(isinstance(universe, list) and len(universe) >= 3 and len(set(universe)) == len(universe),
             'Frozen dataset universe is invalid')
    universe_size = len(universe)
    minimum = result['config']['min_assets']
    _require(type(minimum) is int and 3 <= minimum <= universe_size, 'Recorded min_assets is invalid')
    points, gross_values, ics = [], [], []
    skipped, purged, available_total = 0, 0, 0
    for position, row in enumerate(daily):
        _require(isinstance(row, dict) and row.get('signal_date') == dates[position],
                 'Saved daily rows are missing, duplicated or out of order')
        status = row['status']
        tail = position + 2 >= len(dates)
        expected_entry = None if tail else dates[position + 1]
        expected_exit = None if tail else dates[position + 2]
        _require(row['entry_date'] == expected_entry and row['exit_date'] == expected_exit,
                 'Saved entry/exit dates differ from the recorded next-open holding period')
        _require((status == 'purged') == tail, 'Saved boundary-purge states differ from the evaluation interval')
        point = {key: row[key] for key in ('signal_date', 'entry_date', 'exit_date', 'status', 'reason',
                                          'gross_return', 'rank_ic', 'rank_ic_state', 'available_assets')}
        point.update(cumulative_gross_return=None, coverage=None)
        if status == 'purged':
            _require(row['reason'] == 'label_would_cross_split_boundary' and row['available_assets'] == 0,
                     'Saved boundary-purge reason or placeholder is invalid')
            point['available_assets'] = None
            purged += 1
        else:
            available = row['available_assets']
            _require(type(available) is int and 0 <= available <= universe_size,
                     'Saved available-asset count is invalid')
            point['coverage'] = available / universe_size
            available_total += available
        if status == 'evaluated':
            _require(row['reason'] is None and row['available_assets'] >= minimum and _finite(row['gross_return']),
                     'Evaluated daily return or coverage is invalid')
            gross_values.append(row['gross_return'])
            point['cumulative_gross_return'] = math.fsum(gross_values)
            if row['rank_ic_state'] == 'defined':
                _require(_finite(row['rank_ic']), 'Defined Rank IC is missing or nonfinite')
                ics.append(row['rank_ic'])
            else:
                _require(row['rank_ic_state'] == 'constant_forward_returns' and row['rank_ic'] is None,
                         'Undefined Rank IC must retain its recorded cause and null value')
        else:
            _require(row['gross_return'] is None and row['rank_ic'] is None and row['rank_ic_state'] == 'not_evaluated',
                     'Unevaluated daily rows must retain null returns and IC')
            if status == 'skipped':
                _require((row['reason'] == 'insufficient_finite_assets' and row['available_assets'] < minimum)
                         or (row['reason'] == 'constant_factor' and row['available_assets'] >= minimum),
                         'Skipped daily state has an unsupported or inconsistent reason')
                skipped += 1
            else:
                _require(status == 'purged', 'Saved daily status is unsupported')
        points.append(CandidateSeriesPoint.model_validate(point).model_dump())
    metrics = result['metrics']
    counts = {'evaluated_days': len(gross_values), 'rank_ic_days': len(ics), 'skipped_days': skipped,
              'purged_days': purged, 'eligible_days': len(dates) - purged,
              'finite_factor_observations': available_total,
              'possible_factor_observations': (len(dates) - purged) * universe_size}
    for key, value in counts.items():
        _require(type(metrics.get(key)) is int and metrics[key] == value,
                 f'Saved {key} differs from daily result rows')
    expected = {'sum_gross_return': math.fsum(gross_values) if gross_values else None,
                'mean_gross_return': math.fsum(gross_values) / len(gross_values) if gross_values else None,
                'mean_rank_ic': math.fsum(ics) / len(ics) if ics else None,
                'factor_coverage': available_total / counts['possible_factor_observations']}
    for key, value in expected.items():
        _require(key in metrics and _equal_number(metrics[key], value), f'Saved {key} differs from daily result rows')
    _require(result['status'] == ('evaluated' if gross_values else 'not_evaluable'),
             'Saved result status differs from daily evaluation states')
    return points


class CandidateSeriesService:
    def __init__(self, store):
        self.store = store

    def get(self, run_id, candidate_id, attempt_id, result_digest):
        for value in (run_id, candidate_id, attempt_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 128 or '/' in value or '\\' in value:
                raise ServiceError('Provide an explicit run, candidate and attempt identity', 422)
        if not isinstance(result_digest, str) or not re.fullmatch('[0-9a-f]{64}', result_digest):
            raise ServiceError('Provide the selected review target result_digest', 422)
        try:
            with closing(connect(self.store.db_path)) as connection:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                size = connection.execute('SELECT length(CAST(state AS BLOB)) FROM runs WHERE id=?', (run_id,)).fetchone()
                if size is not None and size[0] is not None and size[0] > MAX_RECORD_BYTES:
                    raise ServiceError('Run state exceeds the candidate-series byte limit', 413)
                run = self.store._one(connection, 'runs', run_id)
                _require(run['attempt_id'] == attempt_id, 'Selected attempt is stale; reload the experiment before viewing its series')
                _require(run['status'] in {'completed', 'failed'} and (loads(run['verification']) or {}).get('verified'),
                         'A terminal, verified experiment is required for candidate series')
                attempt = self.store._one(connection, 'attempts', attempt_id)
                _require(attempt['run_id'] == run_id, 'Selected attempt does not belong to this run')
                base = self.store.root / 'runs' / run_id / 'attempts' / attempt_id
                output = base / 'output'
                _bounded_tree(base)
                state = _safe_json(output / 'state.json', output)
                manifest = _safe_json(output / 'manifest.json', output)
                _safe_snapshot_references(output, manifest, state)
                count = connection.execute('SELECT COUNT(*) FROM artifacts WHERE attempt_id=?', (attempt_id,)).fetchone()[0]
                if count > MAX_TREE_FILES:
                    raise ServiceError('Candidate-series artifact inventory exceeds its file limit', 413)
                artifacts = [dict(row) for row in connection.execute('SELECT * FROM artifacts WHERE attempt_id=?', (attempt_id,))]
                for artifact in artifacts:
                    size = _safe_file(artifact['path'], base / 'exports')
                    _require(artifact['run_id'] == run_id and size == artifact['size'], 'Registered result export binding or size changed')
                self.store._check_integrity(connection, run)
                candidates = [item for item in state['candidates'] if item['id'] == candidate_id]
                if not candidates:
                    raise ServiceError('Candidate is absent from this frozen run', 404)
                _require(len(candidates) == 1, 'Frozen run contains duplicate candidate identities')
                candidate = candidates[0]
                _require(digest(candidate) == result_digest, 'Selected result digest is stale; reload the experiment before viewing its series')
                result = candidate.get('result')
                _require(isinstance(result, dict) and candidate['status'] in {'evaluated', 'not_evaluable'},
                         'Selected candidate has no computed daily result; inspect its recorded failure or blocked reason')
                matches = [(name, checksum) for name, checksum in candidate['artifacts'].items() if name.endswith('/result.json')]
                _require(len(matches) == 1, 'Candidate must have one bound result artifact')
                name, checksum = matches[0]
                original = _safe_json(output / name, output)
                _require(original == result and sha256(output / name) == checksum, 'Candidate result differs from its original saved artifact')
                exports = [item for item in artifacts if item['name'] == name]
                _require(len(exports) == 1, 'Candidate result export is missing or ambiguous')
                artifact = exports[0]
                exported = _safe_json(artifact['path'], base / 'exports')
                _require(exported == self.store._public(original) and sha256(artifact['path']) == artifact['sha256'],
                         'Candidate result export differs from its verified original')
                semantics = recorded_semantics(output)
                semantic_hash = manifest['signature']['code']['paper_alpha/evaluation.py']
                _require(manifest['snapshot_files'].get('source/paper_alpha/evaluation.py') == semantic_hash,
                         'Recorded execution semantics are not bound to the source snapshot')
                _require(semantics['execution_semantics'] == original.get('execution'),
                         'Result execution semantics differ from the verified historical source')
                revision = self.store._one(connection, 'revisions', run['revision_id'])
                research = self.store._one(connection, 'researches', run['research_id'])
                dataset = self.store._one(connection, 'datasets', research['dataset_id'])
                metadata = _safe_json(output / 'inputs/metadata.json', output)
                _require(original['market_metadata'] == metadata and original['config'] == state['task']['evaluation'],
                         'Result metadata or evaluation differs from its frozen inputs')
                points = _project_points(original, metadata)
                bounds = original['config']['splits'][original['split']]
                payload = {'schema_version': 1, 'run_id': run_id, 'research_id': run['research_id'],
                           'revision_id': run['revision_id'], 'revision_digest': revision['digest'],
                           'attempt_id': attempt_id, 'candidate_id': candidate_id, 'result_digest': result_digest,
                           'integrity': 'verified', 'status': original['status'], 'reason': original['reason'],
                           'source': {'artifact_id': artifact['id'], 'artifact_name': name,
                                      'artifact_sha256': artifact['sha256'], 'original_sha256': checksum,
                                      'result_sha256': digest(original), 'json_pointer': '/daily'},
                           'data': {'dataset_id': dataset['id'], 'dataset_sha256': dataset['sha256'],
                                    'version': metadata['version'], 'data_kind': metadata['data_kind'],
                                    'universe_size': len(metadata['universe'])},
                           'evaluation': {'split': original['split'], **bounds, 'min_assets': original['config']['min_assets']},
                           'summary': {key: original['metrics'][key] for key in SUMMARY_FIELDS},
                           'points': points, 'limitations': list(LIMITATIONS) + original.get('limitations', [])}
                response = CandidateSeriesResponse.model_validate(self.store._public(payload)).model_dump()
                if len(json_text(response).encode()) > MAX_RESPONSE_BYTES:
                    raise ServiceError('Candidate-series response exceeds 2 MiB; download the complete saved result', 413)
                return response
        except ServiceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, ValidationError) as exc:
            raise ServiceError('Saved candidate series cannot be verified or its recorded schema is unsupported', 409) from exc
