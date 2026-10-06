"""v0.12 request receipts and short publication fences, using real local runs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha.server import db, mutations, service
from paper_alpha.server.backup import create_backup, restore_backup, verify_backup
from paper_alpha.server.service import ServiceError
from paper_alpha.storage import digest
from paper_alpha.workflow import run_task
from tests import test_migrations, test_server


class WorkflowReceiptTests(unittest.TestCase):
    setUp = test_server.ServerCase.setUp
    tearDown = test_server.ServerCase.tearDown
    execute = test_server.ServerCase.execute

    def revision_args(self, key='revision-request'):
        research = self.store.get_research(self.example['research_id'])
        return {'research_id': research['id'], 'base_revision_id': research['latest_revision_id'],
                'task': deepcopy(research['revisions'][-1]['task']), 'note': 'Explicit test revision',
                'idempotency_key': key}

    def approved(self, candidate='alpha101'):
        run, claim = self.execute(mode='normalized_fixed')
        review = self.store.create_review(run['id'], candidate, 'accepted', 'implementation',
                                         'Automated receipt fixture; no financial judgment', source='automation')
        case = self.store.approve_case(review['id'], 'evaluated', 'Frozen fixture expectation')
        target = run['regression_target']
        args = {'run_id': run['id'], 'case_ids': [case['id']], 'idempotency_key': 'check-request',
                'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest']}
        return run, claim, case, args

    def counts(self):
        with closing(db.connect(self.store.db_path)) as connection:
            return {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('revisions', 'regression_checks', 'workflow_mutation_receipts', 'events')}

    def test_revision_response_loss_replays_original_even_after_newer_revision(self):
        args = self.revision_args()
        committed = self.store.create_revision(**args)
        newer = self.store.create_revision(**{**args, 'base_revision_id': committed['id'],
                                              'note': 'Later independent change', 'idempotency_key': 'later'})
        before = self.counts()
        with patch.object(self.store, '_task', side_effect=AssertionError('Replay revalidated task')):
            replay = self.store.create_revision(**args)
        self.assertEqual(replay, committed)
        self.assertEqual(before, self.counts())
        self.assertEqual(self.store.get_research(args['research_id'])['latest_revision_id'], newer['id'])

    def test_revision_key_conflicts_and_new_key_cannot_rebase_stale_request(self):
        args = self.revision_args()
        self.store.create_revision(**args)
        for altered in ({**args, 'note': 'Different request'}, {**args, 'idempotency_key': 'new-key'}):
            with self.subTest(altered=altered['idempotency_key']), self.assertRaises(ServiceError) as caught:
                self.store.create_revision(**altered)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.counts()['revisions'], 2)
        self.assertEqual(self.counts()['workflow_mutation_receipts'], 1)

    def test_concurrent_revision_replay_has_one_business_row_and_receipt(self):
        args = self.revision_args()
        gate = threading.Barrier(4)
        def invoke(_):
            gate.wait(timeout=10)
            return self.store.create_revision(**args)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(invoke, range(4)))
        self.assertTrue(all(value == results[0] for value in results))
        self.assertEqual(self.counts()['revisions'], 2)
        self.assertEqual(self.counts()['workflow_mutation_receipts'], 1)

    def test_concurrent_revision_different_requests_with_same_key_conflict(self):
        args = self.revision_args()
        gate = threading.Barrier(2)
        normalize = self.store._task
        def validate_before_writer(*arguments):
            task = normalize(*arguments)
            gate.wait(timeout=10)
            return task
        def invoke(note):
            try:
                return self.store.create_revision(**{**args, 'note': note})
            except ServiceError as exc:
                return exc
        with patch.object(self.store, '_task', side_effect=validate_before_writer), \
                ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, ('First competing request', 'Second competing request')))
        failures = [result for result in results if isinstance(result, ServiceError)]
        successes = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(successes), 1)
        self.assertEqual([failure.status for failure in failures], [409])
        self.assertEqual(self.counts()['revisions'], 2)
        self.assertEqual(self.counts()['workflow_mutation_receipts'], 1)
        self.assertEqual(self.store.create_revision(**{**args, 'note': successes[0]['note']}), successes[0])

    def test_revision_business_row_latest_pointer_and_receipt_roll_back_together(self):
        args = self.revision_args()
        before = self.counts()
        original = mutations.record
        def fail_after_receipt(*arguments, **keywords):
            original(*arguments, **keywords)
            raise RuntimeError('Injected failure after receipt insertion')
        with patch.object(mutations, 'record', side_effect=fail_after_receipt):
            with self.assertRaisesRegex(RuntimeError, 'after receipt'):
                self.store.create_revision(**args)
        self.assertEqual(before, self.counts())
        self.assertEqual(self.store.get_research(args['research_id'])['latest_revision_id'], args['base_revision_id'])
        result = self.store.create_revision(**args)
        self.assertEqual(result['number'], 2)

    def test_regression_exact_target_rejects_stale_and_partial_identity(self):
        _, _, _, args = self.approved()
        for altered in ({**args, 'expected_result_digest': '0' * 64},
                        {**args, 'expected_attempt_id': 'another-attempt'}):
            with self.subTest(altered=altered), self.assertRaises(ServiceError) as caught:
                self.store.run_regression_check(**altered)
            self.assertEqual(caught.exception.status, 412)
        with self.assertRaises(ServiceError) as caught:
            self.store.run_regression_check(**{**args, 'expected_result_digest': None})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.counts()['regression_checks'], 0)
        self.assertEqual(self.counts()['workflow_mutation_receipts'], 0)

    def test_regression_response_loss_replays_without_revalidating_later_damage(self):
        _, claim, _, args = self.approved()
        committed = self.store.run_regression_check(**args)
        receipts = self.store._read("SELECT idempotency_key FROM workflow_mutation_receipts WHERE operation='regression.check'")
        self.assertEqual([row['idempotency_key'] for row in receipts], [args['idempotency_key']])
        before = self.counts()
        (Path(claim['output_dir']) / 'report.md').write_text('Changed after original publication')
        with patch.object(self.store, '_check_integrity', side_effect=AssertionError('Replay hashed again')), \
                patch.object(service, 'verify_candidate', side_effect=AssertionError('Replay recomputed reference')):
            self.assertEqual(self.store.run_regression_check(**args), committed)
        self.assertEqual(before, self.counts())
        # A receipt acknowledges the old commit. Fresh detail and fresh requests
        # still fail integrity rather than borrowing trust from the old receipt.
        detail = self.store.get_run(args['run_id'])
        self.assertFalse(detail['verification']['verified'])
        self.assertIsNone(detail['regression_target'])
        with self.assertRaises(ServiceError):
            self.store.run_regression_check(**{**args, 'idempotency_key': 'new-after-damage'})

    def test_regression_key_conflict_retains_original_check(self):
        _, _, _, args = self.approved()
        original = self.store.run_regression_check(**args)
        with self.assertRaises(ServiceError) as caught:
            self.store.run_regression_check(**{**args, 'expected_result_digest': '1' * 64})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.list_checks(), [original])

    def test_legacy_regression_requests_without_key_are_independent_checks(self):
        _, _, _, args = self.approved()
        arguments = {name: value for name, value in args.items() if name != 'idempotency_key'}
        first = self.store.run_regression_check(**arguments)
        second = self.store.run_regression_check(**arguments)
        keyed = self.store.run_regression_check(**args)
        self.assertEqual(len({first['id'], second['id'], keyed['id']}), 3)
        self.assertTrue(all(check['passed'] for check in (first, second, keyed)))
        self.assertEqual(self.counts()['regression_checks'], 3)
        receipts = self.store._read("SELECT idempotency_key FROM workflow_mutation_receipts WHERE operation='regression.check'")
        self.assertEqual([row['idempotency_key'] for row in receipts], [args['idempotency_key']])
        self.assertEqual(self.store.run_regression_check(**args), keyed)

    def test_concurrent_regression_replay_publishes_one_check_and_one_event(self):
        _, _, _, args = self.approved()
        gate = threading.Barrier(2)
        original = self.store._check_integrity
        def wait_after_integrity(*arguments):
            original(*arguments)
            gate.wait(timeout=10)
        with patch.object(self.store, '_check_integrity', side_effect=wait_after_integrity), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.store.run_regression_check, **args) for _ in range(2)]
            values = [future.result(timeout=20) for future in futures]
        self.assertEqual(values[0], values[1])
        self.assertEqual(self.counts()['regression_checks'], 1)
        self.assertEqual(self.counts()['workflow_mutation_receipts'], 1)
        self.assertEqual(len(self.store._read("SELECT * FROM events WHERE kind='regression_checked'")), 1)

    def test_regression_check_receipt_and_event_rollback_atomically(self):
        _, _, _, args = self.approved()
        before = self.counts()
        event = self.store._event
        def fail_after_event(connection, run_id, kind, payload, attempt_id=None):
            event(connection, run_id, kind, payload, attempt_id)
            if kind == 'regression_checked':
                raise RuntimeError('Injected failure after regression event')
        with patch.object(self.store, '_event', side_effect=fail_after_event):
            with self.assertRaisesRegex(RuntimeError, 'regression event'):
                self.store.run_regression_check(**args)
        self.assertEqual(before, self.counts())
        self.assertTrue(self.store.run_regression_check(**args)['passed'])

    def test_regression_file_mutation_with_restored_mtime_is_fenced(self):
        _, claim, _, args = self.approved()
        path = Path(claim['output_dir']) / 'report.md'
        original = service.verify_candidate
        def mutate_after_reference(*arguments):
            result = original(*arguments)
            previous, payload = path.stat(), path.read_bytes()
            path.write_bytes(payload[:-1] + (b' ' if payload[-1:] != b' ' else b'\n'))
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            return result
        with patch.object(service, 'verify_candidate', side_effect=mutate_after_reference):
            with self.assertRaisesRegex(ServiceError, 'changed during verification'):
                self.store.run_regression_check(**args)
        self.assertEqual(self.counts()['regression_checks'], 0)
        self.assertEqual(self.counts()['workflow_mutation_receipts'], 0)

    def test_regression_attempt_result_change_is_fenced_after_reference(self):
        _, _, _, args = self.approved()
        original = service.verify_candidate
        def change_attempt_row(*arguments):
            result = original(*arguments)
            with db.transaction(self.store.db_path) as connection:
                connection.execute('UPDATE attempts SET state_digest=? WHERE id=?', ('changed', args['expected_attempt_id']))
            return result
        with patch.object(service, 'verify_candidate', side_effect=change_attempt_row):
            with self.assertRaisesRegex(ServiceError, 'changed during verification'):
                self.store.run_regression_check(**args)
        self.assertEqual(self.counts()['regression_checks'], 0)

    def test_regression_fully_hashes_once_and_reuses_reference_for_same_candidate(self):
        _, _, case, args = self.approved()
        other = self.store.approve_case(case['review_id'], 'evaluated', 'Separate approval, same candidate')
        args['case_ids'] = [case['id'], other['id']]
        with patch.object(self.store, '_check_integrity', wraps=self.store._check_integrity) as integrity, \
                patch.object(service, 'verify_run', wraps=service.verify_run) as hashes, \
                patch.object(service, 'verify_candidate', wraps=service.verify_candidate) as reference:
            result = self.store.run_regression_check(**args)
        self.assertTrue(result['passed'])
        self.assertEqual(integrity.call_count, 1)
        self.assertEqual(hashes.call_count, 1)
        self.assertEqual(reference.call_count, 1)

    def test_light_status_is_unverified_and_never_hashes_or_exposes_metric_state(self):
        run, claim, _, _ = self.approved()
        with patch.object(self.store, '_check_integrity', side_effect=AssertionError('Status performed full verification')), \
                patch.object(service, 'read_json', side_effect=AssertionError('Status read a file')), \
                patch.object(service, 'sha256', side_effect=AssertionError('Status hashed a file')):
            status = self.client.get(f"/api/runs/{run['id']}/status")
        self.assertEqual(status.status_code, 200, status.text)
        payload = status.json()
        self.assertIs(payload['integrity_checked'], False)
        for key in ('state', 'verification', 'review_targets', 'regression_target'):
            self.assertNotIn(key, payload)
        self.assertEqual(payload['change_token'], self.store.get_run(run['id'])['status_token'])
        (Path(claim['output_dir']) / 'report.md').write_text('Filesystem-only change')
        self.assertEqual(self.store.get_run_status(run['id'])['change_token'], payload['change_token'])
        checked = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertFalse(checked.json()['verification']['verified'])
        self.assertEqual(checked.json()['review_targets'], [])
        self.assertIsNone(checked.json()['regression_target'])

    def test_new_api_models_accept_bound_requests_and_preserve_replay_responses(self):
        args = self.revision_args()
        path = f"/api/researches/{args['research_id']}/revisions"
        body = {key: value for key, value in args.items() if key != 'research_id'}
        first, replay = self.client.post(path, json=body), self.client.post(path, json=body)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(replay.json(), first.json())
        _, _, _, check = self.approved()
        result = self.client.post('/api/regression-checks', json=check)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(self.client.post('/api/regression-checks', json=check).json(), result.json())
        self.assertEqual(result.json()['attempt_id'], check['expected_attempt_id'])
        self.assertEqual(result.json()['result_digest'], check['expected_result_digest'])
        partial = self.client.post('/api/regression-checks', json={**check, 'expected_attempt_id': None})
        self.assertEqual(partial.status_code, 422)

    def test_schema_eight_backup_restore_replays_receipts_and_supports_new_operations(self):
        old_run, _, case, check_args = self.approved()
        old_check = self.store.run_regression_check(**check_args)
        revision_args = self.revision_args()
        old_revision = self.store.create_revision(**revision_args)
        receipt_sql = 'SELECT * FROM workflow_mutation_receipts ORDER BY operation,idempotency_key'
        original_receipts = self.store._read(receipt_sql)
        with tempfile.TemporaryDirectory() as destination:
            base = Path(destination).resolve()
            backup, home, offline = base / 'backup', base / 'restored', base / 'original-offline'
            manifest = create_backup(self.root.resolve(), backup)
            self.assertEqual(manifest['database_schema'], str(db.SCHEMA_VERSION))
            self.assertTrue(restore_backup(backup, home)['restored'])
            restored = service.Store(home)
            self.assertEqual(restored._read(receipt_sql), original_receipts)
            # Original inputs are unavailable: new work must use rebased paths,
            # while a receipt still refers to its originally committed result.
            self.root.rename(offline)
            try:
                self.assertEqual(restored.create_revision(**revision_args), old_revision)
                self.assertEqual(restored.run_regression_check(**check_args), old_check)
                frozen_review = restored._fetch('reviews', case['review_id'])
                self.assertEqual(frozen_review['run_id'], old_run['id'])
                self.assertEqual(frozen_review['attempt_id'], check_args['expected_attempt_id'])
                self.assertEqual(restored.list_cases(), [case])
                new_revision = restored.create_revision(**{**revision_args,
                    'base_revision_id': old_revision['id'], 'note': 'New work after restore',
                    'idempotency_key': 'restored-revision'})
                submitted = restored.submit_run(new_revision['id'], 'normalized_fixed', 'restored-run')
                job = restored.claim('restored-worker')
                state = run_task(job['task_path'], job['output_dir'], mode='normalized_fixed')
                restored.finish(submitted['id'], 'restored-worker', job['attempt_id'], state['status'])
                detail = restored.get_run(submitted['id'])
                self.assertTrue(detail['verification']['verified'], detail['verification'])
                with self.assertRaises(ServiceError) as caught:
                    restored.run_regression_check(**{**check_args, 'run_id': submitted['id'],
                        'idempotency_key': 'old-target-on-new-run'})
                self.assertEqual(caught.exception.status, 412)
                target = detail['regression_target']
                fresh = restored.run_regression_check(submitted['id'], [case['id']],
                    idempotency_key='restored-check', expected_attempt_id=target['attempt_id'],
                    expected_result_digest=target['result_digest'])
                self.assertTrue(fresh['passed'])
                self.assertNotEqual(fresh['attempt_id'], old_check['attempt_id'])
                self.assertEqual(restored.run_regression_check(**check_args), old_check)
                self.assertEqual(restored.create_revision(**revision_args), old_revision)
                self.assertTrue(restored.get_run(old_run['id'])['verification']['verified'])
                self.assertEqual(verify_backup(backup), manifest)
            finally:
                offline.rename(self.root)


class WorkflowReceiptMigrationTests(unittest.TestCase):
    setUp = test_migrations.MigrationCase.setUp
    tearDown = test_migrations.MigrationCase.tearDown
    snapshot = test_migrations.MigrationCase.snapshot

    def version_seven(self):
        test_migrations.MigrationCase.legacy_database(self)
        with db.transaction(self.path) as connection:
            for migrate in (db._migrate_v1, db._migrate_v2, db._migrate_v3,
                            db._migrate_v4, db._migrate_v5, db._migrate_v6):
                migrate(connection)
            connection.execute("UPDATE settings SET value='7' WHERE key='schema_version'")
            response = '{ "id":"review", "created_at":"now", "note":"原始 receipt 字节" }'
            connection.execute('INSERT INTO mutation_receipts VALUES (?,?,?,?,?,?,?,?,?)',
                ('review.create', 'original-key', 'request-digest', response,
                 digest({'id': 'review', 'created_at': 'now', 'note': '原始 receipt 字节'}), 'review', None, None, 'now'))
            return [dict(row) for row in connection.execute('SELECT * FROM mutation_receipts')]

    def test_schema_seven_upgrade_preserves_receipt_bytes_and_existing_records(self):
        receipts = self.version_seven()
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            self.assertEqual([dict(row) for row in connection.execute('SELECT * FROM mutation_receipts')], receipts)
            replay = mutations.replay(connection, 'review.create', 'original-key', 'request-digest')
            self.assertEqual(replay['note'], '原始 receipt 字节')
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM workflow_mutation_receipts').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT note FROM revisions').fetchone()[0], 'Original task')
            self.assertEqual(connection.execute('SELECT payload FROM regression_checks').fetchone()[0], '{"passed":true,"scope":"status only"}')
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_schema_seven_failure_after_new_table_rolls_back_and_can_retry(self):
        self.version_seven()
        before = self.snapshot()
        original = db._migrate_v7
        def fail_after_new_table(connection):
            original(connection)
            raise RuntimeError('Injected v8 migration failure')
        with patch.object(db, '_migrate_v7', side_effect=fail_after_new_table):
            with self.assertRaisesRegex(RuntimeError, 'v8 migration'):
                db.initialize(self.path)
        self.assertEqual(self.snapshot(), before)
        db.initialize(self.path)
        with closing(db.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mutation_receipts').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
