"""Research decisions from bounded eligibility aggregates; no raw-source authentication."""
from copy import deepcopy

from .author_archive_contract import source_metadata
from .author_panel import month_index, month_string
from .eligibility_schema import EligibilityPlan, EligibilityScan, EligibilityResult
from .storage import digest, json_text

SEMANTICS_VERSION = 'gjs-author-eligibility-v1'
MAX_SCAN_BYTES = 256 * 1024
MAX_MONTHS = 180
LIMITATIONS = [
    'Public author data are scrambled / randomly deleted, not unaltered market data.',
    'HTTP checks aggregate consistency only. Source hashes in imported JSON are claims; use CLI verify --source for raw-file recomputation.',
    'MOM uses H-12..H-2; DGW and positive market cap use H-1. Missing values are never filled; H labels do not determine eligibility.',
    'DGW is precomputed; its original daily construction and missing-date policy remain unresolved.',
    'All original rows are counted before country, REIT or microcap filters. Counts are upper bounds for the paper universe.',
    'A count threshold is not a valid portfolio: author-source sorts, neutralization, weights, label coverage and returns have not been executed.',
    'The reserved boundary is a project declaration. Prior source-quality audits and checkpoint diagnostics already inspected parts of that period.',
    'No investment performance, full paper replication, final-test result, human approval or human-efficiency improvement is inferred.',
]


def evidence():
    return {
        'paper_id': 'goyal-jegadeesh-subrahmanyam-rof-2025',
        'doi': '10.1093/rof/rfae038',
        'pdf_sha256': '07b8dad425b23328588a9668e8fccb58f18f79a4d1665d8fc49680198b0fbc2e',
        'version': 'gjs-rof-2025-eligibility-evidence-v1',
        'citations': [
            {'id': 'momentum-window', 'origin': 'paper', 'locator': 'Frozen PDF p.24; momentum-window anchor in docs/research/momentum_sources.json',
             'claim': 'The momentum definition uses the previous eleven monthly returns, excluding the most recent month.'},
            {'id': 'source-limit', 'origin': 'paper', 'locator': 'Frozen PDF p.30; data-limit anchor',
             'claim': 'The public author data are scrambled and randomly deleted.'},
            {'id': 'monthly-alignment', 'origin': 'author_code',
             'locator': 'Dataverse V2 SetupDataA.m lines9-11, SHA256 ba7dc4080c833fa99f7d090ff755c2ce73b44c5174b7b34ed4256e5733895fd1; Table8A.m lines11-19, SHA256 ece0bfd2091362842ef6237e9ddfa4719886a4fdb41e9bde204b5e13777c399e',
             'claim': 'Author code compounds eleven returns and aligns precomputed DGW and market cap one month before the target return.'},
            {'id': 'table8-prerequisite', 'origin': 'author_code', 'locator': 'Dataverse V2 Table8A.m lines3,14-16 and portfolio loops',
             'claim': 'The original Table8 starts with at least 3*3*50 eligible assets and also applies universe and per-cell constraints; an unfiltered total of450 is only a necessary upper-bound count check.'},
            {'id': 'screen-policy', 'origin': 'project', 'locator': SEMANTICS_VERSION,
             'claim': 'The plan declares the task, asset threshold and development/reserved months. Complete calendar-month history is required, with no fill or future-label selection. These screening conventions do not establish paper replication.'},
        ],
    }


def validate_plan(plan):
    normalized = EligibilityPlan.model_validate(plan).model_dump()
    if not normalized['rationale'].strip():
        raise ValueError('A nonblank rationale must justify the predeclared screening threshold')
    normalized['rationale'].encode('utf-8')
    start, end, reserved = (month_index(normalized[field]) for field in ('development_start', 'development_end', 'reserved_from'))
    source = source_metadata(normalized['source_file'])
    if not 1 <= end - start + 1 <= MAX_MONTHS:
        raise ValueError('Declare 1..180 continuous development months')
    if start - 12 < month_index(source['first_month']) or not end < reserved <= month_index(source['last_month']):
        raise ValueError('Development requires12 prior source months and must end before the in-source reserved boundary')
    if normalized['threshold_origin'] == 'table8_initial_upper_bound' and (
            normalized['task'] != 'momentum_dgw' or not normalized['requires_market_cap'] or normalized['minimum_assets'] != 450):
        raise ValueError('Table8 count screening requires MOM+DGW+MV and450 assets; other thresholds are project conventions')
    return normalized


def validate_scan(scan):
    if len(json_text(scan).encode('utf-8')) > MAX_SCAN_BYTES:
        raise ValueError('Eligibility scan exceeds256 KiB')
    normalized = EligibilityScan.model_validate(scan).model_dump()
    plan = validate_plan(normalized['plan'])
    source = source_metadata(plan['source_file'])
    if normalized['source'] != source or normalized['plan_digest'] != digest(plan):
        raise ValueError('Scan source or plan digest differs from the declared pinned identity')
    expected = [month_string(i) for i in range(month_index(plan['development_start']), month_index(plan['development_end']) + 1)]
    if [row['month'] for row in normalized['months']] != expected:
        raise ValueError('Scan must cover every declared month exactly once in order')
    for row in normalized['months']:
        counts = row['patterns']
        if min(counts) < 0 or sum(counts) != source['assets']:
            raise ValueError('Pattern counts must partition every original source row')
        reasons = sum(row[key] for key in ('momentum_missing', 'momentum_invalid', 'momentum_unrepresentable'))
        if reasons != sum(counts[::2]):
            raise ValueError('Missing/invalid/unrepresentable history must partition the unavailable MOM rows')
    return normalized


def evaluate(scan):
    scan = validate_scan(scan)
    plan = scan['plan']
    required = 1 | (2 if plan['task'] == 'momentum_dgw' else 0) | (4 if plan['requires_market_cap'] else 0)
    rows = []
    for row in scan['months']:
        def count(bits):
            return sum(value for mask, value in enumerate(row['patterns']) if mask & bits == bits)
        selected = count(required)
        rows.append({'month': row['month'], 'assets': scan['source']['assets'],
                     'momentum_ready': count(1), 'momentum_dgw_ready': count(3),
                     'momentum_mv_ready': count(5), 'momentum_dgw_mv_ready': count(7),
                     'selected_ready': selected, 'threshold_met': selected >= plan['minimum_assets'],
                     **{key: row[key] for key in ('momentum_missing', 'momentum_invalid', 'momentum_unrepresentable')}})
    passing = sum(row['threshold_met'] for row in rows)
    result = {'schema_version': 1, 'semantics_version': SEMANTICS_VERSION,
              'input_digest': digest(scan), 'plan_digest': scan['plan_digest'],
              'source': scan['source'], 'plan': plan, 'evidence': evidence(), 'months': rows,
              'summary': {'months': len(rows), 'months_meeting_threshold': passing,
                          'min_selected': min(row['selected_ready'] for row in rows),
                          'max_selected': max(row['selected_ready'] for row in rows),
                          'status': 'screen_passed' if passing == len(rows) else 'screen_blocked'},
              'limitations': list(LIMITATIONS), 'execution_ready': False,
              'verification_scope': 'aggregate_consistency_only'}
    return EligibilityResult.model_validate(result).model_dump()


def render_report(result):
    """Format computed screening outputs, never estimate performance numbers."""
    result = EligibilityResult.model_validate(result).model_dump()
    plan, summary, source = result['plan'], result['summary'], result['source']
    lines = ['# Author research eligibility study', '',
             'This is a label-free feasibility screen, not a portfolio backtest.', '',
             f"Paper: {result['evidence']['doi']}; evidence {result['evidence']['version']}",
             f"Source: {source['filename']}; Dataverse {source['doi']} V{source['version']}; SHA256 {source['sha256']}",
             f"Input digest: {result['input_digest']}", f"Plan digest: {result['plan_digest']}",
             f"Development: {plan['development_start']} .. {plan['development_end']}; reserved from {plan['reserved_from']}",
             f"Task: {plan['task']}; require market cap: {plan['requires_market_cap']}",
             f"Minimum assets: {plan['minimum_assets']}; origin: {plan['threshold_origin']}",
             'Rationale: ' + plan['rationale'], '',
             f"Screen result: {summary['status']}; qualifying months: {summary['months_meeting_threshold']}/{summary['months']}; selected-count range: {summary['min_selected']}..{summary['max_selected']}",
             'Portfolio execution ready: false. Missing method/universe/labels must be resolved in a separate version.', '',
             '| Month | All rows | MOM | MOM+DGW | MOM+MV | MOM+DGW+MV | Selected | Threshold met | Missing | Invalid | Unrepresentable |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    keys = ('month', 'assets', 'momentum_ready', 'momentum_dgw_ready', 'momentum_mv_ready',
            'momentum_dgw_mv_ready', 'selected_ready', 'threshold_met', 'momentum_missing', 'momentum_invalid', 'momentum_unrepresentable')
    lines += ['| ' + ' | '.join(str(row[key]) for key in keys) + ' |' for row in result['months']]
    lines += ['', '## Evidence and conventions', '']
    for citation in result['evidence']['citations']:
        lines += [f"- {citation['id']} ({citation['origin']}): {citation['claim']} Source: {citation['locator']}"]
    lines += ['', '## Verification and limits', ''] + ['- ' + value for value in result['limitations']] + ['']
    return '\n'.join(lines)


def demo_scan():
    """Synthetic aggregate contract fixture, never a claim of actual raw scanning."""
    plan = {'schema_version': 1, 'source_file': 'IntnlData.mat', 'development_start': '1993-03',
            'development_end': '1993-04', 'reserved_from': '2007-01', 'task': 'momentum',
            'requires_market_cap': False, 'minimum_assets': 30, 'threshold_origin': 'project_screen',
            'rationale': 'Synthetic test fixture only; counts were constructed, not scanned from the source.'}
    source = source_metadata(plan['source_file'])
    rows = [{'month': month, 'patterns': [source['assets'] - 60, 30, 0, 10, 0, 10, 0, 10],
             'momentum_missing': source['assets'] - 60, 'momentum_invalid': 0,
             'momentum_unrepresentable': 0} for month in ('1993-03', '1993-04')]
    return deepcopy({'schema_version': 1, 'kind': 'author_eligibility_scan', 'semantics_version': SEMANTICS_VERSION,
                     'source': source, 'plan': plan, 'plan_digest': digest(plan), 'months': rows})
