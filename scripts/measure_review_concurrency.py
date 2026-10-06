"""Measure review verification and concurrent writers on completed synthetic output.

No delays are injected. Each invocation owns a new workspace; every timing sample
is retained. Duplicate asset paths enlarge files, not independent market evidence.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import hashlib
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
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest
from paper_alpha.workflow import run_task


def prepare(store, copies=10):
    """One frozen 400-session, 120-asset fixture; no padded fake result files."""
    example = store.seed_example()
    research = store.get_research(example['research_id'])
    folder = ROOT / 'examples/alpha101'
    rows = list(csv.DictReader(io.StringIO((folder / 'market.csv').read_text())))
    meta = json.loads((folder / 'metadata.json').read_text())
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=meta['columns'], lineterminator='\n')
    writer.writeheader()
    for row in rows:
        for n in range(copies):
            writer.writerow({**row, 'asset': f"{row['asset']}_COPY{n:02d}"})
    raw = buffer.getvalue().encode()
    meta.update(universe=[f'{asset}_COPY{n:02d}' for asset in meta['universe'] for n in range(copies)],
                rows=len(rows) * copies, version=f'review-contention-fixture-{copies}',
                market_sha256=hashlib.sha256(raw).hexdigest())
    imports = DatasetImports(store.root, store.db_path)
    uploaded = imports.create('Controlled duplicated-asset concurrency fixture', raw, json.dumps(meta).encode(), 'fixture-upload')
    valid = imports.validate(uploaded['id'], 'fixture-validate')
    if valid['status'] != 'valid':
        raise RuntimeError(f'Fixture validation failed: {valid}')
    registered = imports.register(valid['id'], valid['input_digest'], valid['latest_validation']['id'],
                                  valid['latest_validation']['report_digest'], 'fixture-register')
    task = deepcopy(research['revisions'][0]['task'])
    task['candidates'] = [c for c in task['candidates'] if c['id'] == 'alpha101']
    task['budget']['max_seconds'] = 120
    created = store.create_research('Concurrency measurement only', research['paper_id'],
                                   registered['registered_dataset_id'], task, 'fixture-research')
    submitted = store.submit_run(created['latest_revision_id'], 'normalized_fixed', 'fixture-run')
    claim = store.claim('measurement-worker')
    state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
    store.finish(submitted['id'], 'measurement-worker', claim['attempt_id'], state['status'])
    run = store.get_run(submitted['id'])
    if run['status'] != 'completed' or not run['verification']['verified']:
        raise RuntimeError('Measurement requires a completed, verified experiment')
    target = next(t for t in run['review_targets'] if t['candidate_id'] == 'alpha101')
    return run, target, {'sessions': meta['sessions'], 'assets': len(meta['universe']), 'market_sha256': meta['market_sha256'],
        'task_digest': digest(task), 'export_bytes': sum(a['size'] for a in store.list_artifacts(run['id'])),
        'scope': 'Duplicated synthetic asset paths for file-size workload; no additional research samples.'}


def measure(out, repeats=3, copies=10):
    service_hash = hashlib.sha256((ROOT / 'paper_alpha/server/service.py').read_bytes()).hexdigest()
    report = {'schema_version': 1, 'passed': False, 'python': platform.python_version(), 'platform': platform.platform(),
        'service_sha256': service_hash, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope': 'Local direct service calls on completed synthetic output; no HTTP, artificial sleep or production throughput claim.',
        'samples': [], 'limits': ['Warm OS/SQLite caches; no outliers removed.',
            'Threads start when integrity verification begins; completion ordering is observed, not assumed.',
            'Concurrent writer timing includes scheduling and commit, not only lock acquisition.']}
    try:
        store = Store(out / 'workspace')
        run, target, workload = prepare(store, copies)
        report['workload'] = workload
        for action in ('review', 'approve'):
            for n in range(repeats):
                key = f'{action}-{n}'
                review_args = dict(run_id=run['id'], candidate_id='alpha101', verdict='accepted', category='implementation',
                    note='Automated concurrency measurement; no human judgment.', source='automation', idempotency_key=key,
                    expected_attempt_id=target['attempt_id'], expected_result_digest=target['result_digest'])
                review = store.create_review(**{**review_args, 'idempotency_key': f'pre-{key}'}) if action == 'approve' else None
                entered, verified = threading.Event(), threading.Event()
                verify_samples = []
                original = store._check_integrity
                def checking(connection, row):
                    started = time.perf_counter()
                    entered.set()
                    try:
                        return original(connection, row)
                    finally:
                        verify_samples.append(time.perf_counter() - started)
                        verified.set()
                def operate():
                    started = time.perf_counter()
                    result = (store.create_review(**review_args) if action == 'review' else
                        store.approve_case(review['id'], 'evaluated', 'Automated measurement expectation.', key))
                    return {'elapsed_seconds': time.perf_counter() - started, 'id': result['id']}
                def write(kind):
                    if not entered.wait(20):
                        raise RuntimeError('Verification did not begin')
                    started = time.perf_counter()
                    if kind == 'heartbeat':
                        store.heartbeat('measurement-heartbeat')
                    else:
                        store.submit_run(run['revision_id'], 'normalized_fixed', f'parallel-{key}')
                    return {'kind': kind, 'elapsed_seconds': time.perf_counter() - started,
                            'finished_before_verification': not verified.is_set()}
                with patch.object(store, '_check_integrity', side_effect=checking), ThreadPoolExecutor(max_workers=3) as pool:
                    futures = [pool.submit(write, kind) for kind in ('heartbeat', 'submit')]
                    operation = pool.submit(operate)
                    row = {'action': action, 'repetition': n, 'operation': operation.result(timeout=60),
                           'writers': [f.result(timeout=60) for f in futures], 'verification_seconds': verify_samples}
                report['samples'].append(row)
        report['service_unchanged'] = service_hash == hashlib.sha256((ROOT / 'paper_alpha/server/service.py').read_bytes()).hexdigest()
        report['passed'] = report['service_unchanged']
    except Exception as exc:
        report['error'] = str(exc)
    atomic_json(out / 'measurement.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--copies', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.copies <= 10:
        parser.error('repeats and copies must be between 1 and 10')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = measure(out, args.repeats, args.copies)
    print(json.dumps({'passed': report['passed'], 'report': str(out / 'measurement.json'), 'error': report.get('error')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
