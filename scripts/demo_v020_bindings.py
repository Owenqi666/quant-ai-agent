#!/usr/bin/env python3
"""Portable fixture-document binding demo; no GJS PDF/MAT or market claims."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_alpha import eligibility, research_protocol
from paper_alpha.evidence import sha256
from paper_alpha.storage import atomic_json, digest, json_text, read_json
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.research_bindings import ResearchBindings
from paper_alpha.server.domain_research_jobs import DomainResearchJobs
from paper_alpha.server.research_cases import ResearchCases


def run(output_dir):
    out = Path(output_dir).absolute()
    if out.resolve() != out:
        raise ValueError('Demo destination cannot traverse symlinks')
    out.mkdir(parents=True, exist_ok=False)
    store = Store(out / 'workspace')
    paper_path = ROOT / 'examples/alpha101/paper.pdf'
    paper = store.add_paper(paper_path.read_bytes(), 'Bundled Alpha101: document contract only')
    protocol = ResearchProtocols(store).create('Synthetic project declarations', 'This protocol is not Alpha101 or GJS reproduction.', research_protocol.presets()[1]['config'])
    scan = eligibility.demo_scan()
    scan['plan']['minimum_assets'] = 50000
    scan['plan_digest'] = digest(scan['plan'])
    atomic_json(out / 'synthetic-scan.json', scan)
    study = AuthorStudies(store).create('Synthetic screen fixture', 'All counts are synthetic; source metadata is a declared contract fixture.', scan, None, [], 'study')
    service = ResearchBindings(store)
    preview = service.prepare(paper['id'], protocol['id'], study['id'], 'controlled_contract_fixture', 'Portable engineering binding fixture')
    request = preview['request'] | {'preview_digest': preview['preview_digest'], 'idempotency_key': 'fixture-binding'}
    binding = service.create(**request)
    replay = service.create(**request)
    jobs = DomainResearchJobs(store)
    job = jobs.create({'kind': 'author_study_diagnostic', 'study_id': study['id'], 'study_digest': study['digest']},
                      {'max_steps': 2, 'max_failures': 1, 'max_seconds': 60}, 'diagnostic')
    jobs.advance(job['id'], 'validate', 'validate')
    job = jobs.get(job['id'])
    cases = ResearchCases(store)
    case_preview = cases.preview('author_study', study['id'])
    case = cases.create('Synthetic diagnostic review', 'No human review.', 'author_study', study['id'], case_preview['source_digest'], 'case')
    checks = {'exact_replay': replay == binding, 'get_preserved': service.get(binding['id']) == binding,
              'blocked': binding['context']['status'] == 'blocked' and not binding['context']['execution_ready'],
              'fixture_explicit': binding['context']['paper']['evidence_scope'] == 'unrelated_fixture_document_contract',
              'data_stop': job['state'] == 'blocked' and job['stop_reason'] == 'DATA_INSUFFICIENT' and job['experiment_id'] is None,
              'case_no_reviews': case['context']['reviews'] == []}
    try:
        service.preview(**(preview['request'] | {'source_scope': 'author_paper'}))
    except ServiceError:
        checks['fixture_cannot_elevate'] = True
    else:
        checks['fixture_cannot_elevate'] = False
    export = service.export(binding['id'])
    atomic_json(out / 'binding-export.json', export)
    (out / 'binding.md').write_text(service.markdown(binding['id']), encoding='utf-8')
    atomic_json(out / 'diagnostic-job.json', job)
    atomic_json(out / 'research-case.json', case)
    atomic_json(out / 'inputs.json', {'scope': 'Synthetic fixed engineering contract; unrelated Alpha101 document, not selected GJS paper.',
        'bundled_document_sha256': sha256(paper_path),
        'implementation_sha256': {name: sha256(ROOT / name) for name in ('paper_alpha/research_binding.py', 'paper_alpha/server/research_bindings.py')},
        'source_counts': 'synthetic', 'raw_source_verified': False,
        'binding_id': binding['id'], 'binding_digest': binding['digest'], 'study_id': study['id'],
        'external_associations': {'domain_job_id': job['id'], 'domain_job_digest': job['digest'], 'research_case_id': case['id'], 'research_case_digest': case['digest']}})
    names = ('synthetic-scan.json', 'binding-export.json', 'binding.md', 'diagnostic-job.json', 'research-case.json', 'inputs.json')
    atomic_json(out / 'manifest.json', {'schema_version': 1, 'files': {name: sha256(out / name) for name in names}})
    checks['saved_export_consistent'] = read_json(out / 'binding-export.json') == service.export(binding['id'])
    result = {'passed': all(checks.values()), 'scope': 'Controlled synthetic engineering contract with bundled Alpha101 document; no GJS reproduction, raw MAT authentication or portfolio returns.',
              'checks': checks, 'binding_id': binding['id'], 'binding_digest': binding['digest'],
              'status': 'blocked', 'execution_ready': False, 'raw_source_reverified': False,
              'human_reviews_written': 0, 'llm_api_called': False}
    atomic_json(out / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = run(args.out)
    print(json_text(result))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
