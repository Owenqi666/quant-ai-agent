"""Exercise the real HTTP/worker/review loop; label every review as a fixture.

Run against a running local workbench. It saves new revisions/runs, never edits
existing output. No model, external market API, or user identity is impersonated.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'}:
        parser.error('Use a local HTTP workbench')
    if args.out.exists():
        parser.error('Output already exists; choose a new path')
    # Loopback acceptance must not route through machine-wide HTTP proxies.
    opener = build_opener(ProxyHandler({}))

    def api(path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(base + '/api' + path, data=payload,
                          headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    def run(revision_id):
        job = api('/runs', {'revision_id': revision_id, 'mode': 'fixed', 'idempotency_key': str(uuid.uuid4())})
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = api('/runs/' + job['id'])
            if job['status'] not in {'queued', 'running', 'cancelling'}:
                if job['status'] != 'completed' or not job.get('verification', {}).get('verified'):
                    raise RuntimeError('Demonstration run failed: ' + json.dumps(job.get('error')))
                return job
            time.sleep(.3)
        raise TimeoutError('Worker did not complete. Check /api/health and worker logs.')

    health = api('/health')
    if not health['worker']['online']:
        raise RuntimeError('Start the independent worker before running this demonstration')
    example = api('/examples/alpha101', {})
    research = api('/researches/' + example['research_id'])
    # A fresh research avoids depending on prior edits to the user's demo project.
    initial = deepcopy(research['revisions'][0]['task'])
    research = api('/researches', {'title': 'HTTP acceptance: manual alias correction',
                                   'paper_id': research['paper_id'], 'dataset_id': research['dataset_id'],
                                   'task': initial})
    first = run(research['latest_revision_id'])
    candidate = next(c for c in first['state']['candidates'] if c['id'] == 'alpha006')
    assert candidate['status'] == 'rejected', candidate['status']
    review = api('/runs/' + first['id'] + '/reviews', {
        'candidate_id': 'alpha006', 'verdict': 'needs_changes', 'category': 'implementation', 'source': 'automation',
        'note': 'Automated acceptance fixture, not a human research judgment: explicitly normalize the documented correlation alias; keep paper evidence and formula meaning unchanged.'})
    case = api('/regression-cases', {'review_id': review['id'], 'expected_status': 'evaluated',
                                   'note': 'Automated development fixture: corrected canonical formula should execute under fixed mode; not a semantic or profitability evaluation.'})
    before = api('/regression-checks', {'run_id': first['id'], 'case_ids': [case['id']]})
    assert before['passed'] is False
    task = deepcopy(initial)
    next(c for c in task['candidates'] if c['id'] == 'alpha006')['expression'] = '-1 * ts_corr(open, volume, 10)'
    revision = api('/researches/' + research['id'] + '/revisions', {
        'base_revision_id': research['latest_revision_id'], 'task': task,
        'note': 'Automated fixture: canonical operator spelling only; original revision retained.'})
    second = run(revision['id'])
    after = api('/regression-checks', {'run_id': second['id'], 'case_ids': [case['id']]})
    assert after['passed'] is True
    unchanged = api('/runs/' + first['id'])
    assert next(c for c in unchanged['state']['candidates'] if c['id'] == 'alpha006')['status'] == 'rejected'
    summary = {'scope': 'Automated synthetic HTTP acceptance fixture. No AI or human research measurement.',
               'research_id': research['id'], 'before_run_id': first['id'], 'after_run_id': second['id'],
               'review_id': review['id'], 'case_id': case['id'], 'before_check': before, 'after_check': after,
               'before_verification': first['verification'], 'after_verification': second['verification'],
               'original_output_preserved': True, 'ai_enabled': health['ai_enabled']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
