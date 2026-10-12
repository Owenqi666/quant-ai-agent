"""Local source-linked preparation with separate raw/archive and scan scopes."""
from datetime import datetime, timezone
import argparse
from pathlib import Path
import re
import stat

from . import author_workflow, eligibility_workflow, research_protocol
from .author_archive import _verified_archive
from .evidence import sha256
from .storage import atomic_json, digest, json_text, read_json
from .workflow import environment

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_FIELDS = {'schema_version', 'kind', 'checked_at', 'binding_id', 'binding_digest', 'source', 'source_axis',
    'raw_archive_verified_now', 'existing_eligibility_artifacts_verified_now', 'eligibility_scan_recomputed_now',
    'existing_scan_check', 'existing_manifest_sha256', 'plan_digest', 'scan_digest', 'result_digest',
    'paper_pdf_sha256', 'panel_checks', 'external_associations', 'implementation_sha256', 'scope'}


def _path(path):
    value = Path(path).absolute()
    if value.resolve() != value:
        raise ValueError('Preparation paths cannot traverse symlinks')
    return value


def _json(path, maximum=2 * 1024 * 1024):
    path = _path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        raise ValueError('Preparation artifact must be a bounded independent regular file')
    return read_json(path)


def prepare(source_path, study_directory, paper_path, output_dir, *, protocol_config=None, panel_directories=()):
    """Reuse existing scans; verify fixed raw bytes now without another all-row scan."""
    from .server.service import Store
    from .server.author_panels import AuthorPanels
    from .server.author_studies import AuthorStudies
    from .server.research_protocols import ResearchProtocols
    from .server.research_bindings import ResearchBindings
    from .server.domain_research_jobs import DomainResearchJobs
    from .server.research_cases import ResearchCases
    source_path, study_directory, paper_path, out = map(_path, (source_path, study_directory, paper_path, output_dir))
    config = research_protocol.validate_config(protocol_config or research_protocol.presets()[0]['config'])
    if len(panel_directories) > 8:
        raise ValueError('At most eight exact author panels may be linked')
    out.mkdir(parents=True, exist_ok=False)
    request = {'schema_version': 1, 'kind': 'author_research_local_preparation',
               'source_filename': source_path.name, 'protocol_config': config,
               'study_directory': str(study_directory), 'paper_path': str(paper_path),
               'panel_directories': [str(_path(path)) for path in panel_directories],
               'policy': 'Verify pinned archive and existing artifacts; never rescan all development months or execute portfolios.'}
    atomic_json(out / 'request.json', request)
    try:
        artifact_check = eligibility_workflow.verify(study_directory)
        scan = _json(study_directory / 'input.json', 256 * 1024)
        if scan['source']['filename'] != source_path.name:
            raise ValueError('Scan and current source filename differ')
        with _verified_archive(source_path) as (_, source, _, metadata):
            if source != scan['source']:
                raise ValueError('Verified current archive differs from the frozen scan source')
            axis = {'first_month': metadata['months'][0], 'last_month': metadata['months'][-1], 'months': len(metadata['months'])}
        paper_info = paper_path.lstat()
        if not stat.S_ISREG(paper_info.st_mode) or paper_info.st_nlink != 1 or not 0 < paper_info.st_size <= 16 * 1024 * 1024:
            raise ValueError('Selected PDF must be a bounded independent regular file')
        from .research_binding import registry
        if sha256(paper_path) != registry()['pdf_sha256']:
            raise ValueError('Local preparation requires the exact fixed GJS PDF')
        store = Store(out / 'workspace')
        paper = store.add_paper(paper_path.read_bytes(), registry()['title'])
        protocol = ResearchProtocols(store).create('GJS author-source linked rules', 'Original unresolved paper rules remain declared.', config)
        panel_ids, panel_checks = [], []
        for path in panel_directories:
            path = _path(path)
            check = author_workflow.verify(path, source_path=source_path)
            panel = _json(path / 'input.json', 768 * 1024)
            item = AuthorPanels(store).create('Verified original-row diagnostic', 'Current local raw re-extraction; HTTP stores normalized consistency only.', panel, digest(panel))
            panel_ids.append(item['id'])
            panel_checks.append({'panel_id': item['id'], 'panel_digest': item['digest'], 'artifact_check': check,
                                 'manifest_sha256': sha256(path / 'manifest.json')})
        study = AuthorStudies(store).create('Existing predeclared GJS source scan',
            'Current fixed archive bytes verified; existing scan artifact verified, not rescanned in this preparation.',
            scan, None, panel_ids, 'existing-scan')
        preview = ResearchBindings(store).prepare(paper['id'], protocol['id'], study['id'], 'author_paper', 'GJS source-linked blocked research preparation')
        binding = ResearchBindings(store).create(**preview['request'], preview_digest=preview['preview_digest'], idempotency_key='binding')
        jobs = DomainResearchJobs(store)
        job = jobs.create({'kind': 'author_study_diagnostic', 'study_id': study['id'], 'study_digest': study['digest']},
                          {'max_steps': 2, 'max_failures': 1, 'max_seconds': 60}, 'author-diagnostic')
        jobs.advance(job['id'], 'validate', 'author-validate')
        job = jobs.get(job['id'])
        cases = ResearchCases(store)
        case_preview = cases.preview('author_study', study['id'])
        case = cases.create('GJS source-linked diagnostic review', 'No human labels or portfolio returns.',
                            'author_study', study['id'], case_preview['source_digest'], 'author-case')
        atomic_json(out / 'diagnostic-job.json', job)
        atomic_json(out / 'research-case.json', case)
        atomic_json(out / 'binding.json', binding)
        atomic_json(out / 'binding-export.json', ResearchBindings(store).export(binding['id']))
        (out / 'binding.md').write_text(ResearchBindings(store).markdown(binding['id']), encoding='utf-8')
        code_paths = ('paper_alpha/research_binding.py', 'paper_alpha/author_research_workflow.py',
                      'paper_alpha/author_archive.py', 'paper_alpha/author_archive_contract.py',
                      'paper_alpha/eligibility.py', 'paper_alpha/eligibility_workflow.py')
        receipt = {'schema_version': 1, 'kind': 'author_research_local_receipt', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'binding_id': binding['id'], 'binding_digest': binding['digest'], 'source': source, 'source_axis': axis,
            'raw_archive_verified_now': True, 'existing_eligibility_artifacts_verified_now': True,
            'eligibility_scan_recomputed_now': False, 'existing_scan_check': artifact_check,
            'existing_manifest_sha256': sha256(study_directory / 'manifest.json'),
            'plan_digest': digest(scan['plan']), 'scan_digest': digest(scan), 'result_digest': digest(study['result']),
            'paper_pdf_sha256': sha256(paper_path), 'panel_checks': panel_checks,
            'external_associations': {'domain_job_id': job['id'], 'domain_job_digest': job['digest'],
                'research_case_id': case['id'], 'research_case_digest': case['digest'],
                'scope': 'Separate existing study diagnostic/case references, not binding-internal execution authority.'},
            'implementation_sha256': {name: sha256(ROOT / name) for name in code_paths},
            'scope': 'Offline local provenance receipt. It does not authenticate HTTP imports, human identity, count creation time or portfolio authority.'}
        atomic_json(out / 'local-source-receipt.json', receipt)
        atomic_json(out / 'environment.json', environment())
        files = {name: sha256(out / name) for name in ('request.json', 'binding.json', 'binding-export.json', 'binding.md',
                 'local-source-receipt.json', 'environment.json', 'diagnostic-job.json', 'research-case.json')}
        atomic_json(out / 'manifest.json', {'schema_version': 1, 'kind': 'author_research_preparation_bundle', 'binding_digest': binding['digest'], 'files': files})
        return verify(out)
    except Exception as exc:
        atomic_json(out / 'error.json', {'status': 'failed', 'request_digest': digest(request), 'error_type': type(exc).__name__, 'message': str(exc)[:4000]})
        raise


def verify(output_dir, *, source_path=None):
    """Verify frozen outputs; optional current raw bytes never imply scan recomputation."""
    from .server.service import Store
    from .server.research_bindings import ResearchBindings
    from .server.domain_research_jobs import DomainResearchJobs
    from .server.research_cases import ResearchCases
    out = _path(output_dir)
    manifest = _json(out / 'manifest.json')
    expected = {'request.json', 'binding.json', 'binding-export.json', 'binding.md', 'local-source-receipt.json',
                'environment.json', 'diagnostic-job.json', 'research-case.json'}
    if (set(manifest) != {'schema_version', 'kind', 'binding_digest', 'files'} or manifest['schema_version'] != 1
            or manifest['kind'] != 'author_research_preparation_bundle' or set(manifest['files']) != expected
            or (out / 'error.json').exists()):
        raise ValueError('Preparation manifest differs or attempt failed')
    for name, fingerprint in manifest['files'].items():
        info = (out / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024 or sha256(out / name) != fingerprint:
            raise ValueError('Preparation file identity changed')
    binding = _json(out / 'binding.json')
    service = ResearchBindings(Store(out / 'workspace'))
    if (service.get(binding['id']) != binding or binding['digest'] != manifest['binding_digest']
            or service.export(binding['id']) != _json(out / 'binding-export.json')
            or service.markdown(binding['id']) != (out / 'binding.md').read_text(encoding='utf-8')):
        raise ValueError('Preparation binding differs from original exact references')
    receipt = _json(out / 'local-source-receipt.json')
    context = binding['context']
    check = receipt.get('existing_scan_check', {})
    if (set(receipt) != RECEIPT_FIELDS or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or receipt['kind'] != 'author_research_local_receipt' or context['source_scope'] != 'author_paper'
            or receipt['scope'] != 'Offline local provenance receipt. It does not authenticate HTTP imports, human identity, count creation time or portfolio authority.'
            or receipt['binding_id'] != binding['id'] or receipt['binding_digest'] != binding['digest']
            or receipt['source'] != context['source'] or receipt['plan_digest'] != context['study']['plan_digest']
            or receipt['scan_digest'] != context['study']['scan_digest'] or receipt['result_digest'] != context['study']['result_digest']
            or receipt['paper_pdf_sha256'] != context['paper']['pdf_sha256']
            or receipt['raw_archive_verified_now'] is not True or receipt['eligibility_scan_recomputed_now'] is not False
            or receipt['existing_eligibility_artifacts_verified_now'] is not True):
        raise ValueError('Local receipt is not bound to the exact preparation')
    timestamp = datetime.fromisoformat(receipt['checked_at'])
    if (timestamp.tzinfo is None or receipt['source_axis'] != {'first_month': context['source']['first_month'],
            'last_month': context['source']['last_month'], 'months': context['source']['periods']}
            or type(receipt['source_axis']['months']) is not int
            or check.get('verified') is not True or check.get('raw_source_reverified') is not False
            or check.get('verification_scope') != 'aggregate_consistency_only'
            or check.get('plan_digest') != receipt['plan_digest'] or check.get('input_digest') != receipt['scan_digest']
            or check.get('result_digest') != receipt['result_digest'] or check.get('summary') != context['study']['summary']
            or not re.fullmatch(r'[0-9a-f]{64}', receipt['existing_manifest_sha256'])):
        raise ValueError('Receipt historical artifact verification scope changed')
    refs_expected = context['study']['panels']
    panel_checks = receipt['panel_checks']
    if (not isinstance(panel_checks, list) or len(panel_checks) != len(refs_expected)
            or any(set(p) != {'panel_id', 'panel_digest', 'artifact_check', 'manifest_sha256'}
                or p['panel_id'] != ref['id'] or p['panel_digest'] != ref['digest']
                or p['artifact_check'].get('verified') is not True or p['artifact_check'].get('raw_source_reverified') is not True
                or not re.fullmatch(r'[0-9a-f]{64}', p['manifest_sha256']) for p, ref in zip(panel_checks, refs_expected))):
        raise ValueError('Receipt original-row checks do not cover the exact linked panels')
    refs = receipt['external_associations']
    case = ResearchCases(service.store).get(refs['research_case_id'])
    job = DomainResearchJobs(service.store).get(refs['domain_job_id'])
    frozen_job = _json(out / 'diagnostic-job.json')
    # Wall time is a live observation, separate from frozen steps/results.
    job['usage'].pop('elapsed_seconds'); frozen_job['usage'].pop('elapsed_seconds')
    if (job != frozen_job or job['digest'] != refs['domain_job_digest'] or job['state'] != 'blocked'
            or job['source']['study_id'] != context['study']['id'] or job['experiment_id'] is not None
            or case != _json(out / 'research-case.json') or case['digest'] != refs['research_case_digest']
            or case['source_id'] != context['study']['id']):
        raise ValueError('Separate diagnostic/case association differs from the exact study')
    raw_now = False
    if source_path is not None:
        with _verified_archive(source_path) as (_, source, _, __):
            if source != context['source']:
                raise ValueError('Current raw source differs from the preparation')
        raw_now = True
    return {'passed': True, 'scope': 'Source-linked blocked research preparation; no portfolio returns or paper replication.',
            'binding_id': binding['id'], 'binding_digest': binding['digest'], 'status': 'blocked',
            'raw_archive_verified_at_preparation_claim': True, 'raw_archive_reverified_now': raw_now,
            'existing_eligibility_artifacts_verified_at_preparation_claim': True, 'eligibility_scan_recomputed_now': False,
            'execution_ready': False, 'human_reviews_written': 0, 'llm_api_called': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('prepare')
    run.add_argument('--source', required=True, type=Path)
    run.add_argument('--study', required=True, type=Path)
    run.add_argument('--paper', required=True, type=Path)
    run.add_argument('--out', required=True, type=Path)
    run.add_argument('--panel', type=Path, action='append', default=[])
    check = sub.add_parser('verify')
    check.add_argument('directory', type=Path)
    check.add_argument('--source', type=Path)
    args = parser.parse_args(argv)
    result = (prepare(args.source, args.study, args.paper, args.out, panel_directories=args.panel)
              if args.command == 'prepare' else verify(args.directory, source_path=args.source))
    print(json_text(result))


if __name__ == '__main__':
    main()
