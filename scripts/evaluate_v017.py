"""Evaluate frozen engineering policies against provider-free research tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from demo_v017 import build_sources, load_manifest, scripted_proposal, snapshot_runner
from paper_alpha.evidence import sha256
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.service import ServiceError
from paper_alpha.storage import atomic_json, digest, read_json


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_evaluation(out):
    from paper_alpha.server.research_tools import ResearchTools
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'status.json', {'status': 'running', 'passed': False})
    checks = []
    active = None
    try:
        manifest = load_manifest()
        atomic_json(out / 'manifest.json', manifest)
        runner_hashes = snapshot_runner(out)
        store, cases, sources, origin = build_sources(out, manifest)
        tools = ResearchTools(store)
        records = out / 'decisions'
        records.mkdir()

        def session(key, source='positive', max_calls=8, max_errors=4):
            case = cases[source]
            return tools.create(case['id'], case['digest'],
                                {'max_calls': max_calls, 'max_errors': max_errors, 'max_seconds': 120}, key)

        def call(sess, name, arguments=None, key=None):
            value = tools.call(sess['id'], name, {} if arguments is None else arguments, key or name)
            active['calls'].append(value)
            atomic_json(records / (active['id'] + '.json'), active)
            return value['response']

        def proposal(action, source='negative', **extra):
            return {'action': action, 'rationale': 'Unverified scripted policy statement; all metrics stay tool-owned.',
                    'evidence_ids': [item['id'] for item in cases[source]['context']['evidence']][:1], **extra}

        def expect_error(response, code):
            require(response['ok'] is False and response['error']['code'] == code,
                    'Expected structured error ' + code)
            require(response['error']['retryable'] is False, 'Policy violations must not request automatic retry')

        for declared in manifest['cases']:
            active = {**declared, 'passed': False, 'checks': [], 'calls': [], 'sessions': [],
                      'case_ids': {key: value['id'] for key, value in cases.items()},
                      'expectation_origin': 'frozen_engineering_policy_not_human_semantic_label'}
            atomic_json(records / (active['id'] + '.json'), active)
            identity = declared['id']
            if identity == 'computed_result_trace':
                sess = session(identity)
                response = call(sess, 'read_result')
                require(response['ok'] is True, 'Computed result read failed')
                result_refs = json.loads(response['result_json'])
                require(result_refs == cases['positive']['context']['results'], 'Tool did not return the frozen result references')
                require(any(json.loads(ref['payload_json']) == sources['positive']['result'] for ref in result_refs),
                        'No tool result payload equals the actual monthly computation')
                require(all(digest(json.loads(ref['payload_json'])) == ref['digest'] for ref in result_refs),
                        'Tool result digest differs from its payload')
                require(sources['positive']['verification']['reference_passed'] is True, 'Independent monthly check failed')
                active['checks'] = ['exact_result_reference', 'fixed_flow_equal', 'independent_monthly_reference']
                active['sessions'] = [tools.get(sess['id'])]
            elif identity == 'data_insufficient_stop':
                sess = session(identity, 'negative')
                response = call(sess, 'read_case')
                require(response['ok'] is True, 'Scientific blocking must not be a tool error')
                require(cases['negative']['context']['state'] == 'data_insufficient', 'Expected blocked scientific state')
                require(sources['negative']['result']['summary']['months_meeting_threshold'] == manifest['negative']['expected_qualifying_months'],
                        'Frozen negative scan no longer has the declared screen outcome')
                response = call(sess, 'propose_next_action', proposal(manifest['negative']['expected_action']))
                require(response['ok'] is True and response['proposal']['action'] == manifest['negative']['expected_action'],
                        'Correct stop proposal failed')
                final = tools.get(sess['id'])
                require(final['usage']['errors'] == 0, 'Scientific blocking was incorrectly charged as a technical error')
                active['checks'] = ['data_insufficient', 'declared_stop', 'no_scientific_error_retry']
                active['sessions'] = [final]
            elif identity == 'evidence_identity':
                sess = session(identity, 'negative')
                response = call(sess, 'read_evidence')
                require(response['ok'] is True and json.loads(response['evidence_json']) == cases['negative']['context']['evidence'],
                        'Evidence must come from the exact case')
                bad = proposal('stop_data_insufficient', evidence_ids=['nonexistent-evidence'])
                expect_error(call(sess, 'propose_next_action', bad), 'EVIDENCE_NOT_FOUND')
                active['checks'] = ['frozen_evidence', 'unknown_evidence_rejected']
                active['sessions'] = [tools.get(sess['id'])]
            elif identity == 'attribution_preserved':
                for source in ('positive', 'negative'):
                    sess = session(identity + '-' + source, source)
                    response = call(sess, 'read_case')
                    require(response['ok'] is True and json.loads(response['case_json']) == cases[source],
                            'Context attribution changed across the tool boundary')
                    context = cases[source]['context']
                    require(any(item['attribution'] == 'project_convention' for item in context['definitions']),
                            'Project conventions must not disappear into paper attribution')
                    require(any(item['origin'] == 'project' for item in context['evidence']), 'Project evidence provenance missing')
                    active['sessions'].append(tools.get(sess['id']))
                require(any(item['origin'] == 'paper' for item in cases['negative']['context']['evidence']), 'Paper evidence provenance missing')
                require(manifest['ground_truth']['semantic_fidelity'] == 'pending_human_confirmation', 'Program cannot stamp semantic approval')
                active['checks'] = ['project_and_paper_distinguished', 'semantic_confirmation_pending']
            elif identity == 'forbidden_capability':
                sess = session(identity, max_calls=3, max_errors=3)
                for tool in ('execute_python', 'create_human_review', 'execute_author_portfolio'):
                    expect_error(call(sess, tool), 'TOOL_DENIED')
                active['checks'] = ['arbitrary_code_denied', 'human_approval_denied', 'author_portfolio_denied']
                active['sessions'] = [tools.get(sess['id'])]
            elif identity == 'metric_injection':
                sess = session(identity, 'negative')
                expect_error(call(sess, 'propose_next_action', proposal('stop_data_insufficient', counts={'ready': 99999})),
                             'ARGUMENTS_INVALID')
                require(AuthorStudies(store).get(sources['negative']['id']) == sources['negative'], 'Metric injection changed the source')
                active['checks'] = ['generated_counts_rejected', 'source_unchanged']
                active['sessions'] = [tools.get(sess['id'])]
            elif identity == 'disallowed_action':
                sess = session(identity, 'negative')
                expect_error(call(sess, 'propose_next_action', proposal('resolve_method')), 'ACTION_NOT_ALLOWED')
                require('execute_author_portfolio' not in cases['negative']['context']['allowed_actions'], 'Author portfolio execution was exposed')
                active['checks'] = ['wrong_next_action_rejected', 'no_author_portfolio_action']
                active['sessions'] = [tools.get(sess['id'])]
            elif identity == 'idempotent_replay':
                sess = session(identity)
                first = call(sess, 'read_case', key='same-read')
                before = tools.get(sess['id'])
                replay = call(sess, 'read_case', key='same-read')
                require(replay == first and tools.get(sess['id']) == before, 'Replay changed response or spent budget again')
                try:
                    tools.call(sess['id'], 'read_result', {}, 'same-read')
                except ServiceError as exc:
                    require(exc.status == 409, 'Changed replay payload did not conflict')
                    active['rejected_request'] = {'tool': 'read_result', 'arguments': {}, 'idempotency_key': 'same-read', 'http_status': exc.status}
                else:
                    raise AssertionError('Changed idempotency payload was accepted')
                active['checks'] = ['exact_replay', 'no_double_charge', 'changed_payload_conflict']
                active['sessions'] = [tools.get(sess['id'])]
            elif identity == 'restart_budget':
                sess = session(identity, max_calls=2)
                call(sess, 'read_case')
                before = tools.get(sess['id'])
                # A fresh interpreter has no access to the parent's objects or locks.
                program = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from paper_alpha.server.research_tools import ResearchTools
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import atomic_json
tools = ResearchTools(Store(Path(sys.argv[2])))
identity = sys.argv[3]
before = tools.get(identity)
second = tools.call(identity, "read_result", {}, "after-restart")
rejected = None
try:
    tools.call(identity, "read_evidence", {}, "beyond-budget")
except ServiceError as exc:
    rejected = {"tool": "read_evidence", "arguments": {}, "idempotency_key": "beyond-budget", "http_status": exc.status}
atomic_json(Path(sys.argv[4]), {"before": before, "second": second, "rejected": rejected, "after": tools.get(identity)})
'''
                process_output = records / 'restart-budget-process.json'
                process = subprocess.run([sys.executable, '-c', program, str(ROOT), str(store.root), sess['id'], str(process_output)],
                                         capture_output=True, text=True, timeout=60, cwd=ROOT)
                atomic_json(records / 'restart-budget-process-status.json',
                            {'returncode': process.returncode, 'stdout': process.stdout, 'stderr': process.stderr})
                require(process.returncode == 0, 'Fresh process could not restore the tool session; see saved process status')
                recovered = read_json(process_output)
                require(recovered['before'] == before, 'A fresh process lost or refreshed the session ledger')
                second = recovered['second']
                active['calls'].append(second)
                require(second['response']['ok'] is True, 'Remaining call budget was unavailable after restart')
                require(recovered['rejected'] is not None and recovered['rejected']['http_status'] == 429,
                        'Restart refreshed an exhausted budget')
                active['rejected_request'] = recovered['rejected']
                final = recovered['after']
                require(final['usage']['calls'] == 2 and final['budget']['max_calls'] == 2, 'Persisted call budget changed')
                active['checks'] = ['fresh_process_persisted_ledger', 'unchanged_budget', 'exhaustion_stops_execution']
                active['sessions'] = [final]
            elif identity == 'proposal_not_approval':
                for source in ('positive', 'negative'):
                    final = scripted_proposal(store, cases[source], manifest[source]['expected_action'], identity + '-' + source)
                    value = final['calls'][-1]['response']['proposal']
                    require(value['actor'] == 'automation' and value['status'] == 'draft' and value['semantic_fidelity'] == 'unverified',
                            'A proposal acquired human approval or semantic fidelity')
                    require(value['action'] == manifest[source]['expected_action'], 'Scripted action differs from the frozen fixed-flow decision')
                    active['sessions'].append(final)
                    active['calls'].extend(final['calls'])
                require(MonthlyExperiments(store).get(sources['positive']['experiment']['id'])['reviews'] == [], 'Monthly approval was created')
                require(AuthorStudies(store).reviews(sources['negative']['id']) == {'items': []}, 'Author approval was created')
                active['checks'] = ['automation_draft', 'unverified_semantics', 'no_source_approval', 'fixed_flow_actions_equal']
            else:
                raise AssertionError('Frozen policy has no evaluator')
            active['passed'] = True
            atomic_json(records / (active['id'] + '.json'), active)
            checks.append({'id': identity, 'passed': True, 'checks': active['checks'], 'record': 'decisions/' + identity + '.json'})
            atomic_json(out / 'progress.json', checks)

        result = {'schema_version': 1, 'passed': len(checks) == len(manifest['cases']) and all(item['passed'] for item in checks),
                  'scope': manifest['scope'], 'manifest_digest': digest(manifest), 'cases_total': len(manifest['cases']),
                  'cases_passed': sum(item['passed'] for item in checks), 'checks': checks, 'input_origin': origin,
                  'ground_truth': manifest['ground_truth'], 'comparison': manifest['comparison'],
                  'fixed_flow_results_equal': True, 'fixed_flow_actions_equal': True, 'monthly_independent_reference_passed': True,
                  'runner_hashes': runner_hashes, 'llm_api_called': False,
                  'human_time_saved': None, 'model_call_cost': None}
        atomic_json(out / 'result.json', result)
        lines = ['# v0.17 research policy evaluation', '',
                 f"Declared policy cases passed: {result['cases_passed']}/{result['cases_total']}",
                 f"Frozen manifest digest: {result['manifest_digest']}", '',
                 'Provider-free engineering policy checks; semantic fidelity awaits human confirmation.',
                 'Fixed flow and scripted tools share the same frozen domain results. This is integration consistency, not LLM or human-efficiency evidence.',
                 'Controlled monthly computation passed its existing independent numerical reference; author aggregate fixture is intentionally blocked.', '',
                 '| Case | Passed | Saved evidence |', '|---|---|---|']
        lines += [f"| {item['id']} | {item['passed']} | [{item['record']}]({item['record']}) |" for item in checks]
        lines += ['', 'LLM API calls: none. Human time saved: not measured. Model cost: not measured.',
                  'Original MAT authentication, actual market returns and original-paper replication are outside this suite.', '']
        (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
        # Hash human-readable results and individual records without indexing the mutable SQLite workspace.
        files = sorted(path for path in out.rglob('*') if path.is_file() and 'workspace' not in path.relative_to(out).parts
                       and path.name not in {'status.json', 'artifact-index.json'})
        atomic_json(out / 'artifact-index.json', {'schema_version': 1,
                    'files': {str(path.relative_to(out)): sha256(path) for path in files}})
        atomic_json(out / 'status.json', {'status': 'completed', 'passed': result['passed']})
        return result
    except Exception as exc:
        if active is not None:
            active['error'] = {'type': type(exc).__name__, 'message': str(exc)}
            atomic_json(out / 'decisions' / (active['id'] + '.json'), active)
        atomic_json(out / 'status.json', {'status': 'failed', 'passed': False, 'cases_completed': len(checks),
                                        'error_type': type(exc).__name__, 'error': str(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = run_evaluation(args.out)
    print(json.dumps({'passed': result['passed'], 'cases_passed': result['cases_passed'], 'result': str(args.out.resolve() / 'result.json')}))


if __name__ == '__main__':
    main()
