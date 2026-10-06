"""Cross-domain real queue execution, scientific stops and durable ownership."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha import eligibility, monthly_workflow, research_protocol
from paper_alpha.server import domain_research_jobs as domain
from paper_alpha.server import monthly_runner
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import connect, transaction
from paper_alpha.server.domain_research_jobs import DomainResearchJobs, domain_execution_remaining_seconds
from paper_alpha.server.domain_research_jobs_schema import DomainResearchJob
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.research_jobs import JobServiceError
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import digest, json_text

CONFIG = {'schema_version': 1, 'start_month': '2025-01', 'end_month': '2025-02', 'cost_bps': 0, 'min_assets': 18}
BUDGET = {'max_steps': 12, 'max_failures': 2, 'max_seconds': 180}


class DomainJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.store = Store(self.root)
        self.jobs = DomainResearchJobs(self.store)
        self.monthly = MonthlyExperiments(self.store)
        self.protocol = ResearchProtocols(self.store).create('Explicit project fixture', '', research_protocol.presets()[1]['config'])
        self.source = {'kind': 'monthly_fixture', 'protocol_id': self.protocol['id'], 'protocol_digest': self.protocol['digest'], 'config': CONFIG}

    def create(self, **changes):
        return self.jobs.create(**(dict(source=self.source, budget=BUDGET, idempotency_key='job') | changes))

    def submit(self, job=None):
        job = job or self.create()
        for action in ('validate', 'submit'):
            step = self.jobs.advance(job['id'], action, action)
            self.assertIsNone(step['error'], step)
        return self.jobs.get(job['id'])

    def execute_worker(self):
        with worker_lock(self.root) as descriptor:
            worker = Worker(self.store, descriptor, poll_seconds=.01)
            self.assertTrue(worker.run_once())

    def finish_direct(self, job):
        child = self.monthly.claim('test-worker')
        self.assertIsNotNone(child)
        monthly_workflow.run(child['input_path'], child['output_dir'])
        self.monthly.finish(job['experiment_id'], 'test-worker', child['attempt_id'], 'completed')
        return child

    def test_actual_worker_matches_fixed_flow_and_has_no_human_approval(self):
        job = self.submit()
        self.assertEqual(self.monthly.list()['total'], 1)
        self.assertGreater(domain_execution_remaining_seconds(self.store, job['experiment_id']), 0)
        self.assertEqual(self.jobs.advance(job['id'], 'observe', 'waiting')['next_state'], 'submitted')
        self.execute_worker()
        self.assertEqual(self.jobs.advance(job['id'], 'observe', 'done')['next_state'], 'observed')
        complete = self.jobs.advance(job['id'], 'complete', 'complete')
        self.assertEqual(complete['next_state'], 'completed', complete)
        saved = DomainResearchJob.model_validate(self.jobs.get(job['id'])).model_dump()
        exact = json.loads(saved['result_json'])
        baseline = self.monthly.create(self.protocol['id'], self.protocol['digest'], CONFIG, 'fixed-baseline')['experiment_id']
        self.execute_worker()
        self.assertEqual(self.monthly.get(baseline)['result'], exact['result'])
        self.assertTrue(exact['verification']['verified'])
        self.assertFalse(saved['provider_connected'])
        self.assertEqual(saved['semantic_fidelity'], 'unverified')
        self.assertEqual(self.store._read('SELECT * FROM monthly_reviews'), [])
        self.assertEqual(self.jobs.markdown(job['id']), self.jobs.markdown(job['id']))
        self.assertEqual(self.jobs.advance(job['id'], 'complete', 'complete'), complete)
        self.assertIsNone(domain_execution_remaining_seconds(self.store, baseline))

    def test_validation_and_create_do_not_enqueue_monthly(self):
        job = self.create()
        self.assertEqual(self.monthly.list()['total'], 0)
        step = self.jobs.advance(job['id'], 'validate', 'validate')
        self.assertEqual(step['next_state'], 'validated')
        self.assertIn('paper_unresolved', json.loads(step['output_json']))
        self.assertEqual(self.monthly.list()['total'], 0)

    def test_paper_rules_and_both_author_screen_outcomes_are_stable_stops(self):
        paper = ResearchProtocols(self.store).create('Unresolved original', '', research_protocol.presets()[0]['config'])
        job = self.create(source={**self.source, 'protocol_id': paper['id'], 'protocol_digest': paper['digest']})
        step = self.jobs.advance(job['id'], 'validate', 'validate')
        self.assertEqual(step['error']['code'], 'RULES_UNRESOLVED')
        for threshold, expected in [(50000, 'DATA_INSUFFICIENT'), (1, 'AUTHOR_METHOD_UNRESOLVED')]:
            scan = deepcopy(eligibility.demo_scan())
            scan['plan']['minimum_assets'] = threshold
            scan['plan_digest'] = digest(scan['plan'])
            study = AuthorStudies(self.store).create('Declared synthetic aggregate', '', scan, None, [], str(threshold))
            job = self.create(source={'kind': 'author_study_diagnostic', 'study_id': study['id'], 'study_digest': study['digest']}, idempotency_key='author-' + str(threshold))
            step = self.jobs.advance(job['id'], 'validate', 'validate')
            self.assertEqual(step['next_state'], 'blocked')
            self.assertEqual(step['error']['code'], expected)
            diagnostic = json.loads(step['output_json'])
            self.assertFalse(diagnostic['execution_ready'])
            self.assertFalse(diagnostic['raw_source_reverified'])
            self.assertEqual(self.jobs.advance(job['id'], 'validate', 'validate'), step)
            with self.assertRaises(JobServiceError):
                self.jobs.advance(job['id'], 'submit', 'illegal')
        self.assertEqual(self.monthly.list()['total'], 0)

    def test_closed_source_wrong_digest_and_arbitrary_authority_rejected(self):
        for extra in ('path', 'execute_python', 'approved', 'max_seconds'):
            with self.assertRaises(JobServiceError):
                self.create(source={**self.source, extra: '/tmp/source'})
        with self.assertRaises(JobServiceError):
            self.create(source={**self.source, 'protocol_digest': '0' * 64})
        for action in ('retry', 'approve', 'execute_author_portfolio'):
            with self.assertRaises(JobServiceError):
                self.jobs.advance(self.create()['id'], action, 'bad')
        self.assertEqual(self.monthly.list()['total'], 0)

    def test_create_replay_concurrent_and_changed_body_conflict(self):
        gate = threading.Barrier(4)
        def invoke(_):
            gate.wait(timeout=10)
            return self.create()
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(invoke, range(4)))
        self.assertEqual(len({job['id'] for job in jobs}), 1)
        self.assertEqual(self.jobs.list()['total'], 1)
        with self.assertRaises(JobServiceError):
            self.create(budget={**BUDGET, 'max_seconds': 600})
        self.assertEqual(self.jobs.list(source_kind='author_study_diagnostic')['total'], 0)

    def test_uncertain_submit_replays_same_effect_after_deadline(self):
        job = self.create()
        self.jobs.advance(job['id'], 'validate', 'validate')
        original = MonthlyExperiments.create
        def lost(service, *args):
            original(service, *args)
            raise sqlite3.OperationalError('Lost response after commit')
        with patch.object(MonthlyExperiments, 'create', lost):
            with self.assertRaises(JobServiceError) as caught:
                self.jobs.advance(job['id'], 'submit', 'submit')
        self.assertEqual(caught.exception.code, 'EFFECT_UNCERTAIN')
        self.assertEqual(self.monthly.list()['total'], 1)
        with patch.object(domain, '_elapsed', return_value=181):
            step = self.jobs.advance(job['id'], 'submit', 'submit')
        self.assertEqual(step['next_state'], 'exhausted')
        self.assertIsNotNone(json.loads(step['output_json'])['experiment_id'])
        self.assertEqual(self.monthly.list()['total'], 1)

    def test_real_process_crash_after_queue_commit_has_one_effect(self):
        job = self.create()
        self.jobs.advance(job['id'], 'validate', 'validate')
        code = """
import os, sys
from paper_alpha.server.service import Store
from paper_alpha.server.domain_research_jobs import DomainResearchJobs
from paper_alpha.server.monthly_experiments import MonthlyExperiments
original = MonthlyExperiments.create
def crash(self, *args):
    original(self, *args)
    os._exit(23)
MonthlyExperiments.create = crash
DomainResearchJobs(Store(sys.argv[1])).advance(sys.argv[2], 'submit', 'submit')
"""
        process = subprocess.run([sys.executable, '-c', code, str(self.root), job['id']], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 23, process.stderr.decode())
        restored = DomainResearchJobs(Store(self.root))
        step = restored.advance(job['id'], 'submit', 'submit')
        self.assertEqual(step['next_state'], 'submitted')
        self.assertEqual(self.monthly.list()['total'], 1)
        self.assertEqual(len(restored.get(job['id'])['steps']), 2)

    def test_unacknowledged_submit_losing_three_anchors_cannot_become_ordinary(self):
        job = self.create(); self.jobs.advance(job['id'], 'validate', 'validate')
        original = MonthlyExperiments.create
        def lost(service, *args):
            original(service, *args)
            raise sqlite3.OperationalError('Lost queue acknowledgement')
        with patch.object(MonthlyExperiments, 'create', lost):
            with self.assertRaises(JobServiceError):
                self.jobs.advance(job['id'], 'submit', 'submit')
        identity = self.monthly.list()['items'][0]['id']
        self.assertIsNone(self.jobs.get(job['id'])['experiment_id'])
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM domain_monthly_effects')
            connection.execute("DELETE FROM monthly_receipts WHERE operation='create'")
            connection.execute("DELETE FROM monthly_events WHERE kind='domain_parent_bound'")
        with self.assertRaises(JobServiceError) as caught:
            domain_execution_remaining_seconds(self.store, identity)
        self.assertEqual(caught.exception.code, 'PARENT_INTEGRITY')
        self.assertIsNone(self.monthly.claim('worker'))
        self.assertEqual(self.monthly.status(identity)['status'], 'failed')

    def test_parse_bounds_and_nonobject_step_output_fail_closed(self):
        with self.assertRaises(ValueError):
            domain._parse(' ' * (domain.MAX_BODY + 1), domain.MAX_BODY)
        job = self.submit()
        with transaction(self.store.db_path) as connection:
            row = connection.execute('SELECT payload,id FROM domain_research_job_steps WHERE sequence=2').fetchone()
            step = json.loads(row['payload']); step['output_json'] = '[]'
            connection.execute('UPDATE domain_research_job_steps SET payload=?,digest=? WHERE id=?', (json_text(step), digest(step), row['id']))
            anchors = [r[0] for r in connection.execute('SELECT digest FROM domain_research_job_steps ORDER BY sequence')]
            connection.execute('UPDATE domain_research_jobs SET ledger_digest=?', (digest(anchors),))
        with self.assertRaises(JobServiceError) as caught:
            self.jobs.get(job['id'])
        self.assertEqual(caught.exception.code, 'LEDGER_INTEGRITY')
        self.assertIsNone(self.monthly.claim('worker'))
        self.assertEqual(self.monthly.status(job['experiment_id'])['status'], 'failed')

    def test_concurrent_submit_cannot_duplicate_effect(self):
        job = self.create(); self.jobs.advance(job['id'], 'validate', 'validate')
        gate = threading.Barrier(4)
        def invoke(_):
            gate.wait(timeout=10)
            try:
                return self.jobs.advance(job['id'], 'submit', 'submit')['next_state']
            except JobServiceError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(invoke, range(4)))
        self.assertIn('submitted', outcomes)
        self.assertLessEqual(set(outcomes), {'submitted', 'JOB_BUSY'})
        self.assertEqual(self.monthly.list()['total'], 1)

    def test_queued_expiry_cannot_launch_or_publish(self):
        job = self.submit()
        with patch.object(domain, '_elapsed', return_value=181):
            self.assertIsNone(self.monthly.claim('worker'))
        detail = self.monthly.get(job['experiment_id'])
        self.assertEqual(detail['experiment']['status'], 'failed')
        self.assertIsNone(detail['result'])
        self.assertEqual(detail['attempts'][0]['status'], 'failed')

    def test_publish_expiry_retains_files_but_no_registered_success(self):
        job = self.submit()
        child = self.monthly.claim('worker')
        monthly_workflow.run(child['input_path'], child['output_dir'])
        with patch.object(domain, '_elapsed', return_value=181):
            self.monthly.finish(job['experiment_id'], 'worker', child['attempt_id'], 'completed')
        detail = self.monthly.get(job['experiment_id'])
        self.assertEqual(detail['experiment']['status'], 'failed')
        self.assertIsNone(detail['result'])
        self.assertTrue((Path(child['output_dir']) / 'result.json').is_file())
        self.assertIn('deadline', detail['experiment']['error'])

    def test_actual_running_child_is_stopped_by_original_wall_deadline(self):
        job = self.submit(self.create(budget={**BUDGET, 'max_seconds': 2}))
        marker = Path(self.temp.name).resolve() / 'child-started'
        child_code = 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(20)'
        with worker_lock(self.root) as descriptor:
            worker = Worker(self.store, descriptor, poll_seconds=.01)
            with patch.object(monthly_runner, 'command', return_value=[sys.executable, '-c', child_code, str(marker)]):
                self.assertTrue(worker.run_once())
        detail = self.monthly.get(job['experiment_id'])
        self.assertTrue(marker.exists())
        self.assertEqual(detail['experiment']['status'], 'failed')
        self.assertIn('deadline', detail['experiment']['error'])
        self.assertIsNone(detail['result'])
        self.assertEqual(self.jobs.get(job['id'])['state'], 'exhausted')

    def test_serviceerror_verification_finishes_failed_instead_of_running(self):
        # Exercises the previously unhandled ServiceError from _verified.
        identity = self.monthly.create(self.protocol['id'], self.protocol['digest'], CONFIG, 'legacy')['experiment_id']
        child = self.monthly.claim('worker')
        self.monthly.finish(identity, 'worker', child['attempt_id'], 'completed')
        detail = self.monthly.get(identity)
        self.assertEqual(detail['experiment']['status'], 'failed')
        self.assertEqual(detail['attempts'][0]['status'], 'failed')
        self.assertTrue(Path(child['input_path']).exists())

    def test_deleted_submit_budget_or_any_anchor_cannot_remove_worker_cap(self):
        # Every mutation is on a disposable independent workspace.
        for part in ('step', 'binding', 'marker', 'receipt', 'budget', 'three_anchors', 'all_monthly_anchors'):
            with self.subTest(part=part):
                store = Store(Path(self.temp.name) / part)
                protocol = ResearchProtocols(store).create('Fixture', '', research_protocol.presets()[1]['config'])
                jobs = DomainResearchJobs(store)
                source = {**self.source, 'protocol_id': protocol['id'], 'protocol_digest': protocol['digest']}
                job = jobs.create(source, BUDGET, 'job')
                jobs.advance(job['id'], 'validate', 'validate'); jobs.advance(job['id'], 'submit', 'submit')
                job = jobs.get(job['id'])
                with transaction(store.db_path) as connection:
                    if part == 'step':
                        connection.execute('DELETE FROM domain_research_job_steps WHERE sequence=2')
                    elif part == 'binding':
                        connection.execute('DELETE FROM domain_monthly_effects')
                    elif part == 'marker':
                        connection.execute("DELETE FROM monthly_events WHERE kind='domain_parent_bound'")
                    elif part == 'receipt':
                        connection.execute("DELETE FROM monthly_receipts WHERE operation='create'")
                    elif part in {'three_anchors', 'all_monthly_anchors'}:
                        connection.execute('DELETE FROM domain_monthly_effects')
                        connection.execute("DELETE FROM monthly_receipts WHERE operation='create'")
                        connection.execute("DELETE FROM monthly_events WHERE kind='domain_parent_bound'")
                        if part == 'all_monthly_anchors':
                            connection.execute("DELETE FROM monthly_events WHERE kind='queued'")
                    else:
                        row = connection.execute('SELECT payload FROM domain_research_jobs').fetchone()
                        body = json.loads(row['payload']); body['request']['budget']['max_seconds'] = 600
                        connection.execute('UPDATE domain_research_jobs SET payload=?', (json_text(body),))
                with self.assertRaises(JobServiceError) as caught:
                    domain_execution_remaining_seconds(store, job['experiment_id'])
                self.assertEqual(caught.exception.code, 'PARENT_INTEGRITY')
                self.assertIsNone(MonthlyExperiments(store).claim('worker'))
                self.assertEqual(MonthlyExperiments(store).status(job['experiment_id'])['status'], 'failed')

    def test_unique_safety_cancel_after_budget_and_uncertain_stop(self):
        job = self.submit(self.create(budget={**BUDGET, 'max_steps': 2}))
        with patch.object(MonthlyExperiments, 'cancel', side_effect=sqlite3.OperationalError('Busy')):
            with self.assertRaises(JobServiceError):
                self.jobs.cancel(job['id'], 'stop')
        stopped = self.jobs.cancel(job['id'], 'stop')
        self.assertEqual(stopped['next_state'], 'cancelled')
        self.assertEqual(self.jobs.cancel(job['id'], 'stop'), stopped)
        with self.assertRaises(JobServiceError):
            self.jobs.cancel(job['id'], 'new-stop')
        self.assertEqual(self.monthly.status(job['experiment_id'])['status'], 'cancelled')
        self.assertEqual(len(self.jobs.get(job['id'])['steps']), 3)

    def test_cancel_abandoned_submit_never_creates_missing_experiment(self):
        job = self.create(); self.jobs.advance(job['id'], 'validate', 'validate')
        with patch.object(MonthlyExperiments, 'create', side_effect=sqlite3.OperationalError('No effect')):
            with self.assertRaises(JobServiceError):
                self.jobs.advance(job['id'], 'submit', 'submit')
        stopped = self.jobs.cancel(job['id'], 'stop')
        self.assertEqual(stopped['next_state'], 'cancelled')
        self.assertEqual(self.monthly.list()['total'], 0)

    def test_cancellation_cannot_adopt_external_retry_attempt(self):
        job = self.submit(); child = self.monthly.claim('worker')
        self.monthly.finish(job['experiment_id'], 'worker', child['attempt_id'], 'failed', 'Fixture failure')
        self.monthly.retry(job['experiment_id'], child['attempt_id'], 'manual-retry')
        second = self.monthly.claim('worker')
        stopped = self.jobs.cancel(job['id'], 'stop')
        self.assertEqual(stopped['error']['code'], 'ATTEMPT_CHANGED')
        self.assertEqual(self.monthly.status(job['experiment_id'])['status'], 'running')
        self.monthly.finish(job['experiment_id'], 'worker', second['attempt_id'], 'failed', 'Cleanup')

    def test_backup_restore_preserves_exact_job_and_effect_receipts(self):
        job = self.submit(); stopped = self.jobs.cancel(job['id'], 'stop')
        backup = Path(self.temp.name).resolve() / 'backup'
        create_backup(self.root, backup)
        restored_root = Path(self.temp.name).resolve() / 'restored'
        restore_backup(backup, restored_root)
        restored = DomainResearchJobs(Store(restored_root))
        self.assertEqual(restored.advance(job['id'], 'cancel', 'stop'), stopped)
        self.assertEqual(restored.get(job['id'])['experiment_id'], job['experiment_id'])
        self.assertEqual(restored.markdown(job['id']), self.jobs.markdown(job['id']))


if __name__ == '__main__':
    unittest.main()
