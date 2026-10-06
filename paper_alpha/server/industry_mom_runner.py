"""Industry MOM supervision under the inherited single-workspace worker lock."""
from pathlib import Path
import os
import subprocess
import sys
import time

from .industry_mom import IndustryMomExperiments, MAX_SECONDS
from .service import ServiceError


def command(job):
    value = [sys.executable, '-B', '-m', 'paper_alpha.mom_only_workflow', 'run',
             '--source', job['source_path'], '--source-sha256', job['source_sha256'],
             '--config', job['config_path'], '--method', job['method_path'],
             '--evidence-dir', job['evidence_dir'], '--out', job['output_dir']]
    if job.get('source_receipt_path') is not None:
        value.extend(('--source-receipt', job['source_receipt_path']))
    return value


def execute(worker, job):
    from .runner import PROJECT, stop_group
    service = worker.industry_mom if worker.industry_mom is not None else IndustryMomExperiments(worker.store)
    identity, attempt_id = job['id'], job['attempt_id']
    process, status, error = None, 'failed', None
    with service.heartbeat_scope(identity, worker.worker_id, attempt_id):
        try:
            if type(job['max_seconds']) is not int or job['max_seconds'] != MAX_SECONDS:
                raise ValueError('Industry supervisor budget must be the fixed 60 seconds')
            deadline = time.monotonic() + MAX_SECONDS
            if service.remaining_seconds(identity, worker.worker_id, attempt_id) <= 0:
                raise ValueError('Industry attempt deadline elapsed before launch')
            if service.cancel_requested(identity):
                status = 'cancelled'
            elif worker.stopping.is_set():
                status, error = 'interrupted', 'Worker stopped; partial output retained. Retry explicitly.'
            else:
                service.record_phase(identity, worker.worker_id, attempt_id, 'executing')
                folder = Path(job['output_dir']).parent
                with (folder / 'worker.stdout.log').open('xb') as stdout, (folder / 'worker.stderr.log').open('xb') as stderr:
                    process = subprocess.Popen(command(job), cwd=PROJECT, stdout=stdout, stderr=stderr,
                        start_new_session=True, pass_fds=(worker.lock_fd,), env={**os.environ,
                        'PYTHONDONTWRITEBYTECODE': '1', 'PAPER_ALPHA_WORKER_LOCK_FD': str(worker.lock_fd)})
                    while process.poll() is None:
                        if worker.stopping.is_set():
                            status, error = 'interrupted', 'Worker stopped; partial output retained. Retry explicitly.'
                            stop_group(process)
                            break
                        if service.cancel_requested(identity):
                            status = 'cancelled'
                            stop_group(process)
                            break
                        if time.monotonic() >= deadline or service.remaining_seconds(identity, worker.worker_id, attempt_id) <= 0:
                            status, error = 'failed', 'Industry supervisor deadline exceeded'
                            stop_group(process)
                            break
                        worker.stopping.wait(worker.poll_seconds)
                    else:
                        if process.returncode == 0:
                            status = 'completed'
                        else:
                            error = f'Industry workflow exited with status {process.returncode}; retained stderr contains details.'
        except (OSError, ValueError, KeyError, TypeError, ServiceError) as exc:
            status, error = 'failed', f'{type(exc).__name__}: {exc}'
        finally:
            if process is not None:
                stop_group(process)
        service.finish(identity, worker.worker_id, attempt_id, status, error)
    return service.status(identity)['status']
