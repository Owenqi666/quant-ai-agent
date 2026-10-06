"""Isolated fixed-flow versus workbench screening, review/revision and restore.

Default aggregates are synthetic contract fixtures. --input can use a source-
scanned artifact, but HTTP never authenticates the absent original MAT file.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_portable_release import clean_environment, stop_process
from paper_alpha.eligibility import demo_scan, evaluate, validate_scan, render_report
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    scan = validate_scan(read_json(args.input) if args.input else demo_scan())
    atomic_json(out / 'input.json', scan)
    baseline = evaluate(scan)
    atomic_json(out / 'fixed-flow-result.json', baseline)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    trace = []

    def request(path, body=None, markdown=False, expected=200):
        req = Request(f'http://127.0.0.1:{port}' + path,
                      data=None if body is None else json.dumps(body, allow_nan=False).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            response = opener.open(req, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode('utf-8')
            value = raw if markdown else json.loads(raw)
            trace.append({'path': path, 'method': 'GET' if body is None else 'POST',
                          'status': response.status, 'response_digest': digest(value)})
            assert response.status == expected, (response.status, value)
        atomic_json(out / 'http-trace.json', trace)
        return value

    process = None
    try:
        with (out / 'server.log').open('wb') as stream:
            process = subprocess.Popen([sys.executable, '-m', 'paper_alpha.server.launcher', '--home', str(out / 'workspace'), '--port', str(port)],
                cwd=ROOT, env=clean_environment(), stdout=stream, stderr=stream, start_new_session=True)
        deadline = time.monotonic() + 45
        while True:
            try:
                health = request('/api/health')
                if health['worker']['online']:
                    break
            except OSError:
                pass
            assert process.poll() is None and time.monotonic() < deadline, 'Launcher not healthy'
            time.sleep(.1)
        body = {'title': 'Author eligibility source import' if args.input else 'Synthetic eligibility contract demonstration',
                'note': 'Automated software acceptance; counts are not authenticated by HTTP and this is not human approval.',
                'scan': scan, 'parent_review_id': None, 'author_panel_ids': [], 'idempotency_key': 'v016-import'}
        detail = request('/api/author-studies', body, expected=201)
        assert request('/api/author-studies', body, expected=201) == detail
        route = '/api/author-studies/' + detail['id']
        assert request(route) == detail
        assert detail['result'] == baseline, 'Fixed-flow and workbench decisions differ'
        assert detail['verification_scope'] == 'aggregate_consistency_only' and detail['raw_source_reverified'] is False
        assert request(route + '/reviews') == {'items': []}
        markdown = request(route + '/markdown', markdown=True)
        assert markdown == render_report(baseline)
        atomic_json(out / 'detail.json', detail)
        (out / 'report.md').write_text(markdown, encoding='utf-8')
        review_body = {'study_digest': detail['digest'], 'decision': 'rules_unresolved',
                       'note': 'Automated diagnostic review: portfolio method remains unimplemented.',
                       'actor': 'automation', 'idempotency_key': 'v016-review'}
        review = request(route + '/reviews', review_body, expected=201)
        assert request(route + '/reviews', review_body, expected=201) == review
        request(route + '/reviews', {**review_body, 'study_digest': '0' * 64, 'idempotency_key': 'stale'}, expected=409)
        revised = deepcopy(scan)
        revision_note = 'Automated review revision; original rationale remains in the parent study. Task parameters were not optimized from results.'
        revised['plan']['rationale'] = revision_note if scan['plan']['rationale'] != revision_note else revision_note + ' Follow-up record.'
        revised['plan_digest'] = digest(revised['plan'])
        child_body = {**body, 'scan': revised, 'parent_review_id': review['id'], 'idempotency_key': 'v016-revision'}
        child = request('/api/author-studies', child_body, expected=201)
        assert child['parent_study_id'] == detail['id'] and child['parent_review_id'] == review['id']
        assert child['changes'] and child['result'] == evaluate(revised)
        assert request('/api/author-studies/' + child['id'] + '/reviews') == {'items': []}
        assert request('/api/author-studies?limit=20&offset=0')['total'] == 2
        invalid = deepcopy(scan)
        invalid['months'][0]['patterns'][0] += 1
        request('/api/author-studies', {**body, 'scan': invalid, 'idempotency_key': 'invalid'}, expected=422)
        atomic_json(out / 'review.json', review)
        atomic_json(out / 'revision.json', child)
        stop_process(process)
        process = None
        create_backup(out / 'workspace', out / 'backup')
        restore_backup(out / 'backup', out / 'restored')
        restored = AuthorStudies(Store(out / 'restored'))
        assert restored.get(detail['id']) == detail
        assert restored.get(child['id']) == child
        assert restored.create(**body) == detail
        assert restored.markdown(detail['id']) == markdown
        assert restored.reviews(detail['id']) == {'items': [review]}
        result = {'passed': True, 'schema': health['database_schema'],
                  'input_origin': str(args.input.resolve()) if args.input else 'synthetic-aggregate-fixture',
                  'input_digest': digest(scan), 'baseline_result_digest': digest(baseline),
                  'study_id': detail['id'], 'summary': baseline['summary'],
                  'fixed_flow_workbench_equal': True, 'revision_does_not_inherit_review': True,
                  'stale_review_rejected': True, 'invalid_partition_rejected': True,
                  'backup_restore_replay': True, 'raw_source_reverified': False,
                  'human_efficiency_measured': False, 'portfolio_execution_measured': False,
                  'scope': 'Task-level software consistency against a fixed deterministic flow; both use the same decision engine, not an independent numerical or human baseline.'}
        atomic_json(out / 'result.json', result)
        print(json.dumps({'passed': True, 'result': str(out / 'result.json')}))
    finally:
        if process is not None:
            stop_process(process)


if __name__ == '__main__':
    main()
