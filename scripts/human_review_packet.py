"""Pure Alpha101 review preparation and snapshot integrity, not semantic scoring.

No network, database, PDF access, experiment or human-label write occurs in
these functions. A caller must separately verify the live workspace and saved
PDF bytes. Inputs retain original server projections rather than re-signing
historical business records.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper_alpha import semantic_materials as materials
from paper_alpha.storage import digest, json_text
from paper_alpha.server.claim_reviews import LIMITATIONS as TARGET_LIMITATIONS
from paper_alpha.server.claim_reviews_schema import ClaimReviewTarget
from paper_alpha.server.observation_context_schema import ObservationContext
from paper_alpha.server.research_assessments import parse_timestamp
from paper_alpha.server.research_cases import CASE_FIELDS
from paper_alpha.server.research_cases_schema import CaseDetail
from paper_alpha.server.research_claims import FIELDS as CLAIM_FIELDS, LIMITATIONS as CLAIM_LIMITATIONS, _resolve
from paper_alpha.server.research_claims_schema import ClaimDetail, ClaimPreviewRequest
from paper_alpha.server.response_schemas import HealthResponse
from paper_alpha.server.semantic_annotations_schema import SemanticMaterialDetail

MAX_PACKET_BYTES = 8 * 1024 * 1024
FORMULA = '((close - open) / ((high - low) + .001))'
FORMULA_EVIDENCE_ID = 'alpha101-formula'
MATERIAL_CASE_DIGEST = '632bc2fe39ba26a0aed1dd960ad700bb7b4cc8c7f2fca5f03cd3c08058533c9a'
PAPER_SHA256 = materials.SOURCE_FILES['alpha101_paper'][1]
PROTOCOL = {'id': 'alpha101-human-workflow-v1',
            'digest': 'd8d9bb221db8d5be1b087311840e4f7e760e05f50304649d7ac34b873815eb0b',
            'task_sha256': '412b89980af41d423d377b13fdfef5400bc57963855454e6f08e840367d972be',
            'candidate_id': 'alpha101', 'engine_mode': 'normalized_fixed'}
METRICS = ('mean_rank_ic', 'mean_gross_return', 'evaluated_days', 'factor_coverage')
CLAIM_IDS = ('alpha101-paper-formula', 'alpha101-validation-metrics', 'alpha101-research-limitations')
SCOPE = 'existing_synthetic_alpha101_validation_snapshot'
LIMITATIONS = [
    'This packet checks frozen snapshot integrity and internal linkage only, not semantic quality or trusted authorship.',
    'The Alpha101 formula is linked to fixed PDF page 15; its citation does not establish economic causality or profitability.',
    'Tool values are synthetic validation metrics, not paper backtest reproduction, real-market evidence or independent final-test performance.',
    'Existing source reviews do not approve this new exact claim batch. Actual human judgments remain unfilled.',
    'Runtime and health are captured server declarations; offline integrity cannot authenticate the server or prove a complete history.',
    'The saved PDF must be independently byte-checked against the fixed material and case citation; this pure verifier does not read PDF files.',
    'No model call, semantic score, human timing measurement or WorldQuant BRAIN equivalence is claimed.',
]
PACKET_FIELDS = {'schema_version', 'kind', 'scope', 'case', 'material', 'claim_request', 'claims', 'targets',
                 'runtime', 'health', 'selection', 'limitations', 'human_judgments',
                 'semantic_quality_score', 'llm_api_called'}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _same(first, second):
    return json_text(first) == json_text(second)


def _bounded(value):
    _require(len(json_text(value).encode('utf-8')) <= MAX_PACKET_BYTES, 'Review packet/input exceeds 8 MiB')
    return value


def _json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            _require(key not in result, 'Duplicate JSON key in frozen input')
            result[key] = item
        return result
    def invalid(value):
        raise ValueError('Nonfinite JSON constant in frozen input: ' + value)
    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)


def _typed(model, value, *, historical=False):
    _require(isinstance(value, dict), 'Frozen input must be a JSON object')
    _bounded(value)
    checked = model.model_validate(value).model_dump(exclude_unset=historical)
    _require(_same(checked, value), 'Frozen input differs from its exact typed server projection')
    return value


def _material(material):
    _typed(SemanticMaterialDetail, material)
    _require(material['schema_version'] == 1 and material['case_id'] == 'alpha101_formula'
             and material['material_sha256'] == materials.MATERIAL_SHA256
             and material['material_case_digest'] == MATERIAL_CASE_DIGEST, 'Wrong fixed Alpha101 material version')
    original = {'id': material['case_id'], 'evidence_ids': [item['id'] for item in material['evidence']],
                'human_confirmation': material['original_confirmation'],
                **{key: material[key] for key in ('proposal', 'reference_draft', 'review_focus')}}
    _require(digest(original) == MATERIAL_CASE_DIGEST, 'Fixed material case snapshot differs')
    expected_evidence = {'document_sha256': PAPER_SHA256, 'end_char': 728, 'id': FORMULA_EVIDENCE_ID,
        'page': 15, 'paper_id': 'arxiv:1601.00991v3', 'quote': FORMULA,
        'source': 'examples/alpha101/paper.pdf', 'start_char': 688,
        'verification': 'literal_quote_candidate', 'visual_checked': True, 'source_id': 'alpha101_paper'}
    expected_sources = [{'source_id': 'alpha101_paper', 'path': 'examples/alpha101/paper.pdf', 'sha256': PAPER_SHA256,
        'media_type': 'application/pdf', 'verification': 'bundled_source_digest_verified', 'raw_pdf_reverified': True}]
    _require(_same(material['evidence'], [expected_evidence]) and _same(material['sources'], expected_sources),
             'Fixed formula, page, source PDF or evidence bytes differ')
    _require(material['proposal']['expression'] == FORMULA and material['proposal']['attribution'] == 'paper_original'
             and material['reference_status'] == 'unverified_reference_draft'
             and material['scope'] == 'source_linked_semantic_review_material', 'Material scope or formula differs')


def _case(case, material):
    _typed(CaseDetail, case, historical=True)
    _material(material)
    body = {key: case[key] for key in CASE_FIELDS}
    _require(case['digest'] == digest(body) and case['id'] == 'research_case_' + case['digest'], 'Case identity/digest differs')
    preview_body = {key: case[key] for key in ('source_kind', 'source_id', 'context')}
    _require(case['source_digest'] == digest(preview_body), 'Case original source digest differs')
    parse_timestamp(case['created_at'])
    context = case['context']
    _require(case['source_kind'] == 'daily_run' and context['state'] == 'ready_for_review'
             and context['stop_reason'] is None, 'Only a completed daily review context is supported')
    provenance = {item['label']: item['value'] for item in context['provenance']}
    _require(len(provenance) == len(context['provenance']), 'Duplicate case provenance labels')
    required = {'research_id', 'revision_id', 'attempt_id', 'full_state_digest', 'manifest_digest',
                'signature_digest', 'code_digest', 'evaluation', 'mode'}
    _require(required <= provenance.keys() and provenance['mode'] == 'normalized_fixed', 'Missing provenance or non-normalized execution mode')
    _require(all(re.fullmatch(r'[0-9a-f]{64}', provenance[name]) for name in
                 ('full_state_digest', 'manifest_digest', 'signature_digest', 'code_digest')), 'Invalid original execution/code digest')
    _require(len(context['results']) == 1, 'Daily packet requires one exact candidate result collection')
    result = context['results'][0]
    _require(result['kind'] == 'daily_candidates' and result['id'] == provenance['attempt_id'], 'Result does not bind the exact daily attempt')
    candidates = _json(result['payload_json'])
    _require(isinstance(candidates, list) and 1 <= len(candidates) <= 4 and _same(json_text(candidates), result['payload_json'])
             and result['digest'] == digest(candidates), 'Candidate collection/digest differs')
    identities = []
    for item in candidates:
        _require(isinstance(item, dict), 'Candidate must be a structured engine result')
        identity = item.get('id', item.get('candidate_id'))
        _require(isinstance(identity, str) and identity.strip(), 'Candidate has no valid identity')
        _require('id' not in item or 'candidate_id' not in item or item['id'] == item['candidate_id'], 'Conflicting candidate identity fields')
        identities.append(identity)
    _require(len(set(identities)) == len(identities) and identities.count('alpha101') == 1, 'Missing, duplicate or wrong Alpha101 candidate')
    index = identities.index('alpha101'); candidate = candidates[index]
    value = candidate.get('result')
    _require(isinstance(value, dict) and candidate.get('status') == 'evaluated' and value.get('status') == 'evaluated', 'Alpha101 candidate has no evaluated result')
    _require(candidate.get('expression') == FORMULA and value.get('expression') == FORMULA
             and candidate.get('origin') == 'paper_original' and candidate.get('changes') == [], 'Alpha101 formula was modified or misattributed')
    _require(value.get('split') == 'validation' and isinstance(value.get('market_metadata'), dict)
             and value['market_metadata'].get('data_kind') == 'synthetic'
             and context['data_scope'].split(';')[0].strip() == 'synthetic', 'Packet is restricted to synthetic validation results')
    _require(value.get('config') == _json(provenance['evaluation']) and 'validation' in value['config'].get('splits', {}), 'Result and original evaluation config differ')
    metrics = value.get('metrics')
    _require(isinstance(metrics, dict) and all(type(metrics.get(name)) in (int, float) and math.isfinite(metrics[name]) for name in METRICS), 'Required tool metrics are missing, nonfinite or not numeric')
    _require(isinstance(value.get('limitations'), list) and value['limitations']
             and all(isinstance(item, str) and item.strip() for item in value['limitations']), 'Original research limitations are missing')
    evidence = [item for item in context['evidence'] if item['id'] == FORMULA_EVIDENCE_ID]
    _require(len(evidence) == 1, 'Exact Alpha101 formula evidence is missing or duplicated')
    item = evidence[0]
    _require(item['origin'] == 'paper' and item['verification'] == 'literal_quote_verified' and item['text'] == FORMULA
             and re.fullmatch(r'.+ p\.15; PDF SHA256 ' + PAPER_SHA256, item['locator']) is not None, 'Formula citation does not bind original PDF page 15')
    definitions = [item for item in context['definitions'] if item['id'] == candidate.get('hypothesis_id')]
    _require(len(definitions) == 1 and definitions[0]['attribution'] == 'paper_original'
             and FORMULA_EVIDENCE_ID in definitions[0]['evidence_ids'], 'Candidate is not linked to the cited paper hypothesis')
    definition = _json(definitions[0]['text'])
    _require(definition.get('id') == candidate.get('hypothesis_id') and definition.get('attribution') == 'paper_original'
             and definition.get('mechanism_attribution') == 'model_conjecture'
             and FORMULA_EVIDENCE_ID in definition.get('evidence_ids', [])
             and set(definition.get('required_fields', [])) == {'close', 'open', 'high', 'low'}, 'Hypothesis attribution or fields differ')
    return {'result': result, 'candidate': candidate, 'index': index, 'provenance': provenance}


def build_claim_request(case, material):
    selected = _case(case, material)
    result, index = selected['result'], selected['index']
    references = [{'case_id': case['id'], 'case_digest': case['digest'], 'result_id': result['id'],
                   'result_digest': result['digest'], 'pointer': f'/{index}/result/metrics/{name}'} for name in METRICS]
    limits = 'Project scope: synthetic development data; validation interval; normalized_fixed mode.\n' + '\n'.join(selected['candidate']['result']['limitations'])
    limits += '\nNo new economic-mechanism fact or human semantic approval is asserted by this preparation.'
    claims = [
        {'id': CLAIM_IDS[0], 'kind': 'evidence_statement', 'attribution': 'paper_original',
         'text': material['proposal']['claim'], 'evidence_ids': [FORMULA_EVIDENCE_ID], 'metric_references': []},
        {'id': CLAIM_IDS[1], 'kind': 'metric', 'attribution': 'project_convention',
         'text': 'These server-derived Alpha101 project metrics describe synthetic validation data in normalized_fixed mode; they are not original-paper or market-performance results.',
         'evidence_ids': [], 'metric_references': references},
        {'id': CLAIM_IDS[2], 'kind': 'project_rule', 'attribution': 'project_convention',
         'text': limits, 'evidence_ids': [], 'metric_references': []},
    ]
    value = {'case_id': case['id'], 'case_digest': case['digest'], 'claims': claims}
    return deepcopy(ClaimPreviewRequest.model_validate(value).model_dump())


def _claims(case, request, claims):
    _typed(ClaimDetail, claims)
    parse_timestamp(claims['created_at'])
    _require(claims['case_id'] == case['id'] and claims['case_digest'] == case['digest']
             and _same(claims['submitted_claims'], request['claims']) and claims['limitations'] == CLAIM_LIMITATIONS, 'Saved batch does not match the exact preparation request')
    body = {key: claims[key] for key in CLAIM_FIELDS}
    _require(claims['digest'] == digest(body) and claims['id'] == 'research_claims_' + claims['digest'], 'Claim batch identity/digest differs')
    evidence = {item['id']: item for item in case['context']['evidence']}
    results = {item['id']: item for item in case['context']['results']}
    expected = []
    for draft in request['claims']:
        metrics = [_resolve(results[ref['result_id']], ref) for ref in draft['metric_references']]
        expected.append({key: draft[key] for key in ('id', 'kind', 'attribution', 'evidence_ids')} | {
            'narrative_text': draft['text'], 'evidence_verifications': [evidence[name]['verification'] for name in draft['evidence_ids']],
            'metrics': metrics, 'authoritative_display': '\n'.join(item['display'] for item in metrics) if metrics else None,
            'citation_integrity': 'verified' if draft['evidence_ids'] else 'not_applicable',
            'semantic_fidelity': 'unverified', 'status': 'draft', 'actor': 'automation'})
    _require(_same(claims['claims'], expected), 'Saved claim wording or server numeric resolution differs from the exact result')


def _targets(case, claims, targets):
    _require(type(targets) is list and len(targets) == len(CLAIM_IDS), 'Every prepared claim requires one exact target')
    by_id = {}
    for target in targets:
        _typed(ClaimReviewTarget, target)
        _require(target['claim_id'] not in by_id, 'Duplicate review target')
        by_id[target['claim_id']] = target
    _require(set(by_id) == set(CLAIM_IDS), 'Missing, extra or foreign review target')
    context = case['context']
    for claim in claims['claims']:
        expected = {'schema_version': 1, 'claims_id': claims['id'], 'claims_digest': claims['digest'],
            'claim_id': claim['id'], 'claim_digest': digest(claim), 'case_id': case['id'], 'case_digest': case['digest'],
            **{key: case[key] for key in ('source_kind', 'source_id', 'source_digest')},
            'case_context_digest': digest(context), 'claim': claim, 'evidence': context['evidence'], 'definitions': context['definitions'],
            'results': [{key: result[key] for key in ('id', 'kind', 'digest', 'summary')} for result in context['results']],
            'provenance': context['provenance'], **{key: context[key] for key in ('data_scope', 'method_scope', 'stop_reason')},
            'case_state': context['state'], 'case_limitations': context['limitations'],
            'original_claim_limitations': claims['limitations'], 'limitations': TARGET_LIMITATIONS}
        expected['target_digest'] = digest(expected)
        _require(_same(by_id[claim['id']], expected), 'Review target does not bind the exact claim/case/source projection')
    return [deepcopy(by_id[name]) for name in CLAIM_IDS]


def _runtime(case, selected, runtime, health):
    _typed(ObservationContext, runtime)
    _typed(HealthResponse, health)
    _require(health['status'] == 'ok' and health['ai_enabled'] is False and type(health['database_schema']) is int
             and (health['database_schema'], health['version']) in {(16, '0.20.0'), (17, '0.21.0'), (17, '0.22.0')}
             and health['version'] == runtime['runtime']['version']
             and re.fullmatch(r'[0-9a-f]{64}', health['workspace_id']), 'Wrong declared workbench runtime/schema or AI state')
    _require(_same(runtime['protocol'], PROTOCOL) and runtime['compatibility']['compatible'] is True
             and runtime['compatibility']['reason'] is None, 'Runtime is not compatible with the exact Alpha101 workflow protocol')
    provenance = selected['provenance']
    _require(runtime['research_id'] == provenance['research_id'] and runtime['revision_id'] == provenance['revision_id'], 'Runtime belongs to another research or revision')
    execution = runtime['selected']
    _require(isinstance(execution, dict) and execution['status'] == 'completed' and execution['verified'] is True
             and execution['verification_error'] is None and execution['run_id'] == case['source_id']
             and execution['revision_id'] == provenance['revision_id'] and execution['attempt_id'] == provenance['attempt_id'], 'Runtime does not bind the original verified attempt')
    outputs = execution['bound_outputs']
    _require(outputs['task_sha256'] == PROTOCOL['task_sha256'] and outputs['run_id'] == case['source_id']
             and outputs['revision_id'] == provenance['revision_id'] and outputs['attempt_id'] == provenance['attempt_id'], 'Runtime output binding differs')
    actual_runtime = runtime['runtime']
    _require(actual_runtime['provenance'] == 'server_runtime' and re.fullmatch(r'[0-9a-f]{64}', actual_runtime['code_digest'] or ''), 'Runtime has no exact code inventory digest')
    _require((actual_runtime['code_commit_verified'] is True and re.fullmatch(r'[0-9a-f]{40}', actual_runtime['code_commit'] or '') is not None)
             or (actual_runtime['code_commit_verified'] is False and actual_runtime['code_commit'] is None), 'Runtime code identity declaration is inconsistent')


def validate_inputs(case, material, runtime, health):
    """Shared strict preflight before any caller POST; no external side effect.

    The report validates a declared snapshot, never authenticates a server. A
    loopback client must additionally check its expected workspace and commit.
    """
    selected = _case(case, material)
    _runtime(case, selected, runtime, health)
    return {'passed': True, 'scope': 'snapshot_integrity_only_not_semantic_quality',
            'case_id': case['id'], 'case_digest': case['digest'],
            'candidate_id': 'alpha101', 'candidate_index': selected['index']}


def build_packet(case, material, claims, targets, runtime, health):
    selected = _case(case, material)
    request = build_claim_request(case, material)
    _claims(case, request, claims)
    targets = _targets(case, claims, targets)
    _runtime(case, selected, runtime, health)
    value = {'schema_version': 1, 'kind': 'alpha101_human_review_packet', 'scope': SCOPE,
        'case': deepcopy(case), 'material': deepcopy(material), 'claim_request': request,
        'claims': deepcopy(claims), 'targets': targets, 'runtime': deepcopy(runtime), 'health': deepcopy(health),
        'selection': {'candidate_id': 'alpha101', 'candidate_index': selected['index'], 'candidate_digest': digest(selected['candidate']),
                      'result_id': selected['result']['id'], 'result_digest': selected['result']['digest'],
                      'formula_evidence_id': FORMULA_EVIDENCE_ID, 'formula': FORMULA,
                      'paper_document_sha256': PAPER_SHA256, 'paper_page': 15, 'mode': 'normalized_fixed', 'split': 'validation'},
        'limitations': LIMITATIONS, 'human_judgments': None, 'semantic_quality_score': None, 'llm_api_called': False}
    value['packet_digest'] = digest(value)
    return _bounded(value)


def verify_packet(packet):
    _require(type(packet) is dict and set(packet) == PACKET_FIELDS | {'packet_digest'}, 'Unknown or missing packet field')
    _bounded(packet)
    _require(type(packet['schema_version']) is int and packet['schema_version'] == 1
             and packet['kind'] == 'alpha101_human_review_packet', 'Unsupported packet kind/version')
    body = {key: packet[key] for key in PACKET_FIELDS}
    _require(packet['packet_digest'] == digest(body), 'Packet snapshot digest differs')
    expected = build_packet(packet['case'], packet['material'], packet['claims'], packet['targets'], packet['runtime'], packet['health'])
    _require(_same(packet, expected), 'Packet derived selection, attribution, scope or pending judgments differ')
    return {'schema_version': 1, 'passed': True, 'scope': 'snapshot_integrity_only_not_semantic_quality',
        'packet_digest': packet['packet_digest'], 'case_id': packet['case']['id'], 'claims_id': packet['claims']['id'],
        'claim_ids': list(CLAIM_IDS), 'candidate_id': 'alpha101', 'split': 'validation', 'data_kind': 'synthetic',
        'human_judgments': None, 'semantic_quality_score': None, 'llm_api_called': False,
        'source_authorship_verified': False, 'runtime_authenticity_verified': False,
        'pdf_bytes_reverified': False, 'limitations': LIMITATIONS}
