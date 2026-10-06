"""Actual version/queue effects, uncertain acknowledgements and bounded jobs."""
from concurrent.futures import ThreadPoolExecutor
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

from paper_alpha.server.service import Store
from paper_alpha.server.db import transaction
from paper_alpha.server.research_jobs import ResearchJobs, JobServiceError, execution_remaining_seconds
from paper_alpha.server.research_jobs_schema import ResearchJob, ResearchJobCreate
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.backup import create_backup, restore_backup


def alpha101_draft(task):
    return {name: [deepcopy(item) for item in task[name] if item['id'] == identity]
            for name, identity in [('evidence', 'alpha101-formula'), ('hypotheses', 'h-alpha101'), ('candidates', 'alpha101')]}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'workspace')
        self.seed = self.store.seed_example()
        self.research = self.store.get_research(self.seed['research_id'])
        self.base = self.research['revisions'][0]
        self.jobs = ResearchJobs(self.store)
        self.draft = alpha101_draft(self.base['task'])

    def create(self, **changes):
        args = dict(research_id=self.seed['research_id'], base_revision_id=self.seed['revision_id'], draft=self.draft,
                    budget={'max_steps': 10, 'max_failures': 3, 'max_seconds': 180}, idempotency_key='job')
        args.update(changes)
        return self.jobs.create(**args)

    def advance_to_submit(self, job):
        for action in ('validate', 'commit', 'submit'):
            step = self.jobs.advance(job['id'], action, action)
            self.assertIsNone(step['error'], step)
        return self.jobs.get(job['id'])

    def test_real_job_reuses_revision_worker_result_and_never_human_approval(self):
        job = self.advance_to_submit(self.create())
        waiting = self.jobs.advance(job['id'], 'observe', 'pending')
        self.assertEqual(waiting['next_state'], 'submitted')
        self.assertGreater(execution_remaining_seconds(self.store, job['run_id']), 0)
        with worker_lock(self.store.root) as descriptor:
            worker = Worker(self.store, descriptor)
            self.assertTrue(worker.run_once())
        observed = self.jobs.advance(job['id'], 'observe', 'done')
        self.assertEqual(observed['next_state'], 'observed')
        completed = self.jobs.advance(job['id'], 'complete', 'complete')
        self.assertEqual(completed['next_state'], 'completed', completed)
        value = ResearchJob.model_validate(self.jobs.get(job['id'])).model_dump()
        result = json.loads(value['result_json'])
        self.assertTrue(result['verification']['verified'])
        self.assertEqual(result['reviews'], [])
        self.assertEqual(value['semantic_fidelity'], 'unverified')
        self.assertEqual(self.jobs.markdown(job['id']), self.jobs.markdown(job['id']))
        self.assertEqual(self.jobs.advance(job['id'], 'complete', 'complete'), completed)
        self.assertEqual(len(self.store.list_runs()), 1)
        context = json.loads(value['context_json'])
        self.assertNotIn('daily', context)
        self.assertIn('raw_market_rows', context['omissions'])
        self.assertIsNone(execution_remaining_seconds(self.store, 'no-such-run'))

    def test_illegal_expression_bad_quote_and_false_origin_block_before_commit(self):
        for name, change in [('unsafe', lambda d: d['candidates'][0].update(expression="__import__('os').system('ls')")),
                             ('quote', lambda d: d['evidence'][0].update(quote='Not present in the PDF')),
                             ('origin', lambda d: d['candidates'][0].update(expression='close - open')),
                             ('hypothesis', lambda d: d['hypotheses'][0].update(claim='Invented paper claim'))]:
            draft = deepcopy(self.draft); change(draft)
            job = self.create(draft=draft, idempotency_key=name)
            result = self.jobs.advance(job['id'], 'validate', name)
            self.assertEqual(result['next_state'], 'blocked', result)
            self.assertEqual(len(self.store.get_research(self.seed['research_id'])['revisions']), 1)
            self.assertEqual(self.store.list_runs(), [])

    def test_partial_execution_requires_explicit_authorization_and_preserves_exclusions(self):
        draft = {k: deepcopy(self.base['task'][k]) for k in ('evidence', 'hypotheses', 'candidates')}
        closed = self.create(draft=draft)
        self.assertEqual(self.jobs.advance(closed['id'], 'validate', 'check')['next_state'], 'blocked')
        partial = self.create(draft=draft, allow_partial_execution=True, idempotency_key='partial')
        step = self.jobs.advance(partial['id'], 'validate', 'check')
        self.assertEqual(step['next_state'], 'validated', step)
        checks = self.jobs.get(partial['id'])['candidate_checks']
        self.assertEqual([c['candidate_id'] for c in checks if c['status'] == 'blocked'], ['alpha005'])
        self.assertEqual(checks[0]['normalization'], 'documented_operator_alias')
        accepted = json.loads(step['output_json'])['task']['candidates']
        self.assertEqual([c['id'] for c in accepted], ['alpha006', 'alpha101'])
        self.assertEqual(self.jobs.get(partial['id'])['draft'], draft)

    def test_missing_data_and_unresolved_rules_are_scientific_stops(self):
        draft = deepcopy(self.draft)
        draft['hypotheses'][0]['required_fields'].append('vwap')
        draft['hypotheses'][0]['attribution'] = 'user_modification'
        job = self.create(draft=draft)
        step = self.jobs.advance(job['id'], 'validate', 'check')
        self.assertEqual(step['next_state'], 'blocked')
        self.assertEqual(json.loads(step['output_json'])['candidate_checks'][0]['code'], 'required_field_missing')
        unresolved = self.create(method_status='unresolved', unresolved_rules=['Unknown publication timing'], idempotency_key='unresolved')
        stopped = self.jobs.advance(unresolved['id'], 'validate', 'check')
        self.assertEqual(stopped['error']['code'], 'RULES_UNRESOLVED')

    def test_step_budget_replay_and_error_budget_persist_across_restart(self):
        job = self.create(budget={'max_steps': 1, 'max_failures': 1, 'max_seconds': 180})
        first = self.jobs.advance(job['id'], 'validate', 'one')
        restored = ResearchJobs(Store(self.store.root))
        self.assertEqual(restored.advance(job['id'], 'validate', 'one'), first)
        self.assertEqual(restored.get(job['id'])['state'], 'exhausted')
        with self.assertRaises(JobServiceError) as raised:
            restored.advance(job['id'], 'commit', 'new')
        self.assertEqual(raised.exception.code, 'JOB_EXHAUSTED')
        with self.assertRaises(JobServiceError):
            self.create(budget={'max_steps': 100, 'max_failures': 10, 'max_seconds': 600})
        retry_job = self.create(idempotency_key='errors', budget={'max_steps': 10, 'max_failures': 1, 'max_seconds': 180})
        with patch.object(self.jobs, '_validate', side_effect=sqlite3.OperationalError('busy')):
            failed = self.jobs.advance(retry_job['id'], 'validate', 'failed')
        self.assertTrue(failed['error']['retryable'])
        self.assertEqual(self.jobs.get(retry_job['id'])['state'], 'exhausted')

    def test_commit_acknowledgement_loss_replays_saved_effect_key_even_after_deadline(self):
        job = self.create()
        self.jobs.advance(job['id'], 'validate', 'validate')
        original = self.store.create_revision
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt
        with patch.object(self.store, 'create_revision', side_effect=lost), self.assertRaises(KeyboardInterrupt):
            self.jobs.advance(job['id'], 'commit', 'commit')
        self.assertEqual(len(self.store.get_research(self.seed['research_id'])['revisions']), 2)
        restored = ResearchJobs(Store(self.store.root))
        with patch('paper_alpha.server.research_jobs._wall_seconds', return_value=200):
            step = restored.advance(job['id'], 'commit', 'commit')
        self.assertEqual(step['next_state'], 'exhausted')
        self.assertIsNotNone(json.loads(step['output_json'])['revision_id'])
        self.assertEqual(len(self.store.get_research(self.seed['research_id'])['revisions']), 2)

    def test_submit_uncertain_database_ack_reconciles_same_key_without_duplicate_run(self):
        job = self.create()
        for action in ('validate', 'commit'):
            self.jobs.advance(job['id'], action, action)
        original = self.store.submit_run
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError('lost acknowledgement')
        with patch.object(self.store, 'submit_run', side_effect=lost), self.assertRaises(JobServiceError) as raised:
            self.jobs.advance(job['id'], 'submit', 'submit')
        self.assertEqual(raised.exception.code, 'EFFECT_UNCERTAIN')
        step = ResearchJobs(Store(self.store.root)).advance(job['id'], 'submit', 'submit')
        self.assertEqual(step['next_state'], 'submitted')
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(self.jobs.get(job['id'])['usage']['steps'], 3)

    def test_real_process_exit_after_effect_retains_key_and_no_duplicate_revision(self):
        job = self.create()
        self.jobs.advance(job['id'], 'validate', 'validate')
        script = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
from paper_alpha.server.service import Store
from paper_alpha.server.research_jobs import ResearchJobs
store=Store(Path(sys.argv[1])); original=store.create_revision
def lost(*args,**kwargs):
    original(*args,**kwargs); os._exit(24)
with patch.object(store,'create_revision',side_effect=lost):
    ResearchJobs(store).advance(sys.argv[2],'commit','commit')
'''
        process = subprocess.run([sys.executable, '-c', script, str(self.store.root), job['id']], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 24, process.stderr)
        replay = ResearchJobs(Store(self.store.root)).advance(job['id'], 'commit', 'commit')
        self.assertEqual(replay['next_state'], 'committed')
        self.assertEqual(len(self.store.get_research(self.seed['research_id'])['revisions']), 2)

    def test_deleted_ledger_tail_does_not_replenish_budget(self):
        job = self.create()
        self.jobs.advance(job['id'], 'validate', 'validate')
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM research_job_steps WHERE job_id=?', (job['id'],))
        with self.assertRaises(JobServiceError) as raised:
            self.jobs.get(job['id'])
        self.assertEqual(raised.exception.code, 'LEDGER_INTEGRITY')

    def test_cancel_queued_run_and_backup_preserve_job_receipts(self):
        job = self.advance_to_submit(self.create())
        step = self.jobs.cancel(job['id'], 'cancel')
        self.assertEqual(step['next_state'], 'cancelled')
        self.assertEqual(self.store.get_run_status(job['run_id'])['status'], 'cancelled')
        base = Path(self.temp.name).resolve()
        create_backup(self.store.root, base / 'backup')
        restore_backup(base / 'backup', base / 'restored')
        restored = ResearchJobs(Store(base / 'restored'))
        self.assertEqual(restored.advance(job['id'], 'cancel', 'cancel'), step)
        self.assertEqual(restored.get(job['id'])['usage']['steps'], 4)

    def test_concurrent_advance_cannot_duplicate_effect(self):
        job = self.create()
        entered, release = threading.Event(), threading.Event()
        original = self.jobs._validate
        def pause(*args):
            entered.set(); self.assertTrue(release.wait(10)); return original(*args)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.jobs, '_validate', side_effect=pause):
            pending = pool.submit(self.jobs.advance, job['id'], 'validate', 'same')
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(JobServiceError) as raised:
                    ResearchJobs(self.store).advance(job['id'], 'validate', 'same')
                self.assertEqual(raised.exception.code, 'JOB_BUSY')
            finally:
                release.set()
            result = pending.result(timeout=15)
        self.assertEqual(self.jobs.advance(job['id'], 'validate', 'same'), result)
        self.assertEqual(self.jobs.get(job['id'])['usage']['steps'], 1)

    def test_source_change_and_closed_schema_reject_unsafe_inputs(self):
        job = self.create()
        path = Path(self.store._fetch('datasets', self.research['dataset_id'])['data_path'])
        path.write_bytes(path.read_bytes() + b'\n')
        self.assertEqual(self.jobs.advance(job['id'], 'validate', 'validate')['error']['code'], 'SOURCE_INTEGRITY')
        value = dict(research_id=self.seed['research_id'], base_revision_id=self.seed['revision_id'],
                     draft=self.draft, budget={'max_steps': 10, 'max_failures': 3, 'max_seconds': 180}, idempotency_key='closed')
        with self.assertRaises(ValueError):
            ResearchJobCreate.model_validate({**value, 'path': '/etc/passwd'})
        with self.assertRaises(JobServiceError):
            self.jobs.advance(job['id'], 'execute_python', 'arbitrary')

    def test_worker_cap_verifies_parent_and_survives_deleted_submit_binding(self):
        job = self.advance_to_submit(self.create())
        row = self.store._fetch('research_jobs', job['id'])
        body = json.loads(row['payload'])
        body['request']['budget']['max_seconds'] = 600
        from paper_alpha.storage import json_text
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json_text(body), job['id']))
        with self.assertRaises(JobServiceError) as raised:
            execution_remaining_seconds(self.store, job['run_id'])
        self.assertEqual(raised.exception.code, 'LEDGER_INTEGRITY')
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_jobs SET payload=? WHERE id=?', (row['payload'], job['id']))
            connection.execute('DELETE FROM research_job_steps WHERE job_id=? AND sequence=3', (job['id'],))
        with self.assertRaises(JobServiceError):
            execution_remaining_seconds(self.store, job['run_id'])

    def test_safety_cancel_after_step_budget_stops_owned_queue_without_new_experiment(self):
        job = self.create(budget={'max_steps': 3, 'max_failures': 2, 'max_seconds': 180})
        job = self.advance_to_submit(job)
        self.assertEqual(job['state'], 'exhausted')
        report = self.jobs.markdown(job['id'])
        self.assertEqual(report, self.jobs.markdown(job['id']))
        stopped = self.jobs.cancel(job['id'], 'safety-stop')
        self.assertEqual(stopped['next_state'], 'cancelled')
        self.assertEqual(self.store.get_run_status(job['run_id'])['status'], 'cancelled')
        self.assertEqual(self.jobs.get(job['id'])['usage']['steps'], 4)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_late_complete_retains_exact_result_but_cannot_claim_success(self):
        job = self.advance_to_submit(self.create())
        with worker_lock(self.store.root) as descriptor:
            Worker(self.store, descriptor).run_once()
        self.jobs.advance(job['id'], 'observe', 'observed')
        original = self.store.get_run
        calls = []
        def clock(_):
            return 200.0 if calls else 1.0
        def slow(*args):
            result = original(*args); calls.append(True); return result
        with patch.object(self.store, 'get_run', side_effect=slow), patch('paper_alpha.server.research_jobs._wall_seconds', side_effect=clock):
            step = self.jobs.advance(job['id'], 'complete', 'late-complete')
        self.assertEqual(step['next_state'], 'exhausted')
        self.assertEqual(step['error']['code'], 'BUDGET_EXHAUSTED')
        self.assertIsNotNone(json.loads(step['output_json'])['result_json'])

    def test_cancel_abandoned_submit_does_not_create_the_missing_effect(self):
        job = self.create()
        for action in ('validate', 'commit'):
            self.jobs.advance(job['id'], action, action)
        with patch.object(self.store, 'submit_run', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.jobs.advance(job['id'], 'submit', 'submit')
        self.assertEqual(self.store.list_runs(), [])
        cancelled = ResearchJobs(Store(self.store.root)).cancel(job['id'], 'cancel')
        self.assertEqual(cancelled['next_state'], 'cancelled')
        self.assertEqual(self.store.list_runs(), [])
        replay = self.jobs.advance(job['id'], 'submit', 'submit')
        self.assertEqual(replay['status'], 'interrupted')
        self.assertEqual(replay['error']['code'], 'CANCELLED_BEFORE_EFFECT')

    def test_uncertain_safety_cancel_reuses_original_receipt_and_never_corrupts_budget(self):
        job = self.advance_to_submit(self.create(budget={'max_steps': 3, 'max_failures': 2, 'max_seconds': 180}))
        with patch.object(self.store, 'cancel_run', side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(JobServiceError) as raised:
                self.jobs.cancel(job['id'], 'original-stop')
            self.assertEqual(raised.exception.code, 'EFFECT_UNCERTAIN')
        pending = self.jobs.get(job['id'])
        self.assertEqual(pending['usage']['steps'], 4)
        self.assertEqual(pending['steps'][-1]['status'], 'running')
        stopped = ResearchJobs(Store(self.store.root)).cancel(job['id'], 'original-stop')
        self.assertEqual(stopped['next_state'], 'cancelled')
        self.assertEqual(self.jobs.cancel(job['id'], 'original-stop'), stopped)
        with self.assertRaises(JobServiceError):
            self.jobs.cancel(job['id'], 'new-stop')
        self.assertEqual(self.jobs.get(job['id'])['usage']['steps'], 4)
        self.assertEqual(self.store.get_run_status(job['run_id'])['status'], 'cancelled')

    def test_failed_safety_cancel_cannot_append_a_second_over_budget_receipt(self):
        job = self.advance_to_submit(self.create(budget={'max_steps': 3, 'max_failures': 2, 'max_seconds': 180}))
        from paper_alpha.server.service import ServiceError
        with patch.object(self.store, 'cancel_run', side_effect=ServiceError('Permanent cancellation rejection')):
            failed = self.jobs.cancel(job['id'], 'stop')
        self.assertIsNotNone(failed['error'])
        self.assertEqual(self.jobs.cancel(job['id'], 'stop'), failed)
        with self.assertRaises(JobServiceError) as raised:
            self.jobs.cancel(job['id'], 'second-stop')
        self.assertEqual(raised.exception.code, 'STOP_BUDGET_EXHAUSTED')
        self.assertEqual(self.jobs.get(job['id'])['usage']['steps'], 4)


if __name__ == '__main__':
    unittest.main()
