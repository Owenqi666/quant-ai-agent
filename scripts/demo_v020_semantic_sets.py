"""New-directory automation-only reference versions, comparison and offline export."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha import semantic_materials as materials
from paper_alpha.evidence import sha256
from paper_alpha.semantic_evaluation import verify_bundle
from paper_alpha.server.semantic_annotations import SemanticAnnotations
from paper_alpha.server.semantic_evaluation_sets import SemanticEvaluationSets
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import atomic_json

ENGINEERING_CASES = ['automation_attachment_not_reference', 'preview_has_no_write',
    'durable_exact_reference_replay', 'changed_reference_request_conflict',
    'exact_complete_comparison', 'comparison_unknown_is_null',
    'later_annotation_preserves_old_reference', 'bounded_offline_export_verification']


def build_demo(out):
    out = Path(out).resolve()
    if any(out.iterdir()):
        raise ValueError('Semantic evaluation demo needs a new empty directory')
    atomic_json(out / 'status.json', {'passed': False, 'status': 'running'})
    store = Store(out / 'workspace')
    annotations, service = SemanticAnnotations(store), SemanticEvaluationSets(store)
    label_request = {'material_sha256': materials.MATERIAL_SHA256, 'case_id': 'alpha101_formula',
                     'source': 'automation', 'reviewer': 'v0.20 automation demo; no human judgment',
                     'confirmed_at': '2020-01-01T00:00:00+00:00',
                     'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Automation-only demonstration leaves human assessment pending.'}
                                    for name in materials.DIMENSIONS},
                     'supersedes_id': None, 'idempotency_key': 'v020-set-auto-label'}
    label = annotations.create(**label_request)
    atomic_json(out / 'automation-label-request.json', label_request)
    atomic_json(out / 'automation-label.json', label)
    preview = service.preview(material_sha256=materials.MATERIAL_SHA256,
                              case_ids=[item['id'] for item in materials.load_material()['cases']])
    atomic_json(out / 'reference-preview.json', preview)
    checks = {'automation_attachment_not_reference': (preview['summary']['active_human_records'] == 0
        and preview['summary']['automation_audit_records'] == 1 and preview['summary']['pending_dimensions'] == 35),
        'preview_has_no_write': service.list()['total'] == 0}
    request = {'material_sha256': materials.MATERIAL_SHA256, 'case_ids': preview['case_ids'],
               'expected_active_annotations': preview['active_human_annotations'], 'idempotency_key': 'v020-set-demo'}
    reference, replay = service.create(**request), service.create(**request)
    atomic_json(out / 'reference-request.json', request)
    atomic_json(out / 'reference.json', reference); atomic_json(out / 'reference-replay.json', replay)
    checks['durable_exact_reference_replay'] = reference == replay and service.list()['total'] == 1
    changed = deepcopy(request); changed['case_ids'] = ['alpha101_formula']
    atomic_json(out / 'changed-request.json', changed)
    try:
        service.create(**changed)
        raise AssertionError('Changed exact request was accepted')
    except ServiceError as exc:
        atomic_json(out / 'changed-response.json', {'rejected': True, 'status': exc.status, 'error': str(exc)})
        checks['changed_reference_request_conflict'] = exc.status == 409
    compare_request = {'set_id': reference['id'], 'set_digest': reference['digest'], 'source': 'automation',
                       'reviewer': 'v0.20 automation declaration comparison; no provider',
                       'declared_at': '2020-01-01T00:00:00+00:00',
                       'execution_reference': 'Automation fixture declarations; no model execution or verified run provenance.',
                       'case_declarations': [{'case_id': case, 'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'No model or human declaration is synthesized.'}
                                             for name in materials.DIMENSIONS}} for case in reference['case_ids']],
                       'idempotency_key': 'v020-set-comparison'}
    comparison = service.compare(**compare_request)
    atomic_json(out / 'comparison-request.json', compare_request); atomic_json(out / 'comparison.json', comparison)
    checks['exact_complete_comparison'] = (comparison == service.compare(**compare_request)
        and comparison['set_digest'] == reference['digest'] and comparison['summary']['total_dimensions'] == 35)
    checks['comparison_unknown_is_null'] = (comparison['summary']['comparable_dimensions'] == 0
        and comparison['summary']['declaration_agreement_rate'] is None
        and comparison['summary']['semantic_quality_score'] is None)
    replacement_request = deepcopy(label_request)
    replacement_request.update(idempotency_key='v020-set-auto-replacement', supersedes_id=label['id'],
                               confirmed_at='2020-01-02T00:00:00+00:00')
    replacement_request['dimensions']['evidence_accuracy']['reason'] += ' Revised audit note only.'
    replacement = annotations.create(**replacement_request)
    atomic_json(out / 'automation-replacement-request.json', replacement_request)
    atomic_json(out / 'automation-replacement.json', replacement)
    next_request = {**request, 'idempotency_key': 'v020-set-next-version'}
    next_reference = service.create(**next_request)
    atomic_json(out / 'next-reference.json', next_reference)
    checks['later_annotation_preserves_old_reference'] = (service.get(reference['id']) == reference
        and service.create(**request) == reference and next_reference['digest'] != reference['digest'])
    bundle = service.export(reference['id'])
    atomic_json(out / 'semantic-evaluation-bundle.json', bundle)
    snapshot_verification = verify_bundle(bundle)
    source_verification = verify_bundle(bundle, root=ROOT)
    atomic_json(out / 'snapshot-verification.json', snapshot_verification)
    atomic_json(out / 'source-verification.json', source_verification)
    checks['bounded_offline_export_verification'] = (snapshot_verification['passed'] and source_verification['passed']
        and source_verification['source_bytes_reverified'] and not bundle['binary_sources_included'])
    if not all(checks.values()):
        raise AssertionError('Semantic evaluation software checks failed: ' + json.dumps(checks))
    summary = annotations.summary()
    if summary['human_records'] != 0:
        raise AssertionError('Automation-only demonstration created a human record')
    result = {'schema_version': 1, 'passed': True, 'scope': 'automation_reference_version_and_declaration_agreement_software_policy_only',
              'cases_total': len(checks), 'cases_passed': sum(checks.values()), 'checks': checks,
              'reference_ids': [reference['id'], next_reference['id']], 'comparison_id': comparison['id'],
              'human_records': 0, 'human_labels_pending': True, 'llm_api_called': False,
              'semantic_quality_score': None, 'declaration_agreement_rate': None,
              'experiment_approvals_written': 0, 'claim_approvals_written': 0}
    result['artifacts'] = {str(path.relative_to(out)): sha256(path) for path in sorted(out.rglob('*'))
                           if path.is_file() and 'workspace' not in path.relative_to(out).parts and path.name != 'status.json'}
    atomic_json(out / 'result.json', result)
    (out / 'report.md').write_text('\n'.join([
        '# v0.20 frozen semantic reference demonstration', '',
        'Eight software-policy checks use two automation audit records, seven pending material cases and no model call.', '',
        'The first reference remains unchanged after the automation history is revised. A second reference records the new snapshot.', '',
        'All 35 dimensions remain pending human reference. Comparable declarations are zero; agreement and model quality are null.', '',
        'The JSON export includes bounded material, manifest and registry text. The Alpha101 PDF is excluded and verified separately against installed bytes.', '',
        'Hash verification proves content integrity, not actual reviewer authorship or semantic truth.', '',
    ]), encoding='utf-8')
    atomic_json(out / 'status.json', {'passed': True, 'status': 'completed', 'scope': result['scope']})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(); out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    try:
        result = build_demo(out)
        print(json.dumps({'passed': True, 'cases_passed': result['cases_passed'], 'result': str(out / 'result.json')}))
    except Exception as exc:
        atomic_json(out / 'status.json', {'passed': False, 'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
