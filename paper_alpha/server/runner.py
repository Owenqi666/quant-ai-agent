"""Single-host durable worker. AI is deliberately absent from this boundary."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from paper_alpha.storage import read_json
from paper_alpha.workflow import verify_run
from .maintenance import workspace_lease
from .execution_lifecycle import heartbeat_scope, record_phase

LOG = logging.getLogger("paper_alpha.worker")
PROJECT = Path(__file__).resolve().parents[2]


@contextmanager
def worker_lock(root: Path):
    """Fence workers, including a child left alive after its supervisor dies.

    The descriptor is inherited by the engine child. Do NOT explicitly unlock
    it: closing the last inherited descriptor is what releases the flock.
    """
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".worker.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another worker or its engine child still owns this workspace") from exc
        yield stream.fileno()


def stop_group(process: subprocess.Popen):
    """Terminate the engine and all its compute subprocesses, even after exit."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A grandchild may survive after the group leader has exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def engine_command(job):
    return [sys.executable, "-m", "paper_alpha", "run", str(job["task_path"]),
            "--out", str(job["output_dir"]), "--mode", job["mode"]]


class Worker:
    def __init__(self, store, lock_fd: int, *, poll_seconds: float = .2):
        self.store = store
        self.lock_fd = lock_fd
        self.worker_id = f"worker-{uuid.uuid4()}"
        self.stopping = threading.Event()
        self.poll_seconds = poll_seconds
        # Lightweight process tests deliberately supply a non-SQLite store.
        self.monthly = None
        self.industry_mom = None
        if hasattr(store, 'db_path') and hasattr(store, 'root'):
            from .monthly_experiments import MonthlyExperiments
            from .industry_mom import IndustryMomExperiments
            self.monthly = MonthlyExperiments(store)
            self.industry_mom = IndustryMomExperiments(store)
        self._monthly_first = False
        self._queue_cursor = 0

    def execute(self, job):
        with heartbeat_scope(self.store, job['id'], self.worker_id, job['attempt_id']):
            return self._execute(job)

    def _execute(self, job):
        started = time.monotonic()
        engine_finished, verify_seconds = None, None
        run_id, attempt_id = job["id"], job["attempt_id"]
        output = Path(job["output_dir"])
        output.parent.mkdir(parents=True, exist_ok=True)
        process = None
        status, error, verification = "failed", None, None
        try:
            task = read_json(job["task_path"])
            seconds = float(task.get("budget", {}).get("max_seconds", 60))
            if not 0 < seconds <= 600:
                raise ValueError("Invalid execution time budget")
            deadline = time.monotonic() + seconds + 10  # bounded startup/verification grace
            parent_remaining = None
            if hasattr(self.store, 'db_path'):
                from .research_jobs import execution_remaining_seconds
                parent_remaining = execution_remaining_seconds(self.store, run_id)
                if parent_remaining is not None:
                    if parent_remaining <= 0:
                        raise TimeoutError('Research job wall time budget exhausted before execution')
                    deadline = min(deadline, time.monotonic() + parent_remaining)
            if self.store.cancel_requested(run_id):
                status = "cancelled"
            elif self.stopping.is_set():
                status, error = 'interrupted', 'Worker stopped; earlier attempt retained. Retry explicitly.'
            else:
                record_phase(self.store, run_id, self.worker_id, attempt_id, 'executing')
                with (output.parent / "worker.stdout.log").open("w") as stdout, \
                     (output.parent / "worker.stderr.log").open("w") as stderr:
                    process = subprocess.Popen(
                        engine_command(job), cwd=PROJECT, stdout=stdout, stderr=stderr,
                        start_new_session=True, pass_fds=(self.lock_fd,),
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                             "PAPER_ALPHA_WORKER_LOCK_FD": str(self.lock_fd)},
                    )
                    next_heartbeat = 0.0
                    while process.poll() is None:
                        if self.stopping.is_set():
                            status, error = "interrupted", "Worker stopped; earlier attempt retained. Retry explicitly."
                            stop_group(process)
                            break
                        if self.store.cancel_requested(run_id):
                            status = "cancelled"
                            stop_group(process)
                            break
                        now = time.monotonic()
                        if now >= deadline:
                            status, error = "failed", "Supervisor execution deadline exceeded"
                            stop_group(process)
                            break
                        if now >= next_heartbeat:
                            self.store.heartbeat(self.worker_id, run_id)
                            next_heartbeat = now + .5
                        self.stopping.wait(self.poll_seconds)
                    else:
                        engine_finished = time.monotonic()
                        record_phase(self.store, run_id, self.worker_id, attempt_id, 'verifying')
                        state = read_json(output / "state.json")
                        verification = verify_run(output)
                        verify_seconds = time.monotonic() - engine_finished
                        engine_status = state["status"]
                        if engine_status not in {"completed", "failed", "budget_exhausted"}:
                            raise ValueError(f"Engine ended in unexpected state: {engine_status}")
                        if process.returncode != (0 if engine_status == "completed" else 2):
                            raise ValueError("Engine exit status and result disagree")
                        status = "failed" if engine_status == "budget_exhausted" else engine_status
                        if state.get("error"):
                            error = state["error"].get("message", "Engine failed")
                        if parent_remaining is not None and execution_remaining_seconds(self.store, run_id) <= 0:
                            status, error = 'failed', 'Research job wall time budget exhausted before publication'
        except Exception as exc:
            LOG.exception("execution_failed run=%s attempt=%s", run_id, attempt_id)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if process is not None:
                stop_group(process)
        self.store.finish(run_id, self.worker_id, attempt_id, status,
                          error=error, verification=verification, timings={
                'execution_seconds': (engine_finished or time.monotonic()) - started,
                'supervisor_verification_seconds': verify_seconds,
                'total_before_publication_seconds': time.monotonic() - started,
                'scope': 'Supervisor wall time before finish; execution includes subprocess startup; finish records output verification separately.'})
        LOG.info("run_finished %s", json.dumps({"run_id": run_id, "attempt_id": attempt_id,
                                               "status": status}, ensure_ascii=False))
        return status

    def run_once(self):
        self.store.heartbeat(self.worker_id)
        # Round-robin between families; FIFO within each family. An empty queue
        # falls through without consuming the once-mode's one execution budget.
        families = ('daily', 'monthly', 'industry_mom')
        cursor = self._queue_cursor
        # Preserve the existing explicit monthly-first startup control.
        if self._monthly_first and cursor == 0:
            cursor = 1
        order = families[cursor:] + families[:cursor]
        for kind in order:
            pending = None
            if self.monthly is not None and hasattr(self.store, '_read'):
                table = {'daily': 'runs', 'monthly': 'monthly_experiments',
                         'industry_mom': 'industry_mom_experiments'}[kind]
                pending = bool(self.store._read(f"SELECT 1 FROM {table} WHERE status='queued' LIMIT 1"))
                if not pending:
                    continue
            if kind == 'monthly':
                if self.monthly is None:
                    continue
                job = self.monthly.claim(self.worker_id)
            elif kind == 'industry_mom':
                if self.industry_mom is None:
                    continue
                job = self.industry_mom.claim(self.worker_id)
            else:
                job = self.store.claim(self.worker_id)
            if job is None:
                if pending:
                    # Claim can consume an attempt whose input preparation
                    # fails. Once mode still processes at most one queued job.
                    self._queue_cursor = (families.index(kind) + 1) % len(families)
                    self._monthly_first = self._queue_cursor == 1
                    return True
                continue
            self._queue_cursor = (families.index(kind) + 1) % len(families)
            self._monthly_first = self._queue_cursor == 1
            LOG.info("run_started %s", json.dumps({"run_id": job["id"], "attempt_id": job["attempt_id"], 'kind': kind}))
            if kind == 'monthly':
                from .monthly_runner import execute
                execute(self, job)
            elif kind == 'industry_mom':
                from .industry_mom_runner import execute
                execute(self, job)
            else:
                self.execute(job)
            return True
        return False

    def run(self, *, once=False):
        # Acquiring the inherited process lock proves the previous engine is gone.
        self.store.recover_stale(stale_seconds=0)
        if self.monthly is not None:
            self.monthly.recover_stale(stale_seconds=0)
        if self.industry_mom is not None:
            self.industry_mom.recover_stale(stale_seconds=0)
        while not self.stopping.is_set():
            worked = self.run_once()
            if once:
                return
            if not worked:
                self.stopping.wait(.5)


def main(argv=None):
    from .service import Store

    parser = argparse.ArgumentParser(description="Paper Alpha single-host worker (no AI)")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("PAPER_ALPHA_HOME", PROJECT / "var/workbench")))
    parser.add_argument("--once", action="store_true", help="Process at most one queued run and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        with workspace_lease(args.home.resolve()), worker_lock(args.home.resolve()) as fd:
            worker = Worker(Store(args.home.resolve()), fd)
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: worker.stopping.set())
            worker.run(once=args.once)
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
