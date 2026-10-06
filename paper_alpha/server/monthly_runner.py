"""Supervise monthly workflow children under the existing single-worker flock."""
from pathlib import Path
import os
import subprocess
import sys
import time

from .monthly_experiments import MonthlyExperiments
from .domain_research_jobs import domain_execution_remaining_seconds
from .service import ServiceError


def command(job):
    return [sys.executable, '-m', 'paper_alpha.monthly_workflow', 'run', job['input_path'], '--out', job['output_dir']]


def execute(worker, job):
    # Delayed import permits the existing runner to dispatch this separate engine.
    from .runner import PROJECT, stop_group
    service = worker.monthly if worker.monthly is not None else MonthlyExperiments(worker.store)
    identity, attempt_id = job['id'], job['attempt_id']
    process, status, error = None, 'failed', None
    with service.heartbeat_scope(identity, worker.worker_id, attempt_id):
        try:
            seconds = job['max_seconds']
            if type(seconds) not in (int, float) or not 0 < seconds <= 600:
                raise ValueError('Invalid monthly supervisor budget')
            deadline = time.monotonic() + seconds + 10
            parent_remaining = domain_execution_remaining_seconds(worker.store, identity)
            if parent_remaining is not None and parent_remaining <= 0:
                status, error = 'failed', 'Original domain wall deadline elapsed before child launch'
            elif service.cancel_requested(identity):
                status = 'cancelled'
            elif worker.stopping.is_set():
                status, error = 'interrupted', 'Worker stopped; partial output retained. Retry explicitly.'
            else:
                service.record_phase(identity, worker.worker_id, attempt_id, 'executing')
                folder = Path(job['output_dir']).parent
                with (folder / 'worker.stdout.log').open('w') as stdout, (folder / 'worker.stderr.log').open('w') as stderr:
                    process = subprocess.Popen(command(job), cwd=PROJECT, stdout=stdout, stderr=stderr,
                        start_new_session=True, pass_fds=(worker.lock_fd,), env={**os.environ,
                        'PYTHONDONTWRITEBYTECODE': '1', 'PAPER_ALPHA_WORKER_LOCK_FD': str(worker.lock_fd)})
                    while process.poll() is None:
                        parent_remaining = domain_execution_remaining_seconds(worker.store, identity)
                        if parent_remaining is not None and parent_remaining <= 0:
                            status, error = 'failed', 'Original domain wall deadline elapsed during calculation'
                            stop_group(process)
                            break
                        if worker.stopping.is_set():
                            status, error = 'interrupted', 'Worker stopped; partial output retained. Retry explicitly.'
                            stop_group(process)
                            break
                        if service.cancel_requested(identity):
                            status = 'cancelled'
                            stop_group(process)
                            break
                        if time.monotonic() >= deadline:
                            status, error = 'failed', 'Monthly supervisor execution deadline exceeded'
                            stop_group(process)
                            break
                        worker.stopping.wait(worker.poll_seconds)
                    else:
                        if process.returncode == 0:
                            status = 'completed'
                        else:
                            status, error = 'failed', f'Monthly workflow exited with status {process.returncode}; retained worker log contains details.'
        except (OSError, ValueError, KeyError, TypeError, ServiceError) as exc:
            status, error = 'failed', f'{type(exc).__name__}: {exc}'
        finally:
            if process is not None:
                stop_group(process)
        service.finish(identity, worker.worker_id, attempt_id, status, error=error)
    return service.status(identity)['status']
