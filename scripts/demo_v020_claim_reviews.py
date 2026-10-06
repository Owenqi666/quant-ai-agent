"""New-dir exact claim review controls on a verified synthetic Alpha101 run."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import atomic_json, digest, json_text
from paper_alpha.workflow import run_task

SCOPE = 'Exact-version semantic-review engineering controls on a verified synthetic Alpha101 validation run; no human labels, model call, market performance or paper-replication claim.'


def build_demo(out):
    out = Path(out).resolve()
    if any(out.iterdir()):
        raise ValueError('Claim review demo requires a new empty output directory')
    atomic_json(out / 'status.json', {'passed': False, 'status': 'running', 'scope': SCOPE})
    store = Store(out / 'workspace')
    seed = store.seed_example()
    run = store.submit_run(seed['revision_id'], 'normalized_fixed', 'v020-demo-daily')
    job = store.claim('v020-claim-review-demo')
    run_task(job['task_path'], job['output_dir'], mode='normalized_fixed')
    store.finish(run['id'], 'v020-claim-review-demo', job['attempt_id'], 'completed')
    verified_run = store.get_run(run['id'])
    if verified_run['verification']['verified'] is not True:
        raise AssertionError('Demo requires a real verified synthetic daily result')
    atomic_json(out / 'original-run.json', verified_run)
    cases = ResearchCases(store)
    preview = cases.preview('daily_run', run['id'])
    case = cases.create('Alpha101 exact wording review example', 'Synthetic development data and validation only.',
                        'daily_run', run['id'], preview['source_digest'], 'v020-demo-case')
    atomic_json(out / 'original-case.json', case)
    result = case['context']['results'][0]
    draft = {'id': 'alpha101-validation-ic', 'kind': 'metric', 'attribution': 'project_convention',
             'text': 'This untrusted sentence claims IC=999999. Review the separate server-derived value and exact synthetic validation scope.',
             'evidence_ids': [item['id'] for item in case['context']['evidence']],
             'metric_references': [{'case_id': case['id'], 'case_digest': case['digest'],
                 'result_id': result['id'], 'result_digest': result['digest'], 'pointer': '/0/result/metrics/mean_rank_ic'}]}
    claims = ResearchClaims(store)
    batch = claims.create(case['id'], case['digest'], [draft], 'v020-demo-claim-batch')
    atomic_json(out / 'original-claims.json', batch)
    original_rows = {table: store._read(f'SELECT * FROM {table} ORDER BY rowid') for table in (
        'research_cases', 'research_case_receipts', 'research_claims', 'research_claim_receipts')}
    reviews = ClaimReviews(store)
    target = reviews.target(batch['id'], draft['id'])
    atomic_json(out / 'exact-target.json', target)
    metric = target['claim']['metrics'][0]
    candidate = json.loads(result['payload_json'])[0]
    checks = {'target_links_verified_execution': (target['source_id'] == run['id']
        and target['case_digest'] == case['digest'] and target['claims_digest'] == batch['digest']
        and target['case_context_digest'] == digest(case['context'])
        and metric['value'] == candidate['result']['metrics']['mean_rank_ic']
        and metric['result_digest'] == result['digest'] and bool(target['evidence'])
        and any(item['label'] == 'code_digest' for item in target['provenance'])),
        'verified_number_does_not_certify_prose': ('999999' not in target['claim']['authoritative_display']
            and '999999' in target['claim']['narrative_text'] and target['claim']['semantic_fidelity'] == 'unverified')}
    request = {'claims_id': batch['id'], 'claim_id': draft['id'], 'expected_target_digest': target['target_digest'],
               'source': 'automation', 'reviewer': 'v020 automation demonstration; no actual human judgment',
               'confirmed_at': '2020-01-01T00:00:00+00:00',
               'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Automation control demonstration preserves the unknown human judgment.'} for name in DIMENSIONS},
               'supersedes_id': None, 'expected_supersedes_digest': None, 'idempotency_key': 'v020-demo-review'}
    atomic_json(out / 'submitted-automation-review.json', request)
    review_preview = reviews.preview(**{key: value for key, value in request.items() if key != 'idempotency_key'})
    atomic_json(out / 'review-preview.json', review_preview)
    checks['preview_has_no_write'] = reviews.list()['total'] == 0
    first = reviews.create(**request)
    replay = reviews.create(**request)
    atomic_json(out / 'first-review.json', first); atomic_json(out / 'same-key-replay.json', replay)
    checks['durable_replay_one_record'] = first == replay and reviews.list()['total'] == 1

    def refused(name, attempted, expected):
        atomic_json(out / (name + '-request.json'), attempted)
        before = reviews.list()['total']
        try:
            reviews.create(**attempted)
        except ServiceError as exc:
            response = {'rejected': True, 'status': exc.status, 'error': str(exc),
                        'records_before': before, 'records_after': reviews.list()['total']}
            atomic_json(out / (name + '-response.json'), response)
            return exc.status == expected and before == response['records_after']
        raise AssertionError('Expected exact-claim refusal: ' + name)

    changed_request = deepcopy(request); changed_request['dimensions']['evidence_accuracy']['reason'] += ' Changed body.'
    checks['changed_request_conflicts'] = refused('changed-request', changed_request, 409)
    replacement = deepcopy(request)
    replacement.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'],
                       idempotency_key='v020-demo-replacement', confirmed_at='2020-01-02T00:00:00+00:00')
    replacement['dimensions']['evidence_accuracy']['reason'] = 'Automation reason corrected; still no human declaration.'
    second = reviews.create(**replacement)
    atomic_json(out / 'replacement-request.json', replacement); atomic_json(out / 'replacement-review.json', second)
    checks['replacement_keeps_history'] = (reviews.get(first['id']) == first and reviews.create(**request) == first
        and second['supersedes_digest'] == first['digest'] and reviews.list()['total'] == 2)
    branch = deepcopy(replacement); branch['idempotency_key'] = 'v020-demo-branch'
    checks['branching_refused'] = refused('illegal-branch', branch, 409)
    wrong_parent = deepcopy(replacement)
    wrong_parent.update(supersedes_id=second['id'], expected_supersedes_digest='0' * 64, idempotency_key='v020-demo-parent-digest')
    checks['wrong_parent_digest_refused'] = refused('wrong-parent-digest', wrong_parent, 409)
    forged = deepcopy(request); forged['idempotency_key'] = 'v020-demo-forged'; forged['approved'] = True
    checks['approval_injection_refused'] = refused('forged-approval', forged, 422)
    edited = deepcopy(draft); edited['text'] = 'Revised wording remains pending its own exact-version human judgment.'
    next_batch = claims.create(case['id'], case['digest'], [edited], 'v020-demo-new-wording')
    atomic_json(out / 'revised-claims.json', next_batch)
    stale = deepcopy(request); stale.update(claims_id=next_batch['id'], idempotency_key='v020-demo-stale-target')
    checks['new_wording_rejects_old_target'] = refused('stale-target', stale, 409)
    original_status = reviews.status(batch['id'], draft['id'])
    new_status = reviews.status(next_batch['id'], draft['id'])
    atomic_json(out / 'original-status.json', original_status); atomic_json(out / 'revised-status.json', new_status)
    checks['no_inherited_human_judgment'] = (new_status['records'] == 0 and new_status['human_declared_status'] == 'pending')
    checks['automation_excluded_from_human_confirmation'] = (original_status['human_records'] == 0
        and original_status['automation_records'] == 2 and original_status['human_declared_status'] == 'pending'
        and original_status['semantic_quality_score'] is None)
    (out / 'exported-review.json').write_text(reviews.export(second['id']), encoding='utf-8')
    checks['export_reverifies_exact_target'] = json.loads((out / 'exported-review.json').read_text()) == second
    # The intentionally new batch adds draft rows; none of the prior rows move.
    checks['original_source_rows_unchanged'] = all(
        rows == [row for row in store._read(f'SELECT * FROM {table} ORDER BY rowid') if row.get('id', row.get('idempotency_key')) in
                 {old.get('id', old.get('idempotency_key')) for old in rows}]
        for table, rows in original_rows.items())
    result_summary = {'schema_version': 1, 'passed': all(checks.values()), 'scope': SCOPE,
        'checks': checks, 'cases_total': len(checks), 'cases_passed': sum(checks.values()),
        'human_records': 0, 'human_labels_pending': True, 'llm_api_called': False, 'semantic_quality_score': None,
        'run_id': run['id'], 'case_id': case['id'], 'claims_id': batch['id'], 'target_digest': target['target_digest'],
        'review_ids': [first['id'], second['id']], 'original_result_approvals_written': 0,
        'workspace': str(store.root), 'limitations': original_status['limitations']}
    atomic_json(out / 'result.json', result_summary)
    (out / 'report.md').write_text('\n'.join([
        '# v0.20 exact claim review control example', '',
        f"Software checks: {result_summary['cases_passed']}/{result_summary['cases_total']}.", '',
        'The Alpha101 example actually runs on synthetic development data and its declared validation interval.',
        'The exact target retains paper evidence, batch/claim/source digests, computed metric values and code/evaluation provenance.',
        'The false number in narrative prose stays separate from the verified numeric display.',
        'Two saved judgments are automation records. Actual human confirmation remains pending; no AI provider is called.',
        'Rejected requests, exact response replay, replacement history and new-wording scope are saved beside this report.', '',
        'These checks measure software controls, not semantic quality, investment performance or independent paper replication.', '',
    ]), encoding='utf-8')
    atomic_json(out / 'status.json', {'passed': result_summary['passed'], 'status': 'completed', 'scope': SCOPE})
    if not result_summary['passed']:
        raise AssertionError('Claim-review demo checks failed: ' + json_text(checks))
    return result_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    try:
        result = build_demo(out)
    except Exception as exc:
        atomic_json(out / 'result.json', {'passed': False, 'scope': SCOPE, 'error': type(exc).__name__ + ': ' + str(exc)})
        atomic_json(out / 'status.json', {'passed': False, 'status': 'failed', 'scope': SCOPE})
        raise
    print(json.dumps({'passed': result['passed'], 'cases_passed': result['cases_passed'], 'cases_total': result['cases_total'], 'out': str(out)}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
