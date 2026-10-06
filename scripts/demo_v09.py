"""Isolated HTTP workflow-observation recovery and backup acceptance.

All observations are explicit automation fixtures, never human measurements.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.server.service import TASK_KEYS
from paper_alpha.storage import atomic_json, read_json
from paper_alpha.research_readiness import check_readiness
from demo_v06 import stop_owned_launcher
from demo_v07 import commit_then_disconnect, require


def fixture(template, *, condition, minute):
    value = deepcopy(template['observation'])
    start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)
    value.update(record_status='observed', source='automation', participant='v09 HTTP automation fixture',
                 session_id='v09-fixture-' + condition, condition=condition,
                 execution_order=1 if condition == 'manual_cli' else 2, code_commit='a' * 40,
                 environment={'machine': 'isolated software fixture', 'os': 'fixture', 'python': 'fixture', 'dependencies': 'fixture'},
                 allowed_tools=['frozen software fixture'], prior_familiarity='Fixture only', practice_session=False,
                 predeclared_stop_condition='Stop on terminal software result', started_at=start.isoformat(),
                 finished_at=(start + timedelta(minutes=7)).isoformat(), completion='incomplete',
                 incomplete_reason='Automation fixture; not a human observation',
                 limitations=['Invented timestamps test arithmetic only; no real duration or semantic judgment.'])
    value['intervals'] = [{'phase': phase, 'kind': 'active', 'started_at': (start + timedelta(minutes=i)).isoformat(),
                           'ended_at': (start + timedelta(minutes=i, seconds=10)).isoformat(),
                           'status': 'ended', 'source_reference': None} for i, phase in enumerate(template['protocol']['phases'])]
    value['bound_outputs']['external_run_reference'] = 'inert-cli-fixture-reference' if condition == 'manual_cli' else None
    return value


def run_acceptance(out):
    out = out.resolve(); out.mkdir(parents=True, exist_ok=False)
    trace, faults, processes = [], [], []
    lock = threading.Lock()
    report = {'schema_version': 1, 'passed': False, 'scope': 'Synthetic automated observation integration; no human observations or efficiency results.',
              'human_observations_added': 0, 'service_ports': [], 'services_stopped': False}

    def call(base, path, body=None, *, expected=200, lose_response=False, raw=False):
        record = {'method': 'GET' if body is None else 'POST', 'path': path}
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
        try:
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
                    status, content = response.status, response.read()
            except HTTPError as exc:
                status, content = exc.code, exc.read()
            record.update(status=status, sha256=hashlib.sha256(content).hexdigest())
            require(not lose_response, 'Expected response fault did not occur')
            require(status == expected, f'{path}: HTTP {status}, expected {expected}: {content[:500]!r}')
            return content if raw else json.loads(content)
        except OSError as exc:
            record.update(error=type(exc).__name__, response_unknown=True)
            if not lose_response: raise
        finally:
            with lock:
                trace.append(record); atomic_json(out / 'http-trace.json', trace)

    def start(home, label):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        require(port != 8765, 'Never touch live preview')
        with (out / (label + '.log')).open('wb') as log:
            p = subprocess.Popen([sys.executable, '-m', 'paper_alpha.server.launcher', '--home', str(home), '--port', str(port)],
                                 cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        processes.append(p); report['service_ports'].append(port)
        base = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(p.poll() is None, 'Owned launcher exited')
            try:
                health = call(base, '/api/health')
                if health['worker']['online']:
                    require(health['database_schema'] == SCHEMA_VERSION and not health['ai_enabled'], 'Wrong schema or AI boundary')
                    return p, base
            except OSError: pass
            time.sleep(.15)
        raise TimeoutError('Owned launcher not ready')

    try:
        for name in ('real_data', 'heldout'):
            readiness = check_readiness(ROOT, f'evaluation_suites/v09/readiness_{name}_template.json',
                                        as_of='2026-10-01T00:00:00Z')
            require(readiness['status'] == 'blocked' and not readiness['execution']['enabled'], 'Blank declarations granted readiness')
            atomic_json(out / f'pending-{name}-readiness.json', readiness)
        home = out / 'workspace'; p, base = start(home, 'server')
        template = call(base, '/api/workflow-observation-template')
        atomic_json(out / 'blank-observation-template.json', template)
        example = call(base, '/api/examples/alpha101', {})
        original = call(base, '/api/researches/' + example['research_id'])
        task = read_json(ROOT / 'evaluation_suites/v05/task.json')
        research = call(base, '/api/researches', {'title': 'v09 automated observation fixture', 'paper_id': original['paper_id'],
            'dataset_id': original['dataset_id'], 'task': {key: task[key] for key in TASK_KEYS}, 'idempotency_key': 'v09-research'}, expected=201)
        path = '/api/researches/' + research['id'] + '/workflow-observations'
        call(base, path + '/validate', {'observation': template['observation']}, expected=422)
        manual = fixture(template, condition='manual_cli', minute=0)
        manual['intervals'] = manual['intervals'][:1]
        checked = call(base, path + '/validate', {'observation': manual})
        require(checked['timing']['observed_active_seconds'] == 10 and checked['timing']['full_active_seconds'] is None,
                'Missing phases were treated as zero/full timing')
        body = {'observation': manual, 'idempotency_key': 'v09-import-response-fault'}
        atomic_json(out / 'automation-import.json', body)
        with commit_then_disconnect(base, {path}, faults) as proxy:
            call(proxy, path, body, expected=201, lose_response=True)
        require(len(faults) == 1, 'Did not prove commit-then-disconnect')
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: call(base, path, body, expected=201), range(4)))
        saved = replies[0]
        require(all(x == saved for x in replies) and saved == faults[0]['committed_response'], 'Retry changed immutable observation')
        changed = deepcopy(body); changed['observation']['incomplete_reason'] = 'different payload'
        call(base, path, changed, expected=409)
        call(base, path, {**body, 'idempotency_key': 'new-key-same-observation'}, expected=409)
        frozen = call(base, path + '/' + saved['id'] + '/export', raw=True)
        (out / 'frozen-observation.json').write_bytes(frozen)
        require(json.loads(frozen) == saved, 'Export differs from saved response')
        run = call(base, '/api/runs', {'revision_id': research['latest_revision_id'], 'mode': 'normalized_fixed', 'idempotency_key': 'v09-bound-run'}, expected=201)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = call(base, '/api/runs/' + run['id'])
            if run['status'] in {'completed', 'failed', 'cancelled', 'interrupted'}: break
            time.sleep(.2)
        require(run['status'] == 'completed' and run['verification']['verified'], 'Engine fixture did not complete')
        workbench = fixture(template, condition='workbench', minute=20)
        target = next(x for x in run['review_targets'] if x['candidate_id'] == 'alpha101')
        dimensions = {key: {'outcome': 'not_applicable', 'reason': 'Automated contract fixture, not a human judgment.'} for key in (
            'evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution', 'field_semantics', 'implementation_alignment')}
        interval = workbench['intervals'][4]
        review = call(base, '/api/runs/' + run['id'] + '/reviews', {'candidate_id': 'alpha101', 'verdict': 'needs_changes', 'category': 'other',
            'source': 'automation', 'note': 'Automated binding fixture only', 'idempotency_key': 'v09-review',
            'assessment': {'reviewer': workbench['participant'], 'expected_attempt_id': target['attempt_id'],
            'expected_result_digest': target['result_digest'], 'dimensions': dimensions,
            'active_intervals': [{key: interval[key] for key in ('started_at', 'ended_at')}]},
            'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest']}, expected=201)
        workbench.update(completion='completed', incomplete_reason=None, dimensions=dimensions)
        workbench['bound_outputs'].update(revision_id=research['latest_revision_id'], run_id=run['id'], attempt_id=target['attempt_id'],
            review_ids=[review['id']], verification_evidence='Actual verified engine fixture', report_reference='run:' + run['id'] + '/report')
        interval['source_reference'] = review['id']
        bound = call(base, path, {'observation': workbench, 'idempotency_key': 'v09-bound-observation'}, expected=201)
        require(bound['binding']['outputs_verified'] and bound['timing']['full_active_seconds'] == 60, 'Binding or copied timing failed/doubled')
        atomic_json(out / 'bound-automation-observation.json', bound)
        summary = call(base, path + '/summary'); atomic_json(out / 'summary.json', summary)
        require(summary['automation_records'] == 2 and summary['human_nonpractice_records'] == 0 and summary['human_completion_rate'] is None
                and not summary['comparisons'], 'Automation leaked into human efficacy')
        require(call(base, path + '?limit=1&offset=1')['total'] == 2, 'Pagination lost records')
        require(call(base, path + '/' + saved['id'] + '/export', raw=True) == frozen, 'Later record changed frozen export')
        stop_owned_launcher(p)
        backup = create_backup(home, out / 'backup'); restore_backup(out / 'backup', out / 'restored')
        restored_process, restored = start(out / 'restored', 'restored-server')
        require(call(restored, path, body, expected=201) == saved, 'Backup lost idempotent identity')
        require(call(restored, path + '/' + saved['id'] + '/export', raw=True) == frozen, 'Restored export changed')
        require(call(restored, path + '/' + bound['id'] + '/export') == bound, 'Restored result-bound observation changed')
        require(call(restored, path + '/summary')['automation_records'] == 2, 'Restored observation history missing')
        report.update(passed=True, observations=2, backup_schema=backup['database_schema'], research_id=research['id'], run_id=run['id'],
                      frozen_export_sha256=hashlib.sha256(frozen).hexdigest())
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        for process in processes: stop_owned_launcher(process)
        report['services_stopped'] = all(p.poll() is not None for p in processes)
        atomic_json(out / 'faults.json', faults); atomic_json(out / 'delivery.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_acceptance(args.out), ensure_ascii=False))
