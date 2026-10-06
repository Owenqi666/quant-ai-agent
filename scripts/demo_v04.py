"""Real local HTTP import/review/fix/restore acceptance using synthetic fixtures."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.storage import atomic_json, digest, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    home = out / 'workspace'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    trace = []
    def api(path, body=None, raw=None, content_type='application/json'):
        started = time.monotonic()
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = Request(f'http://127.0.0.1:{port}/api' + path, data=data, headers={'Content-Type': content_type})
        with opener.open(req, timeout=40) as response:
            result = json.load(response)
        trace.append({'path':path, 'elapsed_seconds':time.monotonic()-started, 'response':result})
        atomic_json(out / 'http-trace.json', trace)
        return result
    def execute(revision):
        run = api('/runs', {'revision_id':revision, 'mode':'fixed', 'idempotency_key':str(uuid.uuid4())})
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = api('/runs/' + run['id'])
            if run['status'] in {'completed','failed','interrupted','cancelled'}:
                assert run['status'] == 'completed' and run['verification']['verified'], run.get('error')
                return run
            time.sleep(.25)
        raise TimeoutError('Experiment did not finish')
    with (out / 'server.log').open('wb') as log:
        process = subprocess.Popen([sys.executable,'-m','paper_alpha.server.launcher','--home',str(home),'--port',str(port)],
                                   cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
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
                    raise RuntimeError('Local service did not become ready')
                time.sleep(.2)
            assert health['ai_enabled'] is False
            boundary = 'alpha-' + uuid.uuid4().hex
            body = bytearray()
            fields = {'title':b'Independent synthetic OHLCV v0.4 acceptance', 'idempotency_key':uuid.uuid4().hex.encode()}
            for name, value in fields.items():
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value + b'\r\n')
            for name, filename, content_type in [('file','market.csv','text/csv'), ('metadata','metadata.json','application/json')]:
                body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
                body.extend((ROOT / 'tests/fixtures/datasets' / filename).read_bytes() + b'\r\n')
            body.extend(f'--{boundary}--\r\n'.encode())
            receipt = api('/dataset-imports', raw=bytes(body), content_type='multipart/form-data; boundary=' + boundary)
            receipt = api('/dataset-imports/' + receipt['id'] + '/validate', {'idempotency_key':'validate-v04'})
            assert receipt['status'] == 'valid'
            validation = receipt['latest_validation']
            receipt = api('/dataset-imports/' + receipt['id'] + '/register', {
                'idempotency_key':'register-v04', 'input_digest':receipt['input_digest'],
                'validation_attempt_id':validation['id'], 'report_digest':validation['report_digest']})
            assert receipt['status'] == 'registered'
            example = api('/examples/alpha101', {})
            original = api('/researches/' + example['research_id'])
            task = deepcopy(original['revisions'][0]['task'])
            task['evaluation'] = read_json(ROOT / 'tests/fixtures/datasets/research_config.json')
            research = api('/researches', {'title':'v0.4 synthetic import and explicit alias correction',
                'paper_id':original['paper_id'], 'dataset_id':receipt['registered_dataset_id'], 'task':task})
            before = execute(research['latest_revision_id'])
            assert next(c for c in before['state']['candidates'] if c['id']=='alpha006')['status'] == 'rejected'
            review = api('/runs/' + before['id'] + '/reviews', {'candidate_id':'alpha006', 'verdict':'needs_changes',
                'category':'implementation', 'source':'automation', 'note':'Automated acceptance fixture: explicitly normalize documented alias without changing scientific conditions.'})
            issue = api('/issues', {'review_id':review['id'], 'source':'automation', 'disposition':'implementation_fix',
                'note':'Track this synthetic acceptance correction.', 'idempotency_key':'issue-v04'})
            case = api('/regression-cases', {'review_id':review['id'], 'expected_status':'evaluated',
                'note':'Automated fixture expectation; canonical spelling should pass independent reference.'})
            failed_check = api('/regression-checks', {'run_id':before['id'], 'case_ids':[case['id']]})
            assert failed_check['outcome'] == 'failed'
            next(c for c in task['candidates'] if c['id']=='alpha006')['expression'] = '-1 * ts_corr(open, volume, 10)'
            revision = api('/researches/' + research['id'] + '/revisions', {'base_revision_id':research['latest_revision_id'],
                'task':task, 'note':'Automated fixture canonical spelling; no economic interpretation changed.'})
            def decision(state, **links):
                return api('/issues/' + issue['id'] + '/events', {'base_event_id':issue['latest_event_id'],
                    'state':state, 'disposition':'implementation_fix', 'source':'automation',
                    'note':'Explicit automated fixture decision: ' + state, 'idempotency_key':'v04-' + state, **links})
            issue = decision('proposed', revision_id=revision['id'])
            after = execute(revision['id'])
            issue = decision('awaiting_review', revision_id=revision['id'], target_run_id=after['id'])
            passed_check = api('/regression-checks', {'run_id':after['id'], 'case_ids':[case['id']]})
            assert passed_check['passed']
            issue = decision('resolved', revision_id=revision['id'], target_run_id=after['id'], check_id=passed_check['id'])
            summary = api('/feedback-summary?research_id=' + research['id'])
            from paper_alpha.feedback_metrics import aggregate_feedback
            assert summary['input_digest'] == digest(summary['inputs'])
            assert aggregate_feedback(**summary['inputs']) == summary['metrics']
            assert summary['metrics']['human_review_coverage']['numerator'] == 0
            assert summary['metrics']['same_condition_fix_verification']['numerator'] == 1
            atomic_json(out / 'feedback-summary.json', summary)
            with opener.open(f'http://127.0.0.1:{port}/api/feedback-summary/export?format=csv&research_id=' + research['id']) as response:
                (out / 'feedback-summary.csv').write_bytes(response.read())
            unchanged = api('/runs/' + before['id'])
            assert unchanged['state'] == before['state']
            evidence = {'scope':'Automated synthetic HTTP and recovery acceptance; no human research judgment.',
                'ai_enabled':False, 'import':receipt, 'research_id':research['id'], 'before_run_id':before['id'],
                'after_run_id':after['id'], 'issue':issue, 'before_check':failed_check, 'after_check':passed_check,
                'summary_recomputed':True, 'original_output_preserved':True}
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                import os
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    from paper_alpha.server.backup import create_backup, restore_backup
    from paper_alpha.server.service import Store
    from paper_alpha.server.dataset_imports import DatasetImports
    from paper_alpha.server.feedback import Feedback
    manifest = create_backup(home, out / 'backup')
    restore = restore_backup(out / 'backup', out / 'restored')
    restored = Store(out / 'restored')
    assert restored.get_run(before['id'])['verification']['verified']
    assert restored.get_run(after['id'])['verification']['verified']
    assert Feedback(restored).get(issue['id']) == issue
    assert DatasetImports(restored.root, restored.db_path).get(receipt['id']) == receipt
    evidence['restore'] = {'restored':restore['restored'], 'database_schema':manifest['database_schema'],
                           'import_and_issue_preserved':True, 'both_outputs_verified':True}
    atomic_json(out / 'delivery.json', evidence)
    print(json.dumps({'passed':True, 'evidence':str(out / 'delivery.json')}))


if __name__ == '__main__':
    main()
