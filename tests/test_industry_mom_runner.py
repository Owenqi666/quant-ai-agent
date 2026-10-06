"""Actual child supervision and inherited-lock crash recovery, isolated only."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from paper_alpha import mom_only_workflow as workflow
from paper_alpha.server import industry_mom_runner as runner
from paper_alpha.server.industry_mom import IndustryMomExperiments, IndustryMomSources
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import Store
from paper_alpha.storage import read_json
import tests.test_mom_only_workflow as synthetic

PROJECT = Path(__file__).resolve().parents[1]


class IndustryMomRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = synthetic.WorkflowIntegrityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture._run('original')
        self.store = Store(self.root / 'workspace')
        self.sources = IndustryMomSources(self.store)
        self.experiments = IndustryMomExperiments(self.store)
        self.source = self.sources.register(self.root / 'original', 'Synthetic runner fixture', '', 'source')

    def queue(self):
        return self.experiments.create(self.source['id'], self.source['digest'], 'experiment')['experiment_id']

    def sleepy_command(self, marker):
        return [sys.executable, '-B', '-c',
            'import os,time;from pathlib import Path;'
            'Path(' + repr(str(marker)) + ').write_text(str(os.getpid()));time.sleep(30)']

    def await_file(self, path, process=None, seconds=8):
        end = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < end:
            if process is not None and process.poll() is not None:
                self.fail('Supervisor exited before launching child: ' + str(process.returncode))
            time.sleep(.01)
        self.assertTrue(path.exists(), 'Child marker was not written')

    def test_command_installed_module_b_and_owned_inputs(self):
        self.queue()
        job = self.experiments.claim('worker')
        command = runner.command(job)
        self.assertEqual(command[:5], [sys.executable, '-B', '-m', 'paper_alpha.mom_only_workflow', 'run'])
        for name in ('source_path', 'config_path', 'method_path', 'evidence_dir', 'output_dir'):
            self.assertIn(job[name], command)
            self.assertTrue(Path(job[name]).is_relative_to(self.store.root))
        self.assertNotIn(str(self.root / 'original'), command)

    def test_real_subprocess_computes_then_verifies_and_publishes(self):
        identity = self.queue()
        # Child replaces only evidence stubs/fixed hashes, exactly as the
        # established synthetic workflow fixture. Computation is installed
        # parser/MOM/reference/report code, not mocked or a saved source tree.
        program = '''
import hashlib,json,sys
from pathlib import Path
from unittest.mock import patch
from paper_alpha import mom_only_workflow as w
source,config,method,out,sha,evidence=sys.argv[1:]
m=w.read_json(method)
def stub(destination,body,evidence_dir=None):
    (destination/'inputs/paper.pdf').write_bytes(b'synthetic test paper; not the original source')
    for item in body['sources']:
        p=destination/'inputs/author-code'/Path(item['path']).name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(('synthetic author stub '+item['id']).encode())
with patch.object(w,'PAPER_SHA256',m['paper']['document_sha256']),patch.object(w,'SUPPORTED_METHOD_DIGEST',m['contract_digest']),patch.object(w,'_verify_paper_anchors',return_value=None),patch.object(w,'_evidence_snapshot',side_effect=stub):
    value=w.run(source,config,method,out,sha,evidence_dir=evidence)
    print(json.dumps(value))
'''
        with worker_lock(self.store.root) as descriptor:
            worker = Worker(self.store, descriptor, poll_seconds=.02)
            job = worker.industry_mom.claim(worker.worker_id)
            command = [sys.executable, '-B', '-c', program, job['source_path'], job['config_path'], job['method_path'],
                       job['output_dir'], job['source_sha256'], job['evidence_dir']]
            with patch.object(runner, 'command', return_value=command):
                self.assertEqual(runner.execute(worker, job), 'completed')
        detail = self.experiments.get(identity)
        self.assertTrue(detail['verification']['calculation_verified'])
        self.assertEqual(detail['verification']['human_review'], 'pending')
        self.assertTrue((Path(job['output_dir']).parent / 'worker.stdout.log').is_file())
        self.assertFalse(detail['verification']['reserved_evaluated'])

    def test_running_cancel_stops_actual_child_and_keeps_logs(self):
        identity = self.queue()
        marker = self.root / 'child.pid'
        with worker_lock(self.store.root) as descriptor:
            worker = Worker(self.store, descriptor, poll_seconds=.01)
            job = worker.industry_mom.claim(worker.worker_id)
            failures, result = [], []
            def execute():
                try:
                    result.append(runner.execute(worker, job))
                except Exception as exc:
                    failures.append(exc)
            with patch.object(runner, 'command', return_value=self.sleepy_command(marker)):
                thread = threading.Thread(target=execute)
                thread.start()
                self.await_file(marker)
                self.experiments.cancel(identity, job['attempt_id'], 'cancel')
                thread.join(8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(result, ['cancelled'])
        detail = self.experiments.get(identity)
        self.assertEqual(detail['experiment']['status'], 'cancelled')
        self.assertIsNone(detail['result_json'])
        self.assertTrue((Path(job['output_dir']).parent / 'worker.stderr.log').exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_worker_stop_before_launch_retains_interrupted_attempt(self):
        identity = self.queue()
        with worker_lock(self.store.root) as descriptor:
            worker = Worker(self.store, descriptor)
            job = worker.industry_mom.claim(worker.worker_id)
            worker.stopping.set()
            with patch.object(runner.subprocess, 'Popen') as child:
                self.assertEqual(runner.execute(worker, job), 'interrupted')
            child.assert_not_called()
        self.assertEqual(self.experiments.status(identity)['status'], 'interrupted')
        self.assertTrue((Path(job['output_dir']).parent / 'queued-input.json').is_file())

    def test_deadline_before_launch_fails_without_spawning(self):
        identity = self.queue()
        with worker_lock(self.store.root) as descriptor:
            worker = Worker(self.store, descriptor)
            job = worker.industry_mom.claim(worker.worker_id)
            with patch.object(worker.industry_mom, 'remaining_seconds', return_value=0), patch.object(runner.subprocess, 'Popen') as child:
                self.assertEqual(runner.execute(worker, job), 'failed')
            child.assert_not_called()
        self.assertIsNone(self.experiments.get(identity)['verification'])

    def test_deadline_stops_actual_child(self):
        identity = self.queue()
        marker = self.root / 'child.pid'
        with worker_lock(self.store.root) as descriptor:
            worker = Worker(self.store, descriptor, poll_seconds=.01)
            job = worker.industry_mom.claim(worker.worker_id)
            real_remaining = worker.industry_mom.remaining_seconds
            def remaining(*args):
                return 0 if marker.exists() else real_remaining(*args)
            with patch.object(runner, 'command', return_value=self.sleepy_command(marker)), patch.object(worker.industry_mom, 'remaining_seconds', side_effect=remaining):
                self.assertEqual(runner.execute(worker, job), 'failed')
        self.assertTrue(marker.exists())
        self.assertIn('deadline', self.experiments.get(identity)['experiment']['error'])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_actual_supervisor_sigkill_child_lock_fences_recovery_until_child_exit(self):
        identity = self.queue()
        marker = self.root / 'orphan.pid'
        stdout_path, stderr_path = self.root / 'supervisor.out', self.root / 'supervisor.err'
        program = '''
import json,sys
from pathlib import Path
from unittest.mock import patch
from paper_alpha import mom_only_workflow as w
from paper_alpha.server.service import Store
from paper_alpha.server.runner import Worker,worker_lock
from paper_alpha.server import industry_mom_runner as r
root,method_path,marker=sys.argv[1:]
method=w.read_json(method_path)
with patch.object(w,'PAPER_SHA256',method['paper']['document_sha256']),patch.object(w,'SUPPORTED_METHOD_DIGEST',method['contract_digest']),patch.object(w,'_verify_paper_anchors',return_value=None):
    store=Store(Path(root))
    with worker_lock(store.root) as descriptor:
        worker=Worker(store,descriptor,poll_seconds=.02)
        job=worker.industry_mom.claim(worker.worker_id)
        sleepy=[sys.executable,'-B','-c',"import os,time;from pathlib import Path;Path("+repr(marker)+").write_text(str(os.getpid()));time.sleep(30)"]
        with patch.object(r,'command',return_value=sleepy):
            r.execute(worker,job)
'''
        child_pid = None
        with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            process = subprocess.Popen([sys.executable, '-B', '-c', program, str(self.store.root), str(self.fixture.method_path), str(marker)],
                cwd=PROJECT, stdout=stdout, stderr=stderr)
            try:
                self.await_file(marker, process)
                child_pid = int(marker.read_text())
                process.kill(); process.wait(timeout=8)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                with self.assertRaises(RuntimeError):
                    with worker_lock(self.store.root):
                        self.fail('Orphan engine child released the inherited worker lock')
                os.killpg(child_pid, signal.SIGTERM)
                end = time.monotonic() + 8
                acquired = False
                while time.monotonic() < end:
                    try:
                        with worker_lock(self.store.root):
                            self.assertEqual(self.experiments.recover_stale(0), [identity])
                            acquired = True
                            break
                    except RuntimeError:
                        time.sleep(.02)
                self.assertTrue(acquired)
                detail = self.experiments.get(identity)
                self.assertEqual(detail['experiment']['status'], 'interrupted')
                self.assertTrue((self.experiments._path(identity, detail['experiment']['attempt_id']) / 'worker.stdout.log').exists())
                self.experiments.retry(identity, detail['experiment']['attempt_id'], 'retry-after-crash')
                next_job = self.experiments.claim('new-worker')
                self.assertNotEqual(next_job['attempt_id'], detail['experiment']['attempt_id'])
                self.experiments.finish(identity, 'new-worker', next_job['attempt_id'], 'failed', 'Test cleanup')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=8)
                if child_pid is not None:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    unittest.main()
