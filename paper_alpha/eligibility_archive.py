"""Label-free, all-original-row screening of the two pinned author archives."""
from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import author_archive, eligibility
from .storage import digest

SEMANTICS_VERSION = 'gjs-author-eligibility-v1'
ROW_BLOCK = 512


def _representable(values):
    # A zero gross factor must be handled before large positive factors.
    if np.any(values == -1):
        return True
    try:
        exponent = math.fsum(math.log1p(float(item)) for item in values)
        # Near the float maximum, log rounding can turn a true overflow into a
        # finite result. Exact arithmetic is bounded to these eleven factors.
        if exponent > 700:
            product = Fraction(1)
            for item in values:
                product *= 1 + Fraction.from_float(float(item))
            return math.isfinite(float(product - 1))
        value = math.expm1(exponent)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(value)


def scan(path, plan):
    """Verify once, then read each field once per bounded original-row block.

    Return reads stop at the last target's H-2; DGW/MV stop at H-1. Earlier
    target returns can legitimately form part of a later target's history.
    """
    plan = eligibility.validate_plan(plan)
    if Path(path).name != plan['source_file']:
        raise ValueError('Eligibility source filename differs from the frozen plan')
    with author_archive._verified_archive(path) as (archive, source, _, metadata):
        axis = metadata['months']
        first, last = axis.index(plan['development_start']), axis.index(plan['development_end'])
        count = last - first + 1
        months = [{'month': month, 'patterns': [0] * 8, 'momentum_missing': 0,
                   'momentum_invalid': 0, 'momentum_unrepresentable': 0}
                  for month in axis[first:last + 1]]
        chunks = archive['Return'].chunks
        chunk_width = chunks[1] if chunks else 1
        width = max(1, ROW_BLOCK // chunk_width) * chunk_width if chunk_width <= ROW_BLOCK else ROW_BLOCK
        for start in range(0, source['assets'], width):
            stop = min(start + width, source['assets'])
            # There is no target-return label slice anywhere in this scanner.
            returns = np.asarray(archive['Return'][first - 12:last - 1, start:stop], dtype=np.float64)
            dgw = np.asarray(archive['DGW'][first - 1:last, start:stop], dtype=np.float64)
            mv = np.asarray(archive['MV'][first - 1:last, start:stop], dtype=np.float64)
            if returns.shape != (count + 10, stop - start) or dgw.shape != (count, stop - start) or mv.shape != dgw.shape:
                raise ValueError('Eligibility source slice dimensions changed')
            for offset, record in enumerate(months):
                history = returns[offset:offset + 11]
                missing = np.any(~np.isfinite(history), axis=0)
                invalid = ~missing & np.any(history < -1, axis=0)
                candidates = ~(missing | invalid)
                mom = np.zeros(stop - start, dtype=bool)
                for position in np.flatnonzero(candidates):
                    mom[position] = _representable(history[:, position])
                usable_dgw = np.isfinite(dgw[offset]) & (np.abs(dgw[offset]) <= 1)
                usable_mv = np.isfinite(mv[offset]) & (mv[offset] > 0)
                pattern = mom.astype(np.uint8) + 2 * usable_dgw.astype(np.uint8) + 4 * usable_mv.astype(np.uint8)
                observed = np.bincount(pattern, minlength=8)
                record['patterns'] = [old + int(new) for old, new in zip(record['patterns'], observed)]
                record['momentum_missing'] += int(np.count_nonzero(missing))
                record['momentum_invalid'] += int(np.count_nonzero(invalid))
                record['momentum_unrepresentable'] += int(np.count_nonzero(candidates & ~mom))
        result = eligibility.validate_scan({'schema_version': 1, 'kind': 'author_eligibility_scan',
                    'semantics_version': SEMANTICS_VERSION, 'source': source, 'plan': plan,
                    'plan_digest': digest(plan), 'months': months})
    return result
