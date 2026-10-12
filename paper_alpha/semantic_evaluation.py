"""Pure frozen-reference derivation and offline integrity checks.

No DB, provider, label generation or experiment approval is performed here.
Hashes detect changed snapshots; they do not authenticate a declared person.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import semantic_materials as materials
from .evidence import sha256
from .storage import digest, json_text
from .server.semantic_annotations_schema import SemanticAnnotationDetail, SemanticMaterialDetail
from .server.semantic_evaluation_sets_schema import (
    SemanticEvaluationBundle, SemanticEvaluationComparisonCreate,
    SemanticEvaluationComparisonDetail, SemanticEvaluationSetDetail,
    SemanticEvaluationSetPreview,
)

MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_RECORDS = 500
LIMITATIONS = [
    'These seven fixed cases are development materials, not an independent final test set.',
    'Human source and reviewer identity are explicit local declarations, not authenticated authorship.',
    'All active human declarations are frozen; disagreement and unknown judgments are not resolved automatically.',
    'Automation is an audit attachment and cannot provide a human reference.',
    'Reference drafts remain unverified suggestions; labels do not approve experiments or claims.',
    'Agreement compares submitted declarations, not model output or semantic truth; model quality remains null.',
    'Bounded UTF-8 evidence is included; the Alpha101 binary PDF is digest-only and the original Momentum PDF is not reverified.',
    'A supplied execution reference is inert declared text, not verified execution provenance.',
    'Content and manifest hashes provide integrity, not trusted authorship or proof that an omitted reviewer exists.',
]
SET_FIELDS = set(SemanticEvaluationSetPreview.model_fields)
COMPARISON_FIELDS = set(SemanticEvaluationComparisonDetail.model_fields) - {'id', 'digest', 'created_at'}
SOURCE_PATHS = {
    'semantic_material': 'evaluation_suites/v018/semantic_cases.json',
    'semantic_manifest': 'evaluation_suites/v018/manifest.json',
    **{name: entry[0] for name, entry in materials.SOURCE_FILES.items()},
}


def bounded(value, maximum=MAX_BYTES):
    if len(json_text(value).encode('utf-8')) > maximum:
        raise ValueError('Semantic reference exceeds its bounded canonical JSON size')
    return value


def project_case(material, case):
    evidence = [deepcopy(item) for item in material['evidence'] if item['id'] in case['evidence_ids']]
    source_ids = {materials.SOURCE_IDS[item['source']] for item in evidence}
    for item in evidence:
        item['source_id'] = materials.SOURCE_IDS[item['source']]
    sources = [{'source_id': name, 'path': materials.SOURCE_FILES[name][0],
                'sha256': materials.SOURCE_FILES[name][1], 'media_type': materials.SOURCE_FILES[name][2],
                'verification': 'bundled_source_digest_verified', 'raw_pdf_reverified': name == 'alpha101_paper'}
               for name in sorted(source_ids)]
    return SemanticMaterialDetail.model_validate({
        'schema_version': 1, 'material_sha256': materials.MATERIAL_SHA256,
        'case_id': case['id'], 'material_case_digest': digest(case), 'proposal': deepcopy(case['proposal']),
        'evidence': evidence, 'review_focus': case['review_focus'], 'reference_draft': case['reference_draft'],
        'reference_status': 'unverified_reference_draft', 'sources': sources,
        'original_confirmation': case['human_confirmation'], 'scope': material['scope'],
    }).model_dump()


def sources_snapshot(root=None):
    """Only fixed installed paths are opened; raw PDFs are never embedded."""
    root = materials.ROOT if root is None else Path(root).resolve()
    materials.load_material(root=root)
    result = []
    for source_id, relative in sorted(SOURCE_PATHS.items()):
        path = root / relative
        if any(item.is_symlink() for item in [path, *path.parents] if item != root and root in item.parents):
            raise ValueError('Frozen source cannot be a symlink')
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('Frozen source is missing or outside the fixed bundle')
        fingerprint = sha256(path)
        binary = source_id == 'alpha101_paper'
        content = None
        if not binary:
            if path.stat().st_size > MAX_TEXT_BYTES:
                raise ValueError('Frozen text source exceeds 512 KiB')
            raw = path.read_bytes()
            if len(raw) > MAX_TEXT_BYTES or hashlib.sha256(raw).hexdigest() != fingerprint:
                raise ValueError('Frozen text source changed while reading')
            content = raw.decode('utf-8')
        result.append({'source_id': source_id, 'path': relative, 'sha256': fingerprint,
                       'media_type': 'application/pdf' if binary else 'application/json', 'content': content,
                       'content_scope': 'digest_only_binary_not_exported' if binary else 'bounded_utf8_text'})
    return result


def references(history):
    replaced = {item['supersedes_id'] for item in history if item['supersedes_id'] is not None}
    active = [item for item in history if item['source'] == 'human' and item['id'] not in replaced]
    return sorted(({'id': item['id'], 'digest': item['digest']} for item in active), key=lambda item: item['id'])


def dimension_references(case_ids, history):
    active_refs = references(history)
    by_id = {item['id']: item for item in history}
    active = [by_id[item['id']] for item in active_refs]
    result = []
    for case_id in case_ids:
        candidates = [item for item in active if item['case_id'] == case_id]
        for name in materials.DIMENSIONS:
            outcomes = sorted({item['dimensions'][name]['outcome'] for item in candidates})
            unknown = sorted(set(outcomes) & {'not_assessed', 'not_applicable'})
            status = ('pending' if not candidates else 'conflicting' if len(outcomes) > 1 else
                      'unknown' if unknown else 'eligible')
            result.append({'case_id': case_id, 'dimension': name, 'status': status,
                           'outcome': outcomes[0] if status == 'eligible' else None,
                           'annotation_refs': [{'id': item['id'], 'digest': item['digest']} for item in sorted(candidates, key=lambda row: row['id'])],
                           'declared_outcomes': outcomes, 'unknown_outcomes': unknown})
    counts = Counter(item['status'] for item in result)
    summary = {'selected_cases': len(case_ids), 'total_dimensions': len(result),
               **{name + '_dimensions': counts[name] for name in ('eligible', 'pending', 'unknown', 'conflicting')},
               'active_human_records': len(active_refs),
               'human_history_records': sum(item['source'] == 'human' for item in history),
               'automation_audit_records': sum(item['source'] == 'automation' for item in history)}
    return result, summary


def build_snapshot(case_ids, material, history, source_snapshots):
    case_ids = sorted(case_ids)
    if len(set(case_ids)) != len(case_ids) or not 1 <= len(case_ids) <= 7:
        raise ValueError('Select 1 to 7 distinct fixed cases')
    by_id = {case['id']: case for case in material['cases']}
    if not set(case_ids) <= set(by_id):
        raise ValueError('Selected semantic case is unknown')
    history = sorted((deepcopy(item) for item in history if item['case_id'] in case_ids), key=lambda item: item['id'])
    dimensions, summary = dimension_references(case_ids, history)
    value = {'schema_version': 1, 'kind': 'fixed_development_semantic_reference',
             'material_sha256': materials.MATERIAL_SHA256, 'case_ids': case_ids,
             'material_snapshot': deepcopy(material),
             'case_snapshots': [project_case(material, by_id[case_id]) for case_id in case_ids],
             'source_snapshots': deepcopy(source_snapshots), 'annotation_history': history,
             'active_human_annotations': references(history), 'dimension_references': dimensions, 'summary': summary,
             'scope': 'seven_fixed_development_materials_not_independent_final_test',
             'identity_verified': False, 'software_verified_semantic_truth': False,
             'semantic_quality_score': None, 'limitations': LIMITATIONS}
    value = SemanticEvaluationSetPreview.model_validate(bounded(value)).model_dump()
    verify_snapshot(value)
    return value


def _history_integrity(history, material, case_ids):
    from .server.semantic_annotations import LIMITATIONS as ANNOTATION_LIMITATIONS
    if len(history) > MAX_RECORDS or [item['id'] for item in history] != sorted(item['id'] for item in history):
        raise ValueError('Frozen annotation history is excessive or noncanonical')
    by_id = {}
    roots, parents = set(), set()
    cases = {case['id']: case for case in material['cases']}
    for item in history:
        if SemanticAnnotationDetail.model_validate(item).model_dump() != item or item['id'] in by_id:
            raise ValueError('Frozen annotation detail differs')
        body = {key: value for key, value in item.items() if key not in {'id', 'digest', 'created_at'}}
        if digest(body) != item['digest'] or item['id'] != 'semantic_annotation_' + item['digest']:
            raise ValueError('Frozen annotation identity/digest differs')
        materials.parse_timestamp(item['created_at'])
        declaration = materials.Annotation.model_validate({key: item[key] for key in ('case_id', 'reviewer', 'confirmed_at', 'dimensions')}).model_dump()
        if (item['case_id'] not in case_ids or item['material_sha256'] != materials.MATERIAL_SHA256
                or item['material_case_digest'] != digest(cases[item['case_id']])
                or item['declared_semantic_status'] != materials.declared_status(declaration['dimensions'])
                or item['limitations'] != ANNOTATION_LIMITATIONS):
            raise ValueError('Frozen annotation material/scope/status differs')
        scope = tuple(item[key] for key in ('material_sha256', 'case_id', 'source', 'reviewer'))
        if item['supersedes_id'] is None:
            if item['supersedes_digest'] is not None or scope in roots:
                raise ValueError('Frozen annotation root/replacement scope differs')
            roots.add(scope)
        elif item['supersedes_id'] in parents:
            raise ValueError('Frozen replacement history branches')
        else:
            parents.add(item['supersedes_id'])
        by_id[item['id']] = item
    for item in history:
        chain, cursor = set(), item
        while True:
            if cursor['id'] in chain or len(chain) >= 100:
                raise ValueError('Frozen annotation history cycles or exceeds 100 records')
            chain.add(cursor['id'])
            if cursor['supersedes_id'] is None:
                break
            parent = by_id.get(cursor['supersedes_id'])
            if parent is None or cursor['supersedes_digest'] != parent['digest']:
                raise ValueError('Frozen annotation parent is missing or altered')
            if any(cursor[key] != parent[key] for key in ('material_sha256', 'case_id', 'source', 'reviewer')):
                raise ValueError('Frozen annotation parent scope differs')
            if materials.parse_timestamp(cursor['confirmed_at']) < materials.parse_timestamp(parent['confirmed_at']):
                raise ValueError('Frozen annotation replacement predates its parent')
            cursor = parent


def verify_snapshot(value, *, root=None):
    """Verify frozen bytes/derivations; root optionally proves installed source bytes.

    This intentionally never checks whether a frozen annotation is still the
    active endpoint of the *current* DB: legitimate later revisions are safe.
    """
    value = SemanticEvaluationSetPreview.model_validate(bounded(value)).model_dump()
    if value['limitations'] != LIMITATIONS or value['material_sha256'] != materials.MATERIAL_SHA256:
        raise ValueError('Frozen semantic material version/limitations differ')
    case_ids = value['case_ids']
    if len(set(case_ids)) != len(case_ids) or case_ids != sorted(case_ids) or not 1 <= len(case_ids) <= 7:
        raise ValueError('Frozen selected cases differ')
    sources = value['source_snapshots']
    if [row['source_id'] for row in sources] != sorted(SOURCE_PATHS):
        raise ValueError('Frozen source manifest membership differs')
    for row in sources:
        source_id = row['source_id']
        if row['path'] != SOURCE_PATHS[source_id]:
            raise ValueError('Frozen source manifest path differs')
        binary = source_id == 'alpha101_paper'
        if (row['media_type'] != ('application/pdf' if binary else 'application/json')
                or row['content_scope'] != ('digest_only_binary_not_exported' if binary else 'bounded_utf8_text')
                or (binary and row['content'] is not None)):
            raise ValueError('Frozen source content scope differs')
        if source_id in materials.SOURCE_FILES and row['sha256'] != materials.SOURCE_FILES[source_id][1]:
            raise ValueError('Frozen original source digest differs')
        if not binary:
            if row['content'] is None:
                raise ValueError('Frozen bounded evidence text is missing')
            raw = row['content'].encode('utf-8')
            if len(raw) > MAX_TEXT_BYTES or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('Frozen source text/digest differs')
        if root is not None:
            local = Path(root).resolve() / row['path']
            if not local.is_file() or any(part.is_symlink() for part in [local, *local.parents] if part != Path(root).resolve() and Path(root).resolve() in part.parents):
                raise ValueError('Installed frozen source is missing or linked')
            if sha256(local) != row['sha256']:
                raise ValueError('Installed source does not match frozen source manifest')
    by_source = {row['source_id']: row for row in sources}
    if by_source['semantic_material']['sha256'] != materials.MATERIAL_SHA256:
        raise ValueError('Original fixed material digest differs')
    material = json.loads(by_source['semantic_material']['content'])
    manifest = json.loads(by_source['semantic_manifest']['content'])
    if material != value['material_snapshot'] or manifest['semantic_material_digest'] != materials.MATERIAL_SHA256:
        raise ValueError('Frozen material/manifest snapshot differs')
    cases = {case['id']: case for case in material['cases']}
    if not set(case_ids) <= set(cases) or len(cases) != 7:
        raise ValueError('Frozen selected material case differs')
    if value['case_snapshots'] != [project_case(material, cases[case_id]) for case_id in case_ids]:
        raise ValueError('Frozen projected case/evidence snapshot differs')
    _history_integrity(value['annotation_history'], material, case_ids)
    dimensions, summary = dimension_references(case_ids, value['annotation_history'])
    if (value['active_human_annotations'] != references(value['annotation_history'])
            or value['dimension_references'] != dimensions or value['summary'] != summary):
        raise ValueError('Frozen human reference/conflict/unknown derivation differs')
    return deepcopy(value)


def build_comparison(reference, request):
    case_ids = reference['case_ids']
    declarations = request['case_declarations']
    if ([item['case_id'] for item in declarations] != case_ids or request['set_id'] != reference['id']
            or request['set_digest'] != reference['digest']):
        raise ValueError('Comparison must bind the exact reference and every selected case')
    by_case = {item['case_id']: item['dimensions'] for item in declarations}
    results = []
    for row in reference['dimension_references']:
        outcome = by_case[row['case_id']][row['dimension']]['outcome']
        reason = ('reference_' + row['status'] if row['status'] != 'eligible' else
                  'declaration_unknown' if outcome in {'not_assessed', 'not_applicable'} else None)
        results.append({'case_id': row['case_id'], 'dimension': row['dimension'],
                        'reference_status': row['status'], 'reference_outcome': row['outcome'],
                        'declared_outcome': outcome, 'comparable': reason is None,
                        'agrees': (outcome == row['outcome']) if reason is None else None,
                        'excluded_reason': reason})
    comparable = sum(item['comparable'] for item in results)
    matched = sum(item['agrees'] is True for item in results)
    excluded = dict(sorted(Counter(item['excluded_reason'] for item in results if not item['comparable']).items()))
    summary = {'metric': 'declaration_agreement_not_model_quality', 'matched_dimensions': matched,
               'comparable_dimensions': comparable, 'total_dimensions': len(results),
               'excluded_dimensions': len(results) - comparable, 'excluded_reasons': excluded,
               'declaration_agreement_rate': matched / comparable if comparable else None, 'semantic_quality_score': None}
    return bounded({'schema_version': 1, **{key: request[key] for key in ('set_id', 'set_digest', 'source', 'reviewer', 'declared_at', 'execution_reference', 'case_declarations')},
                    'dimension_results': results, 'summary': summary, 'identity_verified': False,
                    'execution_reference_verified': False, 'software_verified_semantic_truth': False,
                    'claim_approval': False, 'limitations': LIMITATIONS}, 256 * 1024)


def verify_detail(value, *, root=None):
    value = SemanticEvaluationSetDetail.model_validate(bounded(value)).model_dump()
    body = {key: value[key] for key in SET_FIELDS}
    if value['digest'] != digest(body) or value['id'] != 'semantic_evaluation_set_' + value['digest']:
        raise ValueError('Frozen reference identity/digest differs')
    materials.parse_timestamp(value['created_at'])
    verify_snapshot(body, root=root)
    return value


def verify_comparison(value, reference):
    value = SemanticEvaluationComparisonDetail.model_validate(bounded(value, 256 * 1024)).model_dump()
    body = {key: value[key] for key in COMPARISON_FIELDS}
    request = SemanticEvaluationComparisonCreate.model_validate({key: value[key] for key in SemanticEvaluationComparisonCreate.model_fields if key != 'idempotency_key'} | {'idempotency_key': 'offline-integrity-validation'}).model_dump()
    if body != build_comparison(reference, request):
        raise ValueError('Frozen comparison derivation differs')
    if value['digest'] != digest(body) or value['id'] != 'semantic_evaluation_comparison_' + value['digest']:
        raise ValueError('Frozen comparison identity/digest differs')
    materials.parse_timestamp(value['created_at'])
    return value


def metadata_digest(identity, fingerprint, created_at):
    return digest({'schema_version': 1, 'id': identity, 'digest': fingerprint, 'created_at': created_at})


def verify_bundle(value, *, root=None):
    value = SemanticEvaluationBundle.model_validate(bounded(value)).model_dump()
    manifest = {key: item for key, item in value.items() if key != 'manifest_digest'}
    if digest(manifest) != value['manifest_digest'] or value['limitations'] != LIMITATIONS:
        raise ValueError('Portable semantic bundle manifest differs')
    reference = verify_detail(value['reference'], root=root)
    if value['reference_metadata_digest'] != metadata_digest(reference['id'], reference['digest'], reference['created_at']):
        raise ValueError('Portable semantic reference metadata differs')
    comparisons = value['comparisons']
    if len(comparisons) > MAX_RECORDS or len({item['id'] for item in comparisons}) != len(comparisons):
        raise ValueError('Portable comparison manifest is duplicated/excessive')
    if [row['id'] for row in comparisons] != sorted(row['id'] for row in comparisons):
        raise ValueError('Portable comparison order is noncanonical')
    expected_metadata = {}
    for item in comparisons:
        verify_comparison(item, reference)
        expected_metadata[item['id']] = metadata_digest(item['id'], item['digest'], item['created_at'])
    if value['comparison_metadata_digests'] != expected_metadata:
        raise ValueError('Portable comparison metadata manifest differs')
    return {'schema_version': 1, 'passed': True, 'reference_id': reference['id'], 'reference_digest': reference['digest'],
            'source_bytes_reverified': root is not None, 'binary_sources_included': False,
            'scope': value['scope'], 'selected_cases': len(reference['case_ids']),
            'active_human_records': reference['summary']['active_human_records'],
            'automation_audit_records': reference['summary']['automation_audit_records'],
            'comparisons': len(comparisons), 'identity_verified': False,
            'semantic_quality_score': None, 'limitations': LIMITATIONS}
