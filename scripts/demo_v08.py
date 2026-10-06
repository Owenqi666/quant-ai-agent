"""Real HTTP research/run recovery, sourced comparison and frozen-report acceptance."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.storage import atomic_json
from demo_v06 import stop_owned_launcher
from demo_v07 import commit_then_disconnect, require


def run_acceptance(out):
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    trace, faults, processes = [], [], []
    report = {'schema_version': 1, 'passed': False, 'stage': 'start',
              'scope': 'Automated synthetic HTTP recovery acceptance; no human judgment or time-savings claim.',
              'service_ports': [], 'services_stopped': False}
    trace_lock = threading.Lock()

    def call(base, path, body=None, *, expected=200, lose_response=False, raw=False):
        record = {'path': path, 'method': 'POST' if body is not None else 'GET', 'body': body}
        try:
            request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers={'Content-Type': 'application/json'})
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
                    status = response.status
                    payload = response.read()
                    result = payload if raw else json.loads(payload)
            except HTTPError as exc:
                status, result = exc.code, json.load(exc)
            record.update(status=status, **({'bytes': len(result), 'sha256': hashlib.sha256(result).hexdigest()} if raw else {'response': result}))
            require(not lose_response, 'Injected response was not dropped')
            require(status == expected, f'{path}: HTTP {status}, expected {expected}')
            return result
        except OSError as exc:
            record.update(error=type(exc).__name__, response_unknown=True)
            if not lose_response:
                raise
            return None
        finally:
            with trace_lock:
                trace.append(record)
                atomic_json(out / 'http-trace.json', trace)

    def start(home, label):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        require(port != 8765, 'Do not use the live preview port')
        with (out / (label + '.log')).open('wb') as log:
            process = subprocess.Popen([sys.executable, '-m', 'paper_alpha.server.launcher', '--home', str(home), '--port', str(port)],
                                       cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        processes.append(process)
        report['service_ports'].append(port)
        base = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(process.poll() is None, 'Owned test launcher exited')
            try:
                health = call(base, '/api/health')
                if health['worker']['online']:
                    require(health['database_schema'] == SCHEMA_VERSION and health['ai_enabled'] is False, 'Unexpected schema or AI boundary')
                    return process, base
            except OSError:
                pass
            time.sleep(.2)
        raise TimeoutError('Owned test service was not ready')

    def wait_run(base, run_id):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = call(base, '/api/runs/' + run_id)
            if run['status'] in {'completed', 'failed', 'interrupted', 'cancelled'}:
                require(run['status'] == 'completed' and run['verification']['verified'], 'Run failed verification')
                return run
            time.sleep(.25)
        raise TimeoutError('Experiment did not finish')

    def replay_parallel(base, path, body, identity):
        with ThreadPoolExecutor(max_workers=4) as executor:
            replies = list(executor.map(lambda _: call(base, path, body, expected=201), range(4)))
        require(all(item['id'] == identity for item in replies), 'Concurrent retries created another object')

    def exports(base, identity, label):
        result = {}
        for format, extension in [('json', 'json'), ('markdown', 'md')]:
            payload = call(base, f'/api/research-reports/{identity}/export?format={format}', raw=True)
            (out / f'{label}.{extension}').write_bytes(payload)
            result[format] = payload
        return result

    try:
        home = out / 'workspace'
        process, base = start(home, 'server')
        example = call(base, '/api/examples/alpha101', {})
        original = call(base, '/api/researches/' + example['research_id'])
        create_body = {'title': 'v08 automated research acceptance', 'paper_id': original['paper_id'],
            'dataset_id': original['dataset_id'], 'task': original['revisions'][-1]['task'],
            'idempotency_key': 'v08-research-create'}
        atomic_json(out / 'pending-research-request.json', create_body)
        with commit_then_disconnect(base, {'/api/researches', '/api/runs'}, faults) as proxy:
            call(proxy, '/api/researches', create_body, lose_response=True)
            require(len(faults) == 1, 'Research response was not dropped after commit')
            research = call(base, '/api/researches', create_body, expected=201)
            require(research == faults[0]['committed_response'], 'Creation receipt changed')
            replay_parallel(base, '/api/researches', create_body, research['id'])
            call(base, '/api/researches', {**create_body, 'title': 'Changed body'}, expected=409)
            require(len(call(base, '/api/researches')) == 2, 'Research retry duplicated the new research')
            run_body = {'revision_id': research['latest_revision_id'], 'mode': 'normalized_fixed', 'idempotency_key': 'v08-run-create'}
            atomic_json(out / 'pending-run-request.json', run_body)
            call(proxy, '/api/runs', run_body, lose_response=True)
            require(len(faults) == 2, 'Run response was not dropped after commit')
            baseline = call(base, '/api/runs', run_body, expected=201)
            require(baseline['id'] == faults[1]['committed_response']['id'], 'Run retry changed identity')
            replay_parallel(base, '/api/runs', run_body, baseline['id'])
            call(base, '/api/runs', {**run_body, 'mode': 'fixed'}, expected=409)
            require(len(call(base, '/api/runs')) == 1, 'Run retry duplicated the experiment')
        baseline = wait_run(base, baseline['id'])
        atomic_json(out / 'baseline-run.json', baseline)
        report['stage'] = 'comparison'
        task = deepcopy(create_body['task'])
        alpha = next(item for item in task['candidates'] if item['id'] == 'alpha006')
        alpha.update(expression='ts_corr(open, volume, 10)', origin='user_modification',
                     changes=['Automated sign-inversion fixture; not a paper-original conclusion.'])
        path = '/api/researches/' + research['id'] + '/revisions'
        revision = call(base, path, {'base_revision_id': research['latest_revision_id'], 'task': task,
                                   'note': 'Automated validation-only comparison'}, expected=201)
        candidate = call(base, '/api/runs', {'revision_id': revision['id'], 'mode': 'normalized_fixed',
                                          'idempotency_key': 'v08-changed-expression'}, expected=201)
        candidate = wait_run(base, candidate['id'])
        compare_path = '/api/run-comparison?baseline_run_id=' + baseline['id'] + '&candidate_run_id='
        comparison = call(base, compare_path + candidate['id'])
        require(comparison['comparable'], 'Identical evaluation conditions should be comparable')
        alpha = next(item for item in comparison['candidates'] if item['candidate_id'] == 'alpha006')
        require(alpha['expression_changed'] and alpha['comparable'], 'Expression change was not reported')
        metric = next(item for item in alpha['metrics'] if item['name'] == 'mean_rank_ic')
        require(metric['delta'] != 0 and metric['delta'] == metric['candidate'] - metric['baseline'], 'Missing sourced delta')
        for side in ('baseline', 'candidate'):
            source = metric[side + '_source']
            payload = call(base, f"/api/runs/{source['run_id']}/artifacts/{source['artifact_id']}", raw=True)
            require(hashlib.sha256(payload).hexdigest() == source['artifact_sha256'], 'Metric source hash differs')
            value = json.loads(payload)
            for component in source['json_pointer'].lstrip('/').split('/'):
                value = value[component.replace('~1', '/').replace('~0', '~')]
            require(value == metric[side], 'Metric does not match its artifact JSON pointer')
        atomic_json(out / 'comparison.json', comparison)
        task['evaluation']['min_assets'] += 1
        changed_revision = call(base, path, {'base_revision_id': revision['id'], 'task': task,
            'note': 'Automated incompatible evaluation condition'}, expected=201)
        changed = call(base, '/api/runs', {'revision_id': changed_revision['id'], 'mode': 'normalized_fixed',
            'idempotency_key': 'v08-changed-evaluation'}, expected=201)
        changed = wait_run(base, changed['id'])
        incompatible = call(base, compare_path + changed['id'])
        require(not incompatible['comparable'] and all(metric['delta'] is None for item in incompatible['candidates'] for metric in item['metrics']), 'Incompatible run presented metric improvements')
        atomic_json(out / 'incompatible-comparison.json', incompatible)
        require(call(base, '/api/researches', create_body, expected=201) == research, 'Creation replay rebound to latest revision')
        report['stage'] = 'frozen_report'
        report_path = '/api/researches/' + research['id'] + '/reports'
        report_body = {'run_ids': [baseline['id'], candidate['id'], changed['id']], 'idempotency_key': 'v08-frozen-report'}
        atomic_json(out / 'pending-report-request.json', report_body)
        with commit_then_disconnect(base, {report_path}, faults) as proxy:
            call(proxy, report_path, report_body, lose_response=True)
        require(len(faults) == 3, 'Report response was not dropped after commit')
        snapshot = call(base, report_path, report_body, expected=201)
        require(snapshot == faults[2]['committed_response'], 'Report receipt returned a new snapshot')
        replay_parallel(base, report_path, report_body, snapshot['id'])
        call(base, report_path, {**report_body, 'run_ids': [baseline['id']]}, expected=409)
        require(call(base, report_path + '?limit=1&offset=0')['total'] == 1, 'Report retries duplicated snapshots')
        require(call(base, report_path + '?limit=1&offset=1')['items'] == [], 'Report pagination did not honor offset')
        frozen = exports(base, snapshot['id'], 'frozen-research')
        require(json.loads(frozen['json']) == snapshot and snapshot['payload_digest'].encode() in frozen['markdown'], 'Export identity differs')
        require(snapshot['counts']['revisions'] == 3 and snapshot['counts']['selected_runs'] == 3, 'Report omitted selected context')
        require(snapshot['counts']['unreviewed_current_candidates'] > 0, 'Unreviewed outputs were falsely approved')
        target = next(item for item in candidate['review_targets'] if item['candidate_id'] == 'alpha101')
        review = call(base, '/api/runs/' + candidate['id'] + '/reviews', {
            'candidate_id': 'alpha101', 'verdict': 'needs_changes', 'category': 'other', 'source': 'automation',
            'note': 'Automated post-snapshot write; no human semantic or timing observation.',
            'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest'],
            'idempotency_key': 'v08-post-snapshot-review'}, expected=201)
        require(exports(base, snapshot['id'], 'after-review') == frozen, 'Later feedback changed frozen exports')
        require(call(base, report_path, report_body, expected=201) == snapshot, 'Replay regenerated frozen report')
        feedback = call(base, '/api/feedback-summary?research_id=' + research['id'])
        structured = feedback['metrics']['structured_assessments']
        require(structured['human_output_coverage']['numerator'] == 0 and structured['human_active_time']['observed_person_seconds'] is None, 'Automation became human evidence')
        atomic_json(out / 'feedback-summary.json', feedback)
        report['stage'] = 'backup_restore'
        stop_owned_launcher(process)
        create_backup(home, out / 'backup')
        restoration = restore_backup(out / 'backup', out / 'restored')
        restored_process, restored_base = start(out / 'restored', 'restored-server')
        require(call(restored_base, '/api/researches', create_body, expected=201) == research, 'Restored creation receipt changed')
        require(call(restored_base, '/api/runs', run_body, expected=201)['id'] == baseline['id'], 'Restored run retry duplicated run')
        require(call(restored_base, report_path, report_body, expected=201) == snapshot, 'Restored report receipt changed')
        require(exports(restored_base, snapshot['id'], 'restored-research') == frozen, 'Restored export bytes changed')
        restored_run = call(restored_base, '/api/runs/' + candidate['id'])
        require(restored_run['verification']['verified'] and restored_run['state'] == candidate['state'] and restored_run['reviews'] == [review], 'Restored result or feedback changed')
        atomic_json(out / 'restored-run.json', restored_run)
        report.update(passed=True, stage='complete', restore=restoration, fault_count=len(faults),
            research_id=research['id'], baseline_run_id=baseline['id'], candidate_run_id=candidate['id'],
            incompatible_run_id=changed['id'], report_id=snapshot['id'], report_payload_digest=snapshot['payload_digest'],
            original_creation_replayed_after_revisions=True, concurrent_retries_deduplicated=True,
            source_bound_nonzero_delta=True, incompatible_deltas_withheld=True,
            frozen_exports_unchanged_after_review_and_restore=True,
            human_output_coverage=structured['human_output_coverage'], human_active_time=structured['human_active_time'])
    except BaseException as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
        (out / 'failure.txt').write_text(traceback.format_exc())
        raise
    finally:
        for process in processes:
            stop_owned_launcher(process)
        closed = {}
        for port in report['service_ports']:
            with socket.socket() as probe:
                closed[str(port)] = probe.connect_ex(('127.0.0.1', port)) != 0
        report['services_stopped'] = all(closed.values()) and all(process.poll() is not None for process in processes)
        report['ports_closed'] = closed
        if not report['services_stopped']:
            report['passed'] = False
        atomic_json(out / 'injected-faults.json', faults)
        atomic_json(out / 'delivery.json', report)
    require(report['passed'], 'Acceptance or owned service cleanup failed')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = run_acceptance(args.out)
    print(json.dumps({'passed': result['passed'], 'evidence': str(args.out / 'delivery.json')}))
