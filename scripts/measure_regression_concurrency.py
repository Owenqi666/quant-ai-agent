"""Measure regression publication writer contention using a frozen service module.

Writers start only once regression owns SQLite's publication writer lock. No
delay is injected; wall times and observed scheduling/commit order are retained.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import argparse
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import platform
import shutil
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.storage import atomic_json, digest
from measure_review_concurrency import prepare


def frozen_module(source):
    name = 'paper_alpha.server._measured_service'
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # Relative imports remain package imports; the frozen service's fixture and
    # path-redaction root must remain the actual source tree, not artifacts/.
    module.REPO = ROOT
    return module


def measure(out, frozen_service, repeats=3, copies=10):
    module = frozen_module(frozen_service)
    with (out / 'measurement_script.py').open('xb') as stream:
        stream.write(Path(__file__).read_bytes())
    report = {'schema_version': 1, 'passed': False, 'python': platform.python_version(),
        'platform': platform.platform(), 'service_snapshot': frozen_service.name,
        'service_sha256': hashlib.sha256(frozen_service.read_bytes()).hexdigest(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'method_sha256': hashlib.sha256(inspect.getsource(module.Store.run_regression_check).encode()).hexdigest(),
        'scope': 'Local direct service calls on verified synthetic output; publication-lock contention, not HTTP throughput.',
        'limitations': ['Warm OS and SQLite caches; no samples discarded and no sleeps injected.',
            'Other tasks may run on the same host; this does not establish an idle-host or controlled-hardware benchmark.',
            'Writer time includes thread scheduling, SQLite lock waiting and commit.',
            'Both implementations hold the writer lock at the trigger; writers cannot commit before its release.',
            'The service module is frozen; dependency hashes identify the live calculation and reference modules used.'],
        'dependencies': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            'paper_alpha/server/db.py', 'paper_alpha/server/regression.py', 'paper_alpha/expressions.py',
            'paper_alpha/evaluation.py', 'paper_alpha/workflow.py',
            'paper_alpha/server/execution_lifecycle.py', 'scripts/measure_review_concurrency.py')}, 'samples': []}
    try:
        store = module.Store(out / 'workspace')
        run, target, report['workload'] = prepare(store, copies)
        review = store.create_review(run['id'], 'alpha101', 'accepted', 'implementation',
            'Automated regression-contention fixture, no human judgment.', source='automation',
            idempotency_key='review', expected_attempt_id=target['attempt_id'], expected_result_digest=target['result_digest'])
        case = store.approve_case(review['id'], 'evaluated', 'Automated numerical regression fixture.', 'approve')
        for n in range(repeats):
            entered, published = threading.Event(), threading.Event()
            origin = time.perf_counter()
            publication, operation_thread = [], [None]
            transaction = module.transaction
            @contextmanager
            def measured_transaction(*args, **kwargs):
                sample = None
                with transaction(*args, **kwargs) as connection:
                    if threading.get_ident() == operation_thread[0]:
                        sample = {'lock_acquired_at_seconds': time.perf_counter() - origin}
                        entered.set()
                    yield connection
                if sample is not None:
                    sample['committed_at_seconds'] = time.perf_counter() - origin
                    sample['lock_scope_seconds'] = sample['committed_at_seconds'] - sample['lock_acquired_at_seconds']
                    publication.append(sample)
                    published.set()
            def operate():
                operation_thread[0] = threading.get_ident()
                start = time.perf_counter()
                parameters = inspect.signature(store.run_regression_check).parameters
                extra = {}
                if 'idempotency_key' in parameters:
                    extra['idempotency_key'] = f'measured-regression-{n}'
                if 'expected_attempt_id' in parameters:
                    extra.update(expected_attempt_id=target['attempt_id'],
                                 expected_result_digest=digest(json.loads(store._fetch('runs', run['id'])['state'])))
                checked = store.run_regression_check(run['id'], [case['id']], **extra)
                return {'elapsed_seconds': time.perf_counter() - start, 'id': checked['id'],
                        'outcome': checked['outcome'], 'service_timing': checked['timing']}
            def writer(kind):
                if not entered.wait(60):
                    raise RuntimeError('Regression publication did not acquire its writer lock')
                started = time.perf_counter() - origin
                began_after_commit = published.is_set()
                if kind == 'heartbeat':
                    store.heartbeat('regression-parallel')
                else:
                    store.submit_run(run['revision_id'], 'normalized_fixed', f'parallel-{n}')
                ended = time.perf_counter() - origin
                return {'kind': kind, 'started_at_seconds': started, 'completed_at_seconds': ended,
                        'elapsed_seconds': ended - started, 'started_after_publication_commit': began_after_commit,
                        'completed_before_publication_commit': not published.is_set()}
            with patch.object(module, 'transaction', measured_transaction), ThreadPoolExecutor(max_workers=3) as pool:
                writers = [pool.submit(writer, kind) for kind in ('heartbeat', 'submit')]
                operation = pool.submit(operate)
                row = {'repetition': n, 'operation': operation.result(timeout=90),
                       'publication': publication, 'writers': [item.result(timeout=90) for item in writers]}
            if len(publication) != 1:
                raise RuntimeError('Expected exactly one regression publication transaction')
            report['samples'].append(row)
        report['dependencies_unchanged'] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
                                             for name, expected in report['dependencies'].items())
        report['passed'] = report['dependencies_unchanged'] and all(row['operation']['outcome'] == 'passed' for row in report['samples'])
    except Exception as exc:
        report['error'] = str(exc)
    atomic_json(out / 'measurement.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--frozen-service', type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--copies', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.copies <= 10:
        parser.error('repeats/copies must be in 1..10')
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / 'measurement.json').exists() or (args.out / 'workspace').exists():
        parser.error('Output already contains evidence; choose a new directory')
    if args.frozen_service is None:
        args.frozen_service = args.out / 'service.measured.py'
        if args.frozen_service.exists():
            parser.error('Frozen source already exists; select it explicitly')
        shutil.copy2(ROOT / 'paper_alpha/server/service.py', args.frozen_service)
    report = measure(args.out, args.frozen_service.resolve(), args.repeats, args.copies)
    print(json.dumps({'passed': report['passed'], 'report': str(args.out / 'measurement.json'), 'error': report.get('error')}))
    raise SystemExit(0 if report['passed'] else 1)
