"""Real monthly lifecycle, receipts, publication fences and offline restoration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha import monthly_workflow, research_protocol
from paper_alpha.server import db, monthly_experiments
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import read_json

CONFIG = {'schema_version': 1, 'start_month': '2025-01', 'end_month': '2025-02', 'cost_bps': 0, 'min_assets': 18}


class MonthlyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.store = Store(self.root)
        self.service = MonthlyExperiments(self.store)
        self.protocol = ResearchProtocols(self.store).create('Fixture project convention', '', research_protocol.presets()[1]['config'])

    def create(self, key='create', **changes):
        args = {'protocol_id': self.protocol['id'], 'protocol_digest': self.protocol['digest'],
                'config': CONFIG, 'idempotency_key': key}
        return self.service.create(**{**args, **changes})['experiment_id']

    def execute(self, key='create'):
        identity = self.create(key)
        job = self.service.claim('worker')
        monthly_workflow.run(job['input_path'], job['output_dir'])
        self.service.finish(identity, 'worker', job['attempt_id'], 'completed')
        return identity, job, self.service.get(identity)

    def review_args(self, identity, detail, key='review'):
        return {'identity': identity, **detail['review_target'], 'verdict': 'accepted', 'note': 'Automated fixture review.',
                'source': 'automation', 'idempotency_key': key}

    def test_create_replay_concurrency_and_changed_body_conflict(self):
        gate = threading.Barrier(4)
        def invoke(_):
            gate.wait(timeout=10)
            return self.create()
        with ThreadPoolExecutor(max_workers=4) as pool:
            identities = list(pool.map(invoke, range(4)))
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual(self.service.list()['total'], 1)
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM monthly_events')[0]['n'], 1)
        with patch('paper_alpha.monthly_evaluation.demo_bundle', side_effect=AssertionError('Replay regenerated fixture')):
            self.assertEqual(self.create(), identities[0])
        with self.assertRaises(ServiceError) as caught:
            self.create(config={**CONFIG, 'cost_bps': 1})
        self.assertEqual(caught.exception.status, 409)

    def test_execute_review_report_and_restore_have_exact_bound_identities(self):
        identity, job, detail = self.execute()
        self.assertEqual(detail['experiment']['status'], 'completed')
        self.assertTrue(detail['verification']['verified'])
        self.assertTrue(detail['verification']['reference_passed'])
        review = self.service.create_review(**self.review_args(identity, detail))
        report = self.service.create_report(identity, **detail['review_target'], idempotency_key='report')
        self.assertEqual(report['result'], detail['result'])
        self.assertEqual(report['reviews'], [review])
        self.assertIn(review['id'], report['markdown'])
        self.assertEqual(self.service.create_report(identity, **detail['review_target'], idempotency_key='report'), report)
        create_backup(self.root, self.root.parent / 'backup')
        restore_backup(self.root.parent / 'backup', self.root.parent / 'restored')
        restored = MonthlyExperiments(Store(self.root.parent / 'restored'))
        self.assertEqual(restored.get(identity), self.service.get(identity))
        self.assertEqual(restored.get_report(identity, report['id']), report)
        self.assertEqual(restored.create_review(**self.review_args(identity, detail)), review)
        self.assertEqual(restored.create(self.protocol['id'], self.protocol['digest'], CONFIG, 'create'), {'experiment_id': identity})
        # No new absolute path columns or hidden daily experiments are introduced.
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM runs')[0]['n'], 0)

    def test_paper_workflow_completes_diagnostics_but_retains_blocked_semantics(self):
        paper = ResearchProtocols(self.store).create('Unresolved paper', '', research_protocol.presets()[0]['config'])
        identity = self.create(protocol_id=paper['id'], protocol_digest=paper['digest'])
        job = self.service.claim('worker')
        monthly_workflow.run(job['input_path'], job['output_dir'])
        self.service.finish(identity, 'worker', job['attempt_id'], 'completed')
        detail = self.service.get(identity)
        self.assertEqual(detail['experiment']['status'], 'completed')
        self.assertEqual(detail['result']['status'], 'blocked')
        self.assertIsNone(detail['verification']['reference_passed'])

    def test_status_is_lightweight_and_changes_after_reviews_and_reports(self):
        identity, _, detail = self.execute()
        with patch.object(self.service, '_verified', side_effect=AssertionError('Status verified artifacts')):
            before = self.service.status(identity)
        self.assertFalse(before['integrity_checked'])
        self.service.create_review(**self.review_args(identity, detail))
        reviewed = self.service.status(identity)
        self.assertNotEqual(reviewed['change_token'], before['change_token'])
        self.service.create_report(identity, **detail['review_target'], idempotency_key='report')
        self.assertNotEqual(self.service.status(identity)['change_token'], reviewed['change_token'])

    def test_corrupt_artifact_blocks_detail_and_new_mutations_but_replays_original_ack(self):
        identity, job, detail = self.execute()
        args = self.review_args(identity, detail)
        review = self.service.create_review(**args)
        report = self.service.create_report(identity, **detail['review_target'], idempotency_key='report')
        (Path(job['output_dir']) / 'report.md').write_text('Changed behind the server')
        for invoke in (lambda: self.service.get(identity),
                       lambda: self.service.create_review(**{**args, 'idempotency_key': 'new-review'}),
                       lambda: self.service.create_report(identity, **detail['review_target'], idempotency_key='new-report')):
            with self.assertRaises(ServiceError) as caught:
                invoke()
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.create_review(**args), review)
        self.assertEqual(self.service.create_report(identity, **detail['review_target'], idempotency_key='report'), report)
        self.assertEqual(self.service.get_report(identity, report['id']), report)

    def test_exact_target_conflict_and_terminal_cancel_never_change_result(self):
        identity, _, detail = self.execute()
        before = self.service.get(identity)
        self.service.cancel(identity, detail['review_target']['attempt_id'])
        self.assertEqual(before, self.service.get(identity))
        with self.assertRaises(ServiceError) as caught:
            self.service.cancel(identity, None)
        self.assertEqual(caught.exception.status, 412)
        with self.assertRaises(ServiceError) as caught:
            self.service.create_review(**{**self.review_args(identity, detail), 'result_digest': '0' * 64})
        self.assertEqual(caught.exception.status, 412)

    def test_failed_preparation_is_owned_and_does_not_leave_an_untracked_folder(self):
        identity = self.create()
        with patch.object(monthly_experiments, 'atomic_json', side_effect=OSError('Injected preparation I/O failure')):
            self.assertIsNone(self.service.claim('worker'))
        detail = self.service.get(identity)
        self.assertEqual(detail['experiment']['status'], 'failed')
        self.assertEqual(detail['attempts'][0]['status'], 'failed')
        directories = list((self.root / 'monthly' / identity / 'attempts').iterdir())
        self.assertEqual([item.name for item in directories], [detail['attempts'][0]['id']])

    def test_preparation_process_interruption_requires_explicit_retry(self):
        identity = self.create()
        original = monthly_experiments.atomic_json
        def interrupted(path, value):
            original(path, value)
            raise KeyboardInterrupt('Simulated supervisor loss after input file publication')
        with patch.object(monthly_experiments, 'atomic_json', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.service.claim('old-worker')
        previous = self.service.status(identity)['attempt_id']
        self.assertEqual(self.service.recover_stale(), [identity])
        self.assertEqual(self.service.status(identity)['status'], 'interrupted')
        ack = self.service.retry(identity, previous, 'retry')
        new = self.service.claim('new-worker')
        self.assertNotEqual(new['attempt_id'], previous)
        self.assertEqual(self.service.retry(identity, previous, 'retry'), ack)
        with self.assertRaises(ServiceError) as caught:
            self.service.finish(identity, 'old-worker', previous, 'completed')
        self.assertEqual(caught.exception.status, 409)
        self.service.finish(identity, 'new-worker', new['attempt_id'], 'failed', 'Fixture cleanup')
        self.assertEqual(len(self.service.get(identity)['attempts']), 2)

    def test_retry_limit_and_different_retry_body_preserve_history(self):
        identity = self.create()
        for number in range(3):
            job = self.service.claim('worker')
            self.service.finish(identity, 'worker', job['attempt_id'], 'failed', 'Injected compute failure')
            if number < 2:
                self.service.retry(identity, job['attempt_id'], 'retry-' + str(number))
        with self.assertRaises(ServiceError) as caught:
            self.service.retry(identity, job['attempt_id'], 'fourth')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.service.get(identity)['attempts']), 3)

    def test_cancellation_wins_over_valid_publication_and_queued_cancel_stays_attemptless(self):
        cancelled = self.create('cancel-before-claim')
        self.service.cancel(cancelled, None)
        self.assertIsNone(self.service.claim('worker'))
        self.assertEqual(self.service.get(cancelled)['attempts'], [])
        identity = self.create('cancel-at-finish')
        job = self.service.claim('worker')
        monthly_workflow.run(job['input_path'], job['output_dir'])
        self.service.cancel(identity, job['attempt_id'])
        self.service.finish(identity, 'worker', job['attempt_id'], 'completed')
        detail = self.service.get(identity)
        self.assertEqual(detail['experiment']['status'], 'cancelled')
        self.assertIsNone(detail['result'])
        self.assertIsNone(detail['review_target'])

    def test_mutation_after_verify_is_fenced_before_finish_publishes(self):
        identity = self.create()
        job = self.service.claim('worker')
        monthly_workflow.run(job['input_path'], job['output_dir'])
        original = self.service.record_phase
        def mutate_after_verification(*args):
            result = original(*args)
            if args[-1] == 'publishing':
                path = Path(job['output_dir']) / 'report.md'
                previous = path.stat()
                text = path.read_text()
                path.write_text(text[:-1] + (' ' if text[-1] != ' ' else '\n'))
                os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            return result
        with patch.object(self.service, 'record_phase', side_effect=mutate_after_verification):
            self.service.finish(identity, 'worker', job['attempt_id'], 'completed')
        detail = self.service.get(identity)
        self.assertEqual(detail['experiment']['status'], 'failed')
        self.assertFalse(detail['verification']['verified'])
        self.assertIsNone(detail['result'])

    def test_review_publication_fences_file_mutation(self):
        identity, job, detail = self.execute()
        original = self.service._target
        def mutate(*args):
            result = original(*args)
            (Path(job['output_dir']) / 'report.md').write_text('Changed after exact verification')
            return result
        with patch.object(self.service, '_target', side_effect=mutate), self.assertRaises(ServiceError) as caught:
            self.service.create_review(**self.review_args(identity, detail))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM monthly_reviews')[0]['n'], 0)

    def test_review_receipt_and_business_row_rollback_together(self):
        identity, _, detail = self.execute()
        original = monthly_experiments._record_receipt
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('Injected error after receipt insert')
        with patch.object(monthly_experiments, '_record_receipt', side_effect=fail), self.assertRaises(RuntimeError):
            self.service.create_review(**self.review_args(identity, detail))
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM monthly_reviews')[0]['n'], 0)
        self.assertEqual(self.store._read("SELECT COUNT(*) AS n FROM monthly_receipts WHERE operation='review'")[0]['n'], 0)

    def test_concurrent_review_and_report_retries_publish_one_record_each(self):
        identity, _, detail = self.execute()
        original = self.service._target
        for operation, table in (('review', 'monthly_reviews'), ('report', 'monthly_reports')):
            gate = threading.Barrier(2)
            def simultaneous(*args):
                value = original(*args)
                gate.wait(timeout=10)
                return value
            def invoke(_):
                if operation == 'review':
                    return self.service.create_review(**self.review_args(identity, detail))
                return self.service.create_report(identity, **detail['review_target'], idempotency_key='report')
            with patch.object(self.service, '_target', side_effect=simultaneous), ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(invoke, range(2)))
            self.assertEqual(values[0], values[1])
            self.assertEqual(self.store._read(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'], 1)

    def test_reports_keep_original_reviews_and_detect_their_own_tampering(self):
        identity, _, detail = self.execute()
        report = self.service.create_report(identity, **detail['review_target'], idempotency_key='report')
        self.service.create_review(**self.review_args(identity, detail))
        self.assertEqual(self.service.get_report(identity, report['id'])['reviews'], [])
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE monthly_reports SET payload='[]' WHERE id=?", (report['id'],))
        with self.assertRaises(ServiceError) as caught:
            self.service.get_report(identity, report['id'])
        self.assertEqual(caught.exception.status, 409)

    def test_corrupt_frozen_database_input_fails_closed(self):
        identity = self.create()
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE monthly_experiments SET input='[]' WHERE id=?", (identity,))
        with self.assertRaises(ServiceError) as caught:
            self.service.get(identity)
        self.assertEqual(caught.exception.status, 409)
        self.assertIsNone(self.service.claim('worker'))
        self.assertEqual(self.service.status(identity)['status'], 'failed')

    def test_published_attempt_metadata_corruption_blocks_detail_and_new_reviews(self):
        identity, job, detail = self.execute()
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE monthly_attempts SET verification='{}' WHERE id=?", (job['attempt_id'],))
        for operation in (lambda: self.service.get(identity),
                          lambda: self.service.create_review(**self.review_args(identity, detail))):
            with self.assertRaises(ServiceError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 409)

    def test_review_fence_also_checks_attempt_metadata_after_verification(self):
        identity, job, detail = self.execute()
        original = self.service._target
        def tamper(*args):
            value = original(*args)
            with db.transaction(self.store.db_path) as connection:
                connection.execute("UPDATE monthly_attempts SET status='failed' WHERE id=?", (job['attempt_id'],))
            return value
        with patch.object(self.service, '_target', side_effect=tamper), self.assertRaises(ServiceError) as caught:
            self.service.create_review(**self.review_args(identity, detail))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM monthly_reviews')[0]['n'], 0)

    def test_paginated_summaries_validate_bounds_and_order_without_output_access(self):
        first, second = self.create('first'), self.create('second')
        with patch.object(self.service, '_verified', side_effect=AssertionError('List verified output')):
            page = self.service.list(limit=1)
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['items'][0]['id'], second)
        self.assertEqual(self.service.list(limit=1, offset=1)['items'][0]['id'], first)
        for args in ({'limit': True}, {'limit': 101}, {'offset': -1}, {'offset': '0'}):
            with self.assertRaises(ServiceError) as caught:
                self.service.list(**args)
            self.assertEqual(caught.exception.status, 422)


if __name__ == '__main__':
    unittest.main()
