"""Actual isolated Alpha101 execution, exact baseline and bounded stop examples.

No model/network calls, author portfolio or human approval. Output is a new
directory and contains immutable input, job, run, case and baseline artifacts.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.server.service import Store
from paper_alpha.server.research_jobs import ResearchJobs, JobServiceError
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.storage import atomic_json, digest, json_text
from paper_alpha.evidence import sha256


def draft_from_base(task):
    return {key: deepcopy(task[key]) for key in ('evidence', 'hypotheses', 'candidates')}


def _advance(jobs, identity, action, key=None):
    step = jobs.advance(identity, action, key or action)
    if step['error']:
        raise AssertionError('Actual job transition failed: ' + json_text(step))
    return step


def build_demo(out):
    """Out must already be an empty new directory; returns saved result metadata."""
    out = Path(out).resolve()
    store = Store(out / 'workspace')
    jobs = ResearchJobs(store)
    seed = store.seed_example()
    research = store.get_research(seed['research_id'])
    base = research['revisions'][0]
    draft = draft_from_base(base['task'])
    budget = {'max_steps': 10, 'max_failures': 3, 'max_seconds': 180}
    create = {'research_id': seed['research_id'], 'base_revision_id': seed['revision_id'], 'draft': draft,
              'budget': budget, 'idempotency_key': 'v018-demo-positive',
              'note': 'Explicit partial engineering execution; economic interpretation still awaits human review.',
              'allow_partial_execution': True}
    atomic_json(out / 'input.json', create)
    atomic_json(out / 'source-research.json', research)
    job = jobs.create(**create)
    for action in ('validate', 'commit', 'submit'):
        _advance(jobs, job['id'], action)
    submitted = jobs.get(job['id'])
    _advance(jobs, job['id'], 'observe', 'queued-observation')
    # The existing worker launches the existing isolated calculator and verifier.
    with worker_lock(store.root) as descriptor:
        worker = Worker(store, descriptor)
        if not worker.run_once():
            raise AssertionError('The actual submitted run was not executed')
    _advance(jobs, job['id'], 'observe', 'completed-observation')
    _advance(jobs, job['id'], 'complete')
    job = jobs.get(job['id'])
    result = json.loads(job['result_json'])
    if job['state'] != 'completed' or not result['verification']['verified']:
        raise AssertionError('No exact verified run result was produced')
    # Same revision/data/settings and same calculator: a deterministic fixed
    # baseline validates integration, without claiming an independent strategy.
    baseline = store.submit_run(job['revision_id'], 'normalized_fixed', 'v018-demo-fixed-baseline')
    with worker_lock(store.root) as descriptor:
        if not Worker(store, descriptor).run_once():
            raise AssertionError('Fixed baseline did not execute')
    baseline = store.get_run(baseline['id'])
    left = {c['id']: {'status': c['status'], 'expression': c['expression'], 'result': c.get('result')} for c in result['state']['candidates']}
    right = {c['id']: {'status': c['status'], 'expression': c['expression'], 'result': c.get('result')} for c in baseline['state']['candidates']}
    if left != right or not baseline['verification']['verified']:
        raise AssertionError('Actual job differs from the same-condition fixed baseline')
    cases = ResearchCases(store)
    preview = cases.preview('daily_run', job['run_id'])
    case = cases.create('Alpha101 actual provider-free result', 'Software demonstration; unverified economic interpretation.',
                        'daily_run', job['run_id'], preview['source_digest'], 'v018-demo-case')
    atomic_json(out / 'positive-job.json', job)
    atomic_json(out / 'positive-run.json', result)
    atomic_json(out / 'fixed-baseline.json', baseline)
    atomic_json(out / 'research-case.json', case)
    (out / 'positive-job.md').write_text(jobs.markdown(job['id']), encoding='utf-8')
    (out / 'research-case.md').write_text(cases.markdown(case['id']), encoding='utf-8')

    # New explicit jobs keep the original frozen development/test rules.
    fresh_base = store.get_research(seed['research_id'])['revisions'][-1]
    clean = draft_from_base(fresh_base['task'])
    examples = {}
    for name in ('missing_field', 'illegal_expression', 'rules_unresolved', 'evidence_mismatch', 'budget_stop', 'cancelled'):
        args = {**create, 'base_revision_id': fresh_base['id'], 'draft': deepcopy(clean),
                'idempotency_key': 'v018-stop-' + name, 'allow_partial_execution': False}
        if name == 'missing_field':
            args['draft']['hypotheses'][0]['required_fields'].append('vwap')
            args['draft']['hypotheses'][0]['attribution'] = 'user_modification'
        elif name == 'illegal_expression':
            args['draft']['candidates'][0].update(origin='model_conjecture', changes=['Invalid untrusted expression test'], expression="__import__('os').system('ls')")
        elif name == 'rules_unresolved':
            args.update(method_status='unresolved', unresolved_rules=['Unconfirmed data publication time'])
        elif name == 'evidence_mismatch':
            args['draft']['evidence'][0]['quote'] = 'This quote is absent from the source PDF.'
        elif name == 'budget_stop':
            args['budget'] = {'max_steps': 1, 'max_failures': 1, 'max_seconds': 180}
        stopped = jobs.create(**args)
        step = jobs.cancel(stopped['id'], 'cancel') if name == 'cancelled' else jobs.advance(stopped['id'], 'validate', 'validate')
        value = jobs.get(stopped['id'])
        expected = 'cancelled' if name == 'cancelled' else 'exhausted' if name == 'budget_stop' else 'blocked'
        if value['state'] != expected:
            raise AssertionError(name + ' failed to stop correctly: ' + json_text(value))
        if name == 'budget_stop':
            try:
                jobs.advance(stopped['id'], 'commit', 'forbidden-next')
            except JobServiceError as exc:
                if exc.code != 'JOB_EXHAUSTED':
                    raise
            else:
                raise AssertionError('Budget stop allowed another effect')
        atomic_json(out / (name + '-input.json'), args)
        atomic_json(out / (name + '-job.json'), value)
        examples[name] = {'job_id': value['id'], 'state': value['state'], 'stop_reason': value['stop_reason'],
                          'step_error': step['error'], 'revision_id': value['revision_id'], 'run_id': value['run_id']}
    if len(store.get_research(seed['research_id'])['revisions']) != 2 or len(store.list_runs()) != 2:
        raise AssertionError('Blocked/cancelled/budget examples created unapproved effects')
    value = {'passed': True, 'scope': 'provider_free_engineering_integration', 'job_id': job['id'], 'run_id': job['run_id'],
             'case_id': case['id'], 'baseline_run_id': baseline['id'], 'fixed_flow_results_equal': True,
             'baseline_scope': 'Same engine and immutable revision; deterministic integration comparison, not independent financial validation.',
             'retained_exclusions': [c['candidate_id'] for c in job['candidate_checks'] if c['status'] == 'blocked'],
             'stop_examples': examples, 'input_digest': digest(create), 'runner_sha256': sha256(Path(__file__)),
             'human_reviews_written': 0, 'llm_api_called': False, 'semantic_ground_truth': 'pending_human_confirmation',
             'market_performance_established': False}
    atomic_json(out / 'result.json', value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'status.json', {'passed': False, 'status': 'running'})
    try:
        result = build_demo(out)
        atomic_json(out / 'status.json', {'passed': True, 'status': 'completed'})
        print(json.dumps({'passed': result['passed'], 'result': str(out / 'result.json')}))
    except Exception as exc:
        atomic_json(out / 'status.json', {'passed': False, 'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
