"""Monthly process supervision, inherited locks and daily/monthly scheduling."""
from pathlib import Path
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from paper_alpha.server import monthly_experiments, monthly_runner
from paper_alpha.server.runner import Worker, worker_lock
from tests import test_monthly_experiments as fixtures


class QueueStore:
    def __init__(self, kind, count):
        self.jobs = [{'id': f'{kind}-{i}', 'attempt_id': f'attempt-{i}'} for i in range(count)]
        self.recovered = 0

    def heartbeat(self, *_):
        pass

    def claim(self, *_):
        return self.jobs.pop(0) if self.jobs else None

    def recover_stale(self, **_):
        self.recovered += 1


class MonthlyQueueTests(unittest.TestCase):
    def test_both_busy_queues_alternate_and_each_keeps_fifo(self):
        daily, monthly = QueueStore('daily', 3), QueueStore('monthly', 3)
        worker, seen = Worker(daily, -1), []
        worker.monthly = monthly
        with patch.object(worker, 'execute', side_effect=lambda job: seen.append(job['id'])), \
                patch.object(monthly_runner, 'execute', side_effect=lambda _, job: seen.append(job['id'])):
            for _ in range(6):
                self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
        self.assertEqual(seen, ['daily-0', 'monthly-0', 'daily-1', 'monthly-1', 'daily-2', 'monthly-2'])

    def test_once_recovers_both_families_and_executes_only_one(self):
        daily, monthly = QueueStore('daily', 2), QueueStore('monthly', 2)
        worker = Worker(daily, -1)
        worker.monthly = monthly
        with patch.object(worker, 'execute') as execute, patch.object(monthly_runner, 'execute') as monthly_execute:
            worker.run(once=True)
        execute.assert_called_once()
        monthly_execute.assert_not_called()
        self.assertEqual((daily.recovered, monthly.recovered), (1, 1))


class MonthlyProcessTests(unittest.TestCase):
    setUp = fixtures.MonthlyServiceTests.setUp
    create = fixtures.MonthlyServiceTests.create

    def test_real_monthly_child_completes_under_shared_worker(self):
        identity = self.create()
        with worker_lock(self.root) as fd:
            Worker(self.store, fd, poll_seconds=.01).run(once=True)
        detail = self.service.get(identity)
        self.assertEqual(detail['experiment']['status'], 'completed')
        self.assertTrue(detail['verification']['verified'])

    def test_pre_cancelled_attempt_does_not_launch_child(self):
        identity = self.create()
        with worker_lock(self.root) as fd:
            worker = Worker(self.store, fd)
            job = worker.monthly.claim(worker.worker_id)
            worker.monthly.cancel(identity, job['attempt_id'])
            with patch.object(monthly_runner.subprocess, 'Popen') as popen:
                self.assertEqual(monthly_runner.execute(worker, job), 'cancelled')
                popen.assert_not_called()

    def test_failed_preparation_consumes_once_without_starting_other_family(self):
        example = self.store.seed_example()
        daily = self.store.submit_run(example['revision_id'], 'normalized_fixed', 'daily')
        identity = self.create()
        with worker_lock(self.root) as fd:
            worker = Worker(self.store, fd)
            worker._monthly_first = True
            with patch.object(monthly_experiments, 'atomic_json', side_effect=OSError('Preparation fault')):
                worker.run(once=True)
        self.assertEqual(self.service.status(identity)['status'], 'failed')
        self.assertEqual(self.store.get_run(daily['id'])['status'], 'queued')

    def test_preparing_heartbeat_is_live_during_blocking_file_work(self):
        identity = self.create()
        entered, release, stamps = threading.Event(), threading.Event(), []
        original = monthly_experiments.atomic_json
        def blocked(path, value):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(path, value)
        def observer():
            if entered.wait(3):
                stamps.append(self.service._fetch(identity)['heartbeat'])
                # This wait tests heartbeat liveness, not performance.
                release.wait(.65)
                stamps.append(self.service._fetch(identity)['heartbeat'])
                release.set()
        observer_thread = threading.Thread(target=observer)
        observer_thread.start()
        try:
            with patch.object(monthly_experiments, 'atomic_json', side_effect=blocked):
                job = self.service.claim('worker')
        finally:
            release.set()
            observer_thread.join(4)
        self.assertEqual(len(stamps), 2)
        self.assertGreater(stamps[1], stamps[0])
        self.assertTrue(self.store.worker_health()['online'])
        self.service.finish(identity, 'worker', job['attempt_id'], 'failed', 'Fixture cleanup')

    def test_running_child_cancel_and_shutdown_retain_attempts(self):
        for stop in ('cancel', 'shutdown'):
            with self.subTest(stop=stop):
                identity = self.create(stop)
                marker = self.root / (stop + '.started')
                child_code = 'from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(30)'
                with worker_lock(self.root) as fd:
                    worker = Worker(self.store, fd, poll_seconds=.01)
                    job = worker.monthly.claim(worker.worker_id)
                    failures = []
                    def request_stop():
                        deadline = time.monotonic() + 5
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(.01)
                        if not marker.exists():
                            failures.append('Child did not start')
                        if stop == 'cancel':
                            worker.monthly.cancel(identity, job['attempt_id'])
                        else:
                            worker.stopping.set()
                    thread = threading.Thread(target=request_stop)
                    thread.start()
                    try:
                        with patch.object(monthly_runner, 'command', return_value=[sys.executable, '-c', child_code, str(marker)]):
                            status = monthly_runner.execute(worker, job)
                    finally:
                        thread.join(6)
                    self.assertEqual(failures, [])
                self.assertEqual(status, 'cancelled' if stop == 'cancel' else 'interrupted')
                detail = self.service.get(identity)
                self.assertEqual(detail['attempts'][0]['status'], status)
                self.assertIsNone(detail['review_target'])

    def test_killed_supervisor_leaves_child_lock_and_recovery_waits_for_child_exit(self):
        identity = self.create()
        marker = self.root / 'owned-child.pid'
        supervisor_code = r'''
import sys
from pathlib import Path
from paper_alpha.server.service import Store
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server import monthly_runner
root, marker = Path(sys.argv[1]), sys.argv[2]
child_code = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
monthly_runner.command = lambda _: [sys.executable, '-c', child_code, marker]
with worker_lock(root) as fd:
    Worker(Store(root), fd, poll_seconds=.01).run(once=True)
'''
        process = subprocess.Popen([sys.executable, '-c', supervisor_code, str(self.root), str(marker)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        child_pid = None
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), 'Owned test child did not start')
            child_pid = int(marker.read_text())
            process.kill()
            process.wait(timeout=5)
            with self.assertRaisesRegex(RuntimeError, 'owns'):
                with worker_lock(self.root):
                    pass
            previous = self.service.status(identity)['attempt_id']
            self.assertEqual(self.service.status(identity)['status'], 'running')
            os.killpg(child_pid, signal.SIGKILL)
            child_pid = None
            deadline = time.monotonic() + 3
            while True:
                try:
                    with worker_lock(self.root):
                        self.assertEqual(self.service.recover_stale(), [identity])
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
            self.assertEqual(self.service.status(identity)['status'], 'interrupted')
            self.assertEqual(self.service.status(identity)['attempt_id'], previous)
            self.assertTrue((self.root / 'monthly' / identity / 'attempts' / previous / 'input.json').is_file())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    unittest.main()
