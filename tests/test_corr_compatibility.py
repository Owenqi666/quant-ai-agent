"""Environment-local bit compatibility with the frozen pre-optimization operator.

The independent Fraction cases explain selected mathematical endpoints; they
never relax the byte comparison against the historical implementation.
"""
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import platform
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from paper_alpha import expressions
from paper_alpha.evaluation import AVAILABILITY, evaluate
from tests.corr_reference import _stable_correlation as reference_correlation
from tests import corr_reference


def fraction_pearson(left, right):
    """Exact moments of represented inputs, independently rounded selected cases."""
    x, y = [Fraction(float(v)) for v in left], [Fraction(float(v)) for v in right]
    n = len(x)
    sx, sy = sum(x), sum(y)
    vx = n * sum(v * v for v in x) - sx * sx
    vy = n * sum(v * v for v in y) - sy * sy
    covariance = n * sum(a * b for a, b in zip(x, y)) - sx * sy
    if not vx or not vy:
        return np.nan
    if not covariance:
        return 0.0
    if covariance * covariance == vx * vy:
        return 1.0 if covariance > 0 else -1.0
    with localcontext() as context:
        context.prec = 120
        convert = lambda v: Decimal(v.numerator) / Decimal(v.denominator)
        return float(convert(covariance) / (convert(vx) * convert(vy)).sqrt())


def panels(left, right):
    index = pd.bdate_range('2024-01-01', periods=len(left), name='session')
    columns = pd.Index([f'asset-{i}' for i in range(np.shape(left)[1])], name='asset')
    return (pd.DataFrame(left, index=index, columns=columns, copy=False),
            pd.DataFrame(right, index=index, columns=columns, copy=False))


def input_snapshot(frame):
    values = frame.to_numpy(dtype=float, na_value=np.nan)
    return {'index': frame.index.astype(str).tolist(), 'columns': frame.columns.astype(str).tolist(),
            'strides': list(values.strides), 'C_contiguous': bool(values.flags.c_contiguous),
            'F_contiguous': bool(values.flags.f_contiguous), 'dtypes': [str(dtype) for dtype in frame.dtypes],
            'values_hex': [[float(value).hex() for value in row] for row in values]}


class CorrelationCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix_checks = 0
        cls.differences = []

    @classmethod
    def tearDownClass(cls):
        destination = os.environ.get('CORR_COMPATIBILITY_REPORT')
        if destination:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            runtime = StringIO()
            with redirect_stdout(runtime):
                np.show_runtime()
            path.write_text(json.dumps({
                'schema_version': 1, 'matrix_checks': cls.matrix_checks,
                'differences': cls.differences,
                'nan_mask_difference_count': (None if any(item['nan_mask_difference_count'] is None
                    for item in cls.differences) else sum(item['nan_mask_difference_count'] for item in cls.differences)),
                'non_nan_bit_difference_count': (None if any(item['non_nan_bit_difference_count'] is None
                    for item in cls.differences) else sum(item['non_nan_bit_difference_count'] for item in cls.differences)),
                'environment': {'python': platform.python_version(), 'numpy': np.__version__,
                                'pandas': pd.__version__, 'platform': platform.platform(),
                                'numpy_build_config': np.__config__.show(mode='dicts'),
                                'numpy_runtime_observation': runtime.getvalue(),
                                'thread_environment': {name: os.environ.get(name) for name in
                                    ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                                     'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS')}},
                'reference': {'public_commit': corr_reference.SOURCE_PUBLIC_COMMIT,
                              'source_sha256': corr_reference.SOURCE_FILE_SHA256,
                              'functions_sha256': corr_reference.FROZEN_FUNCTIONS_SHA256,
                              'module_sha256': hashlib.sha256(Path(corr_reference.__file__).read_bytes()).hexdigest()},
                'production_source_sha256': hashlib.sha256(Path(expressions.__file__).read_bytes()).hexdigest(),
                'scope': 'Finite environment-local matrix; no cross-platform float gold or universal equivalence claim.'
            }, indent=2) + '\n')

    def identical(self, expected, actual, *, inputs=None, **case):
        type(self).matrix_checks += 1
        try:
            pd.testing.assert_index_equal(expected.index, actual.index, exact=True)
            pd.testing.assert_index_equal(expected.columns, actual.columns, exact=True)
            self.assertEqual(expected.shape, actual.shape)
            self.assertEqual(list(expected.dtypes), list(actual.dtypes))
            self.assertTrue(all(dtype == np.dtype('float64') for dtype in actual.dtypes))
            a, b = expected.to_numpy(), actual.to_numpy()
            missing = np.isnan(a)
            np.testing.assert_array_equal(missing, np.isnan(b))
            # Ordinary equality cannot distinguish +0.0 and -0.0.
            np.testing.assert_array_equal(a.view(np.uint64)[~missing], b.view(np.uint64)[~missing])
            self.assertEqual(expected.attrs, actual.attrs)
        except AssertionError as error:
            details = {'case': dict(case, shape=list(expected.shape)), 'error': str(error),
                       'nan_mask_difference_count': None, 'non_nan_bit_difference_count': None}
            if (expected.shape == actual.shape and expected.to_numpy().dtype == np.float64
                    and actual.to_numpy().dtype == np.float64):
                a, b = expected.to_numpy(dtype=float), actual.to_numpy(dtype=float)
                common = ~np.isnan(a) & ~np.isnan(b)
                details.update(nan_mask_difference_count=int(np.count_nonzero(np.isnan(a) != np.isnan(b))),
                               non_nan_bit_difference_count=int(np.count_nonzero(
                                   a.view(np.uint64)[common] != b.view(np.uint64)[common])))
            if inputs is not None:
                details['inputs'] = {name: input_snapshot(frame) for name, frame in inputs.items()}
            type(self).differences.append(details)
            raise

    def matrix(self, left, right, windows, **case):
        for window in windows:
            with np.errstate(all='ignore'):
                expected = reference_correlation(left, right, window)
                for chunk in (1, 7, max(1, len(left))):
                    details = dict(case, window=window, chunk_size=chunk)
                    with self.subTest(**details):
                        actual = expressions._stable_correlation(left, right, window, chunk_size=chunk)
                        self.identical(expected, actual, inputs={'left': left, 'right': right}, **details)

    def test_windows_labels_and_legal_dtypes(self):
        rng = np.random.default_rng(101)
        x, y = rng.integers(-12, 13, (31, 4)), rng.integers(-12, 13, (31, 4))
        for dtype in (np.float64, np.float32, np.int64):
            left, right = panels(x.astype(dtype), y.astype(dtype))
            self.matrix(left, right, (1, 2, 5, 31, 32), seed=101, dtype=str(dtype))
        left, right = panels(x[:, :1], y[:, :1])
        self.matrix(left, right, (2, 5), family='single-column')
        left, right = panels(x, y)
        left, right = left.astype('Int64'), right.astype('Float64')
        left.iloc[9, 1], right.iloc[13, 2] = pd.NA, pd.NA
        self.matrix(left, right, (2, 5), family='nullable-numeric')

    def test_layout_odd_windows_and_chunk_seams(self):
        rng = np.random.default_rng(202)
        x, y = rng.normal(size=(43, 4)), rng.normal(size=(43, 4))
        for layout in ('C', 'F', 'transposed', 'strided'):
            def arrange(value):
                if layout in {'C', 'F'}:
                    return np.array(value, order=layout)
                if layout == 'transposed':
                    return np.ascontiguousarray(value.T).T
                backing = np.zeros((value.shape[0] * 2, value.shape[1] * 2))
                backing[::2, ::2] = value
                return backing[::2, ::2]
            left, right = panels(arrange(x), arrange(y))
            # Odd windows catch the previously observed batched-row alignment drift.
            self.matrix(left, right, (3, 5, 7, 9, 17), seed=202, layout=layout)

    def test_missing_nonfinite_and_constant_complete_windows(self):
        rng = np.random.default_rng(303)
        x, y = rng.normal(size=(27, 5)), rng.normal(size=(27, 5))
        x[:, 0] = 1e12
        y[3:12, 1] = 4.0
        x[5, 2], y[6, 3], x[11, 4] = np.nan, np.inf, -np.inf
        left, right = panels(x, y)
        self.matrix(left, right, (1, 2, 5, 7, 28), seed=303, family='complete-windows')

    def test_random_missing_rates(self):
        rng = np.random.default_rng(606)
        x, y = rng.normal(size=(29, 4)), rng.normal(size=(29, 4))
        for rate in (0.0, 0.1, 0.4):
            a, b = x.copy(), y.copy()
            a[rng.random(a.shape) < rate] = np.nan
            b[rng.random(b.shape) < rate] = np.nan
            left, right = panels(a, b)
            self.matrix(left, right, (3, 5, 9), seed=606, missing_rate=rate)

    def test_maximum_legal_window(self):
        rng = np.random.default_rng(707)
        left, right = panels(rng.normal(size=(260, 2)), rng.normal(size=(260, 2)))
        self.matrix(left, right, (252,), seed=707, family='maximum-window')

    def test_offsets_scales_overflow_and_near_boundaries(self):
        z = np.array([0, 2, 1, 5, 3, 4, 9, 7], dtype=float)
        cases = [
            ('large-offset-affine', 1e12 + z, 3e12 + 2 * z, 4),
            ('opposite', z, -z, 5),
            ('overflow-centering', np.array([-1e308, 1e308, -1e308, 1e308]),
             np.array([-5e307, 5e307, -5e307, 5e307]), 4),
            ('tiny-scale', z * 1e-300, z[::-1] * 1e-300, 5),
            ('mixed-scale', z * 1e300, z[::-1] * 1e-300, 5),
            ('near-constant', 1 + z * np.spacing(1.0), 1 + z[::-1] * np.spacing(1.0), 5),
            ('near-zero', np.array([-1., -1., 1., 1.]),
             np.array([-1., 1., -1., 1. + 2**-46]), 4),
            ('near-positive-one', np.arange(4.), np.array([0., 1., 2., 3. + 2**-23]), 4),
            ('near-negative-one', np.arange(4.), -np.array([0., 1., 2., 3. + 2**-23]), 4),
        ]
        for name, x, y, window in cases:
            if name.startswith('near-') and name != 'near-constant':
                coefficient = fraction_pearson(x, y)
                self.assertGreater(abs(coefficient), 0.0, name)
                self.assertLess(abs(coefficient), 1.0, name)
                if name == 'near-zero':
                    self.assertLess(abs(coefficient), 1e-12, name)
                else:
                    self.assertGreater(abs(coefficient), 0.99, name)
            left, right = panels(x[:, None], y[:, None])
            self.matrix(left, right, (window,), family=name)

    def test_independent_fraction_endpoints_and_signed_zero(self):
        huge, tiny = 1e308, np.nextafter(0.0, 1.0)
        x = [huge, huge, -huge, -huge, tiny, -tiny]
        cases = [
            ('half', [-1, 0, 1], [-1, 1, 0]),
            ('negative-half', [-1, 0, 1], [1, -1, 0]),
            ('zero', [-1, -1, 1, 1], [-1, 1, -1, 1]),
            ('constant', [3, 3, 3], [-1, 0, 1]),
            ('positive-endpoint', [0, 2, 1, 5], [3, 7, 5, 13]),
            ('negative-endpoint', [0, 2, 1, 5], [3, -1, 1, -7]),
            ('positive-zero-underflow', x, [huge, -huge, huge, -huge, tiny, -tiny]),
            ('negative-zero-underflow', x, [huge, -huge, huge, -huge, -tiny, tiny]),
        ]
        for name, x, y in cases:
            left, right = panels(np.array(x, dtype=float)[:, None], np.array(y, dtype=float)[:, None])
            self.matrix(left, right, (len(x),), family=name)
            with np.errstate(all='ignore'):
                expected = reference_correlation(left, right, len(x))
            mathematical = fraction_pearson(x, y)
            with self.subTest(independent=name):
                if np.isnan(mathematical):
                    self.assertTrue(np.isnan(expected.iloc[-1, 0]))
                else:
                    self.assertEqual(np.float64(expected.iloc[-1, 0]).view(np.uint64),
                                     np.float64(mathematical).view(np.uint64))
                if name.endswith('zero-underflow'):
                    self.assertEqual(mathematical, 0.0)
                    self.assertEqual(bool(np.signbit(mathematical)), name.startswith('negative'))

    def test_future_changes_and_truncated_prefix(self):
        rng = np.random.default_rng(404)
        left, right = panels(rng.normal(size=(49, 4)), rng.normal(size=(49, 4)))
        changed_left, changed_right = left.copy(), right.copy()
        changed_left.iloc[25:] = changed_left.iloc[25:] * 50 + 1000
        changed_right.iloc[25:] = -changed_right.iloc[25:] * 70
        for window in (5, 9, 17):
            for chunk in (1, 7, 49):
                with self.subTest(window=window, chunk_size=chunk), np.errstate(all='ignore'):
                    original = expressions._stable_correlation(left, right, window, chunk_size=chunk)
                    changed = expressions._stable_correlation(changed_left, changed_right, window, chunk_size=chunk)
                    prefix = expressions._stable_correlation(left.iloc[:25], right.iloc[:25], window, chunk_size=chunk)
                    self.identical(original.iloc[:25], changed.iloc[:25], family='future', seed=404,
                                   window=window, chunk=chunk, inputs={'left': left, 'right': right,
                                       'changed_left': changed_left, 'changed_right': changed_right})
                    self.identical(original.iloc[:25], prefix, family='prefix', seed=404,
                                   window=window, chunk=chunk, inputs={'left': left, 'right': right})

    def test_public_validation_still_rejects_invalid_inputs(self):
        left, right = panels(np.arange(24.).reshape(6, 4), np.arange(24.).reshape(6, 4) + 1)
        for window in ('0', '-1', '2.0', '253', 'True'):
            with self.subTest(window=window), self.assertRaises(expressions.ExpressionError) as caught:
                expressions.compute_expression(f'ts_corr(close, volume, {window})', {'close': left, 'volume': right})
            self.assertEqual(caught.exception.code, 'invalid_window')
        bad = right.copy()
        bad.iloc[0, 0] = np.inf
        for right_panel, code in ((right.iloc[:, ::-1], 'panel_alignment'),
                                  (right.iloc[::-1], 'panel_alignment'),
                                  (right.astype(bool), 'invalid_data'), (bad, 'invalid_data')):
            with self.subTest(code=code), self.assertRaises(expressions.ExpressionError) as caught:
                expressions.compute_expression('ts_corr(close, volume, 3)', {'close': left, 'volume': right_panel})
            self.assertEqual(caught.exception.code, code)

    def test_rank_ties_combinations_and_evaluation(self):
        rng = np.random.default_rng(505)
        x, y = 100 + rng.normal(size=(41, 5)), 1000 + rng.normal(size=(41, 5))
        x[:, 1:3], y[:, 1] = x[:, :1], y[:, 0]
        y[:, 2] = y[:, 0]
        y[12, 2] += 2**-20
        left, right = panels(x, y)
        market = {'close': left, 'volume': right, 'open': left + 2,
                  'high': left + 4, 'low': left - 2, 'returns': left.pct_change(fill_method=None)}
        metadata = {'data_kind': 'synthetic', 'calendar_dates': left.index.strftime('%Y-%m-%d').tolist(),
                    'universe': list(left.columns)}
        for field, panel in market.items():
            panel.attrs.update(market_metadata=metadata,
                               field_availability=AVAILABILITY.get(field, 'after session close'))
        dates = metadata['calendar_dates']
        config = {'min_assets': 3, 'splits': {
            'train': {'start': dates[0], 'end': dates[9]},
            'validation': {'start': dates[10], 'end': dates[30]},
            'test': {'start': dates[31], 'end': dates[-1]}}}
        raw = reference_correlation(left, right, 5)
        np.testing.assert_array_equal(raw.iloc[4:, 0].to_numpy().view(np.uint64),
                                      raw.iloc[4:, 1].to_numpy().view(np.uint64))
        near = np.abs(raw.iloc[:, 0].to_numpy() - raw.iloc[:, 2].to_numpy())
        self.assertTrue(np.any((near > 0) & (near < 1e-5)), 'Fixture must contain distinct near ties')
        optimized = expressions._stable_correlation
        for expression in ('rank(ts_corr(close, volume, 5))',
                           '-ts_corr(rank(close), rank(volume), 5)',
                           'rank(ts_corr(close, volume, 7)) + ts_delta(close, 2) / ts_std(close, 4)',
                           'ts_corr(close, volume, 5) > 0'):
            with patch.object(expressions, '_stable_correlation', reference_correlation):
                expected = expressions.compute_expression(expression, market)
            for chunk in (1, 7, 41):
                with self.subTest(expression=expression, chunk_size=chunk):
                    with patch.object(expressions, '_stable_correlation',
                                      lambda a, b, w: optimized(a, b, w, chunk_size=chunk)):
                        actual = expressions.compute_expression(expression, market)
                    self.identical(expected, actual, expression=expression, seed=505,
                                   chunk_size=chunk, inputs=market)
                    self.assertEqual(json.dumps(evaluate(expected, market, config), sort_keys=True, allow_nan=False),
                                     json.dumps(evaluate(actual, market, config), sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    unittest.main()
