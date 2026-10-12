import unittest

import numpy as np
import pandas as pd

from paper_alpha.expressions import (
    ExpressionError,
    OPERATOR_SEMANTICS_VERSION,
    compute_expression,
    repair_expression,
    validate_expression,
)


class ExpressionTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(21)
        index = pd.date_range("2021-01-01", periods=24)
        self.panels = {
            "close": pd.DataFrame(rng.normal(100, 3, (24, 4)), index=index, columns=list("ABCD")),
            "volume": pd.DataFrame(rng.uniform(10, 100, (24, 4)), index=index, columns=list("ABCD")),
        }

    def assert_rejected(self, expression, code="unsafe_expression"):
        with self.assertRaises(ExpressionError) as caught:
            validate_expression(expression, set(self.panels))
        self.assertEqual(caught.exception.code, code, expression)

    def test_metadata_is_canonical_and_deterministic(self):
        metadata = validate_expression(" -ts_corr(rank(close), rank(volume), 6) ", set(self.panels))
        self.assertEqual(metadata["used_fields"], ["close", "volume"])
        self.assertEqual(metadata["operators"], ["rank", "ts_corr"])
        self.assertEqual(metadata["canonical_expression"], "-ts_corr(rank(close), rank(volume), 6)")

    def test_unsafe_syntax_is_rejected(self):
        examples = [
            "close.__class__", "close[0]", "(lambda x: x)(close)",
            "rank(close, axis=1)", "[close]", "{'x': close}",
            "[x for x in close]", "close ** 2", "close and volume",
            "close @ volume", "f'{close}'", "close + True", "close + 'x'",
            "close + 1e309", "close + 1000000001", "0 < close < 100",
            "1 + 2", "~close", "close + " + "1+" * 1100 + "1",
            "-" * 40 + "close", "__import__('os').system('touch /tmp/expression-must-not-run')",
        ]
        for expression in examples:
            with self.subTest(expression=expression[:90]):
                self.assert_rejected(expression)

    def test_unknown_fields_and_operators(self):
        self.assert_rejected("rank(vwap)", "unknown_field")
        self.assert_rejected("correlation(close, volume, 6)", "unknown_operator")
        self.assert_rejected("print(close)", "unknown_operator")
        self.assert_rejected("rank(close, 2)", "invalid_arguments")
        self.assert_rejected("rank(2)", "invalid_arguments")

    def test_strict_window_bounds_prevent_future_access(self):
        for window in ("-1", "0", "2.5", "2.0", "253", "True", "1+1", "close"):
            with self.subTest(window=window):
                self.assert_rejected(f"ts_mean(close, {window})", "invalid_window")
        self.assert_rejected("delay(close, -1)", "invalid_window")
        pd.testing.assert_frame_equal(compute_expression("delay(close, 0)", self.panels), self.panels["close"])
        self.assert_rejected("ts_delta(close, 0)", "invalid_window")

    def test_strict_alignment_checks_even_unused_input(self):
        for altered in (
            self.panels["volume"].iloc[::-1],
            self.panels["volume"].iloc[1:],
            self.panels["volume"].loc[:, list("BACD")],
            self.panels["volume"].set_axis(self.panels["volume"].index + pd.Timedelta(days=1)),
        ):
            with self.subTest(shape=altered.shape):
                with self.assertRaises(ExpressionError) as caught:
                    compute_expression("close", {"close": self.panels["close"], "volume": altered})
                self.assertEqual(caught.exception.code, "panel_alignment")

    def test_duplicate_axes_and_infinite_data_rejected(self):
        duplicate = self.panels["close"].copy()
        duplicate.columns = list("AABC")
        with self.assertRaises(ExpressionError) as caught:
            compute_expression("close", {"close": duplicate})
        self.assertEqual(caught.exception.code, "panel_alignment")
        infinite = self.panels["close"].copy()
        infinite.iloc[0, 0] = np.inf
        with self.assertRaises(ExpressionError) as caught:
            compute_expression("close", {"close": infinite})
        self.assertEqual(caught.exception.code, "invalid_data")

    def test_future_perturbation_does_not_change_past(self):
        expression = "rank(ts_delta(log(close), 2)) - ts_corr(close, volume, 5) / ts_std(close, 4)"
        original = compute_expression(expression, self.panels)
        perturbed = {name: value.copy() for name, value in self.panels.items()}
        for value in perturbed.values():
            value.iloc[16:] = value.iloc[16:] * 40 + 5000
        recomputed = compute_expression(expression, perturbed)
        pd.testing.assert_frame_equal(original.iloc[:16], recomputed.iloc[:16])

    def test_constant_correlation_is_nan(self):
        panels = {"close": self.panels["close"] * 0 + 1e12, "volume": self.panels["volume"]}
        result = compute_expression("ts_corr(close, volume, 4)", panels)
        self.assertTrue(result.isna().all().all())
        self.assertEqual(result.attrs["expression_diagnostics"]["infinite_count"], 0)

    def test_correlation_stays_stable_with_large_offset(self):
        increments = np.array([0, 2, 1, 5, 3, 4, 9, 7], dtype=float)
        close = pd.DataFrame({"A": 1e12 + increments, "B": 1e12 + increments[::-1]})
        volume = 2 * close + 1e12
        result = compute_expression("ts_corr(close, volume, 4)", {"close": close, "volume": volume})
        np.testing.assert_allclose(result.iloc[3:].to_numpy(), 1.0, atol=1e-12)
        self.assertTrue(result.iloc[:3].isna().all().all())
        self.assertEqual(result.attrs["expression_diagnostics"]["operator_semantics_version"], OPERATOR_SEMANTICS_VERSION)

    def test_correlation_matches_independent_reference(self):
        result = compute_expression("ts_corr(close, volume, 5)", self.panels)
        for column in self.panels["close"].columns:
            for end in range(4, len(result)):
                expected = np.corrcoef(
                    self.panels["close"][column].iloc[end - 4:end + 1],
                    self.panels["volume"][column].iloc[end - 4:end + 1],
                )[0, 1]
                self.assertAlmostEqual(result.loc[result.index[end], column], expected, places=12)

    def test_complete_correlation_windows_and_singleton(self):
        panels = {name: value.copy() for name, value in self.panels.items()}
        panels["close"].iloc[6, 0] = np.nan
        result = compute_expression("ts_corr(close, volume, 4)", panels)
        self.assertTrue(result.iloc[6:10, 0].isna().all())
        self.assertTrue(np.isfinite(result.iloc[10, 0]))
        self.assertTrue(compute_expression("ts_corr(close, volume, 1)", panels).isna().all().all())

    def test_vendored_semantics_and_numeric_comparison(self):
        result = compute_expression("ts_mean(close, 3) + ts_delta(close, 2) - delay(close, 1)", self.panels)
        close = self.panels["close"]
        expected = close.rolling(3).mean() + close - close.shift(2) - close.shift(1)
        pd.testing.assert_frame_equal(result, expected)
        compared = compute_expression("close >= 100", self.panels)
        pd.testing.assert_frame_equal(compared, (close >= 100).astype(float))
        pd.testing.assert_frame_equal(compute_expression("ts_std(close, 3)", self.panels), close.rolling(3).std(ddof=1))

    def test_nonfinite_output_is_preserved_in_diagnostics(self):
        result = compute_expression("close / 0", self.panels)
        self.assertTrue(result.isna().all().all())
        self.assertEqual(result.attrs["expression_diagnostics"]["infinite_count"], result.size)
        self.assertEqual(result.attrs["expression_diagnostics"]["nonfinite_count"], result.size)
        scalar_division = compute_expression("close * (1 / 0)", self.panels)
        self.assertTrue(scalar_division.isna().all().all())

    def test_rank_sign_and_comparison_cannot_hide_intermediate_infinities(self):
        for expression in (
            "rank((close - volume) / (close - close))",
            "sign(close / 0)",
            "(close / 0) > 0",
            "rank(log(close - close))",
            "close * ((1 / 0) > 0)",
        ):
            with self.subTest(expression=expression):
                result = compute_expression(expression, self.panels)
                self.assertTrue(result.isna().all().all())
                diagnostics = result.attrs["expression_diagnostics"]
                self.assertGreater(diagnostics["intermediate_infinite_count"], 0)
                self.assertEqual(diagnostics["nonfinite_count"], result.size)

    def test_comparison_preserves_missing_history_on_either_side(self):
        for expression in (
            "(delay(close, 5) > 0) + rank(close)",
            "(0 < delay(close, 5)) + rank(close)",
            "delay(close, 5) != close",
            "delay(close, 5) == delay(volume, 3)",
        ):
            with self.subTest(expression=expression):
                result = compute_expression(expression, self.panels)
                self.assertTrue(result.iloc[:5].isna().all().all())
                self.assertTrue(np.isfinite(result.iloc[5:].to_numpy()).all())
                self.assertEqual(result.attrs["expression_diagnostics"]["nonfinite_count"], 5 * 4)

    def test_alias_repair_only_changes_operator_calls(self):
        error = ExpressionError("Unknown operator: correlation", "unknown_operator")
        repaired = repair_expression("correlation(vwap, delta(volume, 2), 6)", error)
        self.assertEqual(repaired, "ts_corr(vwap, ts_delta(volume, 2), 6)")
        self.assert_rejected(repaired, "unknown_field")
        repaired = repair_expression("delta(delta, 2)", error)
        self.assertEqual(repaired, "ts_delta(delta, 2)")
        self.assertIsNone(repair_expression("rank(vwap)", ExpressionError("vwap", "unknown_field")))
        self.assertIsNone(repair_expression("delta(close, 2.5)", error))
        self.assertIsNone(repair_expression("delay(close, -1)", ExpressionError("window", "invalid_window")))


if __name__ == "__main__":
    unittest.main()
