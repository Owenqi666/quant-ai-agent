#!/usr/bin/env python3
"""Real monthly queue/baseline and author scientific stops in a new workspace."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha import eligibility, research_protocol
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.domain_research_jobs import DomainResearchJobs
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest


def run(output):
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    store = Store(out / 'workspace')
    jobs, monthly = DomainResearchJobs(store), MonthlyExperiments(store)
    protocol = ResearchProtocols(store).create('Monthly MOM/ID explicit project fixture',
        'Source-linked project conventions; not original-paper reproduction.', research_protocol.presets()[1]['config'])
    config = {'schema_version': 1, 'start_month': '2025-01', 'end_month': '2025-02', 'cost_bps': 10, 'min_assets': 18}
    source = {'kind': 'monthly_fixture', 'protocol_id': protocol['id'], 'protocol_digest': protocol['digest'], 'config': config}
    budget = {'max_steps': 12, 'max_failures': 2, 'max_seconds': 300}
    atomic_json(out / 'input.json', {'source': source, 'budget': budget})
    job = jobs.create(source, budget, 'example', 'Deterministic fixture example; no AI provider or human approval.')
    for action in ('validate', 'submit'):
        receipt = jobs.advance(job['id'], action, action)
        assert receipt['error'] is None, receipt
    job = jobs.get(job['id'])
    atomic_json(out / 'submitted.json', job)
    with worker_lock(store.root) as descriptor:
        worker = Worker(store, descriptor, poll_seconds=.01)
        assert worker.run_once()
    assert jobs.advance(job['id'], 'observe', 'observe')['next_state'] == 'observed'
    assert jobs.advance(job['id'], 'complete', 'complete')['next_state'] == 'completed'
    job = jobs.get(job['id'])
    actual = monthly.get(job['experiment_id'])
    atomic_json(out / 'job.json', job)
    atomic_json(out / 'monthly-result.json', actual)
    (out / 'job.md').write_text(jobs.markdown(job['id']), encoding='utf-8')
    preview = ResearchCases(store).preview('monthly_experiment', job['experiment_id'])
    case = ResearchCases(store).create('Bounded monthly project fixture', 'Control flow example; not market research evidence.',
        'monthly_experiment', job['experiment_id'], preview['source_digest'], 'monthly-case')
    atomic_json(out / 'case.json', case)
    baseline_id = monthly.create(protocol['id'], protocol['digest'], config, 'fixed-baseline')['experiment_id']
    with worker_lock(store.root) as descriptor:
        assert Worker(store, descriptor, poll_seconds=.01).run_once()
    baseline = monthly.get(baseline_id)
    atomic_json(out / 'baseline.json', baseline)
    equality = actual['result'] == baseline['result']
    assert equality

    stops = []
    paper = ResearchProtocols(store).create('Original unresolved method', '', research_protocol.presets()[0]['config'])
    blocked = jobs.create({**source, 'protocol_id': paper['id'], 'protocol_digest': paper['digest']}, budget, 'unresolved-paper')
    step = jobs.advance(blocked['id'], 'validate', 'validate')
    assert step['error']['code'] == 'RULES_UNRESOLVED'
    stops.append(jobs.get(blocked['id']))
    for threshold, expected in [(50000, 'DATA_INSUFFICIENT'), (1, 'AUTHOR_METHOD_UNRESOLVED')]:
        scan = deepcopy(eligibility.demo_scan())
        scan['plan']['minimum_assets'] = threshold
        scan['plan_digest'] = digest(scan['plan'])
        study = AuthorStudies(store).create('Synthetic aggregate author screen ' + str(threshold),
            'Not a verified raw-source import and not a return experiment.', scan, None, [], 'study-' + str(threshold))
        blocked = jobs.create({'kind': 'author_study_diagnostic', 'study_id': study['id'], 'study_digest': study['digest']}, budget, 'author-' + str(threshold))
        step = jobs.advance(blocked['id'], 'validate', 'validate')
        assert step['error']['code'] == expected, step
        stops.append(jobs.get(blocked['id']))
    step_closed = jobs.create(source, {**budget, 'max_steps': 1}, 'step-closed')
    jobs.advance(step_closed['id'], 'validate', 'validate')
    assert jobs.get(step_closed['id'])['state'] == 'exhausted'
    stops.append(jobs.get(step_closed['id']))
    cancelled = jobs.create(source, budget, 'cancelled')
    jobs.advance(cancelled['id'], 'validate', 'validate'); jobs.advance(cancelled['id'], 'submit', 'submit')
    jobs.cancel(cancelled['id'], 'cancel')
    stops.append(jobs.get(cancelled['id']))
    expired = jobs.create(source, {**budget, 'max_seconds': 1}, 'expired')
    jobs.advance(expired['id'], 'validate', 'validate'); jobs.advance(expired['id'], 'submit', 'submit')
    time.sleep(1.05)  # Actual elapsed wall time; no synthetic successful experiment.
    with worker_lock(store.root) as descriptor:
        Worker(store, descriptor, poll_seconds=.01).run_once()
    expired = jobs.get(expired['id'])
    assert expired['state'] == 'exhausted'
    assert monthly.status(expired['experiment_id'])['status'] == 'failed'
    stops.append(expired)
    for index, stopped in enumerate(stops, 1):
        atomic_json(out / ('stop-' + str(index) + '.json'), stopped)
    result = {'passed': True, 'scope': 'Bounded deterministic control only; fixture returns and aggregate diagnostics are not paper replication, actual-market evidence, or AI/human workflow advantage.',
              'job_id': job['id'], 'experiment_id': job['experiment_id'], 'case_id': case['id'],
              'actual_monthly_worker_executed': True, 'same_domain_baseline_equal': equality,
              'stops': len(stops), 'human_reviews_written': 0, 'llm_api_called': False,
              'data_kind': actual['result']['data_kind'], 'source_id': actual['result']['source_id']}
    atomic_json(out / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print(run(args.out))


if __name__ == '__main__':
    main()
