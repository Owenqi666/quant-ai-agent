"""Deterministic MOM/DGW alignment diagnostics, never a portfolio backtest."""
from copy import deepcopy
from datetime import date
import math
import re

from .author_archive_contract import (ADAPTER_VERSION, SEMANTICS_VERSION, MAX_PANEL_BYTES,
                                     LIMITATIONS, source_metadata)
from .author_panel_schema import AuthorPanel, AuthorDiagnostic
from .storage import digest, json_text


def month_index(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('Month must have YYYY-MM format')
    year, month = map(int, value.split('-'))
    if not 1 <= year <= 9999:
        raise ValueError('Month year is outside the supported calendar')
    return year * 12 + month - 1


def month_string(index):
    year, month = divmod(index, 12)
    return f'{year:04d}-{month + 1:02d}'


def _state(value, state):
    if state == 'value':
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('A value state requires a finite number (not a boolean)')
    elif value is not None:
        raise ValueError('A missing/nonfinite source state requires null')


def validate_panel(panel):
    """Validate claims and alignment, not authentication of the absent raw MAT."""
    if not isinstance(panel, dict) or type(panel.get('schema_version')) is not int:
        raise ValueError('Author panel schema_version must be integer 1')
    if len(json_text(panel).encode('utf-8')) > MAX_PANEL_BYTES:
        raise ValueError('Author normalized panel exceeds 768 KiB')
    AuthorPanel.model_validate(panel)
    source, selection = panel['source'], panel['selection']
    for key in ('file_id', 'bytes', 'assets', 'periods'):
        if type(source[key]) is not int:
            raise ValueError('Author source integer field is invalid')
    if source != source_metadata(source['filename']):
        raise ValueError('Author source does not match the pinned Dataverse V2 identity')
    target = month_index(selection['target_month'])
    expected = [month_string(i) for i in range(target - 12, target + 1)]
    if (panel['months'] != expected or expected[0] < source['first_month']
            or expected[-1] > source['last_month']):
        raise ValueError('Panel must contain the in-source H-12..H month window')
    for month, observed in zip(expected, panel['source_observation_dates']):
        if source['filename'] == 'IntnlData.mat':
            if observed is not None:
                raise ValueError('International source has months only, not observation dates')
        elif not isinstance(observed, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', observed) or date.fromisoformat(observed).isoformat() != observed or observed[:7] != month:
            raise ValueError('US source observation dates must be valid dates in their month')
    if len(panel['rows']) != selection['row_count'] or selection['row_offset'] + selection['row_count'] > source['assets']:
        raise ValueError('Author panel row selection exceeds source bounds')
    for offset, row in enumerate(panel['rows'], selection['row_offset'] + 1):
        if row['source_row'] != offset or row['asset'] != f"{source['filename']}:row:{offset}":
            raise ValueError('Author identities must be contiguous original one-based row identifiers')
        if source['filename'] == 'USData.mat':
            if row['country'] is not None:
                raise ValueError('US country mapping is not encoded in the normalized source contract')
        elif not isinstance(row['country'], str) or not re.fullmatch(r'[A-Z]{2}', row['country']):
            raise ValueError('International country must be the source two-letter code')
        for value, state in zip(row['returns'], row['return_states']):
            _state(value, state)
        _state(row['dgw'], row['dgw_state'])
        _state(row['market_cap'], row['market_cap_state'])
    return deepcopy(panel)


def evaluate(panel):
    panel = validate_panel(panel)
    rows = []
    for original in panel['rows']:
        reasons, missing = [], []
        valid_history = True
        for month, value, state in zip(panel['months'][:11], original['returns'][:11], original['return_states'][:11]):
            if state != 'value':
                valid_history = False
                missing.append(month)
                reasons.append('momentum_missing:' + month + ':' + state)
            elif value < -1:
                valid_history = False
                reasons.append('momentum_invalid_return:' + month)
        momentum = None
        if valid_history:
            try:
                momentum = (-1.0 if -1 in original['returns'][:11]
                            else math.expm1(math.fsum(math.log1p(value) for value in original['returns'][:11])))
            except OverflowError:
                momentum = None
            if momentum is None or not math.isfinite(momentum):
                momentum = None
                reasons.append('momentum_not_representable')
        if original['dgw_state'] != 'value':
            reasons.append('dgw_missing:' + original['dgw_state'])
        elif not -1 <= original['dgw'] <= 1:
            reasons.append('dgw_out_of_range')
        if original['market_cap_state'] != 'value':
            reasons.append('market_cap_missing:' + original['market_cap_state'])
        elif original['market_cap'] <= 0:
            reasons.append('market_cap_not_positive')
        formation_ready = not reasons
        label = original['returns'][12]
        label_ready = original['return_states'][12] == 'value' and label >= -1
        if original['return_states'][12] != 'value':
            reasons.append('label_missing:' + original['return_states'][12])
        elif label < -1:
            reasons.append('label_invalid_return')
        rows.append({'asset': original['asset'], 'source_row': original['source_row'], 'country': original['country'],
                     'momentum': momentum, 'dgw': original['dgw'], 'market_cap': original['market_cap'],
                     'label': label, 'formation_ready': formation_ready, 'label_ready': label_ready,
                     'reasons': reasons, 'missing_momentum_months': missing})
    result = {'schema_version': 1, 'semantics_version': SEMANTICS_VERSION,
              'data_kind': panel['kind'], 'panel_digest': digest(panel),
              'source': panel['source'], 'selection': panel['selection'],
              'windows': {'momentum_months': panel['months'][:11], 'skip_month': panel['months'][11],
                          'formation_month': panel['months'][11], 'label_month': panel['months'][12]},
              'rows': rows, 'summary': {'assets': len(rows), 'formation_ready': sum(row['formation_ready'] for row in rows),
                  'labels_available': sum(row['label_ready'] for row in rows),
                  'ready_with_label': sum(row['formation_ready'] and row['label_ready'] for row in rows)},
              'limitations': deepcopy(LIMITATIONS), 'returns_included_in_formation': False}
    AuthorDiagnostic.model_validate(result)
    return result


def render_report(result):
    """Format only frozen diagnostic fields; compute no new research statistics."""
    AuthorDiagnostic.model_validate(result)
    def number(value):
        return 'unavailable' if value is None else format(value, '.17g')
    source, summary, window = result['source'], result['summary'], result['windows']
    lines = ['# Author monthly panel alignment diagnostic', '',
             'Public scrambled / randomly deleted author data. No market-performance or portfolio-return claim.',
             'This report checks a normalized panel; the raw MAT requires a separate source verification.', '',
             f"Source: {source['filename']}; DOI {source['doi']}; version {source['version']}",
             f"Raw source claimed SHA256: {source['sha256']}", f"Panel digest: {result['panel_digest']}",
             f"Semantics: {result['semantics_version']}",
             f"MOM: {window['momentum_months'][0]} .. {window['momentum_months'][-1]}; skip {window['skip_month']}",
             f"DGW / market cap: {window['formation_month']}; independent label: {window['label_month']}",
             f"Rows: {summary['assets']}; formation available: {summary['formation_ready']}; labels: {summary['labels_available']}; both: {summary['ready_with_label']}", '',
             '| Original row | Country | MOM | DGW | Market cap | Label | Formation ready | Label ready | Reasons |',
             '|---|---|---|---|---|---|---|---|---|']
    for row in result['rows']:
        lines.append('| ' + ' | '.join([str(row['source_row']), row['country'] or 'not encoded',
             number(row['momentum']), number(row['dgw']), number(row['market_cap']), number(row['label']),
             str(row['formation_ready']), str(row['label_ready']), '; '.join(row['reasons']) or 'none']) + ' |')
    lines.extend(['', '## Boundaries', ''] + ['- ' + value for value in result['limitations']] + [''])
    return '\n'.join(lines)
