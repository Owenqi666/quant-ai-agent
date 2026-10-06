"""Only temporary automated fixtures; no actual human observations are made."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.server.db import connect, transaction
from paper_alpha.server.observation_context import ObservationContextService, runtime_context
from paper_alpha.server.service import REPO, TASK_KEYS, ServiceError, Store
from paper_alpha.storage import digest, json_text, read_json
from paper_alpha.workflow import run_task


class ObservationContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'workspace')
        paper = self.store.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), 'Context test fixture')
        task = read_json(REPO / 'evaluation_suites/v05/task.json')
        self.task = {key: task[key] for key in TASK_KEYS}
        self.research = self.store.create_research('Read-only context fixture', paper['id'], self.store.example_dataset_id, self.task)
        self.rid, self.rev = self.research['id'], self.research['latest_revision_id']
        self.service = ObservationContextService(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def execute(self):
        run = self.store.submit_run(self.rev, 'normalized_fixed', 'context-run')
        claim = self.store.claim('context-fixture-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        self.store.finish(run['id'], 'context-fixture-worker', claim['attempt_id'], state['status'])
        return self.store.get_run(run['id']), claim

    def review(self, run, *, source='automation', reviewer='Automated fixture', key='context-review'):
        target = run['review_targets'][0]
        assessment = {'reviewer': reviewer, 'expected_attempt_id': target['attempt_id'],
                      'expected_result_digest': target['result_digest'],
                      'dimensions': {name: {'outcome': 'passed', 'reason': 'Automated fixture only, no actual judgment.'}
                                     for name in ('evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution',
                                                  'field_semantics', 'implementation_alignment')},
                      'active_intervals': [{'started_at': '2024-01-01T00:00:00Z', 'ended_at': '2024-01-01T00:00:10Z'}]}
        return self.store.create_review(run['id'], 'alpha101', 'accepted', 'other', 'Temporary context fixture',
                                        source, assessment, idempotency_key=key)

    def context(self, run, claim):
        return self.service.get_context(self.rid, run_id=run['id'], attempt_id=claim['attempt_id'])

    def test_empty_context_has_no_observation_and_no_git_inference(self):
        with patch('subprocess.check_output', side_effect=AssertionError('No Git discovery is allowed')):
            result = self.service.get_context(self.rid)
        self.assertTrue(result['compatibility']['compatible'])
        self.assertEqual(result['revision_id'], self.rev)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['runs'], [])
        self.assertIsNone(result['runtime']['code_commit'])
        self.assertFalse(result['runtime']['code_commit_verified'])
        self.assertEqual(len(result['runtime']['code_digest']), 64)
        self.assertEqual(set(result['runtime']['environment']), {'machine', 'os', 'python', 'dependencies'})
        with closing(connect(self.store.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM workflow_observations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mutation_receipts').fetchone()[0], 0)

    def test_portable_mapping_is_explicit_without_creating_human_claim(self):
        with patch('paper_alpha.server.observation_context.portable_source_commit', return_value='a' * 40):
            result = runtime_context()
        self.assertEqual(result['code_commit'], 'a' * 40)
        self.assertTrue(result['code_commit_verified'])
        self.assertNotIn('source', result)

    def test_protocol_incompatible_revision_and_explicit_historical_revision(self):
        task = deepcopy(self.task)
        task['hypotheses'][0]['economic_mechanism'] = 'Changed scientific explanation'
        new = self.store.create_revision(self.rid, self.rev, task, 'Fixture change')
        self.assertFalse(self.service.get_context(self.rid)['compatibility']['compatible'])
        historical = self.service.get_context(self.rid, revision_id=self.rev)
        self.assertTrue(historical['compatibility']['compatible'])
        self.assertNotEqual(historical['revision_id'], new['id'])

    def test_cross_research_revision_and_run_attempt_dependencies_rejected(self):
        other = self.store.create_research('Other', self.research['paper_id'], self.store.example_dataset_id, self.task)
        for kwargs in ({'revision_id': other['latest_revision_id']}, {'attempt_id': 'without-run'}, {'run_id': ' '}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ServiceError):
                self.service.get_context(self.rid, **kwargs)
        run = self.store.submit_run(other['latest_revision_id'], 'normalized_fixed', 'other-run')
        with self.assertRaises(ServiceError):
            self.service.get_context(self.rid, run_id=run['id'])

    def test_run_without_attempt_is_not_implicitly_selected_and_wrong_mode_rejected(self):
        run = self.store.submit_run(self.rev, 'normalized_fixed', 'queued-run')
        result = self.service.get_context(self.rid, run_id=run['id'])
        self.assertIsNone(result['selected']['attempt_id'])
        self.assertFalse(result['selected']['verified'])
        self.assertEqual(result['selected']['attempts'], [])
        wrong = self.store.submit_run(self.rev, 'agent', 'wrong-mode')
        with self.assertRaises(ServiceError) as caught:
            self.service.get_context(self.rid, run_id=wrong['id'])
        self.assertEqual(caught.exception.status, 422)

    def test_run_listing_bounded_but_explicit_older_identity_resolves(self):
        runs = [self.store.submit_run(self.rev, 'normalized_fixed', f'list-{i}') for i in range(23)]
        result = self.service.get_context(self.rid)
        self.assertEqual(result['runs_total'], 23)
        self.assertEqual(len(result['runs']), 20)
        self.assertTrue(result['runs_truncated'])
        explicit = self.service.get_context(self.rid, run_id=runs[0]['id'])
        self.assertEqual(explicit['selected']['run_id'], runs[0]['id'])

    def test_running_attempt_cannot_claim_verified_or_finished(self):
        run = self.store.submit_run(self.rev, 'normalized_fixed', 'running')
        claim = self.store.claim('context-worker')
        result = self.context(run, claim)['selected']
        self.assertEqual(result['status'], 'running')
        self.assertFalse(result['verified'])
        self.assertIsNotNone(result['verification_error'])
        self.assertIsNone(result['bound_outputs']['report_reference'])

    def test_verified_attempt_precise_report_and_review_without_auto_selection(self):
        run, claim = self.execute()
        review = self.review(run)
        with patch.object(self.service.observations, '_binding', wraps=self.service.observations._binding) as binding:
            result = self.context(run, claim)
        self.assertEqual(binding.call_count, 2)  # Shared science + exact attempt, not a full scan per review.
        selected = result['selected']
        self.assertTrue(selected['verified'])
        self.assertEqual(selected['bound_outputs']['attempt_id'], claim['attempt_id'])
        self.assertEqual(selected['bound_outputs']['review_ids'], [])
        self.assertIn('/artifacts/', selected['bound_outputs']['report_reference'])
        self.assertNotEqual(selected['bound_outputs']['report_reference'], f"/api/runs/{run['id']}/report")
        self.assertEqual(selected['reviews'][0]['id'], review['id'])
        self.assertEqual(selected['reviews'][0]['source'], 'automation')
        self.assertEqual(selected['reviews'][0]['reviewer'], 'Automated fixture')
        self.assertEqual(selected['reviews'][0]['dimensions'], review['assessment']['dimensions'])
        self.assertNotIn('intervals', selected)
        self.assertNotIn('completion', selected)

    def test_source_and_reviewer_labels_preserved_unstructured_and_bad_fingerprints_excluded(self):
        run, claim = self.execute()
        automatic = self.review(run)
        human = self.review(run, source='human', reviewer='Declared fixture person', key='human-fixture')
        self.store.create_review(run['id'], 'alpha101', 'accepted', 'other', 'Unstructured fixture', 'human', idempotency_key='unstructured')
        with transaction(self.store.db_path) as connection:
            changed = deepcopy(automatic['assessment'])
            changed['expected_result_digest'] = 'f' * 64
            connection.execute('UPDATE reviews SET assessment=? WHERE id=?', (json_text(changed), automatic['id']))
        selected = self.context(run, claim)['selected']
        self.assertEqual(selected['reviews_total'], 2)
        self.assertEqual(len(selected['reviews']), 1)
        self.assertEqual(selected['reviews'][0]['id'], human['id'])
        self.assertEqual(selected['reviews'][0]['source'], 'human')
        self.assertEqual(selected['reviews'][0]['reviewer'], 'Declared fixture person')

    def test_tampered_output_hides_verification_and_reviews(self):
        run, claim = self.execute()
        self.review(run)
        (Path(claim['output_dir']) / 'report.md').write_text('Corrupt fixture')
        selected = self.context(run, claim)['selected']
        self.assertFalse(selected['verified'])
        self.assertIsNotNone(selected['verification_error'])
        self.assertEqual(selected['reviews'], [])
        self.assertIsNone(selected['bound_outputs']['verification_evidence'])

    def test_snapshot_reference_cannot_escape_server_attempt(self):
        run, claim = self.execute()
        path = Path(claim['output_dir']) / 'manifest.json'
        manifest = read_json(path)
        manifest['snapshot_files']['../../../../outside-secret'] = 'a' * 64
        path.write_text(json_text(manifest))
        with patch.object(self.service.observations, '_binding', wraps=self.service.observations._binding) as binding:
            result = self.context(run, claim)
        self.assertFalse(result['selected']['verified'])
        self.assertEqual(binding.call_count, 1)  # Unsafe snapshot rejected before legacy file iteration.

    def test_symlink_and_oversized_sources_rejected_before_read(self):
        external = self.root / 'outside.csv'
        external.write_text('private fixture')
        with transaction(self.store.db_path) as connection:
            data = connection.execute('SELECT data_path FROM datasets WHERE id=?', (self.store.example_dataset_id,)).fetchone()[0]
        target = Path(data)
        target.unlink()
        target.symlink_to(external)
        with self.assertRaises(ServiceError) as caught:
            self.service.get_context(self.rid)
        self.assertEqual(caught.exception.status, 409)

    def test_selected_attempt_stays_exact_when_run_row_points_elsewhere(self):
        run, claim = self.execute()
        review = self.review(run)
        # A later failed attempt is a fixture row; preserve the old verified source.
        with transaction(self.store.db_path) as connection:
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                'later-attempt', run['id'], 2, 'fixture', 'failed', '2024-01-02T00:00:00Z',
                '2024-01-02T00:00:01Z', 'Fixture failure', '/ignored/path', None, None))
            connection.execute("UPDATE runs SET attempt_id='later-attempt',attempt_count=2,status='failed',state=NULL,verification=NULL WHERE id=?", (run['id'],))
        selected = self.context(run, claim)['selected']
        self.assertTrue(selected['verified'])
        self.assertEqual(selected['bound_outputs']['attempt_id'], claim['attempt_id'])
        self.assertEqual(selected['reviews'][0]['id'], review['id'])
        self.assertEqual(selected['attempts_total'], 2)

    def test_source_limits_return_413(self):
        with patch('paper_alpha.server.observation_context.MAX_TREE_FILES', 1):
            with self.assertRaises(ServiceError) as caught:
                self.service.get_context(self.rid)
        self.assertEqual(caught.exception.status, 413)

    def test_run_selects_its_own_revision_unless_explicit_revision_conflicts(self):
        run = self.store.submit_run(self.rev, 'normalized_fixed', 'old-revision')
        task = deepcopy(self.task)
        task['hypotheses'][0]['economic_mechanism'] = 'Later incompatible description'
        later = self.store.create_revision(self.rid, self.rev, task, 'Fixture revision')
        result = self.service.get_context(self.rid, run_id=run['id'])
        self.assertEqual(result['revision_id'], self.rev)
        self.assertTrue(result['compatibility']['compatible'])
        with self.assertRaises(ServiceError):
            self.service.get_context(self.rid, run_id=run['id'], revision_id=later['id'])

    def test_missing_registered_input_returns_chinese_409(self):
        with closing(connect(self.store.db_path)) as connection:
            path = connection.execute('SELECT data_path FROM datasets WHERE id=?', (self.store.example_dataset_id,)).fetchone()[0]
        Path(path).unlink()
        with self.assertRaises(ServiceError) as caught:
            self.service.get_context(self.rid)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn('上下文', str(caught.exception))

    def test_review_page_has_bound_and_oversized_record_fails_closed(self):
        run, claim = self.execute()
        review = self.review(run)
        with transaction(self.store.db_path) as connection:
            original = dict(connection.execute('SELECT * FROM reviews WHERE id=?', (review['id'],)).fetchone())
            columns = ','.join(original)
            placeholders = ','.join('?' for _ in original)
            for index in range(24):
                row = {**original, 'id': f'fixture-review-{index}', 'idempotency_key': None, 'request_digest': None}
                connection.execute(f'INSERT INTO reviews ({columns}) VALUES ({placeholders})', tuple(row[key] for key in original))
        selected = self.context(run, claim)['selected']
        self.assertEqual(selected['reviews_total'], 25)
        self.assertTrue(selected['reviews_truncated'])
        self.assertEqual(len(selected['reviews']), 20)
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE reviews SET assessment=? WHERE id=?',
                               (' ' * (1024 * 1024 + 1), selected['reviews'][0]['id']))
        with self.assertRaises(ServiceError) as caught:
            self.context(run, claim)
        self.assertEqual(caught.exception.status, 413)

    def test_unsafe_export_path_is_not_read(self):
        run, claim = self.execute()
        private = self.root / 'private.txt'
        private.write_text('Outside authorized artifact root')
        for path in (private, Path(claim['output_dir']).parent / 'exports' / '../../../../../../private.txt'):
            with self.subTest(path=path):
                with transaction(self.store.db_path) as connection:
                    connection.execute("UPDATE artifacts SET path=? WHERE attempt_id=? AND name='report.md'", (str(path), claim['attempt_id']))
                with patch.object(self.service.observations, '_binding', wraps=self.service.observations._binding) as binding:
                    selected = self.context(run, claim)['selected']
                self.assertFalse(selected['verified'])
                self.assertEqual(binding.call_count, 1)


if __name__ == '__main__':
    unittest.main()
