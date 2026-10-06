"""Saved-result comparison and immutable reports use real engine/SQLite fixtures."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.evidence import sha256
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import transaction
from paper_alpha.server.feedback import Feedback
from paper_alpha.server.research_insights import ResearchInsights
from paper_alpha.server import research_insights
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import atomic_json, digest, json_text, read_json
from paper_alpha.workflow import run_task


class ResearchInsightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.base = Path(cls.shared.name).resolve()
        store = Store(cls.base / 'source')
        cls.example = store.seed_example()
        cls.baseline_id = cls.execute(store, cls.example['revision_id'], 'baseline')
        cls.backup = cls.base / 'baseline-backup'
        create_backup(store.root, cls.backup)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.base)
        self.home = Path(self.temp.name) / 'workspace'
        restore_backup(self.backup, self.home)
        self.store = Store(self.home)
        self.insights = ResearchInsights(self.store)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def execute(store, revision_id, key):
        run = store.submit_run(revision_id, 'normalized_fixed', key)
        claim = store.claim('insights-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        store.finish(run['id'], 'insights-worker', claim['attempt_id'], state['status'])
        return run['id']

    def revised_run(self, change):
        research = self.store.get_research(self.example['research_id'])
        task = deepcopy(research['revisions'][-1]['task'])
        change(task)
        revision = self.store.create_revision(research['id'], research['latest_revision_id'], task, 'Automated insights fixture')
        return self.execute(self.store, revision['id'], 'candidate-' + revision['id'])

    def report(self, key='report', runs=None):
        return self.insights.create_report(self.example['research_id'], runs or [self.baseline_id], key)

    @staticmethod
    def alpha006(comparison):
        return next(item for item in comparison['candidates'] if item['candidate_id'] == 'alpha006')

    def historical_source(self, run_id, replacement):
        """Construct a verified older-source fixture, without executing its code."""
        run = self.store._fetch('runs', run_id)
        attempt = self.store._fetch('attempts', run['attempt_id'])
        output = Path(attempt['output_dir'])
        source = output / 'source/paper_alpha/expressions.py'
        content = source.read_text()
        import re
        content, replaced = re.subn(r'^OPERATOR_SEMANTICS_VERSION\s*=.*$',
                                    'OPERATOR_SEMANTICS_VERSION = ' + replacement, content, count=1, flags=re.M)
        self.assertEqual(replaced, 1)
        self.update_source_snapshot(run_id, 'paper_alpha/expressions.py', content)

    def update_source_snapshot(self, run_id, relative, content):
        run = self.store._fetch('runs', run_id)
        output = Path(self.store._fetch('attempts', run['attempt_id'])['output_dir'])
        source = output / 'source' / relative
        source.write_text(content)
        manifest = read_json(output / 'manifest.json')
        manifest['signature']['code'][relative] = sha256(source)
        manifest['snapshot_files']['source/' + relative] = sha256(source)
        self.write_manifest(run_id, manifest)

    def write_manifest(self, run_id, manifest):
        run = self.store._fetch('runs', run_id)
        output = Path(self.store._fetch('attempts', run['attempt_id'])['output_dir'])
        manifest['signature_sha256'] = digest(manifest['signature'])
        atomic_json(output / 'manifest.json', manifest)
        artifact = next(item for item in self.store._read('SELECT * FROM artifacts WHERE attempt_id=?', (run['attempt_id'],)) if item['name'] == 'manifest.json')
        atomic_json(artifact['path'], self.store._public(manifest))
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE artifacts SET sha256=?,size=? WHERE id=?',
                               (sha256(artifact['path']), Path(artifact['path']).stat().st_size, artifact['id']))

    def test_actual_expression_change_has_sourced_candidate_minus_baseline_delta(self):
        def change(task):
            candidate = next(item for item in task['candidates'] if item['id'] == 'alpha006')
            candidate.update(expression='ts_corr(open, volume, 10)', origin='user_modification',
                             changes=['Automated sign inversion fixture, not a paper-original claim.'])
        candidate_id = self.revised_run(change)
        comparison = self.insights.compare(self.baseline_id, candidate_id)
        self.assertTrue(comparison['comparable'])
        candidate = self.alpha006(comparison)
        self.assertTrue(candidate['comparable'])
        self.assertTrue(candidate['expression_changed'])
        metric = next(item for item in candidate['metrics'] if item['name'] == 'mean_rank_ic')
        for side in ('baseline', 'candidate'):
            source = metric[side + '_source']
            path, name = self.store.artifact_path(source['run_id'], source['artifact_id'])
            self.assertEqual(read_json(path)['metrics']['mean_rank_ic'], metric[side])
            self.assertEqual(sha256(path), source['artifact_sha256'])
            self.assertEqual(name, source['artifact_name'])
            self.assertEqual(source['json_pointer'], '/metrics/mean_rank_ic')
        self.assertNotEqual(metric['delta'], 0)
        self.assertEqual(metric['delta'], metric['candidate'] - metric['baseline'])
        self.assertEqual(comparison['delta_direction'], 'candidate_minus_baseline')

    def test_evaluation_changes_retain_sourced_values_but_suppress_all_deltas(self):
        candidate_id = self.revised_run(lambda task: task['evaluation'].update(min_assets=6))
        comparison = self.insights.compare(self.baseline_id, candidate_id)
        self.assertFalse(comparison['comparable'])
        condition = next(item for item in comparison['conditions'] if item['name'] == 'evaluation')
        self.assertEqual(condition['status'], 'changed')
        candidate = self.alpha006(comparison)
        self.assertFalse(candidate['comparable'])
        self.assertTrue(all(item['delta'] is None for item in candidate['metrics']))
        self.assertTrue(any(item['baseline'] is not None and item['candidate'] is not None for item in candidate['metrics']))

    def test_missing_and_added_candidates_are_visible_without_zero_filling(self):
        def change(task):
            task['candidates'] = [item for item in task['candidates'] if item['id'] != 'alpha006']
            item = deepcopy(next(item for item in task['candidates'] if item['id'] == 'alpha101'))
            item['id'] = 'new_candidate'
            task['candidates'].append(item)
        candidate_id = self.revised_run(change)
        comparison = self.insights.compare(self.baseline_id, candidate_id)
        absent = self.alpha006(comparison)
        added = next(item for item in comparison['candidates'] if item['candidate_id'] == 'new_candidate')
        self.assertEqual(absent['presence'], 'baseline_only')
        self.assertEqual(added['presence'], 'candidate_only')
        self.assertTrue(all(item['candidate'] is None and item['delta'] is None for item in absent['metrics']))
        self.assertTrue(all(item['baseline'] is None and item['delta'] is None for item in added['metrics']))

    def test_corrupt_result_blocks_comparison_and_report_withholds_metrics(self):
        run = self.store.get_run(self.baseline_id)
        artifact = next(item for item in self.store.list_artifacts(self.baseline_id) if item['name'].endswith('/result.json'))
        path, _ = self.store.artifact_path(self.baseline_id, artifact['id'])
        path.write_text('{}\n')
        comparison = self.insights.compare(self.baseline_id, self.baseline_id)
        self.assertFalse(comparison['comparable'])
        self.assertFalse(comparison['baseline']['source_verified'])
        self.assertTrue(all(item['metrics'] == [] for item in comparison['candidates']))
        report = self.report()
        self.assertEqual(report['counts']['source_verified_runs'], 0)
        self.assertTrue(all('result' not in item for item in report['payload']['runs'][0]['candidates']))
        self.assertEqual(report['integrity'], 'verified')
        self.assertEqual(report['payload']['runs'][0]['summary']['attempt_id'], run['review_targets'][0]['attempt_id'])

    def test_missing_artifact_is_explicitly_unverified(self):
        artifact = next(item for item in self.store.list_artifacts(self.baseline_id) if item['name'] == 'report.md')
        path, _ = self.store.artifact_path(self.baseline_id, artifact['id'])
        path.unlink()
        comparison = self.insights.compare(self.baseline_id, self.baseline_id)
        self.assertFalse(comparison['baseline']['source_verified'])
        self.assertIsNotNone(comparison['baseline']['verification_error'])
        self.assertTrue(all(not item['comparable'] for item in comparison['candidates']))

    def test_queued_and_failed_attempts_are_preserved(self):
        queued = self.store.submit_run(self.example['revision_id'], 'fixed', 'queued')
        report = self.report(runs=[self.baseline_id, queued['id']])
        self.assertEqual(report['payload']['runs'][1]['summary']['run']['status'], 'queued')
        self.assertFalse(report['payload']['runs'][1]['summary']['source_verified'])
        self.assertTrue(all(item['status'] == 'not_executed' for item in report['payload']['runs'][1]['candidates']))
        claim = self.store.claim('failure-worker')
        self.store.finish(queued['id'], 'failure-worker', claim['attempt_id'], 'failed', error='Deliberate tool startup failure')
        failed = self.report('failed-report', [queued['id']])
        item = failed['payload']['runs'][0]
        self.assertEqual(item['attempts'][0]['status'], 'failed')
        self.assertIn('Deliberate', item['attempts'][0]['error'])
        self.assertEqual(item['events'][-1]['kind'], 'failed')
        self.assertEqual(self.insights.get_report(report['id']), report)

    def test_historical_semantics_are_read_from_source_not_live_constants(self):
        self.historical_source(self.baseline_id, "'historical-operator-v0'")
        comparison = self.insights.compare(self.baseline_id, self.baseline_id)
        self.assertTrue(comparison['baseline']['source_verified'])
        self.assertEqual(comparison['baseline']['recorded_semantics']['operator_semantics'], 'historical-operator-v0')
        self.assertTrue(comparison['comparable'])

    def test_uninterpretable_historical_semantics_stay_unknown(self):
        self.historical_source(self.baseline_id, "''.join(['historical', '-v0'])")
        comparison = self.insights.compare(self.baseline_id, self.baseline_id)
        self.assertTrue(comparison['baseline']['source_verified'])
        self.assertIsNone(comparison['baseline']['recorded_semantics'])
        self.assertFalse(comparison['comparable'])
        self.assertTrue(all(metric['delta'] is None for row in comparison['candidates'] for metric in row['metrics']))

    def test_worker_time_alignment_changes_block_deltas_with_unchanged_constants(self):
        candidate = self.execute(self.store, self.example['revision_id'], 'same-revision')
        run = self.store._fetch('runs', self.baseline_id)
        output = Path(self.store._fetch('attempts', run['attempt_id'])['output_dir'])
        content = (output / 'source/paper_alpha/worker.py').read_text()
        modified = content.replace('panel.loc[:cutoff].copy()', 'panel.loc[:cutoff].shift(1).copy()')
        self.assertNotEqual(content, modified)
        self.update_source_snapshot(self.baseline_id, 'paper_alpha/worker.py', modified)
        comparison = self.insights.compare(self.baseline_id, candidate)
        self.assertTrue(comparison['baseline']['source_verified'])
        self.assertEqual(comparison['baseline']['recorded_semantics'], comparison['candidate']['recorded_semantics'])
        self.assertFalse(comparison['comparable'])
        condition = next(item for item in comparison['conditions'] if item['name'] == 'numerical_code')
        self.assertTrue(condition['blocking'])
        self.assertEqual(condition['status'], 'changed')
        self.assertTrue(all(metric['delta'] is None for row in comparison['candidates'] for metric in row['metrics']))

    def test_incomplete_environment_and_unbound_source_declarations_are_unknown(self):
        run = self.store._fetch('runs', self.baseline_id)
        output = Path(self.store._fetch('attempts', run['attempt_id'])['output_dir'])
        original = read_json(output / 'manifest.json')
        for kind in ('empty_environment', 'partial_environment', 'unbound_source'):
            with self.subTest(kind=kind):
                manifest = deepcopy(original)
                if kind == 'empty_environment':
                    manifest['signature']['environment'] = {}
                elif kind == 'partial_environment':
                    del manifest['signature']['environment']['packages']['numpy']
                else:
                    del manifest['snapshot_files']['source/paper_alpha/expressions.py']
                self.write_manifest(self.baseline_id, manifest)
                comparison = self.insights.compare(self.baseline_id, self.baseline_id)
                self.assertTrue(comparison['baseline']['source_verified'])
                self.assertFalse(comparison['comparable'])
                condition_name = 'numerical_code' if kind == 'unbound_source' else 'environment'
                self.assertEqual(next(item for item in comparison['conditions'] if item['name'] == condition_name)['status'], 'unknown')
                if kind == 'unbound_source':
                    self.assertIsNone(comparison['baseline']['recorded_semantics'])

    def test_report_contains_evidence_attribution_and_all_research_feedback(self):
        review = self.store.create_review(self.baseline_id, 'alpha006', 'needs_changes', 'hypothesis',
            'Automated fixture; semantic judgment not assessed.', source='automation')
        issue = Feedback(self.store).create(review['id'], 'Explicit automated issue', 'automation', 'accept_limitation', 'issue')
        case = self.store.approve_case(review['id'], 'evaluated', 'Automated status expectation')
        check = self.store.run_regression_check(self.baseline_id, [case['id']])
        report = self.report()
        payload = report['payload']
        self.assertEqual(payload['revisions'][0]['task']['evidence'], self.store.get_research(self.example['research_id'])['revisions'][0]['task']['evidence'])
        self.assertIn('attribution', payload['revisions'][0]['task']['hypotheses'][0])
        self.assertEqual(payload['feedback']['reviews'][0]['source'], 'automation')
        self.assertIsNone(payload['feedback']['reviews'][0]['assessment'])
        self.assertEqual(payload['feedback']['issues'][0]['id'], issue['id'])
        self.assertEqual(payload['feedback']['regression_checks'][0]['id'], check['id'])
        self.assertEqual(report['counts']['issue_events'], 1)
        candidates = payload['runs'][0]['candidates']
        self.assertTrue(any(item['status'] == 'blocked' for item in candidates))
        alpha006 = next(item for item in candidates if item['id'] == 'alpha006')
        self.assertEqual(alpha006['current_review_ids'], [review['id']])
        self.assertGreater(alpha006['result_daily_count'], 0)
        self.assertNotIn('daily', alpha006['result'])
        self.assertEqual(alpha006['metric_source']['json_pointer'], '/metrics')

    def test_immutable_json_markdown_payload_and_later_feedback(self):
        report = self.report()
        before_json = self.insights.export_report(report['id'], 'json')
        before_markdown = self.insights.export_report(report['id'], 'markdown')
        self.store.create_review(self.baseline_id, 'alpha006', 'accepted', 'other', 'Later automated review.', source='automation')
        self.assertEqual(self.insights.export_report(report['id'], 'json'), before_json)
        self.assertEqual(self.insights.export_report(report['id'], 'markdown'), before_markdown)
        exported = json.loads(before_json)
        self.assertEqual(digest(exported['payload']), exported['payload_digest'])
        self.assertIn(json_text(exported['payload']).rstrip(), before_markdown)
        self.assertNotIn(str(self.home), before_json)
        self.assertNotIn(str(self.base / 'source'), before_json)
        self.assertEqual(self.report('after-review')['counts']['reviews'], 1)

    def test_replay_and_concurrent_requests_return_original_report_once(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            reports = list(executor.map(lambda _: self.report(), range(3)))
        self.assertTrue(all(item == reports[0] for item in reports))
        self.assertEqual(self.insights.list_reports(self.example['research_id'])['total'], 1)
        run = self.store.submit_run(self.example['revision_id'], 'fixed', 'another')
        with self.assertRaises(ServiceError) as raised:
            self.report(runs=[run['id']])
        self.assertEqual(raised.exception.status, 409)
        self.assertNotEqual(self.report('explicit-new')['id'], reports[0]['id'])
        self.assertEqual(self.insights.list_reports(self.example['research_id'], limit=1, offset=1)['total'], 2)

    def test_concurrent_commit_wins_over_late_snapshot_rejection(self):
        committed = []
        def commit_then_reject(*args):
            other = ResearchInsights(self.store)
            committed.append(other.create_report(self.example['research_id'], [self.baseline_id], 'report'))
            raise ServiceError('Source exceeded capture bound after concurrent commit', 413)
        with patch.object(self.insights, '_report_snapshot', side_effect=commit_then_reject):
            returned = self.report()
        self.assertEqual(returned, committed[0])
        self.assertEqual(self.insights.list_reports(self.example['research_id'])['total'], 1)

    def test_backup_restore_preserves_bytes_and_original_request_replay(self):
        report = self.report()
        json_before = self.insights.export_report(report['id'], 'json')
        markdown_before = self.insights.export_report(report['id'], 'markdown')
        backup = Path(self.temp.name) / 'report-backup'
        create_backup(self.home, backup)
        restored = Path(self.temp.name) / 'restored'
        restore_backup(backup, restored)
        insights = ResearchInsights(Store(restored))
        self.assertEqual(insights.export_report(report['id'], 'json'), json_before)
        self.assertEqual(insights.export_report(report['id'], 'markdown'), markdown_before)
        self.assertEqual(insights.create_report(self.example['research_id'], [self.baseline_id], 'report'), report)

    def test_report_hash_and_identity_tampering_fail_closed(self):
        report = self.report()
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_reports SET payload_digest=? WHERE id=?', ('0' * 64, report['id']))
        for action in (lambda: self.insights.get_report(report['id']), lambda: self.insights.export_report(report['id'], 'markdown'),
                       lambda: self.report(), lambda: self.insights.list_reports(self.example['research_id'])):
            with self.subTest(action=action), self.assertRaises(ServiceError) as raised:
                action()
            self.assertEqual(raised.exception.status, 409)

    def test_input_limits_and_source_limits_never_silently_truncate(self):
        for values in ([], [self.baseline_id] * 2, [str(i) for i in range(21)], [''], [False]):
            with self.subTest(values=values), self.assertRaises(ServiceError) as raised:
                self.insights.create_report(self.example['research_id'], values, 'limit')
            self.assertEqual(raised.exception.status, 422)
        for key in ('', ' ', 'x' * 129, None, True):
            with self.subTest(key=key), self.assertRaises(ServiceError) as raised:
                self.insights.create_report(self.example['research_id'], [self.baseline_id], key)
            self.assertEqual(raised.exception.status, 422)
        for constant in ('MAX_REVISIONS', 'MAX_EVENTS', 'MAX_REPORT_BYTES', 'MAX_SOURCE_BYTES'):
            with self.subTest(constant=constant), patch.object(research_insights, constant, 0):
                with self.assertRaises(ServiceError) as raised:
                    self.report(constant)
                self.assertEqual(raised.exception.status, 413)
        self.assertEqual(self.insights.list_reports(self.example['research_id'])['total'], 0)
        self.report()
        with patch.object(research_insights, 'MAX_RECORD_BYTES', 0), self.assertRaises(ServiceError) as raised:
            self.insights.list_reports(self.example['research_id'])
        self.assertEqual(raised.exception.status, 413)

    def test_external_issue_check_case_and_review_context_is_frozen_explicitly(self):
        research = self.store.get_research(self.example['research_id'])
        original_review = self.store.create_review(self.baseline_id, 'alpha006', 'needs_changes', 'hypothesis',
            'Automated cross-research linkage fixture', source='automation')
        feedback = Feedback(self.store)
        issue = feedback.create(original_review['id'], 'Consider another explicitly linked hypothesis', 'automation', 'hypothesis_change', 'related-issue')
        another = self.store.create_research('External context', research['paper_id'], research['dataset_id'], research['revisions'][0]['task'])
        other_run = self.execute(self.store, another['latest_revision_id'], 'external')
        review = self.store.create_review(other_run, 'alpha006', 'accepted', 'other', 'Automated external review', source='automation')
        case = self.store.approve_case(review['id'], 'evaluated', 'Automated external expectation')
        check = self.store.run_regression_check(other_run, [case['id']])
        feedback.update(issue['id'], issue['latest_event_id'], 'awaiting_review', 'hypothesis_change',
            'Explicit cross-research proposal, not a silent replacement', 'automation', 'related-decision',
            revision_id=another['latest_revision_id'], target_run_id=other_run, check_id=check['id'])
        report = self.report()
        self.assertEqual(report['counts']['external_referenced_reviews'], 1)
        self.assertEqual(report['counts']['external_referenced_cases'], 1)
        self.assertEqual(report['counts']['external_referenced_checks'], 1)
        self.assertEqual(report['selected_run_ids'], [self.baseline_id])
        self.assertEqual(len(report['payload']['runs']), 1)
        self.assertEqual(report['payload']['feedback']['regression_checks'][0]['owning_research_id'], another['id'])
        self.assertEqual(report['payload']['feedback']['regression_cases'][0]['id'], case['id'])
        self.assertIn(review['id'], [item['id'] for item in report['payload']['feedback']['reviews']])

    def test_cross_research_report_selection_is_rejected_but_comparison_supported(self):
        research = self.store.get_research(self.example['research_id'])
        another = self.store.create_research('Other research', research['paper_id'], research['dataset_id'], research['revisions'][0]['task'])
        run = self.execute(self.store, another['latest_revision_id'], 'other-research')
        self.assertTrue(self.insights.compare(self.baseline_id, run)['comparable'])
        with self.assertRaises(ServiceError) as raised:
            self.report(runs=[run])
        self.assertEqual(raised.exception.status, 422)

    def test_capture_uses_read_snapshot_and_does_not_hold_writer_lock(self):
        original = self.insights._source
        emitted = []
        def concurrent_write(connection, run_id):
            if not emitted:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.store.create_review, self.baseline_id, 'alpha006', 'accepted', 'other',
                                             'Written during snapshot capture', 'automation')
                    emitted.append(future.result(timeout=10))
            return original(connection, run_id)
        with patch.object(self.insights, '_source', side_effect=concurrent_write):
            report = self.report()
        self.assertEqual(report['counts']['reviews'], 0)
        self.assertEqual(len(self.store.get_run(self.baseline_id)['reviews']), 1)
        self.assertEqual(self.report('later')['counts']['reviews'], 1)


if __name__ == '__main__':
    unittest.main()
