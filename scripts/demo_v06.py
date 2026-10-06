"""Real HTTP acceptance: structured automation review, located rejection, restore.

This fixture records no human judgment or human time. Every write belongs to a
new output directory; the launcher, API and worker are stopped before backup.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.feedback_metrics import aggregate_feedback
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.dataset_imports import DatasetImports
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.server.feedback import Feedback
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest, read_json


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def multipart(csv, metadata):
    boundary = 'alpha-v06-' + uuid.uuid4().hex
    parts = []
    for name, value in {'title': 'v0.6 duplicate-key diagnostic fixture', 'idempotency_key': uuid.uuid4().hex}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, kind, payload in [('file', 'market.csv', 'text/csv', csv),
                                          ('metadata', 'metadata.json', 'application/json', metadata)]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {kind}\r\n\r\n'.encode() + payload + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def stop_owned_launcher(process):
    """Let the launcher shut down its API/worker; only kill its own group on timeout."""
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    # Catch an orphaned API/worker even if the supervisor has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_acceptance(out):
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    home = out / 'workspace'
    evidence = {'schema_version': 1, 'passed': False,
                'scope': 'Automated synthetic HTTP and backup/restore acceptance; no human judgment, human effort or investment-performance claim.',
                'ai_enabled': False, 'stage': 'starting', 'service_stopped': False}
    atomic_json(out / 'delivery.json', evidence)
    trace = []
    atomic_json(out / 'http-trace.json', trace)
    process = None
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        opener = build_opener(ProxyHandler({}))
        evidence['port'] = port

        def api(path, body=None, *, raw=None, content_type='application/json', expected_status=200):
            started = time.monotonic()
            data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
            record = {'method': 'POST' if data is not None else 'GET', 'path': path,
                      'request': body if raw is None else {'body_bytes': len(raw), 'body_sha256': sha(raw)},
                      'status': None}
            try:
                request = Request(f'http://127.0.0.1:{port}/api' + path, data=data,
                                  headers={'Content-Type': content_type})
                try:
                    with opener.open(request, timeout=40) as response:
                        status, result = response.status, json.load(response)
                except HTTPError as exc:
                    status, result = exc.code, json.load(exc)
                record.update(status=status, response=result)
                require(status == expected_status, f'{path}: expected HTTP {expected_status}, received {status}')
                return result
            except Exception as exc:
                record['error'] = type(exc).__name__ + ': ' + str(exc)
                raise
            finally:
                record['elapsed_seconds'] = time.monotonic() - started
                trace.append(record)
                atomic_json(out / 'http-trace.json', trace)

        with (out / 'server.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-m', 'paper_alpha.server.launcher', '--home', str(home), '--port', str(port)],
                                       cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
            evidence['launcher_pid'] = process.pid
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        health = api('/health')
                        if health['worker']['online']:
                            break
                    except OSError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Owned local API and worker did not become ready')
                    time.sleep(.2)
                require(health['workspace_id'] == sha(str(home.resolve()).encode()), 'Unexpected workspace identity')
                require(health['ai_enabled'] is False and health['database_schema'] == SCHEMA_VERSION, 'Unexpected AI/schema boundary')
                evidence.update(health=health, stage='execute_example')
                example = api('/examples/alpha101', {})
                run = api('/runs', {'revision_id': example['revision_id'], 'mode': 'normalized_fixed',
                                    'idempotency_key': 'v06-acceptance-run'}, expected_status=201)
                deadline = time.monotonic() + 90
                while True:
                    run = api('/runs/' + run['id'])
                    if run['status'] in {'completed', 'failed', 'interrupted', 'cancelled'}:
                        require(run['status'] == 'completed' and run['verification']['verified'], 'Example did not produce a verified completed run')
                        break
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise TimeoutError('Example did not finish within its bounded acceptance wait')
                    time.sleep(.25)
                require(not run['reviews'], 'Fresh example unexpectedly contains reviews')
                target = next(item for item in run['review_targets'] if item['candidate_id'] == 'alpha101')
                assessment = {'reviewer': 'v0.6 automated delivery acceptance',
                              'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest'],
                              'dimensions': {name: {'outcome': 'not_assessed', 'reason': '自动验证占位，未进行人工判断；本记录只验证审核保存与结果绑定。'} for name in DIMENSIONS},
                              'active_intervals': []}
                review_body = {'candidate_id': 'alpha101', 'verdict': 'needs_changes', 'category': 'hypothesis',
                               'source': 'automation', 'note': 'Automated acceptance record: human semantic review remains outstanding; no judgment or human time is asserted.',
                               'assessment': assessment}
                evidence['stage'] = 'structured_automation_review'
                review = api('/runs/' + run['id'] + '/reviews', review_body, expected_status=201)
                require(review['assessment_summary']['semantic_status'] == 'not_human', 'Automation was classified as human')
                require(review['assessment'] == assessment, 'Saved assessment differs from submitted payload')
                after_review = api('/runs/' + run['id'])
                require(after_review['reviews'] == [review], 'Expected exactly one automation review')
                require(after_review['state'] == run['state'], 'Review changed the immutable execution state')
                for key, stale in [('expected_attempt_id', 'stale-acceptance-attempt'), ('expected_result_digest', '0' * 64)]:
                    stale_body = deepcopy(review_body)
                    stale_body['assessment'][key] = stale
                    api('/runs/' + run['id'] + '/reviews', stale_body, expected_status=409)
                    require(api('/runs/' + run['id'])['reviews'] == [review], 'Stale review appended a record')
                summary = api('/feedback-summary?research_id=' + example['research_id'])
                require(summary['input_digest'] == digest(summary['inputs']), 'Feedback input digest does not match raw inputs')
                require(aggregate_feedback(**summary['inputs']) == summary['metrics'], 'Feedback cannot be recomputed')
                structured = summary['metrics']['structured_assessments']
                require(structured['human_output_coverage']['numerator'] == 0, 'Automation increased human coverage')
                require(structured['human_active_time']['observed_person_seconds'] is None, 'Unobserved human effort was invented')
                require(len(structured['records']) == 1 and structured['records'][0]['semantic_status'] == 'not_human', 'Automation summary has incorrect semantic status')
                atomic_json(out / 'feedback-summary.json', summary)
                evidence.update(research_id=example['research_id'], run_id=run['id'], review=review,
                                summary_recomputed=True, stale_targets_rejected_without_appending=True,
                                human_output_coverage=structured['human_output_coverage'], human_active_time=structured['human_active_time'],
                                original_output_preserved=True, stage='located_dataset_rejection')
                original_csv = (ROOT / 'examples/alpha101/market.csv').read_bytes()
                original_metadata = read_json(ROOT / 'examples/alpha101/metadata.json')
                lines = original_csv.splitlines(keepends=True)
                require(len(lines) > 3, 'Fixture does not contain enough rows')
                lines[2] = lines[1]  # CSV record 3 duplicates record 2; original repository input remains untouched.
                bad_csv = b''.join(lines)
                bad_metadata = {**original_metadata, 'market_sha256': sha(bad_csv)}
                bad_metadata_bytes = json.dumps(bad_metadata, ensure_ascii=False).encode()
                raw, content_type = multipart(bad_csv, bad_metadata_bytes)
                receipt = api('/dataset-imports', raw=raw, content_type=content_type, expected_status=201)
                receipt = api('/dataset-imports/' + receipt['id'] + '/validate', {'idempotency_key': 'v06-duplicate-validation'})
                report = receipt['latest_validation']['report']
                require(receipt['status'] == 'invalid' and report['schema_version'] == 2, 'Duplicate fixture was not rejected with v2 diagnostics')
                failed = [item for item in report['checks'] if item['outcome'] == 'failed']
                require(len(failed) == 1 and failed[0]['code'] == 'duplicate_key', 'Incorrect duplicate diagnostic code')
                sample = report['diagnostics']['samples'][0]
                require(sample['row'] == 3 and sample['related_rows'] == [2], 'Incorrect CSV record coordinates')
                require(report['diagnostics']['detected_error_count'] == 1 and not report['diagnostics']['all_errors_enumerated'], 'First-rule limitation was lost')
                inputs = home / 'dataset_imports' / receipt['id'] / 'inputs'
                require(sha((inputs / 'market.csv').read_bytes()) == sha(bad_csv), 'Invalid uploaded CSV was altered')
                require(sha((inputs / 'metadata.json').read_bytes()) == sha(bad_metadata_bytes), 'Invalid uploaded metadata was altered')
                require((ROOT / 'examples/alpha101/market.csv').read_bytes() == original_csv, 'Repository fixture was altered')
                atomic_json(out / 'dataset-diagnostics.json', report)
                atomic_json(out / 'experiment.json', after_review)
                evidence.update(dataset_import=receipt, frozen_input_sha256={'csv': sha(bad_csv), 'metadata': sha(bad_metadata_bytes)},
                                invalid_input_preserved=True, stage='stopping_services')
            finally:
                stop_owned_launcher(process)
                evidence['service_stopped'] = process.poll() is not None
        # A real exclusive backup lease additionally proves that no surviving
        # worker or engine owns this workspace before any files are copied.
        evidence['stage'] = 'backup_restore'
        manifest = create_backup(home, out / 'backup')
        restoration = restore_backup(out / 'backup', out / 'restored')
        restored = Store(out / 'restored')
        restored_run = restored.get_run(run['id'])
        require(restored_run['verification']['verified'], 'Restored experiment no longer verifies')
        require(restored_run['state'] == after_review['state'], 'Restored execution state changed')
        require(restored_run['reviews'] == [review], 'Restored structured review differs')
        require(restored_run['review_targets'] == after_review['review_targets'], 'Restored review bindings changed')
        restored_receipt = DatasetImports(restored.root, restored.db_path).get(receipt['id'])
        require(restored_receipt == receipt, 'Restored dataset diagnostic receipt differs')
        restored_inputs = restored.root / 'dataset_imports' / receipt['id'] / 'inputs'
        require(sha((restored_inputs / 'market.csv').read_bytes()) == sha(bad_csv), 'Restored invalid CSV changed')
        require(sha((restored_inputs / 'metadata.json').read_bytes()) == sha(bad_metadata_bytes), 'Restored invalid metadata changed')
        restored_summary = Feedback(restored).summary(example['research_id'])
        require(restored_summary['inputs'] == summary['inputs'] and restored_summary['metrics'] == summary['metrics'], 'Restored feedback raw inputs or recomputed metrics changed')
        require(digest(restored_summary['inputs']) == restored_summary['input_digest'], 'Restored feedback digest differs')
        require(aggregate_feedback(**restored_summary['inputs']) == restored_summary['metrics'], 'Restored feedback cannot be recomputed')
        atomic_json(out / 'feedback-summary-restored.json', restored_summary)
        evidence.update(passed=True, stage='complete', restore={'restored': restoration['restored'],
                        'database_schema': manifest['database_schema'], 'experiment_verified': True,
                        'assessment_and_bindings_preserved': True, 'diagnostic_receipt_and_input_bytes_preserved': True,
                        'feedback_inputs_and_metrics_preserved': True})
    except BaseException as exc:
        evidence.update(passed=False, error_type=type(exc).__name__, error=str(exc))
        (out / 'failure.txt').write_text(traceback.format_exc())
        raise
    finally:
        if process is not None and process.poll() is None:
            stop_owned_launcher(process)
            evidence['service_stopped'] = process.poll() is not None
        atomic_json(out / 'delivery.json', evidence)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    run_acceptance(args.out)
    print(json.dumps({'passed': True, 'evidence': str(args.out.resolve() / 'delivery.json')}))


if __name__ == '__main__':
    main()
