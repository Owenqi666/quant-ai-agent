"""Isolated automation-only material annotations and retained replacement history.

No actual human annotations, experiment approval, model output or efficiency
measurements. Creates a new artifact directory and preserves negative receipts.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha import semantic_materials as materials
from paper_alpha.evidence import sha256
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.server.semantic_annotations import SemanticAnnotations
from paper_alpha.storage import atomic_json, digest

ENGINEERING_CASES = [
    'frozen_sources_readable', 'preview_has_no_effect', 'durable_response_replay',
    'changed_payload_conflict', 'explicit_replacement_preserves_history',
    'branching_replacement_rejected', 'approval_injection_rejected',
    'automation_excluded_from_human_labels',
]


def build_demo(out):
    out = Path(out).resolve()
    if any(out.iterdir()):
        raise ValueError('Semantic demo output must be a new empty directory')
    atomic_json(out / 'status.json', {'passed': False, 'status': 'running'})
    atomic_json(out / 'engineering-manifest.json', {
        'schema_version': 1, 'scope': 'software_policy_only', 'cases': ENGINEERING_CASES,
        'material_sha256': materials.MATERIAL_SHA256, 'human_labels_pending': True,
    })
    store = Store(out / 'workspace')
    service = SemanticAnnotations(store)
    collection = service.materials()
    atomic_json(out / 'material-collection.json', collection)
    # Preserve the known source files; no path supplied by a declaration is read.
    bundle = out / 'frozen-material'
    for relative in ('evaluation_suites/v018/semantic_cases.json', 'evaluation_suites/v018/manifest.json',
                     *[value[0] for value in materials.SOURCE_FILES.values()]):
        target = bundle / relative; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    checks = {'frozen_sources_readable': collection['total'] == 7 and len(materials.load_material(root=bundle)['cases']) == 7}
    request = {'material_sha256': materials.MATERIAL_SHA256, 'case_id': 'alpha101_formula',
               'source': 'automation', 'reviewer': 'Automation-only v0.19 demonstration; no human judgment',
               'confirmed_at': '2020-01-01T00:00:00+00:00',
               'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Automation demonstration leaves semantic judgment unresolved.'}
                              for name in materials.DIMENSIONS},
               'supersedes_id': None, 'idempotency_key': 'v019-semantic-demo-original'}
    atomic_json(out / 'submitted-automation-input.json', request)
    before = service.list()['total']
    preview = service.preview(**{k: v for k, v in request.items() if k != 'idempotency_key'})
    atomic_json(out / 'preview.json', preview)
    checks['preview_has_no_effect'] = service.list()['total'] == before
    first = service.create(**request)
    replay = service.create(**request)
    checks['durable_response_replay'] = first == replay and service.list()['total'] == 1
    atomic_json(out / 'first-annotation.json', first)
    atomic_json(out / 'same-key-replay.json', replay)

    def rejection(name, submitted):
        atomic_json(out / (name + '-request.json'), submitted)
        try:
            service.create(**submitted)
        except ServiceError as exc:
            atomic_json(out / (name + '-response.json'), {'rejected': True, 'status': exc.status, 'error': str(exc)})
            return exc.status
        raise AssertionError('Expected semantic declaration rejection: ' + name)

    altered = deepcopy(request); altered['dimensions']['evidence_accuracy']['reason'] += ' Changed body.'
    checks['changed_payload_conflict'] = rejection('changed-payload', altered) == 409
    replacement = deepcopy(request)
    replacement.update(idempotency_key='v019-semantic-demo-replacement', supersedes_id=first['id'], confirmed_at='2020-01-02T00:00:00+00:00')
    replacement['dimensions']['evidence_accuracy']['reason'] = 'Automation fixture text revised; still no actual semantic judgment.'
    second = service.create(**replacement)
    atomic_json(out / 'replacement-input.json', replacement)
    atomic_json(out / 'replacement-annotation.json', second)
    history = service.list(); atomic_json(out / 'annotation-history.json', history)
    checks['explicit_replacement_preserves_history'] = (history['total'] == 2
        and service.get(first['id']) == first and second['supersedes_digest'] == first['digest'])
    branch = deepcopy(replacement); branch['idempotency_key'] = 'v019-semantic-demo-illegal-branch'
    checks['branching_replacement_rejected'] = rejection('branch', branch) == 409
    forged = deepcopy(request); forged['idempotency_key'] = 'v019-semantic-demo-forged'; forged['claim_approval'] = True
    checks['approval_injection_rejected'] = rejection('forged-approval', forged) == 422
    summary = service.summary(); atomic_json(out / 'summary.json', summary)
    checks['automation_excluded_from_human_labels'] = (summary['automation_records'] == 2
        and summary['human_records'] == 0 and summary['human_annotated_cases'] == 0
        and summary['pending_cases'] == 7 and summary['semantic_quality_score'] is None)
    if not all(checks.values()):
        raise AssertionError('Semantic demonstration software policy check failed: ' + json.dumps(checks))
    result = {'schema_version': 1, 'passed': True, 'scope': 'automation_material_annotation_software_policy_only',
              'cases_total': len(checks), 'cases_passed': sum(checks.values()), 'checks': checks,
              'annotation_ids': [first['id'], second['id']], 'human_records': 0,
              'human_labels_pending': True, 'llm_api_called': False, 'semantic_quality_score': None,
              'experiment_approvals_written': 0, 'claim_approvals_written': 0,
              'material_sha256': materials.MATERIAL_SHA256}
    result['artifacts'] = {str(path.relative_to(out)): sha256(path) for path in sorted(out.rglob('*'))
                           if path.is_file() and 'workspace' not in path.relative_to(out).parts and path.name != 'status.json'}
    atomic_json(out / 'result.json', result)
    (out / 'report.md').write_text('\n'.join([
        '# v0.19 semantic material control demonstration', '',
        'Eight software-policy checks passed. The two saved records are automation fixtures, not human judgments.', '',
        'Seven source-linked material cases remain pending actual human confirmation; no semantic quality score exists.', '',
        'The exact original declaration, response replay, replacement history and rejected requests are saved beside this report.', '',
        'No model call, experiment approval, claim approval or human-efficiency measurement occurred.', '',
    ]), encoding='utf-8')
    atomic_json(out / 'status.json', {'passed': True, 'status': 'completed', 'scope': result['scope']})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    try:
        result = build_demo(out)
        print(json.dumps({'passed': True, 'cases_passed': result['cases_passed'], 'result': str(out / 'result.json')}))
    except Exception as exc:
        atomic_json(out / 'status.json', {'passed': False, 'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
