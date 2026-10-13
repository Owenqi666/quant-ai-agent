"""Frozen, self-contained pre-optimization Pearson reference.

Copied verbatim from public commit 1c44df62985049af0c19dea2148666e089354f05, paper_alpha/expressions.py.
The three function bodies must remain unchanged during performance work.
This module imports no production numerical helpers.
"""

from __future__ import annotations

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import math

import numpy as np
import pandas as pd

SOURCE_PUBLIC_COMMIT = '1c44df62985049af0c19dea2148666e089354f05'
SOURCE_FILE_SHA256 = 'c8aba5dee72d17996cd4fa8120dd7372884d74120ea038084c2826b925c77203'
SOURCE_FUNCTION_SHA256 = {'_integer_observations': 'a28830500e5af076d00300bb553d12b81a091d4ad4d11bef2664bb5e33945562', '_pearson_binary64': '6004e664a8278fa36b8c3e8250b912ae2e0e506fc970f0f803e1b8a1d43f84bd', '_stable_correlation': 'c2c21466c679b6a858354f31b0245f7c9276f7f245f1ec1a8fce6406fe29357d'}
FROZEN_FUNCTIONS_SHA256 = 'c88bba8f6a86f63d17567f111fc726a32bf4e65f2e4aadb23a04f81861afa098'


def _integer_observations(values):
    """Express binary64 inputs as integers sharing a power-of-two denominator.

    Each axis may use its own denominator: positive scale cancels from Pearson.
    Python integer arithmetic cannot overflow or lose low-order input bits.
    """
    ratios = [float(value).as_integer_ratio() for value in values]
    exponents = [denominator.bit_length() - 1 for _, denominator in ratios]
    shared = max(exponents)
    return [numerator << (shared - exponent)
            for (numerator, _), exponent in zip(ratios, exponents)]


def _pearson_binary64(left, right):
    """Compute exact moments, then a high-precision approximation of Pearson.

    The exactness claim covers represented observations and integer moments,
    not arbitrary real inputs or correctly rounded irrational square roots.
    Endpoint equality is algebraic; near correlation is never epsilon-snapped.
    """
    x, y = _integer_observations(left), _integer_observations(right)
    count = len(x)
    sx, sy = sum(x), sum(y)
    vx = count * sum(value * value for value in x) - sx * sx
    vy = count * sum(value * value for value in y) - sy * sy
    if vx == 0 or vy == 0:
        return np.nan
    covariance = count * sum(a * b for a, b in zip(x, y)) - sx * sy
    if covariance == 0:
        return 0.0
    product = vx * vy
    if covariance * covariance == product:
        return 1.0 if covariance > 0 else -1.0
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return float(Decimal(covariance) / Decimal(product).sqrt())


def _stable_correlation(left: pd.DataFrame, right: pd.DataFrame, window: int) -> pd.DataFrame:
    """Complete-window Pearson with explicit binary64 numerical semantics."""
    output = np.full(left.shape, np.nan, dtype=float)
    if window > len(left):
        return pd.DataFrame(output, index=left.index, columns=left.columns)
    for column in range(left.shape[1]):
        x = left.iloc[:, column].to_numpy(dtype=float, na_value=np.nan)
        y = right.iloc[:, column].to_numpy(dtype=float, na_value=np.nan)
        x_windows = np.lib.stride_tricks.sliding_window_view(x, window)
        y_windows = np.lib.stride_tricks.sliding_window_view(y, window)
        for row, (xw, yw) in enumerate(zip(x_windows, y_windows), start=window - 1):
            if not np.isfinite(xw).all() or not np.isfinite(yw).all():
                continue
            if np.all(xw == xw[0]) or np.all(yw == yw[0]):
                continue
            # Equality here is exact equality of the represented inputs, not
            # an approximate factor threshold. Missing/constant windows have
            # already been excluded.
            if np.array_equal(xw, yw):
                output[row, column] = 1.0
                continue
            if np.array_equal(xw, -yw):
                output[row, column] = -1.0
                continue
            xc, yc = xw - xw[0], yw - yw[0]
            if not np.isfinite(xc).all() or not np.isfinite(yc).all():
                output[row, column] = _pearson_binary64(xw, yw)
                continue
            # Translation removes a large common offset; division before
            # squaring prevents both large- and small-scale norm failures.
            xc, yc = xc / np.max(np.abs(xc)), yc / np.max(np.abs(yc))
            xc, yc = xc - xc.mean(), yc - yc.mean()
            value = float(np.dot(xc, yc) / np.sqrt(np.dot(xc, xc) * np.dot(yc, yc)))
            # This threshold ONLY selects the more exact algorithm. It never
            # rounds, ties or snaps a computed coefficient to 0 or +/-1.
            boundary = 32 * np.finfo(float).eps
            if not math.isfinite(value) or abs(value) <= boundary or abs(value) >= 1 - boundary:
                value = _pearson_binary64(xw, yw)
            output[row, column] = value
    return pd.DataFrame(output, index=left.index, columns=left.columns)
