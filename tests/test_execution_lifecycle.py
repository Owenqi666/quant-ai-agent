"""Actual SQLite/file boundaries, cancellation, heartbeat and process-death cases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from paper_alpha.server import execution_lifecycle as lifecycle
from paper_alpha.server.service import ServiceError, Store, REPO
from paper_alpha.storage import read_json
from paper_alpha.workflow import run_task


class ExecutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.store = Store(self.root)
        self.example = self.store.seed_example()
        self.run = self.store.submit_run(self.example['revision_id'], 'normalized_fixed', 'lifecycle')

    def tearDown(self):
        self.temp.cleanup()

    def claim(self):
        return lifecycle.claim(self.store, 'lifecycle-worker')

    def calculated(self):
        job = self.claim()
        state = run_task(job['task_path'], job['output_dir'], mode='normalized_fixed')
        return job, state

    def finish(self, job, state):
        return lifecycle.finish(self.store, job['id'], 'lifecycle-worker', job['attempt_id'], state['status'])

    def phases(self):
        return [item['payload']['phase'] for item in self.store.events(self.run['id']) if item['kind'] == 'execution_phase']

    def assert_owned_directories(self):
        attempts = {row['id'] for row in self.store._read('SELECT id FROM attempts WHERE run_id=?', (self.run['id'],))}
        folder = self.root / 'runs' / self.run['id'] / 'attempts'
        directories = {path.name for path in folder.iterdir()} if folder.exists() else set()
        self.assertTrue(directories <= attempts)

    def test_attempt_is_durable_before_first_copy_and_other_writer_can_commit(self):
        copy = lifecycle.shutil.copyfile
        seen = []
        def copying(source, target):
            run = self.store._fetch('runs', self.run['id'])
            attempt = self.store._fetch('attempts', run['attempt_id'])
            self.assertEqual(attempt['status'], 'running')
            self.assertEqual(Path(target).parent.name, attempt['id'])
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(self.store.heartbeat, 'parallel-writer').result(timeout=2)
            seen.append(target)
            return copy(source, target)
        with patch.object(lifecycle.shutil, 'copyfile', side_effect=copying):
            job = self.claim()
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.phases(), ['preparing'])
        self.assertTrue(Path(job['task_path']).is_file())
        self.assert_owned_directories()

    def test_materialization_failure_retains_owned_files_and_consumes_attempt(self):
        copy = lifecycle.shutil.copyfile
        def copying(source, target):
            if Path(target).name == 'market.csv':
                raise OSError('Controlled disk-write failure')
            return copy(source, target)
        with patch.object(lifecycle.shutil, 'copyfile', side_effect=copying):
            self.assertIsNone(self.claim())
        run = self.store.get_run(self.run['id'])
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['attempt_count'], 1)
        self.assertIn('Controlled', run['error'])
        self.assertEqual(run['attempts'][0]['status'], 'failed')
        self.assertIsNone(run['verification'])
        self.assert_owned_directories()

    def test_cancel_during_preparation_never_returns_executable_job(self):
        copy = lifecycle.shutil.copyfile
        def copying(source, target):
            result = copy(source, target)
            self.store.cancel_run(self.run['id'])
            return result
        with patch.object(lifecycle.shutil, 'copyfile', side_effect=copying):
            self.assertIsNone(self.claim())
        run = self.store.get_run(self.run['id'])
        self.assertEqual(run['status'], 'cancelled')
        self.assertEqual(run['attempts'][0]['status'], 'cancelled')
        self.assertEqual(self.store.list_artifacts(run['id']), [])
        self.assert_owned_directories()

    def test_copied_byte_drift_fails_before_execution(self):
        copy = lifecycle.shutil.copyfile
        def copying(source, target):
            result = copy(source, target)
            if Path(target).name == 'market.csv':
                with Path(target).open('ab') as stream:
                    stream.write(b'changed-during-copy')
            return result
        with patch.object(lifecycle.shutil, 'copyfile', side_effect=copying):
            self.assertIsNone(self.claim())
        self.assertIn('Copied research inputs differ', self.store.get_run(self.run['id'])['error'])

    def test_preparation_heartbeat_continues_during_blocked_file_io(self):
        entered, release, pulsed = threading.Event(), threading.Event(), threading.Event()
        copy, touch = lifecycle.shutil.copyfile, lifecycle._touch
        def copying(source, target):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release preparation')
            return copy(source, target)
        def touching(*args):
            result = touch(*args)
            if entered.is_set():
                pulsed.set()
            return result
        with patch.object(lifecycle.shutil, 'copyfile', side_effect=copying), patch.object(lifecycle, '_touch', side_effect=touching):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.claim)
                try:
                    self.assertTrue(entered.wait(3))
                    before = self.store._fetch('runs', self.run['id'])['heartbeat']
                    self.assertTrue(pulsed.wait(3))
                    self.assertGreater(self.store._fetch('runs', self.run['id'])['heartbeat'], before)
                finally:
                    release.set()
                self.assertIsNotNone(future.result(timeout=5))

    def test_old_attempt_cannot_emit_phase_or_publish_after_explicit_retry(self):
        old = self.claim()
        self.store.recover_stale(stale_seconds=0)
        self.store.retry_run(self.run['id'])
        new = self.claim()
        self.assertNotEqual(old['attempt_id'], new['attempt_id'])
        with self.assertRaisesRegex(ServiceError, 'no longer owns'):
            lifecycle.record_phase(self.store, old['id'], 'lifecycle-worker', old['attempt_id'], 'publishing')
        with self.assertRaisesRegex(ServiceError, 'no longer owns'):
            lifecycle.finish(self.store, old['id'], 'lifecycle-worker', old['attempt_id'], 'completed')
        self.assertFalse(lifecycle._touch(self.store, old['id'], 'lifecycle-worker', old['attempt_id']))
        self.assertEqual(self.store.get_run(self.run['id'])['attempt_count'], 2)

    def test_success_publishes_verified_artifacts_after_phase_history(self):
        job, state = self.calculated()
        result = self.finish(job, state)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['verification']['verified'])
        self.assertEqual(self.phases(), ['preparing', 'verifying', 'publishing'])
        self.assertGreater(len(self.store.list_artifacts(result['id'])), 0)
        self.assertEqual(read_json(Path(job['output_dir']) / 'state.json')['status'], 'completed')

    def test_source_drift_after_successful_verification_is_not_published(self):
        job, state = self.calculated()
        binding = self.store._bind_output
        def changing(*args):
            value = binding(*args)
            candidate = next(item for item in state['candidates'] if item['id'] == 'alpha101')
            relative = next(name for name in candidate['artifacts'] if name.endswith('/result.json'))
            (Path(job['output_dir']) / relative).write_text('{}\n')
            return value
        with patch.object(self.store, '_bind_output', side_effect=changing):
            result = self.finish(job, state)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['verification'])
        self.assertIsNone(self.store._fetch('runs', self.run['id'])['verification'])
        self.assertEqual(self.store.list_artifacts(self.run['id']), [])
        self.assertIn('source changed', result['error'])

    def test_export_drift_before_its_hash_is_not_registered(self):
        job, state = self.calculated()
        export = lifecycle._export
        def changing(path, content):
            export(path, content)
            path.write_bytes(b'corrupt')
        with patch.object(lifecycle, '_export', side_effect=changing):
            result = self.finish(job, state)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['verification'])
        self.assertEqual(self.store.list_artifacts(self.run['id']), [])
        self.assertIn('verified source projection', result['error'])

    def test_source_or_export_drift_at_publish_fence_is_refused_without_hashing_under_lock(self):
        for which in ('source', 'export'):
            if which == 'export':
                self.store.retry_run(self.run['id'])
            job, state = self.calculated()
            transaction, hashing = lifecycle.transaction, lifecycle.sha256
            operation_thread = threading.get_ident()
            changed, inside_writer = [False], [False]
            @contextmanager
            def writing(*args, **kwargs):
                with transaction(*args, **kwargs) as connection:
                    on_main = threading.get_ident() == operation_thread
                    export_root = Path(job['output_dir']).parent / 'exports'
                    # The final publisher transaction begins after every export,
                    # unlike the earlier stage-event and heartbeat transactions.
                    if on_main and not changed[0] and (export_root / 'state.json').is_file():
                        folder = Path(job['output_dir']) if which == 'source' else export_root
                        path = folder / 'report.md'
                        info, content = path.stat(), path.read_bytes()
                        path.write_bytes(content[:-1] + (b' ' if content[-1:] != b' ' else b'\n'))
                        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                        changed[0] = True
                    if on_main:
                        inside_writer[0] = True
                    try:
                        yield connection
                    finally:
                        if on_main:
                            inside_writer[0] = False
            def checked_hash(path):
                self.assertFalse(inside_writer[0], 'Publication must not hash files under a SQLite writer lock')
                return hashing(path)
            with patch.object(lifecycle, 'transaction', writing), patch.object(lifecycle, 'sha256', side_effect=checked_hash):
                result = self.finish(job, state)
            self.assertTrue(changed[0])
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['verification'])
            self.assertEqual(self.store.list_artifacts(self.run['id']), [])
            self.assertIn('publication failed', result['error'])

    def test_cancel_and_heartbeat_during_verification_withhold_exports(self):
        job, state = self.calculated()
        entered, release, pulsed = threading.Event(), threading.Event(), threading.Event()
        verify, touch = lifecycle.verify_run, lifecycle._touch
        def verifying(output):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release verification')
            return verify(output)
        def touching(*args):
            result = touch(*args)
            if entered.is_set():
                pulsed.set()
            return result
        with patch.object(lifecycle, 'verify_run', side_effect=verifying), patch.object(lifecycle, '_touch', side_effect=touching):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.finish, job, state)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(self.phases()[-1], 'verifying')
                    self.assertTrue(pulsed.wait(3))
                    self.store.cancel_run(self.run['id'])
                finally:
                    release.set()
                result = future.result(timeout=8)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.store.list_artifacts(self.run['id']), [])
        self.assertIsNone(result['verification'])

    def test_publication_heartbeat_and_cancellation_preserve_unregistered_files(self):
        job, state = self.calculated()
        entered, release, pulsed = threading.Event(), threading.Event(), threading.Event()
        export, touch = lifecycle._export, lifecycle._touch
        def exporting(path, content):
            result = export(path, content)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release publishing')
            return result
        def touching(*args):
            result = touch(*args)
            if entered.is_set():
                pulsed.set()
            return result
        with patch.object(lifecycle, '_export', side_effect=exporting), patch.object(lifecycle, '_touch', side_effect=touching):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.finish, job, state)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(self.phases()[-1], 'publishing')
                    self.assertTrue(pulsed.wait(3))
                    self.store.cancel_run(self.run['id'])
                finally:
                    release.set()
                result = future.result(timeout=8)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.store.list_artifacts(self.run['id']), [])
        self.assertTrue(any((Path(job['output_dir']).parent / 'exports').rglob('*')))
        self.assert_owned_directories()

    def kill_at(self, stage, job=None, state=None):
        marker = Path(self.temp.name) / 'paused'
        code = '''
from pathlib import Path
import sys, threading
from unittest.mock import patch
from paper_alpha.server.service import Store
from paper_alpha.server import execution_lifecycle as life
from paper_alpha.server.runner import worker_lock
root, marker, stage, run_id, attempt_id, status = sys.argv[1:]
store = Store(Path(root))
original = life.shutil.copyfile if stage == 'preparing' else life._export
def paused(*args):
    result = original(*args)
    Path(marker).write_text('owned file exists')
    threading.Event().wait(60)
    return result
with worker_lock(store.root):
    if stage == 'preparing':
        with patch.object(life.shutil, 'copyfile', side_effect=paused):
            life.claim(store, 'lifecycle-worker')
    else:
        with patch.object(life, '_export', side_effect=paused):
            life.finish(store, run_id, 'lifecycle-worker', attempt_id, status)
'''
        process = subprocess.Popen([sys.executable, '-c', code, str(self.root), str(marker), stage,
                                    self.run['id'], job['attempt_id'] if job else '', state['status'] if state else ''],
                                   cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), 'Child must reach the exact interrupted file boundary')
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_sigkill_preparation_has_owned_directory_and_explicit_recovery(self):
        self.kill_at('preparing')
        run = self.store.get_run(self.run['id'])
        self.assertEqual(run['status'], 'running')
        self.assertEqual(run['attempt_count'], 1)
        self.assertEqual(self.phases()[-1], 'preparing')
        self.assert_owned_directories()
        self.store.recover_stale(stale_seconds=0)
        self.assertEqual(self.store.get_run(self.run['id'])['status'], 'interrupted')
        self.assertIsNone(self.claim(), 'Recovery does not automatically create another attempt')
        self.store.retry_run(self.run['id'])
        self.assertIsNotNone(self.claim())
        self.assertEqual(self.store.get_run(self.run['id'])['attempt_count'], 2)
        self.assert_owned_directories()

    def test_sigkill_publication_never_marks_success_or_registers_partial_exports(self):
        job, state = self.calculated()
        self.kill_at('publishing', job, state)
        run = self.store.get_run(self.run['id'])
        self.assertEqual(run['status'], 'running')
        self.assertEqual(self.phases()[-1], 'publishing')
        self.assertEqual(self.store.list_artifacts(self.run['id']), [])
        self.assertTrue(any((Path(job['output_dir']).parent / 'exports').rglob('*')))
        self.store.recover_stale(stale_seconds=0)
        self.assertEqual(self.store.get_run(self.run['id'])['status'], 'interrupted')
        self.store.retry_run(self.run['id'])
        new, new_state = self.calculated()
        completed = self.finish(new, new_state)
        self.assertTrue(completed['verification']['verified'])
        self.assertEqual([item['status'] for item in completed['attempts']], ['interrupted', 'completed'])
        self.assertTrue(all(item['attempt_id'] == new['attempt_id'] for item in self.store.list_artifacts(self.run['id'])))
        self.assert_owned_directories()


if __name__ == '__main__':
    unittest.main()
