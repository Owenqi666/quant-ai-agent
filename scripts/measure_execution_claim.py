"""Measure real input materialization against concurrent writers; no injected delays."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import argparse
import csv
import hashlib
import inspect
import io
import json
from pathlib import Path
import platform
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.server.dataset_imports import DatasetImports
from paper_alpha.server import service
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest


def prepare(store, copies):
    example = store.seed_example()
    research = store.get_research(example['research_id'])
    folder = ROOT / 'examples/alpha101'
    rows = list(csv.DictReader(io.StringIO((folder / 'market.csv').read_text())))
    metadata = json.loads((folder / 'metadata.json').read_text())
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=metadata['columns'], lineterminator='\n')
    writer.writeheader()
    for row in rows:
        for n in range(copies):
            writer.writerow({**row, 'asset': f"{row['asset']}_COPY{n:02d}"})
    raw = buffer.getvalue().encode()
    metadata.update(universe=[f'{asset}_COPY{n:02d}' for asset in metadata['universe'] for n in range(copies)],
                    rows=len(rows) * copies, version=f'claim-contention-fixture-{copies}',
                    market_sha256=hashlib.sha256(raw).hexdigest())
    imports = DatasetImports(store.root, store.db_path)
    uploaded = imports.create('Synthetic claim workload', raw, json.dumps(metadata).encode(), 'upload')
    valid = imports.validate(uploaded['id'], 'validate')
    if valid['status'] != 'valid':
        raise RuntimeError('Synthetic workload validation failed')
    registered = imports.register(valid['id'], valid['input_digest'], valid['latest_validation']['id'],
                                  valid['latest_validation']['report_digest'], 'register')
    task = deepcopy(research['revisions'][0]['task'])
    task['candidates'] = [item for item in task['candidates'] if item['id'] == 'alpha101']
    created = store.create_research('Claim contention only', research['paper_id'], registered['registered_dataset_id'], task)
    return created['latest_revision_id'], {'sessions': metadata['sessions'], 'assets': len(metadata['universe']),
        'input_csv_bytes': len(raw), 'input_csv_sha256': metadata['market_sha256'], 'task_digest': digest(task),
        'scope': 'Duplicated synthetic asset paths enlarge valid inputs; no independent market evidence.'}


def measure(out, implementation, repeats=5, copies=20):
    if implementation == 'baseline':
        module, claim = service, lambda store: store.claim('claim-measurement')
        code = inspect.getsource(Store.claim)
    else:
        from paper_alpha.server import execution_lifecycle
        module, claim = execution_lifecycle, lambda store: execution_lifecycle.claim(store, 'claim-measurement')
        code = inspect.getsource(execution_lifecycle.claim)
    report = {'schema_version': 1, 'passed': False, 'implementation': implementation,
        'python': platform.python_version(), 'platform': platform.platform(),
        'claim_source_sha256': hashlib.sha256(code.encode()).hexdigest(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'samples': [],
        'scope': 'Local direct calls, real file hashing/copying; no sleeps, HTTP or production-throughput claim.',
        'limitations': ['Warm OS and SQLite caches; no outliers removed.',
            'Writer time includes thread scheduling and commit, not only lock acquisition.',
            'Writer starts at the first input hash; claim completion ordering is observed rather than assumed.']}
    try:
        store = Store(out / 'workspace')
        revision, report['workload'] = prepare(store, copies)
        for n in range(repeats):
            store.submit_run(revision, 'normalized_fixed', f'run-{n}')
            entered, finished = threading.Event(), threading.Event()
            original = module.sha256
            def hashing(path):
                entered.set()
                return original(path)
            def operate():
                start = time.perf_counter()
                try:
                    result = claim(store)
                    if result is None:
                        raise RuntimeError('Claim did not prepare a task')
                    return {'elapsed_seconds': time.perf_counter() - start, 'run_id': result['id'], 'attempt_id': result['attempt_id']}
                finally:
                    finished.set()
            def write(kind):
                if not entered.wait(20):
                    raise RuntimeError('Input hashing did not begin')
                start = time.perf_counter()
                if kind == 'heartbeat':
                    store.heartbeat('claim-parallel')
                else:
                    store.submit_run(revision, 'normalized_fixed', f'parallel-{n}')
                return {'kind': kind, 'elapsed_seconds': time.perf_counter() - start,
                        'finished_before_claim': not finished.is_set()}
            with patch.object(module, 'sha256', side_effect=hashing), ThreadPoolExecutor(max_workers=3) as pool:
                writers = [pool.submit(write, kind) for kind in ('heartbeat', 'submit')]
                operation = pool.submit(operate)
                report['samples'].append({'repetition': n, 'claim': operation.result(timeout=60),
                                          'writers': [item.result(timeout=60) for item in writers]})
        report['passed'] = True
    except Exception as exc:
        report['error'] = str(exc)
    atomic_json(out / 'measurement.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--implementation', choices=['baseline', 'current'], required=True)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--copies', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20 or not 1 <= args.copies <= 20:
        parser.error('repeats and copies must be in 1..20')
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    result = measure(args.out, args.implementation, args.repeats, args.copies)
    print(json.dumps({'passed': result['passed'], 'report': str(args.out / 'measurement.json'), 'error': result.get('error')}))
    raise SystemExit(0 if result['passed'] else 1)
