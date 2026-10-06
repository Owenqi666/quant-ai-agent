"""Real local industry source → HTTP queue → worker → exact case/claim/report.

Requires operator-owned fixed source materials, stored only in new local output.
No download, human label, AI call or reserved-month evaluation.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_portable_release import clean_environment, stop_process
from paper_alpha.storage import atomic_json, digest
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.industry_mom import IndustryMomSources, IndustryMomExperiments
from paper_alpha.server.service import Store
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.server.research_assessments import DIMENSIONS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--artifact', type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    artifact = args.artifact.absolute()
    result = {'passed': False, 'scope': 'Fixed real industry-portfolio development input through the local workbench; retrospective project modification, not original-paper reproduction or research quality.',
              'human_records_written': 0, 'llm_api_called': False, 'reserved_evaluated': False,
              'started_at': datetime.now(timezone.utc).isoformat()}
    trace, process = [], None
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None, *, expected=200, markdown=False):
        req = Request(f'http://127.0.0.1:{port}' + path,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            response = opener.open(req, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            status = response.status
        if status != expected:
            raise AssertionError(f'{path} returned {status}: {raw[:1000]!r}')
        value = raw.decode() if markdown else json.loads(raw)
        trace.append({'path': path, 'method': 'GET' if body is None else 'POST', 'status': status,
                      'request_digest': digest(body) if body is not None else None,
                      'response_sha256': sha256(raw).hexdigest()})
        atomic_json(out / 'http-trace.json', trace)
        return value

    try:
        original = json.loads((artifact / 'result.json').read_text())
        original_manifest = (artifact / 'manifest.json').read_bytes()
        store = Store(out / 'workspace')
        sources = IndustryMomSources(store)
        source = sources.register(artifact, '49 行业 MOM · 真实来源工作台闭环',
            'Automation integration check on the user-selected project scope; human review remains pending.', 'v021-source')
        assert sources.register(artifact, source['title'], source['note'], 'v021-source') == source
        atomic_json(out / 'source.json', source)
        with (out / 'server.log').open('xb') as stream:
            process = subprocess.Popen([sys.executable, '-B', '-m', 'paper_alpha.server.launcher',
                '--home', str(store.root), '--port', str(port)], cwd=ROOT, env=clean_environment(),
                stdout=stream, stderr=stream, start_new_session=True)
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
        assert (health['version'], health['database_schema']) in {('0.21.0', 17), ('0.22.0', 17)}
        assert request('/api/industry-mom-sources')['items'][0]['digest'] == source['digest']
        body = {'source_id': source['id'], 'source_digest': source['digest'], 'idempotency_key': 'v021-experiment'}
        ack = request('/api/industry-mom-experiments', body, expected=201)
        assert request('/api/industry-mom-experiments', body, expected=201) == ack
        request('/api/industry-mom-experiments', {**body, 'source_digest': '0' * 64}, expected=409)
        request('/api/industry-mom-experiments', {**body, 'path': '/etc/passwd'}, expected=422)
        url = '/api/industry-mom-experiments/' + ack['experiment_id']
        deadline = time.monotonic() + 90
        while request(url + '/status')['status'] in {'queued', 'running', 'cancelling'}:
            assert time.monotonic() < deadline, 'Industry worker did not finish'
            time.sleep(.2)
        detail = request(url)
        assert detail['experiment']['status'] == 'completed', detail['experiment']
        assert detail['verification']['reference_passed'] and not detail['verification']['reserved_evaluated']
        actual = json.loads(detail['result_json'])
        assert digest(actual) == digest(original), 'Workbench computation changed the frozen numerical result'
        target = detail['review_target']
        report_query = urlencode({'expected_attempt_id': target['attempt_id'], 'expected_result_digest': target['result_digest']})
        report = request(url + '/report?' + report_query, markdown=True)
        assert report == detail['report_markdown']
        wrong_query = urlencode({'expected_attempt_id': target['attempt_id'], 'expected_result_digest': '0' * 64})
        request(url + '/report?' + wrong_query, expected=409)
        preview = request('/api/research-cases/preview?' + urlencode({'source_kind': 'industry_mom_experiment', 'source_id': ack['experiment_id']}))
        case_body = {'title': '行业 MOM 的准确结果研究案例', 'note': 'User-selected industry modification; technical integration only.',
            'source_kind': 'industry_mom_experiment', 'source_id': ack['experiment_id'],
            'source_digest': preview['source_digest'], 'idempotency_key': 'v021-case'}
        case = request('/api/research-cases', case_body, expected=201)
        assert request('/api/research-cases', case_body, expected=201) == case
        selected = case['context']['results'][0]
        assert selected['kind'] == 'industry_mom_portfolio'
        assert json.loads(selected['payload_json']) == actual
        draft = {'id': 'mom-development-mean-gross', 'kind': 'metric', 'attribution': 'project_convention',
            'text': '固定行业修改案例在开发区间的月均毛收益，数字由工具解析；没有扣成本，人工判断仍待填。',
            'evidence_ids': [], 'metric_references': [{'case_id': case['id'], 'case_digest': case['digest'],
                'result_id': selected['id'], 'result_digest': selected['digest'], 'pointer': '/summary/0/mean_gross_return'}]}
        claims_body = {'case_id': case['id'], 'case_digest': case['digest'], 'claims': [draft], 'idempotency_key': 'v021-claims'}
        claims = request('/api/research-claims', claims_body, expected=201)
        assert request('/api/research-claims', claims_body, expected=201) == claims
        exact = request(f"/api/claim-review-targets/{claims['id']}?{urlencode({'claim_id': draft['id']})}")
        assert exact['source_kind'] == 'industry_mom_experiment'
        assert exact['claim']['metrics'][0]['value'] == actual['summary'][0]['mean_gross_return']
        review_body = {'claims_id': claims['id'], 'claim_id': draft['id'], 'expected_target_digest': exact['target_digest'],
            'source': 'automation', 'reviewer': 'v021 automated integration; no human declaration',
            'confirmed_at': datetime.now(timezone.utc).isoformat(),
            'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Actual human judgment remains pending.'} for name in DIMENSIONS},
            'supersedes_id': None, 'expected_supersedes_digest': None, 'idempotency_key': 'v021-automation-review'}
        review = request('/api/claim-reviews', review_body, expected=201)
        assert request('/api/claim-reviews', review_body, expected=201) == review
        assert review['source'] == 'automation' and review['semantic_quality_score'] is None
        atomic_json(out / 'experiment.json', detail); atomic_json(out / 'case.json', case)
        atomic_json(out / 'claims.json', claims); atomic_json(out / 'review-target.json', exact)
        atomic_json(out / 'automation-review.json', review)
        (out / 'report.md').write_text(report)
        stop_process(process); process = None
        create_backup(store.root, out / 'backup')
        restore_backup(out / 'backup', out / 'restored')
        restored = Store(out / 'restored')
        assert IndustryMomExperiments(restored).get(ack['experiment_id']) == detail
        assert ResearchCases(restored).get(case['id']) == case
        assert ResearchClaims(restored).get(claims['id']) == claims
        assert ClaimReviews(restored).get(review['id']) == review
        assert (artifact / 'manifest.json').read_bytes() == original_manifest
        result.update(passed=True, version=health['version'], schema=health['database_schema'], source_id=source['id'],
            experiment_id=ack['experiment_id'], case_id=case['id'], claims_id=claims['id'],
            result_digest=detail['verification']['result_digest'], original_result_digest=digest(original),
            numerical_result_equal=True, backup_restore_equal=True, reference_passed=True,
            summary=actual['summary'], original_manifest_preserved=True, http_requests=len(trace))
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if process is not None:
            stop_process(process)
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(out / 'result.json', result)
    print(json.dumps({'passed': result['passed'], 'out': str(out), 'error': result.get('error')}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
