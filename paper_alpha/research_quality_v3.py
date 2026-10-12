"""Versioned engineering quality checks on original authenticated model receipts.

Transport authenticity is separate from semantic fidelity. Missing outputs and
unsupported full-paper strategies retain all declared denominator cells.
"""
from copy import deepcopy
from collections import Counter
import hashlib

from . import research_quality_v2 as v2
from .research_quality_v2_binding import canonical, digest, read_bounded_json, HostRunBinding
from .research_quality_v3_binding import LoopNativeReceiptBinding
from .evidence import normalize

PROTOCOL_ID = 'research-quality-v3'
ResearchQualityError = v2.ResearchQualityError
POLICY = {'model_oracle_access': False, 'human_reviewed': False,
          'scientific_truth': False, 'post_output_relabel': False,
          'comparison_claim': 'declared_reference_access_difference_no_blind_comparison',
          'missing_slots': 'retain_unknown_in_full_198_denominator',
          'model_source_exposure': 'complete_selected_physical_pages',
          'baseline_source_exposure': 'legacy_reference_excerpts_and_controlled_formula_inputs',
          'equal_input_fair_comparison_verified': False}


def extraction_instruction(reference, paper_id):
    tasks = [t for t in reference['tasks'] if t['paper_id'] == paper_id]
    return ('\n'.join(t['prompt'] for t in tasks) +
        '\nReturn exactly two hypotheses in that order when evidence supports them; absent hypotheses remain unfilled. This is extraction, not execution. Frozen local fields: ' +
        ', '.join(reference['catalog']['fields']) + '. Frozen operators: ' +
        ', '.join(reference['catalog']['operators']) +
        '. Missing data/methods must remain unresolved; no market data rows are supplied.')


def validate_protocol(protocol):
    v2._bounded(protocol)
    v2._fields(protocol, ('schema_version', 'protocol_id', 'version', 'scope',
                        'reference_protocol', 'model_slots', 'policy'), 'v3 protocol')
    v2._require(type(protocol['schema_version']) is int and protocol['schema_version'] == 3
                and protocol['protocol_id'] == PROTOCOL_ID and protocol['version'] == '1'
                and protocol['scope'] == 'authenticated_transport_and_engineering_checks',
                'Unsupported v3 protocol')
    v2._require(protocol['policy'] == POLICY, 'Frozen v3 policy differs')
    reference = v2.validate_protocol(protocol['reference_protocol'])
    slots = protocol['model_slots']
    v2._require(type(slots) is list and len(slots) == 3, 'Three original extraction slots required')
    for slot, paper, call in zip(slots, reference['papers'], ('Q01', 'Q03', 'Q05')):
        v2._fields(slot, ('paper_id', 'call_id', 'instruction', 'instruction_sha256',
                         'task_ids', 'hypothesis_indices', 'normalized_page_sha256'), 'model mapping')
        v2._require(slot['paper_id'] == paper['id'] and slot['call_id'] == call and
                    slot['hypothesis_indices'] == [0, 1] and
                    slot['task_ids'] == [paper['id'] + ':hypothesis-' + str(i) for i in (1, 2)],
                    'Frozen model mapping differs')
        v2._require(type(slot['instruction']) is str and 0 < len(slot['instruction']) <= 4000
                    and hashlib.sha256(slot['instruction'].encode()).hexdigest() == slot['instruction_sha256']
                    and slot['instruction'] == extraction_instruction(reference, paper['id']),
                    'Frozen model instruction differs')
        v2._require(type(slot['normalized_page_sha256']) is dict and
            set(slot['normalized_page_sha256']) == {str(p['physical_page']) for p in paper['source']['pages']},
            'Complete selected-page seal inventory differs')
        for sha in slot['normalized_page_sha256'].values():
            v2._sha(sha)
    return deepcopy(protocol)


def load_protocol(path):
    return validate_protocol(read_bounded_json(path)[0])


def _selection(proof, paper, slot):
    v2._require(proof['operation'] == 'extract_material' and proof['source_kind'] == 'paper_selection'
                and proof['call_id'] == slot['call_id']
                and proof['request']['instruction'] == slot['instruction'], 'Foreign model task/instruction')
    selection = proof['context'].get('paper_selection')
    v2._require(type(selection) is dict and selection['paper_id'] == paper['id']
                and selection['paper_sha256'] == paper['source']['sha256']
                and selection['selected_pages'] == [p['physical_page'] for p in paper['source']['pages']],
                'Original model source/pages differ')
    # Selected text may be segmented into chunks. Compare exact normalized page
    # content, not a caller-supplied paper ID or a quote found elsewhere.
    for page in paper['source']['pages']:
        chunks = [c for c in selection['chunks'] if c['page'] == page['physical_page']]
        v2._require(chunks and hashlib.sha256(normalize(''.join(c['text'] for c in chunks)).encode()).hexdigest()
                    == slot['normalized_page_sha256'][str(page['physical_page'])],
                    'Original selected page content differs')
    return selection


def _citation(material, hypothesis, selection):
    evidence = {e['id']: e for e in material['evidence']}
    chunks = {c['id']: c for c in selection['chunks']}
    ids = hypothesis['evidence_ids']
    if not ids:
        return 'failed'
    for eid in ids:
        e = evidence.get(eid)
        if e is None:
            return 'failed'
        c = chunks.get(e['chunk_id'])
        if c is None or c['page'] != e['page']:
            return 'failed'
        lo, hi = e['normalized_start'], e['normalized_end']
        if not (c['start'] <= lo < hi <= c['end']) or c['text'][lo-c['start']:hi-c['start']] != normalize(e['quote']):
            return 'failed'
    return 'passed'


def _historical_model_calls(proofs):
    counts = [p['usage']['model_calls'] for p in proofs]
    return None if any(c is None for c in counts) else sum(counts)


def evaluate(protocol, observations, *, host_bindings=None, model_bindings=None):
    protocol = validate_protocol(protocol)
    v2._fields(observations, ('schema_version', 'protocol_sha256', 'baseline_observations'), 'v3 observations')
    v2._require(type(observations['schema_version']) is int and observations['schema_version'] == 3
                and observations['protocol_sha256'] == digest(protocol), 'V3 observation identity differs')
    baseline = observations['baseline_observations']
    v2._require(baseline['scope'] == 'actual_observation'
                and all(r['mode'] != 'model_assisted' for r in baseline['runs']),
                'Caller model check claims and fixtures are not original model proof')
    result = v2.evaluate(protocol['reference_protocol'], baseline, host_bindings=host_bindings)
    for row in result['runs']:
        if row['mode'] == 'model_assisted':
            row['status'] = 'no_admitted_original_receipt'
            row['absence_scope'] = 'quality_binding_only; original attempt disposition remains in program ledger'
    # A controlled component is narrower than a full strategy. Admit only its
    # independently recomputed numeric values and byte readback, while leaving
    # the paper-level baseline gate and all strategy claims unchanged.
    for row in result['runs']:
        key = (row['task_id'], row['mode'], 1)
        binding = (host_bindings or {}).get(key)
        if binding is None or row['mode'] == 'model_assisted' or row['status'] != 'completed':
            continue
        task = next(t for t in protocol['reference_protocol']['tasks'] if t['id'] == row['task_id'])
        paper = next(p for p in protocol['reference_protocol']['papers'] if p['id'] == task['paper_id'])
        run = next(r for r in baseline['runs'] if (r['task_id'], r['mode'], 1) == key)
        for check in row['checks']:
            if check['category'] == 'independent_numeric_reference':
                check['state'] = v2._check(check['category'], run['checks'][check['category']],
                    task, paper, protocol['reference_protocol'], binding, 'actual_observation')
                check['meaning'] = 'controlled_formula_component_only_not_full_strategy'
            elif check['category'] == 'reproducibility':
                check['state'] = 'passed'
                check['meaning'] = 'externally_pinned_component_bytes_only_not_empirical_reproduction'
        row['baseline_scope'] = 'controlled_formula_component_not_full_paper_strategy'
    bindings = {} if model_bindings is None else model_bindings
    v2._require(type(bindings) is dict and set(bindings) <= {s['call_id'] for s in protocol['model_slots']}
                and all(type(b) is LoopNativeReceiptBinding for b in bindings.values()),
                'Model bindings must be exact original typed receipts')
    proofs = []
    for slot in protocol['model_slots']:
        binding = bindings.get(slot['call_id'])
        if binding is None:
            continue
        proof = binding.to_dict()
        paper = next(p for p in protocol['reference_protocol']['papers'] if p['id'] == slot['paper_id'])
        selection = _selection(proof, paper, slot)
        parsed = proof['parsed_output']
        material = parsed.get('material')
        proofs.append({k: deepcopy(proof[k]) for k in ('call_id', 'proposal_id', 'attempt_id',
            'grant_id', 'context_sha256', 'raw_sha256', 'receipt_sha256', 'profile_digest',
            'domain_status', 'outcome', 'usage', 'wall_seconds')})
        for tid, index in zip(slot['task_ids'], slot['hypothesis_indices']):
            row = next(r for r in result['runs'] if r['task_id'] == tid and r['mode'] == 'model_assisted')
            row.update(status='abstained' if parsed['outcome'] == 'abstain' else
                       ('completed' if proof['domain_status'] in ('ready', 'handed_off') else 'failed'),
                       original_call_id=slot['call_id'], hypothesis_index=index,
                       transport_authenticated=True, semantic_fidelity='unverified')
            hypothesis = (material['hypotheses'][index] if material is not None
                          and index < len(material['hypotheses']) else None)
            for check in row['checks']:
                # Scientific/semantic comparisons intentionally stay unknown.
                # These two mechanical checks use original bound publications.
                if check['category'] == 'reproducibility':
                    check['state'] = 'passed'
                    check['meaning'] = 'immutable_original_receipt_readback_only_not_numerical_reproduction'
                elif check['category'] == 'citation_span' and hypothesis is not None:
                    check['state'] = _citation(material, hypothesis, selection)
                    check['meaning'] = 'exact_selected_source_span_not_semantic_support'
            row['missing_hypothesis'] = hypothesis is None
    counts = Counter(c['state'] for r in result['runs'] for c in r['checks'])
    result.update(protocol_id=PROTOCOL_ID, protocol_sha256=digest(protocol),
                  scope=protocol['scope'], original_model_receipts=proofs,
                  authenticated_model_extraction_calls=len(proofs),
                  authenticated_native_extraction_dispatches=len(proofs),
                  actual_model_receipts='bound' if proofs else 'not_bound',
                  model_quality_evaluated=bool(proofs), model_semantic_quality_evaluated=False,
                  comparison_claim=POLICY['comparison_claim'],
                  source_exposure_policy={k: POLICY[k] for k in ('model_source_exposure',
                      'baseline_source_exposure', 'equal_input_fair_comparison_verified')},
                  human_reviewed=False, scientific_score=None,
                  model_calls_during_evaluation=0, final_numeric_reads=0,
                  historical_model_calls=_historical_model_calls(proofs),
                  engineering_check_results={'passed':counts['passed'], 'failed':counts['failed'],
                                             'unknown':counts['unknown'], 'denominator':198})
    result.pop('automation_reference_agreement', None)
    result.pop('model_calls', None)
    return result
