"""Read-only material export and research-sample evaluation over actual tool output.

Default: one isolated synthetic Alpha101 development calculation. An optional
operator-owned industry artifact adds the fixed real-source development domain.
No human labels, LLM calls, final evaluation or original-paper reproduction.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from check_portable_release import clean_environment, stop_process
from prepare_case_review import prepare, verify
from paper_alpha.research_evaluation import export_sample, verify_bundle
from paper_alpha.server.service import Store
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.storage import atomic_json, digest
from paper_alpha.workflow import run_task

SCOPE = ('Development-only exact-source material and sample software acceptance. '
         'No actual human judgment, LLM quality, efficiency gain or investment claim.')
REVIEW_TABLES = ('research_cases', 'research_case_receipts', 'research_claims', 'research_claim_receipts',
                'claim_reviews', 'claim_review_receipts', 'semantic_annotations',
                'semantic_evaluation_sets', 'semantic_evaluation_comparisons', 'workflow_observations')


def rows(store):
    return {name: store._read(f'SELECT * FROM {name} ORDER BY rowid') for name in REVIEW_TABLES}


def daily_case(out):
    store = Store(out / 'workspace')
    seed = store.seed_example()
    run = store.submit_run(seed['revision_id'], 'normalized_fixed', 'v022-daily-calculation')
    claimed = store.claim('v022-daily-example')
    run_task(claimed['task_path'], claimed['output_dir'], mode='normalized_fixed')
    store.finish(run['id'], 'v022-daily-example', claimed['attempt_id'], 'completed')
    cases = ResearchCases(store)
    preview = cases.preview('daily_run', run['id'])
    case = cases.create('Alpha101 development review materials', SCOPE, 'daily_run', run['id'],
                        preview['source_digest'], 'v022-daily-case')
    result = case['context']['results'][0]
    candidates = json.loads(result['payload_json'])
    index = next(i for i, item in enumerate(candidates) if item['id'] == 'alpha101')
    draft = {'id': 'alpha101-development-gross', 'kind': 'metric', 'attribution': 'project_convention',
             'text': 'This value is the local synthetic Alpha101 development mean gross return; '
                     'it does not demonstrate actual market returns or a paper-proven mechanism.',
             'evidence_ids': [item['id'] for item in case['context']['evidence']],
             'metric_references': [{'case_id': case['id'], 'case_digest': case['digest'],
                 'result_id': result['id'], 'result_digest': result['digest'],
                 'pointer': f'/{index}/result/metrics/mean_gross_return'}]}
    batch = ResearchClaims(store).create(case['id'], case['digest'], [draft], 'v022-daily-claim')
    atomic_json(out / 'case.json', case)
    atomic_json(out / 'claims.json', batch)
    return store, batch['id'], draft['id']


def material_flow(store, claims_id, claim_id, out):
    out.mkdir()
    before = rows(store)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    opener = build_opener(ProxyHandler({}))
    trace = []
    process = None

    def get(path, expected=200):
        try:
            response = opener.open(base + path, timeout=20)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            trace.append({'method': 'GET', 'path': path, 'status': response.status,
                          'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
            atomic_json(out / 'http-trace.json', trace)
            if response.status != expected:
                raise AssertionError(f'Expected {expected}, got {response.status}: {content[:300]!r}')
            return json.loads(content)

    try:
        with (out / 'server.log').open('xb') as stream:
            process = subprocess.Popen([sys.executable, '-B', '-m', 'paper_alpha.server.launcher',
                '--home', str(store.root), '--port', str(port)], cwd=ROOT, env=clean_environment(),
                stdout=stream, stderr=stream, start_new_session=True)
        deadline = time.monotonic() + 45
        while True:
            try:
                health = get('/api/health')
                assert process.poll() is None, 'Isolated launcher exited'
                assert health['workspace_id'] == hashlib.sha256(str(store.root).encode()).hexdigest(), 'Wrong isolated workspace'
                assert (health['version'], health['database_schema']) == ('0.22.0', 17), 'Unexpected isolated runtime'
                if health['worker']['online']:
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Isolated review-material service did not become ready')
            time.sleep(.25)
        target = get('/api/claim-review-targets/' + claims_id + '?' + urlencode({'claim_id': claim_id}))
        route = '/api/review-materials/' + claims_id
        get(route + '?' + urlencode({'claim_id': claim_id}), 422)
        get(route + '?' + urlencode({'claim_id': claim_id, 'expected_target_digest': '0' * 64}), 412)
        query = urlencode({'claim_id': claim_id, 'expected_target_digest': target['target_digest']})
        get(route + '/sources/unknown-source?' + query, 404)
        exported = prepare(base, health['workspace_id'], claims_id, claim_id, out / 'packet')
        assert exported['passed'] and exported['business_writes'] == 0
        assert verify(out / 'packet')['passed']
        evaluated = export_sample(out / 'packet', out / 'sample')
        assert evaluated['passed'] and evaluated['technical_status'] == 'passed'
        assert evaluated['human_reference_status'] == 'pending'
        assert evaluated['model_execution'] == 'model_not_run'
        assert verify_bundle(out / 'sample') == evaluated
        report = json.loads((out / 'sample/report.json').read_text())
        assert report['metrics'] == target['claim']['metrics']
        assert report['semantic_quality_score'] is None and report['model_accuracy'] is None
        assert report['reserved_evaluated'] is False and report['reproduction'] == 'not_rerun'
        assert rows(store) == before
        result = {'passed': True, 'domain': evaluated['domain'], 'packet': exported,
                  'sample': evaluated, 'business_rows_unchanged': True, 'business_tables_checked': list(REVIEW_TABLES),
                  'human_records_written': 0, 'llm_api_called': False,
                  'reserved_evaluated': False, 'scope': SCOPE}
        atomic_json(out / 'result.json', result)
        return result
    finally:
        stop_process(process)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--industry-mom-artifact', type=Path)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {'passed': False, 'scope': SCOPE, 'started_at': datetime.now(timezone.utc).isoformat(),
              'human_records_written': 0, 'llm_api_called': False, 'reserved_evaluated': False}
    try:
        store, claims_id, claim_id = daily_case(out)
        result['daily'] = material_flow(store, claims_id, claim_id, out / 'daily')
        if args.industry_mom_artifact is not None:
            command = [sys.executable, '-B', str(ROOT / 'scripts/demo_v021_mom_workbench.py'),
                       '--artifact', str(args.industry_mom_artifact.absolute()),
                       '--out', str(out / 'industry-calculation')]
            with (out / 'industry-calculation.log').open('xb') as stream:
                subprocess.run(command, cwd=ROOT, env=clean_environment(), stdout=stream,
                               stderr=subprocess.STDOUT, timeout=120, check=True)
            original = json.loads((out / 'industry-calculation/result.json').read_text())
            assert original['passed'] and original['numerical_result_equal']
            industry_store = Store(out / 'industry-calculation/workspace')
            claims = ResearchClaims(industry_store).get(original['claims_id'])
            result['industry'] = material_flow(industry_store, claims['id'], claims['claims'][0]['id'],
                                               out / 'industry')
            result['industry_original_result_digest'] = original['result_digest']
            result['industry_original_manifest_preserved'] = original['original_manifest_preserved']
        result['passed'] = True
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)[:2000]
        raise
    finally:
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(out / 'result.json', result)
    print(json.dumps({'passed': result['passed'], 'domains': ['daily_alpha101'] +
        (['industry_mom'] if 'industry' in result else []), 'out': str(out)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
