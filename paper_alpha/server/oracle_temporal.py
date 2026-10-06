"""Three explicit Alpha101 transformations, independently implemented in stdlib.

This is intentionally not an expression interpreter. The exact AST allowlist is
owned by regression.py. Missing intermediate inputs propagate through complete
trailing windows; sample deviation uses n-1. No production operator is imported.
"""
from __future__ import annotations

import math
import statistics


ALPHA101 = "((close - open) / ((high - low) + .001))"
TEMPORAL_FORMULAS = {
    "alpha101_mean5_modified": f"ts_mean({ALPHA101}, 5)",
    "alpha101_std5_modified": f"ts_std({ALPHA101}, 5)",
    "alpha101_delay1_modified": f"delay({ALPHA101}, 1)",
}


def _signal(row):
    values = [row[field] for field in ("open", "close", "high", "low")]
    if any(value is None or not math.isfinite(value) for value in values):
        return None
    opening, close, high, low = values
    denominator = high - low + .001
    if denominator == 0:
        return None
    value = (close - opening) / denominator
    return value if math.isfinite(value) else None


def temporal_factors(formula, dates, assets, rows, last):
    """Return only the requested prefix; never inspect later market rows."""
    if formula not in TEMPORAL_FORMULAS:
        raise ValueError("Unregistered temporal reference formula")
    result = {}
    for asset in assets:
        history = []
        for index, day in enumerate(dates[:last + 1]):
            history.append(_signal(rows[day, asset]))
            value = None
            if formula == "alpha101_delay1_modified":
                if index >= 1:
                    value = history[index - 1]
            elif index >= 4:
                window = history[index - 4:index + 1]
                if all(item is not None for item in window):
                    value = (statistics.mean(window) if formula == "alpha101_mean5_modified"
                             else statistics.stdev(window))
            if value is not None and not math.isfinite(value):
                raise ValueError("Independent temporal arithmetic produced a nonfinite value")
            result[day, asset] = value
    return result
