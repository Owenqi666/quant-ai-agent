"""Source-specific GJS author research preparation, not a portfolio adapter."""
from copy import deepcopy
from pathlib import Path

from . import eligibility
from .author_archive_contract import source_metadata
from .evidence import normalize, sha256
from .storage import digest, read_json

ROOT = Path(__file__).resolve().parents[1]
SEMANTICS_VERSION = 'gjs-research-binding-v1'
SOURCE_REGISTRY_SHA256 = '58f9620d7727b575d8f0574e0b113938d8d411ed658110e3f194aba424e22992'
LIMITATIONS = [
    'Author public files are perturbed/randomly deleted; this is not unaltered real-market research.',
    'This resource binds existing exact records, not a new scan or portfolio experiment.',
    'HTTP does not open MAT files. Imported source hashes/scan counts remain declarations; raw-source verification is separate offline work.',
    'Panels are selected original-row slices, not independent proof of all-original-row scan counts.',
    'Paper anchors are literal matches/source references, not human semantic approval.',
    'Study thresholds and reserved boundaries are predeclared project screens, not a complete original-paper investment method.',
    'No daily DGW reconstruction, full eligibility universe, holding weights/labels or returns are implemented here.',
    'Binding and scan success never unlock portfolio execution, final test, AI or human approval.',
]


def registry():
    path = ROOT / 'docs/research/momentum_sources.json'
    if sha256(path) != SOURCE_REGISTRY_SHA256:
        raise ValueError('Fixed author-paper registry bytes changed')
    value = read_json(path)
    expected = eligibility.evidence()
    if (value['paper_id'] != expected['paper_id'] or value['doi'] != expected['doi']
            or value['pdf_sha256'] != expected['pdf_sha256']):
        raise ValueError('Fixed author-paper registry differs from the eligibility evidence')
    return value


def _paper_projection(paper, source_scope):
    manifest = registry()
    if source_scope == 'author_paper':
        if paper['sha256'] != manifest['pdf_sha256']:
            raise ValueError('Selected paper is not the exact fixed GJS author paper')
        anchors = manifest['quote_anchors']
        scope = 'author_paper_literal_anchors'
    else:
        fixture = read_json(ROOT / 'examples/alpha101/paper.json')
        if paper['sha256'] != fixture['document_sha256']:
            raise ValueError('Controlled contract fixture accepts only the bundled Alpha101 PDF')
        anchors = [item for item in read_json(ROOT / 'examples/alpha101/task.json')['evidence'] if item['id'] == 'alpha101-formula']
        scope = 'unrelated_fixture_document_contract'
    pages = {page['page']: normalize(page['text']) for page in paper['document']['pages']}
    evidence = []
    for anchor in anchors:
        if normalize(anchor['quote']) not in pages.get(anchor['page'], ''):
            raise ValueError('Fixed paper anchor is absent from the selected PDF extraction')
        evidence.append({'id': 'selected-paper-' + anchor['id'], 'origin': 'paper',
                         'locator': f"Selected PDF p.{anchor['page']}; SHA256 {paper['sha256']}",
                         'claim': anchor['quote'], 'verification': 'literal_quote_verified', 'document_sha256': paper['sha256']})
    return {'id': paper['id'], 'title': paper['title'], 'pdf_sha256': paper['sha256'],
            'document_digest': digest(paper['document']), 'evidence_scope': scope}, evidence


def project(request, paper, protocol, study, panels):
    """Pure projection over independently verified existing resources."""
    source = source_metadata(request['source_filename'])
    scan = eligibility.validate_scan(study['scan'])
    result = eligibility.evaluate(scan)
    if (paper['id'] != request['paper_id'] or paper['sha256'] != request['paper_digest']
            or protocol['id'] != request['protocol_id'] or protocol['digest'] != request['protocol_digest']
            or study['id'] != request['study_id'] or study['digest'] != request['study_digest']
            or source['sha256'] != request['source_sha256'] or source != scan['source']
            or digest(scan['plan']) != request['plan_digest'] or digest(scan) != request['scan_digest']
            or digest(result) != request['result_digest'] or result != study['result']):
        raise ValueError('Binding request does not identify the exact selected resources/scan/source')
    config = protocol['config']
    if config['mom_window_months'] != 11 or config['mom_skip_months'] != 1:
        raise ValueError('Protocol MOM window/skip does not match the fixed author scan H-12..H-2')
    expected_panels = [{'id': panel['id'], 'digest': panel['digest']} for panel in panels]
    if (request['panels'] != expected_panels or [item['id'] for item in expected_panels] != study['author_panel_ids']):
        raise ValueError('Selected panels must exactly cover the original study panel links in order')
    for panel in panels:
        body = panel['panel']
        if (body['source'] != source or not scan['plan']['development_start'] <= body['selection']['target_month'] <= scan['plan']['development_end']):
            raise ValueError('Bound panel uses another source or lies outside the scan plan')
    projected_paper, anchors = _paper_projection(paper, request['source_scope'])
    evidence = anchors + [{'id': item['id'], 'origin': item['origin'], 'locator': item['locator'],
                          'claim': item['claim'], 'verification': 'project_declaration' if item['origin'] == 'project' else 'source_registry_reference',
                          'document_sha256': result['evidence']['pdf_sha256'] if item['origin'] == 'paper' else None}
                         for item in result['evidence']['citations']]
    blockers = []
    if request['source_scope'] == 'controlled_contract_fixture':
        blockers.append('FIXTURE_DOCUMENT_SCOPE: The bundled Alpha101 document verifies the engineering contract only; it is not the selected GJS paper.')
    if result['summary']['status'] == 'screen_blocked':
        blockers.append('DATA_INSUFFICIENT: Not every predeclared development month reaches the declared asset threshold.')
    if config['mode'] == 'paper':
        blockers.append('PAPER_RULES_UNRESOLVED: Original DGW internal daily window and missing-date rules remain unresolved.')
    if scan['plan']['task'] == 'momentum_dgw':
        blockers.append('DGW_PRECOMPUTED_ONLY: This source cannot independently reconstruct the original daily signal.')
    blockers.append('AUTHOR_PORTFOLIO_METHOD_UNEXECUTED: Universe filters, sorts, neutralization, holding labels and weights are not executed.')
    return deepcopy({'schema_version': 1, 'semantics_version': SEMANTICS_VERSION, 'source_scope': request['source_scope'],
        'paper': projected_paper,
        'protocol': {k: protocol[k] for k in ('id', 'digest', 'config_digest', 'config', 'unresolved')},
        'source': source,
        'study': {'id': study['id'], 'digest': study['digest'], 'plan': scan['plan'], 'plan_digest': digest(scan['plan']),
                  'scan_digest': digest(scan), 'result_digest': digest(result), 'summary': result['summary'],
                  'evidence_digest': digest(result['evidence']), 'panels': expected_panels},
        'evidence': evidence, 'compatible': True, 'status': 'blocked', 'blockers': blockers,
        'window_rules': {'momentum_months': 11, 'skip_months': 1, 'momentum_history': 'H-12..H-2',
                         'dgw_market_cap_as_of': 'H-1', 'holding_label': 'H is not read by eligibility scanning',
                         'missing_history': 'all eleven monthly returns finite, >= -1 and representable; no fill',
                         'project_daily_id_policy': 'Not equivalent to author precomputed monthly DGW; original protocol declarations remain separate'},
        'verification_scope': 'linked_resource_consistency_only', 'raw_source_reverified': False,
        'execution_ready': False, 'provider_connected': False, 'semantic_fidelity': 'unverified', 'limitations': list(LIMITATIONS)})
