"""Bound review series use actual worker artifacts and independent hand totals."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.evaluation import EXECUTION
from paper_alpha.evidence import sha256
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server import candidate_series
from paper_alpha.server.candidate_series import CandidateSeriesService, _project_points
from paper_alpha.server.db import transaction
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import atomic_json, digest, json_text, read_json
from paper_alpha.workflow import run_task


def hand_result():
    dates = [f'2022-01-{day:02}' for day in range(3, 11)]
    states = [('evaluated', None, 4, .03, 1., 'defined'),
              ('skipped', 'insufficient_finite_assets', 2, None, None, 'not_evaluated'),
              ('evaluated', None, 4, 0., None, 'constant_forward_returns'),
              ('evaluated', None, 3, -.02, -1., 'defined'),
              ('skipped', 'constant_factor', 4, None, None, 'not_evaluated'),
              ('evaluated', None, 4, .01, 0., 'defined'),
              ('purged', 'label_would_cross_split_boundary', 0, None, None, 'not_evaluated'),
              ('purged', 'label_would_cross_split_boundary', 0, None, None, 'not_evaluated')]
    rows = []
    for i, (status, reason, available, gross, ic, ic_state) in enumerate(states):
        rows.append({'signal_date': dates[i], 'entry_date': dates[i + 1] if i < 6 else None,
                     'exit_date': dates[i + 2] if i < 6 else None, 'status': status,
                     'reason': reason, 'available_assets': available,
                     'gross_return': gross, 'rank_ic': ic, 'rank_ic_state': ic_state})
    result = {'schema_version': 1, 'status': 'evaluated', 'reason': None, 'split': 'validation',
              'config': {'splits': {'validation': {'start': dates[0], 'end': dates[-1]}}, 'min_assets': 3},
              'execution': deepcopy(EXECUTION), 'daily': rows,
              'metrics': {'sum_gross_return': .02, 'mean_gross_return': .005, 'mean_rank_ic': 0.,
                          'evaluated_days': 4, 'rank_ic_days': 3, 'skipped_days': 2, 'purged_days': 2,
                          'eligible_days': 6, 'finite_factor_observations': 21,
                          'possible_factor_observations': 24, 'factor_coverage': .875}}
    return result, {'universe': ['A', 'B', 'C', 'D'], 'calendar_dates': dates}


class CandidateSeriesProjectionTests(unittest.TestCase):
    def test_hand_totals_keep_gaps_real_zero_and_cumulative_continuation(self):
        result, metadata = hand_result()
        points = _project_points(result, metadata)
        expected = [.03, None, .03, .01, None, .02, None, None]
        for point, value in zip(points, expected):
            if value is None:
                self.assertIsNone(point['cumulative_gross_return'])
            else:
                self.assertAlmostEqual(point['cumulative_gross_return'], value)
        self.assertEqual(points[2]['gross_return'], 0)
        self.assertIsNone(points[2]['rank_ic'])
        self.assertEqual(points[2]['rank_ic_state'], 'constant_forward_returns')
        self.assertEqual(points[5]['rank_ic'], 0)
        self.assertEqual(points[1]['available_assets'], 2)
        self.assertEqual(points[1]['coverage'], .5)
        self.assertEqual(points[3]['coverage'], .75)
        self.assertIsNone(points[-1]['available_assets'])
        self.assertIsNone(points[-1]['coverage'])
        self.assertEqual(points[0]['exit_date'], '2022-01-05')
        self.assertEqual(result['daily'][-1]['available_assets'], 0, 'Projection must not rewrite original placeholders')

    def test_all_invalid_result_keeps_reasons_and_null_cumulative(self):
        result, metadata = hand_result()
        for row in result['daily'][:6]:
            row.update(status='skipped', reason='constant_factor', available_assets=4,
                       gross_return=None, rank_ic=None, rank_ic_state='not_evaluated')
        result.update(status='not_evaluable', reason='No nonconstant cross section')
        result['metrics'].update(sum_gross_return=None, mean_gross_return=None, mean_rank_ic=None,
                                 evaluated_days=0, rank_ic_days=0, skipped_days=6,
                                 finite_factor_observations=24, factor_coverage=1.)
        points = _project_points(result, metadata)
        self.assertTrue(all(point['cumulative_gross_return'] is None for point in points))
        self.assertEqual(points[0]['reason'], 'constant_factor')
        self.assertEqual(points[0]['coverage'], 1.)

    def test_inconsistent_saved_summary_and_unknown_semantics_are_rejected(self):
        for change in (lambda r: r['metrics'].update(sum_gross_return=.8),
                       lambda r: r['metrics'].update(evaluated_days=100),
                       lambda r: r['metrics'].update(factor_coverage=.1),
                       lambda r: r['execution'].update(exit='session t+3 open'),
                       lambda r: r['daily'][1].update(gross_return=0.),
                       lambda r: r['daily'][0].update(exit_date='2022-01-06'),
                       lambda r: r['daily'].reverse()):
            result, metadata = hand_result()
            change(result)
            with self.assertRaises(ServiceError) as raised:
                _project_points(result, metadata)
            self.assertEqual(raised.exception.status, 409)

    def test_point_limit_rejects_instead_of_truncating(self):
        result, metadata = hand_result()
        with patch.object(candidate_series, 'MAX_POINTS', 7):
            with self.assertRaises(ServiceError) as raised:
                _project_points(result, metadata)
        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(len(result['daily']), 8)


class CandidateSeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.base = Path(cls.shared.name).resolve()
        store = Store(cls.base / 'source')
        cls.example = store.seed_example()
        cls.run_id = cls.execute(store, cls.example['revision_id'], 'series-baseline')
        cls.backup = cls.base / 'baseline-backup'
        create_backup(store.root, cls.backup)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.base)
        self.root = Path(self.temp.name) / 'workspace'
        restore_backup(self.backup, self.root)
        self.store = Store(self.root)
        self.service = CandidateSeriesService(self.store)
        self.run = self.store.get_run(self.run_id)
        self.target = next(item for item in self.run['review_targets'] if item['candidate_id'] == 'alpha101')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def execute(store, revision_id, key):
        run = store.submit_run(revision_id, 'normalized_fixed', key)
        claim = store.claim('series-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        store.finish(run['id'], 'series-worker', claim['attempt_id'], state['status'])
        return run['id']

    def get(self, **changes):
        return self.service.get(**{'run_id': self.run_id, **self.target, **changes})

    def expect_error(self, status, **changes):
        with self.assertRaises(ServiceError) as raised:
            self.get(**changes)
        self.assertEqual(raised.exception.status, status)

    def test_real_engine_projection_is_bound_to_download_and_is_read_only(self):
        artifacts = self.store.list_artifacts(self.run_id)
        checksums = {item['id']: sha256(self.store.artifact_path(self.run_id, item['id'])[0]) for item in artifacts}
        event_count = len(self.store.events(self.run_id))
        result = self.get()
        path, name = self.store.artifact_path(self.run_id, result['source']['artifact_id'])
        original = read_json(path)
        self.assertEqual(result['source']['artifact_name'], name)
        self.assertEqual(result['source']['artifact_sha256'], sha256(path))
        self.assertEqual(result['source']['result_sha256'], digest(original))
        self.assertEqual(result['result_digest'], self.target['result_digest'])
        self.assertEqual(result['revision_id'], self.example['revision_id'])
        self.assertEqual(result['data']['version'], original['market_metadata']['version'])
        self.assertEqual(result['summary']['sum_gross_return'], original['metrics']['sum_gross_return'])
        self.assertEqual(len(result['points']), len(original['daily']))
        for point, saved in zip(result['points'], original['daily']):
            self.assertEqual(point['gross_return'], saved['gross_return'])
            self.assertEqual(point['rank_ic'], saved['rank_ic'])
            self.assertNotIn('assets', point)
        self.assertLess(len(json_text(result)), len(json_text(original)))
        self.assertEqual(checksums, {item['id']: sha256(self.store.artifact_path(self.run_id, item['id'])[0]) for item in artifacts})
        self.assertEqual(event_count, len(self.store.events(self.run_id)))

    def test_wrong_target_and_unknown_identity_never_fall_back(self):
        self.expect_error(409, attempt_id='another-attempt')
        self.expect_error(409, result_digest='f' * 64)
        self.expect_error(404, candidate_id='unknown_candidate')
        self.expect_error(404, run_id='unknown_run')
        self.expect_error(422, result_digest='')
        self.expect_error(422, attempt_id='../../output')
        # A digest of the inner result is not the review target identity.
        candidate = next(item for item in read_json(self.output() / 'state.json')['candidates'] if item['id'] == 'alpha101')
        self.expect_error(409, result_digest=digest(candidate['result']))

    def output(self):
        return self.root / 'runs' / self.run_id / 'attempts' / self.target['attempt_id'] / 'output'

    def test_unverified_queued_and_no_output_are_explicit(self):
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE runs SET verification=? WHERE id=?', (json_text({'verified': False}), self.run_id))
        self.expect_error(409)
        with transaction(self.store.db_path) as connection:
            connection.execute("UPDATE runs SET status='queued' WHERE id=?", (self.run_id,))
        self.expect_error(409)

    def test_original_or_export_tamper_is_refused(self):
        result = self.get()
        original = self.output() / result['source']['artifact_name']
        before = original.read_bytes()
        original.write_text('{}\n')
        self.expect_error(409)
        original.write_bytes(before)
        exported, _ = self.store.artifact_path(self.run_id, result['source']['artifact_id'])
        exported.write_text('{}\n')
        self.expect_error(409)

    def test_new_research_revision_does_not_change_historical_series(self):
        before = self.get()
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][-1]['task'])
        task['candidates'][0]['origin'] = 'user_modification'
        task['candidates'][0]['changes'].append('Automated later revision fixture')
        self.store.create_revision(research['id'], research['latest_revision_id'], task, 'Automated later revision')
        self.assertEqual(before, self.get())

    def test_real_non_evaluable_result_is_still_available_for_review(self):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][-1]['task'])
        candidate = next(item for item in task['candidates'] if item['id'] == 'alpha101')
        candidate.update(expression='close - close', origin='user_modification',
                         changes=['Automated constant-signal fixture, not a paper-original claim'])
        task['candidates'] = [candidate]
        revision = self.store.create_revision(research['id'], research['latest_revision_id'], task, 'Automated constant signal')
        identity = self.execute(self.store, revision['id'], 'constant-signal')
        run = self.store.get_run(identity)
        target = run['review_targets'][0]
        series = self.service.get(identity, **target)
        self.assertEqual(series['status'], 'not_evaluable')
        self.assertTrue(series['reason'])
        self.assertEqual(series['summary']['evaluated_days'], 0)
        self.assertTrue(all(item['gross_return'] is None for item in series['points']))
        self.assertEqual(series['points'][0]['coverage'], 1.)

    def test_unrecognized_historical_semantics_do_not_use_live_default(self):
        output = self.output()
        source = output / 'source/paper_alpha/evaluation.py'
        source.write_text(source.read_text().replace('session t+2 open', 'session t+3 open'))
        manifest = read_json(output / 'manifest.json')
        manifest['signature']['code']['paper_alpha/evaluation.py'] = sha256(source)
        manifest['snapshot_files']['source/paper_alpha/evaluation.py'] = sha256(source)
        manifest['signature_sha256'] = digest(manifest['signature'])
        atomic_json(output / 'manifest.json', manifest)
        artifact = next(item for item in self.store._read('SELECT * FROM artifacts WHERE attempt_id=?',
                        (self.target['attempt_id'],)) if item['name'] == 'manifest.json')
        atomic_json(artifact['path'], self.store._public(manifest))
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE artifacts SET sha256=?,size=? WHERE id=?',
                (sha256(artifact['path']), Path(artifact['path']).stat().st_size, artifact['id']))
        self.assertTrue(self.store.get_run(self.run_id)['verification']['verified'])
        self.expect_error(409)

    def test_resultless_candidate_is_not_replaced_by_another_candidate(self):
        # The seed intentionally includes a candidate blocked by unavailable data.
        candidate = next(item for item in self.run['state']['candidates'] if not item.get('result'))
        target = next(item for item in self.run['review_targets'] if item['candidate_id'] == candidate['id'])
        self.expect_error(409, **target)

    def test_resource_bounds_reject_files_rows_and_response(self):
        with patch.object(candidate_series, 'MAX_RECORD_BYTES', 1):
            self.expect_error(413)
        with patch.object(candidate_series, 'MAX_RESPONSE_BYTES', 1):
            self.expect_error(413)
        unsafe = self.output() / 'unexpected-link'
        unsafe.symlink_to(self.output() / 'state.json')
        self.expect_error(409)

    def test_series_verification_does_not_hold_sqlite_writer_lock(self):
        verify = self.store._check_integrity
        def checking(connection, run):
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
            # A separate writer must commit while this snapshot is being read.
            with transaction(self.store.db_path) as writer:
                writer.execute("INSERT INTO settings(key,value) VALUES ('series-read-writer-test','ok')")
            return verify(connection, run)
        with patch.object(self.store, '_check_integrity', side_effect=checking):
            self.get()
        self.assertEqual(self.store._read("SELECT value FROM settings WHERE key='series-read-writer-test'")[0]['value'], 'ok')
