"""Source-bound assisted review, never research approval or a trading gate.

This development tool opens explicit task/metadata/PDF inputs only. It has no
Store, provider, grant, market-file loader or final-period execution path.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .evidence import ingest_pdf, normalize, sha256
from .formula_alignment import _try_parse

PROTOCOL = 'research-semantics-v1'
REFERENCE_SHA256 = 'cab8621095ba7f7880a08d3c9d6ebd33bd9306ba0552de38144cf4d86bb3eea1'
REFERENCE_PATH = Path(__file__).resolve().parents[1] / 'evaluation_suites/research_semantics_v1/reference.json'
FIELDS = {'open', 'close', 'high', 'low'}
FORMULA = '((close - open) / ((high - low) + .001))'
DIMENSIONS = (
    'formula_identity', 'required_fields', 'formula_lookback', 'linked_evidence_integrity',
    'formula_evidence_link', 'author_direction_evidence', 'formula_attribution_declaration',
    'mechanism_attribution_declaration', 'project_direction_declaration', 'price_unit_declaration',
    'calendar_adjustment_declaration', 'missing_policy_declaration', 'field_availability_declaration',
    'execution_timing_declaration', 'project_changes_declaration', 'causal_mechanism_truth',
    'overall_semantic_fidelity',
)
LOCAL_CONVENTIONS = {
    'portfolio': 'average_rank_center_gross_one',
    'missing_policy': 'strict_reject_no_fill',
    'signal_time': 'after_session_close',
    'entry_time': 'next_session_open',
    'exit_time': 'following_session_open',
    'costs': 'gross_without_costs_borrow_slippage_capacity',
}


def object_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_reference():
    if sha256(REFERENCE_PATH) != REFERENCE_SHA256:
        raise ValueError('Frozen semantic reference bytes differ')
    value = json.loads(REFERENCE_PATH.read_text())
    if value['protocol'] != PROTOCOL or value['actor'] != 'automation' or value['human_reviewed'] is not False:
        raise ValueError('Invalid assisted-review reference identity')
    return value


def verify_source(pdf_path, paper_id='alpha101'):
    reference = load_reference()
    sources = [reference['alpha101'], *reference['other_papers']]
    source = next((item for item in sources if item['paper_id'] == paper_id), None)
    if source is None:
        raise ValueError('Unsupported source paper')
    if sha256(pdf_path) != source['original_pdf_sha256']:
        raise ValueError('Original PDF bytes differ from the frozen edition')
    paper = ingest_pdf(pdf_path, paper_id, paper_id, 'preserved-original', 'frozen-edition')
    pages = {p['page']: normalize(p['text']) for p in paper['pages']}
    for number, expected in source['normalized_page_sha256'].items():
        if hashlib.sha256(pages[int(number)].encode()).hexdigest() != expected:
            raise ValueError('Complete normalized source page differs')
    for anchor in source['anchors']:
        text = pages[anchor['page']]
        if text.count(anchor['quote']) != 1 or text[anchor['normalized_start']:anchor['normalized_end']] != anchor['quote']:
            raise ValueError('Frozen source anchor differs or is ambiguous')
    return deepcopy(source), pages


def _item(dimension, assessment, basis, reason, evidence=(), observed=None):
    return dict(dimension=dimension, assessment=assessment, basis=basis, reason=reason,
                evidence=list(evidence), observed=deepcopy(observed), human_reviewed=False)


def review_alpha101(task, pdf_path, *, candidate_id, reviewer_id, metadata=None, conventions=None):
    """Review supplied task facts; supplied files do not authenticate a Native run.

    Pass/fail refer to a bounded fact or declaration. Economic truth and overall
    human semantic judgment always remain unknown. Text is retained for review;
    free-form meaning is never approved by substring matching.
    """
    if not isinstance(task, dict) or not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValueError('Task object and automation reviewer identity are required')
    source, pages = verify_source(pdf_path)
    candidates = [c for c in task.get('candidates', []) if c.get('id') == candidate_id]
    if len(candidates) != 1:
        raise ValueError('Candidate identity must resolve exactly once')
    candidate = candidates[0]
    hypotheses = [h for h in task.get('hypotheses', []) if h.get('id') == candidate.get('hypothesis_id')]
    if len(hypotheses) != 1:
        raise ValueError('Candidate hypothesis identity must resolve exactly once')
    hypothesis = hypotheses[0]
    meta = metadata or {}
    local = conventions or {}
    if not isinstance(meta, dict) or not isinstance(local, dict):
        raise ValueError('Metadata and conventions must be objects')
    expected, _, _ = _try_parse(FORMULA, FIELDS, structural=True)
    declared, features, error = _try_parse(candidate.get('expression'), FIELDS, structural=True)
    exact = declared == expected and declared is not None
    checks = []
    def add(*args, **kwargs):
        checks.append(_item(*args, **kwargs))
    anchors = {a['id']: a for a in source['anchors']}
    add('formula_identity', 'pass' if exact else 'fail', 'mathematical_structure',
        'Decimal constants and AST structure compared to the original ratio; this is not an economic judgment.',
        [anchors['formula']], candidate.get('expression'))
    required = hypothesis.get('required_fields')
    fields_match = isinstance(required, list) and len(required) == len(set(required)) and set(required) == FIELDS
    add('required_fields', 'pass' if fields_match else 'fail', 'declaration_vs_formula',
        'Original #101 requires daily open, close, high and low; no substituted field is authorized.',
        [anchors['daily_fields'], anchors['formula']], required)
    add('formula_lookback', 'pass' if exact else ('fail' if features else 'unknown'), 'formula_structure',
        'Original #101 has no rolling operator/window. Author delay-1 describes trading delay; inserting delay() changes the formula.',
        [anchors['formula'], anchors['author_direction']], features)
    selected, invalid = [], []
    ids = hypothesis.get('evidence_ids')
    all_evidence = task.get('evidence', [])
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or not isinstance(all_evidence, list):
        invalid.append('Missing or duplicate evidence identities')
    else:
        for identity in ids:
            items = [e for e in all_evidence if isinstance(e, dict) and e.get('id') == identity]
            if len(items) != 1:
                invalid.append('Evidence identity absent or ambiguous: ' + str(identity)); continue
            item = items[0]
            quote = normalize(item['quote']) if isinstance(item.get('quote'), str) else ''
            page = item.get('page')
            text = pages.get(page, '') if type(page) is int else ''
            if not quote or text.count(quote) != 1:
                invalid.append('Quote absent or ambiguous: ' + str(identity)); continue
            start = text.index(quote)
            # Optional supplied offsets are normalized-page offsets, never raw extraction offsets.
            for key, expected_offset in [('normalized_start', start), ('normalized_end', start+len(quote))]:
                if key in item and (type(item[key]) is not int or item[key] != expected_offset):
                    invalid.append('Quote span differs: ' + str(identity))
            selected.append(dict(evidence_id=identity, page=page, quote=quote,
                                 normalized_start=start, normalized_end=start+len(quote)))
    add('linked_evidence_integrity', 'fail' if invalid else 'pass', 'original_pdf_quote_span',
        'All hypothesis-linked quotes are independently located in the frozen original PDF.', selected, invalid)
    formula_link = any(e['page'] == 15 and anchors['formula']['quote'] in e['quote'] for e in selected)
    author_link = any(e['page'] == 4 and anchors['author_direction']['quote'] in e['quote'] for e in selected)
    add('formula_evidence_link', 'pass' if formula_link and not invalid else 'fail', 'source_linkage',
        'A verified linked page-15 quote must contain the complete original #101 expression.', selected)
    add('author_direction_evidence', 'pass' if author_link and not invalid else 'unknown', 'source_description',
        'The author explicitly describes delay-1 momentum and next-day long after an intraday rise; missing linkage is incomplete task evidence, not absence from the paper.',
        [anchors['author_direction']], hypothesis.get('signal_direction'))
    original = candidate.get('origin') == 'paper_original' and hypothesis.get('attribution') == 'paper_original'
    add('formula_attribution_declaration', 'pass' if exact and original else 'fail', 'bounded_attribution',
        'Only an unchanged original expression is accepted as paper_original by this reference.', [anchors['formula']],
        dict(candidate_origin=candidate.get('origin'), hypothesis_attribution=hypothesis.get('attribution')))
    mechanism = hypothesis.get('mechanism_attribution')
    add('mechanism_attribution_declaration', 'pass' if mechanism == 'model_conjecture' else 'unknown', 'declaration_only',
        'Conjecture labeling is checked separately from the free-form mechanism; no text match establishes causal truth.',
        observed=dict(attribution=mechanism, text=hypothesis.get('economic_mechanism')))
    add('project_direction_declaration', 'pass' if local.get('portfolio') == LOCAL_CONVENTIONS['portfolio'] else 'unknown',
        'local_convention', 'Centered cross-sectional ranks differ from simply buying every positive raw score; project convention requires explicit declaration.',
        observed=dict(convention=local.get('portfolio'), task_text=hypothesis.get('signal_direction')))
    units = meta.get('price_units')
    add('price_unit_declaration', 'pass' if isinstance(units, str) and units.strip() else 'unknown', 'declaration_only',
        '+.001 has the same price unit as high-low, making the ratio dimensionless. The source does not specify dataset currency/adjustment; fixed epsilon is not invariant to price rescaling.',
        [anchors['formula'], anchors['daily_fields']], units)
    add('calendar_adjustment_declaration', 'pass' if all(isinstance(meta.get(k),str) and meta[k].strip() for k in ('calendar_kind','adjustment')) else 'unknown',
        'declaration_only', 'Calendar and price adjustment must be declared locally; metadata completeness does not authenticate a real source.',
        observed={k:meta.get(k) for k in ('calendar_kind','adjustment')})
    missing = local.get('missing_policy')
    add('missing_policy_declaration', 'pass' if missing == LOCAL_CONVENTIONS['missing_policy'] else ('fail' if missing else 'unknown'),
        'local_convention', 'This review contract rejects absent bars and unauthorized fill/substitution; the paper does not define this local missing policy.', observed=missing)
    availability = meta.get('field_availability', {})
    good_availability = isinstance(availability, dict) and all(availability.get(f) in ('after session close','after_session_close') for f in ('close','high','low')) and availability.get('open') in ('session open','session_open')
    add('field_availability_declaration', 'pass' if good_availability else ('unknown' if not availability else 'fail'),
        'local_declaration', 'High/low/close are declared available after the completed session. This checks declarations, not provider timestamps.', observed=availability)
    timing = {k:local.get(k) for k in ('signal_time','entry_time','exit_time')}
    timing_ok = all(timing[k] == LOCAL_CONVENTIONS[k] for k in timing)
    add('execution_timing_declaration', 'pass' if timing_ok else ('fail' if any(timing.values()) else 'unknown'),
        'local_convention', 'After t close, enter t+1 open and exit t+2 open; labels stay inside validation and its last two sessions are purged. The complete local rule is not established by the source next-day description.',
        [anchors['author_direction']], timing)
    changes = candidate.get('changes')
    add('project_changes_declaration', 'pass' if exact and changes == [] else ('pass' if not exact and candidate.get('origin') == 'user_modification' and isinstance(changes,list) and changes else 'fail'),
        'declaration_only', 'Formula changes must be explicit; local portfolio, units/calendar/missing and execution choices remain project adaptations even with unchanged formula.', observed=changes)
    add('causal_mechanism_truth', 'unknown', 'unassessed_scientific_claim',
        'Formula and author description do not prove intraday-pressure causality, next-day profitability or applicability to this synthetic dataset.', observed=hypothesis.get('economic_mechanism'))
    add('overall_semantic_fidelity', 'unknown', 'requires_human_research_review',
        'No automated literal/structure check, supplied metadata or scalar agreement establishes complete scientific fidelity or human approval.')
    if tuple(c['dimension'] for c in checks) != DIMENSIONS:
        raise AssertionError('Semantic review denominator differs')
    counts = Counter(c['assessment'] for c in checks)
    return dict(schema_version=1, protocol=PROTOCOL, reviewer=dict(actor='automation',id=reviewer_id,human_reviewed=False),
                input_binding=dict(task_digest=object_digest(task),metadata_digest=object_digest(meta),conventions_digest=object_digest(local),
                                   reference_file_sha256=REFERENCE_SHA256,original_pdf_sha256=source['original_pdf_sha256'],
                                   candidate_id=candidate_id,hypothesis_id=hypothesis['id'],native_transport_authentication='not_performed'),
                source=source,reviewed_text=deepcopy(hypothesis),items=checks,
                denominator=dict(expected=len(DIMENSIONS),reported=len(checks),counts={k:counts[k] for k in ('pass','fail','unknown')}),
                mechanical_status='fail' if counts['fail'] else 'pass',semantic_status='unknown',
                causal_mechanism_truth='unknown',human_reviewed=False,scientific_score=None,
                scope='bounded source facts/declarations; no human/scientific acceptance',
                final_numeric_rows_used=0,model_calls=0,market_numeric_rows_read=0)


def review_method_gaps(pdf_paths, *, reviewer_id):
    if not isinstance(reviewer_id,str) or not reviewer_id.strip():
        raise ValueError('Automation reviewer identity is required')
    expected = load_reference()['other_papers']
    if set(pdf_paths) != {s['paper_id'] for s in expected}:
        raise ValueError('All three exact source PDFs are required')
    verified = [verify_source(pdf_paths[s['paper_id']],s['paper_id'])[0] for s in expected]
    return dict(schema_version=1,protocol=PROTOCOL,reviewer=dict(actor='automation',id=reviewer_id,human_reviewed=False),
                reference_file_sha256=REFERENCE_SHA256,papers=verified,complete_strategy_reproduction='unknown',
                complete_strategy_baseline='not_run',human_reviewed=False,model_calls=0,final_numeric_rows_used=0,
                limitations=['Method inventory covers selected source pages only; it is not a complete paper review.',
                             'Existing quality-v3 scores, oracle, outputs and denominators are not modified or reinterpreted.'])


def fixture_baseline_inputs():
    """Freeze the same-factor/same-config comparison before any calculation."""
    from evaluation_suites.research_semantics_v1.scalar_baseline import fixture
    value=fixture()
    root=Path(__file__).resolve().parents[1]
    paths=['paper_alpha/research_semantic_review.py','paper_alpha/expressions.py','paper_alpha/evaluation.py',
           'evaluation_suites/research_semantics_v1/scalar_baseline.py','evaluation_suites/research_semantics_v1/reference.json']
    return dict(protocol=PROTOCOL,scope='same_factor_same_config_tool_consistency_only',
                candidate_expression=FORMULA,baseline_expression=FORMULA,
                candidate_input_digest=object_digest(value),baseline_input_digest=object_digest(value),
                candidate_config_digest=object_digest(value['config']),baseline_config_digest=object_digest(value['config']),
                source_files_sha256={p:sha256(root/p) for p in paths},input=value,
                causal_mechanism_truth='unknown',strategy_superiority='not_assessed',
                agent_vs_fixed_process_fairness='not_assessed',final_numeric_rows_used=0)


def run_fixture_baseline(*, reviewer_id):
    """Compute new synthetic development values and independently compare them.

    No caller may supply prices/paths. The scalar implementation does not call
    the DSL, parser, engine evaluation/rank helpers, numpy or pandas.
    """
    if not isinstance(reviewer_id,str) or not reviewer_id.strip():
        raise ValueError('Automation reviewer identity is required')
    import math
    import pandas as pd
    from .expressions import compute_expression
    from .evaluation import evaluate_development
    from evaluation_suites.research_semantics_v1.scalar_baseline import reference
    frozen=fixture_baseline_inputs()
    value=frozen['input']
    frame=pd.DataFrame(value['rows'])
    frame['date']=pd.to_datetime(frame['date'])
    panels={field:frame.pivot(index='date',columns='asset',values=field).reindex(columns=value['assets'])
            for field in ('open','high','low','close','volume')}
    factor=compute_expression(FORMULA,panels)
    engine=evaluate_development(factor,panels,value['config'],value['calendar'])
    scalar=reference(value)
    errors=[]
    maximum={k:0.0 for k in ('factor','weight','label','contribution','gross_return','rank_ic','metric')}
    def compare(left,right,kind,path):
        if left is None or right is None:
            if left is not right:errors.append(path+': missing value differs')
            return
        if not (math.isfinite(left) and math.isfinite(right)):
            errors.append(path+': nonfinite value');return
        delta=abs(left-right);maximum[kind]=max(maximum[kind],delta)
        if delta>1e-12:errors.append(path+': numeric difference')
    for t,row in enumerate(factor.to_numpy().tolist()):
        for a,number in enumerate(row):compare(number,scalar['factors'][t][a],'factor',f'factor/{t}/{a}')
    for t,(actual,expected) in enumerate(zip(engine['daily'],scalar['daily'],strict=True)):
        for key in ('signal_date','status','entry_date','exit_date'):
            if actual[key]!=expected[key]:errors.append(f'daily/{t}/{key}: differs')
        for key in ('gross_return','rank_ic'):compare(actual[key],expected[key],key,f'daily/{t}/{key}')
        if len(actual['assets'])!=len(expected['assets']):errors.append(f'daily/{t}/assets: count differs')
        for a,(left,right) in enumerate(zip(actual['assets'],expected['assets'])):
            if left['asset']!=right['asset']:errors.append(f'daily/{t}/{a}/asset: differs')
            for key,kind in [('signal','factor'),('weight','weight'),('forward_return','label'),('gross_contribution','contribution')]:
                compare(left[key],right[key],kind,f'daily/{t}/{a}/{key}')
    for key,number in scalar['metrics'].items():compare(engine['metrics'][key],number,'metric','metric/'+key)
    if object_digest(value)!=frozen['candidate_input_digest']:
        raise ValueError('Frozen comparison inputs changed during execution')
    return dict(schema_version=1,protocol=PROTOCOL,reviewer=dict(actor='automation',id=reviewer_id,human_reviewed=False),
                status='pass' if not errors else 'fail',semantic_status='unknown',human_reviewed=False,
                input_freeze=frozen,scalar_result=scalar,engine_result=engine,
                comparison=dict(tolerance=1e-12,max_abs_error=maximum,errors=errors,
                                same_factor=True,same_config=True,same_sample=True,independent_scalar=True),
                model_calls=0,market_file_numeric_rows_read=0,synthetic_numeric_rows_generated=len(value['rows']),
                final_numeric_rows_used=0,scientific_score=None,
                limitations=['Tool consistency for unchanged #101 on a fixed synthetic development fixture only.',
                             'No new alpha superiority, profitability, causal proof, full-paper replication or model-versus-template research quality claim.',
                             'Three-paper full strategy baselines remain blocked by recorded method/data gaps.'])
