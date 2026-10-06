from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.storage import atomic_json


class FakeStore:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.finished = []

    def cancel_requested(self, run_id):
        return self.cancelled

    def heartbeat(self, *args):
        self.cancelled = True

    def finish(self, *args, **kwargs):
        self.finished.append((args, kwargs))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        # Process-group tests use a deliberately tiny FakeStore. The real
        # SQLite phase/heartbeat contract is covered in test_execution_lifecycle.
        for target, value in (('heartbeat_scope', lambda *_: nullcontext()), ('record_phase', lambda *_: None)):
            mocked = patch('paper_alpha.server.runner.' + target, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def job(self, root):
        atomic_json(root / "task.json", {"budget": {"max_seconds": 60}})
        return {"id": "run-test", "attempt_id": "attempt-test", "task_path": root / "task.json",
                "output_dir": root / "output", "mode": "fixed"}

    def test_lock_fences_second_worker_and_inherited_child(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            child = None
            try:
                with worker_lock(root) as fd:
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(fd,))
                    with self.assertRaisesRegex(RuntimeError, "owns"):
                        with worker_lock(root):
                            pass
                # Parent descriptor is now closed but the child still fences recovery.
                with self.assertRaisesRegex(RuntimeError, "owns"):
                    with worker_lock(root):
                        pass
            finally:
                if child:
                    child.terminate()
                    child.wait(timeout=5)
            with worker_lock(root):
                pass

    def test_cancel_running_child_retains_cancelled_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = FakeStore()
            with worker_lock(root) as fd, patch("paper_alpha.server.runner.engine_command", return_value=[
                    sys.executable, "-c", "import time; time.sleep(30)"]):
                worker = Worker(store, fd, poll_seconds=.01)
                result = worker.execute(self.job(root))
            self.assertEqual(result, "cancelled")
            self.assertEqual(store.finished[0][0][3], "cancelled")

    def test_shutdown_marks_attempt_interrupted(self):
        with tempfile.TemporaryDirectory() as d:
            root, store = Path(d), FakeStore()
            with worker_lock(root) as fd, patch("paper_alpha.server.runner.engine_command", return_value=[
                    sys.executable, "-c", "import time; time.sleep(30)"]):
                worker = Worker(store, fd)
                worker.stopping.set()
                result = worker.execute(self.job(root))
            self.assertEqual(result, "interrupted")
            self.assertIn("Retry explicitly", store.finished[0][1]["error"])

    def test_pre_cancelled_job_does_not_start_process(self):
        with tempfile.TemporaryDirectory() as d:
            root, store = Path(d), FakeStore(cancelled=True)
            with worker_lock(root) as fd, patch("paper_alpha.server.runner.subprocess.Popen") as popen:
                self.assertEqual(Worker(store, fd).execute(self.job(root)), "cancelled")
                popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
