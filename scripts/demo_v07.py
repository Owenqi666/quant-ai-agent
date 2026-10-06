"""Real HTTP commit-then-disconnect acceptance for review/case safe retries."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.storage import atomic_json
from demo_v06 import stop_owned_launcher


def require(value, message):
    if not value:
        raise AssertionError(message)


@contextmanager
def commit_then_disconnect(upstream, paths, faults):
    """An owned loopback proxy drops a response only after upstream commits it."""
    armed = set(paths)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 * 1024 or self.path not in paths:
                self.send_error(400)
                return
            request = Request(upstream + self.path, data=self.rfile.read(length),
                              headers={'Content-Type': 'application/json'})
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
                    status, payload = response.status, response.read()
            except HTTPError as exc:
                status, payload = exc.code, exc.read()
            with lock:
                drop = self.path in armed and status == 201
                if drop:
                    armed.remove(self.path)
                    faults.append({'path': self.path, 'upstream_status': status,
                                   'committed_response': json.loads(payload),
                                   'downstream_response_dropped': True})
            if drop:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        require(not thread.is_alive(), 'Owned fault proxy did not stop')


def run_acceptance(out):
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    trace, faults, processes = [], [], []
    report = {'schema_version': 1, 'passed': False, 'stage': 'start',
              'scope': 'Automated synthetic HTTP recovery acceptance; no human judgment or time-savings claim.',
              'service_ports': [], 'services_stopped': False}
    trace_lock = threading.Lock()

    def call(base, path, body=None, *, expected=200, lose_response=False):
        record = {'path': path, 'method': 'POST' if body is not None else 'GET', 'body': body}
        try:
            request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers={'Content-Type': 'application/json'})
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
                    status, result = response.status, json.load(response)
            except HTTPError as exc:
                status, result = exc.code, json.load(exc)
            record.update(status=status, response=result)
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

    try:
        home = out / 'workspace'
        process, base = start(home, 'server')
        example = call(base, '/api/examples/alpha101', {})
        run = call(base, '/api/runs', {'revision_id': example['revision_id'], 'mode': 'normalized_fixed', 'idempotency_key': 'v07-example'}, expected=201)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = call(base, '/api/runs/' + run['id'])
            if run['status'] in {'completed', 'failed', 'interrupted', 'cancelled'}:
                break
            time.sleep(.25)
        require(run['status'] == 'completed' and run['verification']['verified'], 'Example did not complete with verified output')
        target = next(item for item in run['review_targets'] if item['candidate_id'] == 'alpha101')
        review_path = '/api/runs/' + run['id'] + '/reviews'
        case_path = '/api/regression-cases'
        review_body = {'candidate_id': 'alpha101', 'verdict': 'needs_changes', 'category': 'hypothesis',
                       'source': 'automation', 'note': 'Automated retry acceptance only; semantic judgment remains unassessed.',
                       'idempotency_key': 'v07-review-commit-then-disconnect',
                       'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest'],
                       'assessment': {'reviewer': 'Automated recovery acceptance', 'expected_attempt_id': target['attempt_id'],
                                      'expected_result_digest': target['result_digest'], 'active_intervals': [],
                                      'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Automated recovery fixture; no human assessment.'} for name in DIMENSIONS}}}
        # This is a durable client fixture, not evidence that a human filled a form.
        atomic_json(out / 'pending-review-request.json', review_body)
        stale = {**review_body, 'idempotency_key': 'v07-stale-target', 'expected_result_digest': '0' * 64}
        stale.pop('assessment')
        call(base, review_path, stale, expected=412)
        require(call(base, '/api/runs/' + run['id'])['reviews'] == [], 'Stale first submission was recorded')
        report['stage'] = 'commit_then_disconnect'
        with commit_then_disconnect(base, {review_path, case_path}, faults) as proxy:
            call(proxy, review_path, review_body, lose_response=True)
            require(len(faults) == 1, 'Review response was not dropped after upstream commit')
            review_body = json.loads((out / 'pending-review-request.json').read_text())
            review = call(base, review_path, review_body, expected=201)
            require(review == faults[0]['committed_response'], 'Review retry returned a different record')
            with ThreadPoolExecutor(max_workers=4) as executor:
                repeated = list(executor.map(lambda _: call(base, review_path, review_body, expected=201), range(4)))
            require(all(item == review for item in repeated), 'Concurrent review retries diverged')
            changed = deepcopy(review_body)
            changed['note'] = 'A changed request must not reuse the committed key'
            call(base, review_path, changed, expected=409)
            case_body = {'review_id': review['id'], 'expected_status': 'evaluated',
                         'note': 'Automated numerical regression fixture; not approval of the economic hypothesis.',
                         'idempotency_key': 'v07-case-commit-then-disconnect'}
            atomic_json(out / 'pending-case-request.json', case_body)
            call(proxy, case_path, case_body, lose_response=True)
            require(len(faults) == 2, 'Case response was not dropped after upstream commit')
            case_body = json.loads((out / 'pending-case-request.json').read_text())
            case = call(base, case_path, case_body, expected=201)
            require(case == faults[1]['committed_response'], 'Case retry returned a different approval')
            with ThreadPoolExecutor(max_workers=4) as executor:
                repeated = list(executor.map(lambda _: call(base, case_path, case_body, expected=201), range(4)))
            require(all(item == case for item in repeated), 'Concurrent case retries diverged')
            changed = {**case_body, 'expected_status': 'blocked'}
            call(base, case_path, changed, expected=409)
        atomic_json(out / 'injected-faults.json', faults)
        after = call(base, '/api/runs/' + run['id'])
        require(after['reviews'] == [review] and after['state'] == run['state'], 'Retries changed or duplicated experiment/review')
        events = call(base, '/api/runs/' + run['id'] + '/events')
        require(sum(item['kind'] == 'review_recorded' for item in events) == 1, 'Review retry added an extra event')
        require(call(base, case_path) == [case], 'Case retry added an extra approval')
        summary = call(base, '/api/feedback-summary?research_id=' + example['research_id'])
        structured = summary['metrics']['structured_assessments']
        require(structured['human_output_coverage']['numerator'] == 0 and structured['human_active_time']['observed_person_seconds'] is None, 'Automation became human evidence')
        atomic_json(out / 'feedback-summary.json', summary)
        report.update(review_id=review['id'], case_id=case['id'], run_id=run['id'],
                      one_review_one_event_one_case=True, altered_requests_rejected=True, stale_target_rejected=True,
                      raw_output_preserved=True, stage='backup_restore')
        stop_owned_launcher(process)
        create_backup(home, out / 'backup')
        restoration = restore_backup(out / 'backup', out / 'restored')
        restored_process, restored_base = start(out / 'restored', 'restored-server')
        require(call(restored_base, review_path, review_body, expected=201) == review, 'Restored review receipt did not replay')
        require(call(restored_base, case_path, case_body, expected=201) == case, 'Restored approval receipt did not replay')
        restored_run = call(restored_base, '/api/runs/' + run['id'])
        require(restored_run['verification']['verified'] and restored_run['reviews'] == [review] and restored_run['state'] == run['state'], 'Restored experiment or review changed')
        require(call(restored_base, case_path) == [case], 'Restored case duplicated')
        restored_events = call(restored_base, '/api/runs/' + run['id'] + '/events')
        require(restored_events == events, 'Replay after restoration appended an event')
        restored_summary = call(restored_base, '/api/feedback-summary?research_id=' + example['research_id'])
        require(restored_summary['inputs'] == summary['inputs'] and restored_summary['metrics'] == summary['metrics'], 'Restored feedback changed')
        atomic_json(out / 'experiment-restored.json', restored_run)
        report.update(passed=True, stage='complete', restore=restoration, receipt_replay_after_restore=True,
                      fault_count=len(faults), human_output_coverage=structured['human_output_coverage'],
                      human_active_time=structured['human_active_time'])
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
