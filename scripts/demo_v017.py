"""Provider-free research cases backed by real controlled calculation and blocking.

All output is isolated and new. --input accepts an already saved blocked scan;
it does not authenticate or re-read an original MAT source.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_alpha import eligibility, monthly_evaluation, monthly_workflow, research_protocol
from paper_alpha.evidence import sha256
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest, read_json

SUITE = ROOT / 'evaluation_suites' / 'v017'


def snapshot_runner(out):
    """Keep the policy interpreter alongside the domain workflow's source archive."""
    folder = out / 'evaluation-source'
    folder.mkdir()
    for name in ('demo_v017.py', 'evaluate_v017.py'):
        shutil.copyfile(ROOT / 'scripts' / name, folder / name)
    return {path.name: sha256(path) for path in sorted(folder.iterdir())}


def load_manifest():
    value = read_json(SUITE / 'manifest.json')
    expected = {'computed_result_trace', 'data_insufficient_stop', 'evidence_identity',
                'attribution_preserved', 'forbidden_capability', 'metric_injection',
                'disallowed_action', 'idempotent_replay', 'restart_budget', 'proposal_not_approval'}
    ids = [item['id'] for item in value['cases']]
    if (value['scope'] != 'engineering_policy_expectations' or set(ids) != expected
            or len(ids) != len(expected) or value['ground_truth']['semantic_fidelity'] != 'pending_human_confirmation'
            or value['ground_truth']['human_reviewer'] is not None
            or value['ground_truth']['human_confirmed_at'] is not None
            or value['ground_truth']['llm_evaluated'] is not False
            or value['ground_truth']['human_efficiency_measured'] is not False):
        raise ValueError('The frozen suite requires all declared policies and unconfirmed semantic labels')
    scan = read_json(SUITE / value['negative']['input'])
    if digest(scan) != value['negative']['input_digest']:
        raise ValueError('Frozen blocked input digest changed')
    return value


def build_sources(out, manifest, scan_path=None):
    """Create actual domain records; restricted tools never receive generated metrics."""
    from paper_alpha.server.research_cases import ResearchCases
    store = Store(out / 'workspace')
    inputs = out / 'inputs'
    inputs.mkdir()
    rules = next(item['config'] for item in research_protocol.presets() if item['config']['mode'] == 'project')
    protocol = ResearchProtocols(store).create(
        'Controlled MOM/ID project convention',
        'Automated engineering example; project rules are not an original-paper replication.', rules)
    monthly = MonthlyExperiments(store)
    receipt = monthly.create(protocol['id'], protocol['digest'], manifest['positive']['config'], 'v017-positive')
    job = monthly.claim('v017-isolated-demonstration')
    if job is None or job['id'] != receipt['experiment_id']:
        raise AssertionError('The isolated workflow did not claim the declared monthly experiment')
    frozen = read_json(job['input_path'])
    atomic_json(inputs / 'monthly.json', frozen)
    fixed_monthly = monthly_evaluation.evaluate(frozen['protocol']['config'], frozen['config'], frozen['bundle'])
    atomic_json(out / 'fixed-monthly-result.json', fixed_monthly)
    monthly_workflow.run(job['input_path'], job['output_dir'])
    monthly.finish(receipt['experiment_id'], 'v017-isolated-demonstration', job['attempt_id'], 'completed')
    positive = monthly.get(receipt['experiment_id'])
    if positive['result'] != fixed_monthly or positive['verification']['reference_passed'] is not True:
        raise AssertionError('Recorded computation differs from the fixed flow or independent reference')
    if positive['result']['status'] != manifest['positive']['expected_source_status']:
        raise AssertionError('Controlled monthly computation did not meet the declared success expectation')
    atomic_json(out / 'monthly-detail.json', positive)
    (out / 'monthly-report.md').write_text(monthly_workflow.render_report(fixed_monthly), encoding='utf-8')

    scan_file = Path(scan_path).resolve() if scan_path else SUITE / manifest['negative']['input']
    scan = eligibility.validate_scan(read_json(scan_file))
    fixed_screen = eligibility.evaluate(scan)
    if fixed_screen['summary']['status'] != manifest['negative']['expected_source_status']:
        raise ValueError('The negative demonstration requires an actually blocked scan; thresholds are never changed')
    atomic_json(inputs / 'author-scan.json', scan)
    atomic_json(out / 'fixed-author-result.json', fixed_screen)
    negative = AuthorStudies(store).create(
        'Saved blocked scan' if scan_path else 'Explicit synthetic blocked scan',
        'Automated software example. Aggregate consistency only; original MAT not reverified; no human approval.',
        scan, None, [], 'v017-negative')
    if negative['result'] != fixed_screen or negative['raw_source_reverified'] is not False:
        raise AssertionError('Blocked study result or source-verification boundary changed')
    atomic_json(out / 'author-detail.json', negative)
    (out / 'author-report.md').write_text(eligibility.render_report(fixed_screen), encoding='utf-8')
    contexts = ResearchCases(store)
    cases = {}
    for key, source_id in [('positive', receipt['experiment_id']), ('negative', negative['id'])]:
        kind = manifest[key]['source_kind']
        preview = contexts.preview(kind, source_id)
        case = contexts.create(
            'Controlled monthly result' if key == 'positive' else 'Blocked eligibility decision',
            'Provider-free engineering demonstration; semantic judgment awaits human confirmation.',
            kind, source_id, preview['source_digest'], 'v017-case-' + key)
        cases[key] = case
        atomic_json(out / (key + '-case.json'), case)
        (out / (key + '-case.md')).write_text(contexts.markdown(case['id']), encoding='utf-8')
    atomic_json(out / 'fixed-decisions.json', {
        'scope': 'Declared policy applied to the same computed domain outcomes; not human semantic ground truth.',
        'positive': {'case_id': cases['positive']['id'], 'case_digest': cases['positive']['digest'],
                     'source_result_digest': digest(fixed_monthly), 'source_status': fixed_monthly['status'],
                     'action': manifest['positive']['expected_action']},
        'negative': {'case_id': cases['negative']['id'], 'case_digest': cases['negative']['digest'],
                     'source_result_digest': digest(fixed_screen), 'source_status': fixed_screen['summary']['status'],
                     'action': manifest['negative']['expected_action']}})
    origin = {'positive': 'controlled_fixture', 'negative': 'saved_scan_not_source_authenticated' if scan_path else 'synthetic_aggregate_fixture',
              'negative_file_sha256': sha256(scan_file), 'raw_source_reverified': False}
    atomic_json(out / 'input-origin.json', origin)
    return store, cases, {'positive': positive, 'negative': negative}, origin


def scripted_proposal(store, case, action, key):
    from paper_alpha.server.research_tools import ResearchTools
    tools = ResearchTools(store)
    session = tools.create(case['id'], case['digest'], {'max_calls': 4, 'max_errors': 2, 'max_seconds': 60}, key)
    for name in ('read_case', 'read_evidence', 'read_result'):
        record = tools.call(session['id'], name, {}, key + '-' + name)
        if record['response']['ok'] is not True:
            raise AssertionError('Scripted diagnostic read was rejected: ' + name)
    evidence_ids = [item['id'] for item in case['context']['evidence']][:1]
    record = tools.call(session['id'], 'propose_next_action', {
        'action': action,
        'rationale': 'Predeclared engineering policy applied to the frozen tool result; this is an unverified automation proposal.',
        'evidence_ids': evidence_ids}, key + '-proposal')
    if record['response']['ok'] is not True:
        raise AssertionError('Declared next action could not be proposed')
    return tools.get(session['id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'status.json', {'status': 'running', 'passed': False})
    try:
        manifest = load_manifest()
        atomic_json(out / 'manifest.json', manifest)
        runner_hashes = snapshot_runner(out)
        store, cases, sources, origin = build_sources(out, manifest, args.input)
        sessions = {}
        for key, case in cases.items():
            session = scripted_proposal(store, case, manifest[key]['expected_action'], 'demo-' + key)
            sessions[key] = session
            atomic_json(out / (key + '-session.json'), session)
            proposal = session['calls'][-1]['response']['proposal']
            if (proposal['action'] != manifest[key]['expected_action'] or proposal['actor'] != 'automation'
                    or proposal['status'] != 'draft' or proposal['semantic_fidelity'] != 'unverified'):
                raise AssertionError('Automation proposal was incorrectly represented as an approval')
        no_reviews = (MonthlyExperiments(store).get(sources['positive']['experiment']['id'])['reviews'] == []
                      and AuthorStudies(store).reviews(sources['negative']['id']) == {'items': []})
        if not no_reviews:
            raise AssertionError('The diagnostic tools unexpectedly created source approvals')
        result = {'passed': True, 'scope': manifest['scope'], 'input_origin': origin,
                  'case_ids': {key: case['id'] for key, case in cases.items()},
                  'session_ids': {key: session['id'] for key, session in sessions.items()},
                  'fixed_flow_results_equal': True, 'monthly_independent_reference_passed': True,
                  'fixed_flow_actions_equal': True, 'source_reviews_unchanged': True, 'ground_truth': manifest['ground_truth'],
                  'manifest_digest': digest(manifest), 'runner_hashes': runner_hashes, 'llm_api_called': False}
        atomic_json(out / 'result.json', result)
        atomic_json(out / 'status.json', {'status': 'completed', 'passed': True})
        print(json.dumps({'passed': True, 'result': str(out / 'result.json')}))
    except Exception as exc:
        atomic_json(out / 'status.json', {'status': 'failed', 'passed': False, 'error_type': type(exc).__name__, 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
