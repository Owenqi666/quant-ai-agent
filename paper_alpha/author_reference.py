"""Independent scalar Fraction oracle; does not call production evaluation/window helpers."""
from fractions import Fraction
import math

from .author_archive_contract import LIMITATIONS, SEMANTICS_VERSION
from .author_panel_schema import AuthorDiagnostic
from .storage import digest


def check(panel, result):
    issues = []
    try:
        AuthorDiagnostic.model_validate(result)
        year, month = [int(x) for x in panel['selection']['target_month'].split('-')]
        absolute = year * 12 + month - 1
        expected_months = [f'{i//12:04d}-{i%12+1:02d}' for i in range(absolute - 12, absolute + 1)]
        expected_windows = {'momentum_months': expected_months[:11], 'skip_month': expected_months[-2],
                            'formation_month': expected_months[-2], 'label_month': expected_months[-1]}
        if (result['schema_version'] != 1 or result['semantics_version'] != SEMANTICS_VERSION
                or result['data_kind'] != 'author_perturbed_monthly_panel'
                or result['panel_digest'] != digest(panel) or result['source'] != panel['source']
                or result['selection'] != panel['selection'] or result['windows'] != expected_windows
                or result['returns_included_in_formation'] is not False or result['limitations'] != LIMITATIONS):
            issues.append('metadata_or_window_mismatch')
        if len(result['rows']) != len(panel['rows']):
            issues.append('row_count_mismatch')
        ready, labels, both = 0, 0, 0
        for index, src in enumerate(panel['rows']):
            reasons, missing = [], []
            product = Fraction(1)
            valid = True
            for j in range(11):
                raw = src['returns'][j]
                if src['return_states'][j] != 'value':
                    missing.append(expected_months[j])
                    reasons.append('momentum_missing:' + expected_months[j] + ':' + src['return_states'][j])
                    valid = False
                elif raw < -1:
                    valid = False
                    reasons.append('momentum_invalid_return:' + expected_months[j])
                else:
                    product *= 1 + Fraction(raw)
            signal = None
            if valid:
                try:
                    signal = float(product - 1)
                except OverflowError:
                    reasons.append('momentum_not_representable')
            if src['dgw_state'] != 'value':
                reasons.append('dgw_missing:' + src['dgw_state'])
            elif src['dgw'] < -1 or src['dgw'] > 1:
                reasons.append('dgw_out_of_range')
            if src['market_cap_state'] != 'value':
                reasons.append('market_cap_missing:' + src['market_cap_state'])
            elif src['market_cap'] <= 0:
                reasons.append('market_cap_not_positive')
            is_ready = len(reasons) == 0
            label_ok = src['return_states'][-1] == 'value' and src['returns'][-1] >= -1
            if src['return_states'][-1] != 'value':
                reasons.append('label_missing:' + src['return_states'][-1])
            elif src['returns'][-1] < -1:
                reasons.append('label_invalid_return')
            ready += is_ready
            labels += label_ok
            both += is_ready and label_ok
            if index >= len(result['rows']):
                continue
            row = result['rows'][index]
            expected = {'asset': src['asset'], 'source_row': src['source_row'], 'country': src['country'],
                        'dgw': src['dgw'], 'market_cap': src['market_cap'], 'label': src['returns'][-1],
                        'formation_ready': is_ready, 'label_ready': label_ok,
                        'reasons': reasons, 'missing_momentum_months': missing}
            if any(row[k] != v for k, v in expected.items()):
                issues.append(f'row_{index}:values_or_eligibility_mismatch')
            actual = row['momentum']
            if (signal is None) != (actual is None) or (signal is not None and not math.isclose(actual, signal, rel_tol=1e-12, abs_tol=1e-12)):
                issues.append(f'row_{index}:momentum_mismatch')
        if result['summary'] != {'assets': len(panel['rows']), 'formation_ready': ready, 'labels_available': labels, 'ready_with_label': both}:
            issues.append('summary_mismatch')
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        issues.append('invalid_reference_input_or_result')
    return {'schema_version': 1, 'method': 'independent-fraction-reference-v1', 'passed': not issues, 'issues': issues}
