"""Build an explicit evidence revision; never mutate a workspace or old artifacts.

Usage: build_alpha101_evidence_revision(base_task, paper_document, pdf_path).
The caller saves the returned task through the ordinary revision API with its
current base revision and an evidence_update issue. This helper does not submit
an experiment or record a semantic/human approval.
"""
from __future__ import annotations

from copy import deepcopy

from .contracts import Evidence, Task, require
from .evidence import match_evidence, verify_paper

ALPHA101_PDF_SHA256 = '1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9'
ALPHA101_FORMULA = '((close - open) / ((high - low) + .001))'
ORIGINAL_CLAIM = 'The source lists Alpha#101: ' + ALPHA101_FORMULA
MAINTAINED_CLAIM = ORIGINAL_CLAIM + '. On page 4 the author describes it as a delay-1 momentum alpha.'
ORIGINAL_DIRECTION = 'Higher values are long under the local evaluation convention; see assumptions.'
MAINTAINED_DIRECTION = (
    'Author description (page 4): after an intraday rise (close > open and high > low), take a long position the next day. '
    'Local evaluation: higher cross-sectional ranks receive higher weights; a positive raw score need not receive a long weight.')
OLD_DIRECTION_ASSUMPTION = ('Higher factor values form the long leg under a local evaluation convention, '
                            'not a conclusion established by this formula citation.')
MAINTAINED_ASSUMPTIONS = (
    'The author gives a next-day momentum interpretation on page 4; this is not proof of a causal economic mechanism. '
    'Any intraday-pressure explanation retained here remains a research conjecture.',
    'Cross-sectional average ranks are centered and normalized to gross exposure one. '
    'The rank portfolio and next-open to following-open holding period are local evaluation choices, not a reproduction of the full paper strategy.',
    'Page 16 defines daily OHLC fields but does not specify their currency or adjustment convention. '
    'Use the registered dataset declarations; do not infer real market units from the paper. '
    'The unchanged +.001 constant is in the same price units as high and low; rescaling prices while leaving +.001 fixed is not equivalent.',
)
ADDITIONAL_EVIDENCE = (
    {'id': 'alpha101-direction', 'page': 4,
     'quote': 'For instance, Alpha#101 in Appendix A is a delay-1 momentum alpha: if the stock runs up intraday '
              '(i.e., close > open and high > low), the next day one takes a long position in the stock.'},
    {'id': 'daily-ohlcv-definition', 'page': 16,
     'quote': 'open, close, high, low, volume = standard definitions for daily price and volume data'},
)


def build_alpha101_evidence_revision(task, paper, pdf_path):
    """Return a new task after exact source checks, preserving executable inputs.

    This intentionally accepts only the maintained original Alpha101 hypothesis,
    not arbitrary edits that could be overwritten or relabelled paper_original.
    It supports an idempotent second call and preserves unrelated research data.
    """
    _validate_task(task)
    require(paper.get('document_sha256') == ALPHA101_PDF_SHA256, 'Alpha101 maintenance requires the verified source edition')
    verify_paper(paper, pdf_path)
    value = deepcopy(task)
    hypotheses = [item for item in value['hypotheses'] if item['id'] == 'h-alpha101']
    require(len(hypotheses) == 1, 'Expected one maintained h-alpha101 hypothesis')
    hypothesis = hypotheses[0]
    require(hypothesis['attribution'] == 'paper_original'
            and hypothesis['claim'] in (ORIGINAL_CLAIM, MAINTAINED_CLAIM)
            and hypothesis['signal_direction'] in (ORIGINAL_DIRECTION, MAINTAINED_DIRECTION)
            and hypothesis['mechanism_attribution'] == 'model_conjecture'
            and set(hypothesis['required_fields']) == {'close', 'open', 'high', 'low'},
            'Alpha101 hypothesis has independent edits; review them before applying maintained evidence')
    linked = [item for item in value['candidates'] if item['hypothesis_id'] == 'h-alpha101']
    require(linked and all(item['expression'] == ALPHA101_FORMULA and item['origin'] == 'paper_original'
                           and not item['changes'] for item in linked),
            'Alpha101 maintenance cannot rewrite modified candidate formulas')
    indexed = {item['id']: item for item in value['evidence']}
    expected_formula = {'id': 'alpha101-formula', 'page': 15, 'quote': ALPHA101_FORMULA}
    require(indexed.get('alpha101-formula') == expected_formula
            and 'alpha101-formula' in hypothesis['evidence_ids'], 'The maintained Alpha101 formula evidence differs')
    match_evidence(Evidence(**expected_formula), paper)
    for item in ADDITIONAL_EVIDENCE:
        match_evidence(Evidence(**item), paper)
        require(item['id'] not in indexed or indexed[item['id']] == item,
                'An evidence identity already exists with different content')
        if item['id'] not in indexed:
            value['evidence'].append(deepcopy(item))
        if item['id'] not in hypothesis['evidence_ids']:
            hypothesis['evidence_ids'].append(item['id'])
    hypothesis['claim'] = MAINTAINED_CLAIM
    hypothesis['signal_direction'] = MAINTAINED_DIRECTION
    hypothesis['assumptions'] = [item for item in hypothesis['assumptions'] if item != OLD_DIRECTION_ASSUMPTION]
    for assumption in MAINTAINED_ASSUMPTIONS:
        if assumption not in hypothesis['assumptions']:
            hypothesis['assumptions'].append(assumption)
    _validate_task(value)
    return value


def _validate_task(task):
    # Stored revisions contain only the five editable sections; file tasks also
    # carry an execution envelope. Validate either form without adding fields.
    require(isinstance(task, dict), 'Task must be an object')
    Task.parse({'schema_version': 1, 'title': 'Alpha101 evidence maintenance',
                'paper': 'paper.json', 'paper_pdf': 'paper.pdf',
                'data': 'market.csv', 'data_metadata': 'metadata.json', **task})
