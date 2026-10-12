"""Independent bounded formula-component references, not paper backtests.

Only stdlib arithmetic is used. Inputs are explicitly controlled numbers;
market datasets, production calculators and final-period access are unsupported.
"""
from __future__ import annotations
from fractions import Fraction
from datetime import date
from calendar import monthrange
import math

REFERENCE_VERSION = 'independent-paper-components-v1'
REFERENCE_IDS = ('52week_ratio', 'equal_weight_spread', 'bab_beta_shrink',
                 'bab_excess_spread', 'tsmom_sign', 'tsmom_position_return')
MAX_ITEMS = 4096


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError('Reference input fields differ')


def _fraction(value):
    if type(value) not in (int, float, str) or type(value) is bool:
        raise ValueError('Reference requires a finite scalar')
    if type(value) is str:
        if len(value) > 80:
            raise ValueError('Reference decimal is too long')
        try:
            out = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ValueError('Invalid reference decimal') from None
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Reference scalar must be finite')
        out = Fraction.from_float(value)
    else:
        out = Fraction(value)
    if out.numerator.bit_length() + out.denominator.bit_length() > 4096:
        raise ValueError('Reference arithmetic bound exceeded')
    return out


def _items(values):
    if type(values) is not list or not 1 <= len(values) <= MAX_ITEMS:
        raise ValueError('Reference array bounds differ')
    return [_fraction(v) for v in values]


def _finite(value):
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError('Reference result is not representable') from None
    if not math.isfinite(result):
        raise ValueError('Reference result must be finite')
    return result


def evaluate_reference(reference_id, payload):
    if reference_id not in REFERENCE_IDS:
        raise ValueError('Unregistered independent reference')
    if type(payload) is not dict or payload.get('scope') != 'controlled_formula_component':
        raise ValueError('Only explicit controlled formula components are admitted')
    data = dict(payload)
    data.pop('scope')
    values = {}
    limitations = ['Controlled-number arithmetic only; no market data, portfolio backtest or original-paper empirical reproduction.']
    if reference_id == '52week_ratio':
        _fields(data, ('as_of', 'declared_calendar', 'prices'))
        end = date.fromisoformat(data['as_of'])
        if end.day != monthrange(end.year, end.month)[1]:
            raise ValueError('Formation date must be calendar month-end')
        first = date(end.year - 1, end.month, 1)
        first = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        calendar = data['declared_calendar']
        if type(calendar) is not list or not 1 <= len(calendar) <= MAX_ITEMS:
            raise ValueError('Calendar bounds differ')
        dates = [date.fromisoformat(v) for v in calendar]
        if dates != sorted(set(dates)) or any(not first <= d <= end for d in dates):
            raise ValueError('Calendar must be unique, ordered and inside twelve calendar months')
        if type(data['prices']) is not list or len(data['prices']) != len(calendar):
            raise ValueError('Complete declared price grid required')
        observed = []
        for row, expected in zip(data['prices'], calendar):
            _fields(row, ('date', 'value'))
            if row['date'] != expected:
                raise ValueError('Price dates differ from declared calendar')
            value = _fraction(row['value'])
            if value <= 0:
                raise ValueError('Prices must be positive and complete')
            observed.append(value)
        values = {'ratio': _finite(observed[-1] / max(observed))}
        limitations += ['Calendar completeness and price adjustment are declared fixture properties, not authenticated market facts.', 'The latest declared session is used for month-end price; fixed 252-row substitution is unsupported.']
    elif reference_id == 'equal_weight_spread':
        _fields(data, ('winner_returns', 'loser_returns'))
        winners, losers = _items(data['winner_returns']), _items(data['loser_returns'])
        values = {'spread': _finite(sum(winners) / len(winners) - sum(losers) / len(losers))}
        limitations += ['Membership and overlapping holding cohorts are supplied, not reconstructed; this is an equal-weight arithmetic component.']
    elif reference_id == 'bab_beta_shrink':
        _fields(data, ('sigma_security', 'sigma_market', 'rho'))
        si, sm, rho = (_fraction(data[k]) for k in ('sigma_security', 'sigma_market', 'rho'))
        if si < 0 or sm <= 0 or not -1 <= rho <= 1:
            raise ValueError('Invalid volatility or correlation')
        raw = rho * si / sm
        values = {'beta_ts': _finite(raw), 'beta_shrunk': _finite(Fraction(3, 5) * raw + Fraction(2, 5))}
        limitations += ['Volatility/correlation estimates are supplied; one/five-year log-return estimators and 120/750 observations are not computed.']
    elif reference_id == 'bab_excess_spread':
        _fields(data, ('low_beta', 'high_beta', 'low_return', 'high_return', 'risk_free'))
        low, high, lr, hr, rf = (_fraction(data[k]) for k in ('low_beta', 'high_beta', 'low_return', 'high_return', 'risk_free'))
        if not 0 < low < high:
            raise ValueError('Positive ordered portfolio betas required')
        values = {'bab_excess': _finite((lr - rf) / low - (hr - rf) / high)}
        limitations += ['Betas, membership and risk-free return are supplied; beta neutrality is distinct from dollar neutrality.']
    elif reference_id == 'tsmom_sign':
        _fields(data, ('monthly_excess_returns',))
        returns = _items(data['monthly_excess_returns'])
        if len(returns) != 12 or any(r < -1 for r in returns):
            raise ValueError('Twelve declared complete monthly excess returns required')
        total = Fraction(1)
        for item in returns:
            total *= 1 + item
        accumulated = total - 1
        if accumulated == 0:
            values = {'past_excess': 0.0, 'signal': None}
            limitations += ['Zero-signal position is not specified in the selected paper definition; no invented zero-position gold.']
        else:
            values = {'past_excess': _finite(accumulated), 'signal': 1 if accumulated > 0 else -1}
        limitations += ['Monthly inputs are controlled aggregates; futures rolling and the original daily excess-return index are not reconstructed.']
    else:
        _fields(data, ('past_excess', 'annualized_volatility', 'next_excess_return'))
        past, volatility, following = (_fraction(data[k]) for k in ('past_excess', 'annualized_volatility', 'next_excess_return'))
        if volatility <= 0:
            raise ValueError('Strictly positive ex-ante volatility required')
        if past == 0:
            values = {'position': None, 'strategy_return': None}
            limitations += ['Zero-signal action remains unresolved rather than choosing a project convention.']
        else:
            position = (1 if past > 0 else -1) * Fraction(2, 5) / volatility
            values = {'position': _finite(position), 'strategy_return': _finite(position * following)}
        limitations += ['Annualized formation-time volatility is supplied; EW variance 261-scale/60-day center of mass and futures data are not recomputed.']
    return {'reference_version': REFERENCE_VERSION, 'reference_id': reference_id,
            'scope': 'controlled_formula_component', 'values': values,
            'original_paper_reproduced': False, 'scientific_score': None,
            'final_numeric_reads': 0, 'limitations': limitations}
