"""Industry queue faults with real parser/calculator/oracle, isolated evidence.

No live workspace, human record or reserved return is read or written.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch

from paper_alpha import mom_only_workflow as workflow
from paper_alpha.server.db import connect, transaction
from paper_alpha.server.industry_mom import IndustryMomExperiments, IndustryMomSources
from paper_alpha.server.industry_mom_schema import IndustryMomCreate, IndustryMomRetry
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import digest, read_json
import tests.test_mom_only_workflow as synthetic


class IndustryMomBackendTests(unittest.TestCase):
    def setUp(self):
        self.fixture = synthetic.WorkflowIntegrityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.original = self.root / 'original'
        self.fixture._run('original')
        self.store = Store(self.root / 'workspace')
        self.sources = IndustryMomSources(self.store)
        self.experiments = IndustryMomExperiments(self.store)
        self.source = self.sources.register(self.original, 'Synthetic fixture', 'Evidence stubs only.', 'source')

    def error(self, callback, status=409):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)

    def create(self, key='create'):
        return self.experiments.create(self.source['id'], self.source['digest'], key)['experiment_id']

    def job(self, key='create', worker='worker'):
        identity = self.create(key)
        job = self.experiments.claim(worker)
        self.assertEqual(identity, job['id'])
        return job

    def calculate(self, job):
        workflow.run(job['source_path'], job['config_path'], job['method_path'],
                     job['output_dir'], job['source_sha256'], job['source_receipt_path'],
                     evidence_dir=job['evidence_dir'])

    def finish(self, job, status='completed', worker='worker'):
        return self.experiments.finish(job['id'], worker, job['attempt_id'], status, 'Test fault' if status != 'completed' else None)

    def sql(self, command, values=()):
        with transaction(self.store.db_path) as connection:
            connection.execute(command, values)

    def rows(self, table):
        with closing(connect(self.store.db_path)) as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM ' + table)]

    def test_source_copy_remains_verified_after_original_removed_and_replay(self):
        before = workflow._inventory(self.original)
        registered = self.sources._path(self.source['id'])
        self.assertEqual(workflow._inventory(registered), before)
        self.assertEqual((registered / 'invocation.json').read_bytes(), (self.original / 'invocation.json').read_bytes())
        shutil.rmtree(self.original)
        self.assertEqual(self.sources.register(self.original, 'Synthetic fixture', 'Evidence stubs only.', 'source'), self.source)
        self.assertEqual(self.sources.get(self.source['id']), self.source)
        self.assertEqual(self.sources.list()['total'], 1)
        self.assertIsNone(self.source['human_judgment'])

    def test_registration_concurrency_and_changed_body(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(lambda _: self.sources.register(self.original, 'Synthetic fixture', 'Evidence stubs only.', 'source'), range(3)))
        self.assertTrue(all(value == self.source for value in outputs))
        self.error(lambda: self.sources.register(self.original, 'Changed title', 'Evidence stubs only.', 'source'))
        self.assertEqual(len(self.rows('industry_mom_sources')), 1)

    def test_registration_copy_crash_orphan_replay_adopts_only_exact_bytes(self):
        # A transaction failure after the immutable copy must not publish a DB
        # success. Replaying adopts the byte-identical orphan, not new results.
        import paper_alpha.server.industry_mom as backend
        original = backend._put_receipt
        def fail_once(connection, operation, *args, **kwargs):
            if operation == 'register':
                raise OSError('Simulated process loss before commit')
            return original(connection, operation, *args, **kwargs)
        with patch.object(backend, '_put_receipt', side_effect=fail_once):
            self.error(lambda: self.sources.register(self.original, 'Second immutable copy', '', 'second'))
        self.assertEqual(len(self.rows('industry_mom_sources')), 1)
        recovered = self.sources.register(self.original, 'Second immutable copy', '', 'second')
        self.assertEqual(self.sources.get(recovered['id']), recovered)
        self.assertEqual(len(self.rows('industry_mom_sources')), 2)

    def test_source_registered_file_tamper_fails_closed(self):
        path = self.sources._path(self.source['id']) / 'inputs/source.zip'
        path.write_bytes(path.read_bytes() + b'tamper')
        self.error(lambda: self.sources.get(self.source['id']))
        self.error(lambda: self.sources.list())
        self.error(lambda: self.create())
        self.assertEqual(self.rows('industry_mom_experiments'), [])

    def test_original_extra_symlink_and_hardlink_rejected(self):
        for kind in ('extra', 'symlink', 'hardlink'):
            with self.subTest(kind=kind):
                path = self.original / 'unexpected'
                if kind == 'extra':
                    path.write_text('extra')
                elif kind == 'symlink':
                    path.symlink_to(self.original / 'report.md')
                else:
                    os.link(self.original / 'report.md', path)
                self.error(lambda: self.sources.register(self.original, kind, '', kind))
                path.unlink()
        self.assertEqual(len(self.rows('industry_mom_sources')), 1)

    def test_missing_registration_receipt_cannot_be_resurrected(self):
        self.sql("DELETE FROM industry_mom_receipts WHERE operation='register'")
        self.error(lambda: self.sources.get(self.source['id']))
        self.error(lambda: self.sources.register(self.original, 'Synthetic fixture', 'Evidence stubs only.', 'source'))
        self.assertEqual(self.rows('industry_mom_receipts'), [])

    def test_create_lost_response_concurrency_and_changed_request(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(lambda _: self.create(), range(3)))
        self.assertEqual(len(set(outputs)), 1)
        identity = outputs[0]
        self.assertEqual(self.create(), identity)
        self.error(lambda: self.experiments.create(self.source['id'], '0' * 64, 'create'))
        self.assertEqual(len(self.rows('industry_mom_experiments')), 1)
        self.assertEqual(len(self.rows('industry_mom_events')), 1)

    def test_missing_create_receipt_cannot_recreate_effect(self):
        identity = self.create()
        self.sql("DELETE FROM industry_mom_receipts WHERE operation='create'")
        self.error(lambda: self.experiments.get(identity))
        self.error(lambda: self.create())
        self.assertEqual(len(self.rows('industry_mom_experiments')), 1)
        self.assertEqual(len(self.rows('industry_mom_receipts')), 1)

    def test_completed_current_attempt_has_exact_source_code_input_and_output(self):
        before = workflow._inventory(self.original)
        job = self.job()
        self.calculate(job)
        self.finish(job)
        value = self.experiments.get(job['id'])
        self.assertEqual(value['experiment']['status'], 'completed')
        verification = value['verification']
        self.assertEqual(verification['input_digest'], value['experiment']['input_digest'])
        self.assertEqual(verification['source_input_digest'], self.source['input_digest'])
        self.assertEqual(verification['source_manifest_digest'], self.source['manifest_digest'])
        self.assertEqual(verification['panel_digest'], digest(read_json(Path(job['output_dir']) / 'panel.json')))
        self.assertEqual(verification['environment_digest'], read_json(Path(job['output_dir']) / 'manifest.json')['files']['environment.json'])
        self.assertEqual(verification['input_digest'], digest(read_json(Path(job['output_dir']) / 'input.json')))
        self.assertEqual(verification['result_digest'], digest(json.loads(value['result_json'])))
        self.assertEqual(value['review_target'], {'attempt_id': job['attempt_id'], 'result_digest': verification['result_digest']})
        self.assertEqual(value['attempts'][0]['id'], job['attempt_id'])
        self.assertTrue(json.loads(value['reference_json'])['passed'])
        self.assertEqual(self.experiments.get_report(job['id']), value['report_markdown'])
        self.assertFalse(verification['reserved_evaluated'])
        self.assertEqual(verification['human_review'], 'pending')
        self.assertEqual(workflow._inventory(self.original), before)

    def test_noncompleted_never_exposes_results_and_report(self):
        identity = self.create()
        for status in ('queued', 'running', 'failed'):
            if status == 'running':
                job = self.experiments.claim('worker')
            elif status == 'failed':
                self.finish(job, 'failed')
            value = self.experiments.get(identity)
            self.assertEqual(value['experiment']['status'], status)
            self.assertTrue(all(value[key] is None for key in ('result_json', 'reference_json', 'report_markdown', 'verification', 'review_target')))
            self.error(lambda: self.experiments.get_report(identity))

    def test_completed_claim_without_output_records_failed_attempt(self):
        job = self.job()
        self.finish(job)
        value = self.experiments.get(job['id'])
        self.assertEqual(value['experiment']['status'], 'failed')
        self.assertEqual(value['attempts'][0]['status'], 'failed')
        self.assertIsNone(value['verification'])

    def test_failed_workflow_verified_artifact_cannot_publish_completed(self):
        job = self.job()
        with self.assertRaises(ValueError):
            workflow.run(job['source_path'], job['config_path'], job['method_path'], job['output_dir'], '0' * 64,
                         evidence_dir=job['evidence_dir'])
        verified = workflow.verify(job['output_dir'])
        self.assertTrue(verified['verified'])
        self.assertEqual(verified['status'], 'failed')
        self.finish(job)
        self.assertEqual(self.experiments.get(job['id'])['experiment']['status'], 'failed')

    def test_cancel_queued_and_cancel_running_prevents_publication(self):
        identity = self.create()
        self.experiments.cancel(identity, None, 'cancel-queued')
        self.assertIsNone(self.experiments.claim('worker'))
        self.assertEqual(self.experiments.status(identity)['status'], 'cancelled')
        job = self.job('next')
        self.calculate(job)
        self.experiments.cancel(job['id'], job['attempt_id'], 'cancel-running')
        self.finish(job)
        value = self.experiments.get(job['id'])
        self.assertEqual(value['experiment']['status'], 'cancelled')
        self.assertIsNone(value['result_json'])
        self.assertTrue(Path(job['output_dir']).exists())

    def test_three_attempt_limit_keeps_failed_outputs_and_stale_worker_fenced(self):
        job = self.job()
        first = job
        worker = 'worker'
        for number in range(1, 4):
            marker = Path(job['output_dir']).parent / 'partial.txt'
            marker.write_text('attempt ' + str(number))
            self.experiments.finish(job['id'], worker, job['attempt_id'], 'failed')
            if number < 3:
                self.experiments.retry(job['id'], job['attempt_id'], 'retry-' + str(number))
                worker = 'worker-' + str(number)
                next_job = self.experiments.claim(worker)
                self.assertNotEqual(next_job['attempt_id'], job['attempt_id'])
                self.assertTrue(marker.exists())
                self.error(lambda: self.finish(first, 'failed'))
                job = next_job
        value = self.experiments.get(job['id'])
        self.assertEqual(value['experiment']['attempt_count'], 3)
        self.assertEqual([a['number'] for a in value['attempts']], [1, 2, 3])
        self.error(lambda: self.experiments.retry(job['id'], job['attempt_id'], 'retry-four'))

    def test_code_change_after_queue_fails_preparation_without_running_new_version(self):
        identity = self.create()
        with patch.object(self.experiments, '_code', return_value={'different': '0' * 64}):
            self.assertIsNone(self.experiments.claim('worker'))
        value = self.experiments.get(identity)
        self.assertEqual(value['experiment']['status'], 'failed')
        self.assertEqual(value['experiment']['attempt_count'], 1)
        self.assertIn('queued snapshot', value['experiment']['error'])

    def test_stale_recovery_requires_new_attempt_and_fences_old_owner(self):
        job = self.job()
        self.assertEqual(self.experiments.recover_stale(0), [job['id']])
        self.assertEqual(self.experiments.status(job['id'])['status'], 'interrupted')
        self.error(lambda: self.finish(job, 'failed'))
        self.experiments.retry(job['id'], job['attempt_id'], 'retry')
        retried = self.experiments.claim('new-worker')
        self.assertNotEqual(job['attempt_id'], retried['attempt_id'])
        self.error(lambda: self.finish(job, 'failed'))
        self.experiments.finish(retried['id'], 'new-worker', retried['attempt_id'], 'failed')

    def test_mutation_receipt_deletion_is_not_silently_rebuilt(self):
        job = self.job()
        self.finish(job, 'failed')
        self.experiments.retry(job['id'], job['attempt_id'], 'retry')
        self.sql("DELETE FROM industry_mom_receipts WHERE operation='retry'")
        self.error(lambda: self.experiments.get(job['id']))
        self.error(lambda: self.experiments.retry(job['id'], job['attempt_id'], 'retry'))

    def test_attempt_event_record_and_receipt_tamper_are_rejected(self):
        identity = self.create()
        for table, column in (('industry_mom_experiments', 'record_digest'), ('industry_mom_events', 'digest'), ('industry_mom_receipts', 'receipt_digest')):
            with self.subTest(table=table):
                saved = self.rows(table)
                self.sql('UPDATE ' + table + ' SET ' + column + "='tampered'")
                self.error(lambda: self.experiments.get(identity))
                with transaction(self.store.db_path) as connection:
                    for row in saved:
                        if 'id' in row:
                            connection.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE id=?', (row[column], row['id']))
                        else:
                            connection.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE operation=? AND idempotency_key=?',
                                               (row[column], row['operation'], row['idempotency_key']))
        job = self.experiments.claim('worker')
        self.sql("UPDATE industry_mom_attempts SET status='completed'")
        self.error(lambda: self.experiments.get(job['id']))

    def test_output_mutation_at_publish_is_failed_and_retained(self):
        job = self.job()
        self.calculate(job)
        original = self.experiments._verified
        def mutate(row):
            result = original(row)
            (Path(job['output_dir']) / 'report.md').write_text('changed after verify')
            return result
        with patch.object(self.experiments, '_verified', side_effect=mutate):
            self.finish(job)
        value = self.experiments.get(job['id'])
        self.assertEqual(value['experiment']['status'], 'failed')
        self.assertIsNone(value['review_target'])
        self.assertIn('Publication verification failed', value['experiment']['error'])

    def test_deadline_includes_independent_verification_and_publication(self):
        job = self.job()
        self.calculate(job)
        import paper_alpha.server.industry_mom as backend
        actual_time = backend.time.time()
        with patch.object(backend.time, 'time', return_value=actual_time + 61):
            self.finish(job)
        value = self.experiments.get(job['id'])
        self.assertEqual(value['experiment']['status'], 'failed')
        self.assertIn('deadline elapsed', value['experiment']['error'])

    def test_source_changes_during_completed_projection_fails_final_fence(self):
        job = self.job(); self.calculate(job); self.finish(job)
        import paper_alpha.server.industry_mom as backend
        original = backend._bytes
        def mutate(path, limit=backend.MAX_BYTES):
            value = original(path, limit)
            if Path(path) == Path(job['output_dir']) / 'report.md':
                source = self.sources._path(self.source['id']) / 'report.md'
                source.write_bytes(source.read_bytes() + b'modified after source verification')
            return value
        with patch.object(backend, '_bytes', side_effect=mutate):
            self.error(lambda: self.experiments.get(job['id']))

    def test_closed_http_contract_rejects_raw_paths_budget_and_retained_dates(self):
        base = {'source_id': self.source['id'], 'source_digest': self.source['digest'], 'idempotency_key': 'request'}
        IndustryMomCreate.model_validate(base)
        for key in ('artifact_path', 'config', 'budget', 'reserved'):
            with self.assertRaises(ValueError):
                IndustryMomCreate.model_validate(base | {key: 'untrusted'})
        with self.assertRaises(ValueError):
            IndustryMomRetry.model_validate({'expected_attempt_id': None, 'idempotency_key': 'retry'})

    def test_operation_ledger_bound_preserves_original_key_recovery(self):
        identity = self.create()
        # Verify source once; this test targets the transactional request
        # budget, not 32 repeated independent numerical source checks.
        source = self.experiments._source(self.experiments._fetch(identity))
        with patch.object(self.experiments, '_source', return_value=source):
            for number in range(32):
                self.experiments.cancel(identity, None, 'cancel-' + str(number))
            self.assertEqual(self.experiments.cancel(identity, None, 'cancel-0'), {'experiment_id': identity})
            self.error(lambda: self.experiments.cancel(identity, None, 'cancel-33'))
            self.assertEqual(self.experiments.cancel(identity, None, 'cancel-31'), {'experiment_id': identity})
        self.assertEqual(self.experiments.status(identity)['status'], 'cancelled')
        self.assertEqual(len(self.rows('industry_mom_events')), 33)
        self.assertEqual(len(self.rows('industry_mom_receipts')), 34)  # source + create + 32 mutations

    def test_json_decoding_uses_bounded_bytes_without_path_reopen(self):
        import paper_alpha.server.industry_mom as backend
        path = self.root / 'bounded.json'
        path.write_text('{"value": 1.5}', encoding='utf8')
        with patch.object(Path, 'read_text', side_effect=AssertionError('Unchecked path reopened')):
            self.assertEqual(backend._json(path), {'value': 1.5})
        for value in ('{"value":1,"value":2}', '{"value":1e999}', '{"value":NaN}'):
            path.write_text(value, encoding='utf8')
            with self.assertRaises(ValueError):
                backend._json(path)
        path.write_bytes(b' ' * (backend.MAX_JSON + 1))
        with self.assertRaises(ValueError):
            backend._json(path)


if __name__ == '__main__':
    unittest.main()
