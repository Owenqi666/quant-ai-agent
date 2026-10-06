"""Numerical contracts at the expression -> rank -> evaluation boundary.

Expected endpoint/orthogonality facts are algebraic. Nontrivial reference values
come from the separate standard-library oracle, not saved production output.
"""
from copy import deepcopy
from decimal import localcontext, ROUND_DOWN
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from paper_alpha.evaluation import evaluate, load_market
from paper_alpha.expressions import OPERATOR_SEMANTICS_VERSION, compute_expression
from paper_alpha.server.regression import (
    ORACLE_OPERATOR_SEMANTICS, OracleUnsupported, _reference_correlation, compare_contract,
    freeze_contract, oracle_support, recorded_semantics, verify_candidate,
)
from paper_alpha.storage import atomic_json, read_json
from tests.test_regression_contract import make_fixture


ROOT = Path(__file__).resolve().parents[1]
OLD_SEMANTICS = "local-panel-v2-centered-corr-missing-propagation"


class CorrelationNumericsTests(unittest.TestCase):
    def panels(self, x, y=None):
        y = x if y is None else y
        index = pd.date_range("2024-01-01", periods=len(x))
        return {"close": pd.DataFrame({"A": x}, index=index),
                "volume": pd.DataFrame({"A": y}, index=index)}

    def last(self, x, y=None):
        panels = self.panels(x, y)
        return compute_expression(f"ts_corr(close,volume,{len(x)})", panels).iloc[-1, 0]

    def test_self_and_exact_affine_correlations_are_exact_endpoints(self):
        x = [0., 2., 1., 5., 3., 4., 9., 7.]
        for offset in (0., 1e12):
            source = [value + offset for value in x]
            for multiplier in (1., 2., -3.):
                target = [multiplier * value + offset for value in source]
                with self.subTest(offset=offset, multiplier=multiplier):
                    self.assertEqual(self.last(source, target), 1. if multiplier > 0 else -1.)

    def test_large_small_and_subnormal_scaling_do_not_change_self_correlation(self):
        for scale in (1., 1e-200, 1e200, float.fromhex("0x0.0000000000001p-1022")):
            x = [value * scale for value in (1., 2., 3., 4., 5.)]
            with self.subTest(scale=scale):
                self.assertEqual(self.last(x), 1.)
                self.assertEqual(self.last(x, [-value for value in x]), -1.)

    def test_legal_expression_scaling_on_regular_inputs(self):
        panels = self.panels([1., 2., 3., 4., 5.])
        for operand in ("close", "close/1e-200", "close*1e-200"):
            result = compute_expression(f"ts_corr({operand},{operand},3)", panels)
            self.assertTrue(result.iloc[:2].isna().all().all())
            self.assertTrue((result.iloc[2:] == 1.).all().all())

    def test_opposite_signed_extremes_do_not_overflow_centering(self):
        maximum = np.finfo(float).max
        x = [-maximum, -maximum / 2, 0., maximum / 2, maximum]
        # Offset y rules out the direct exact-negation shortcut.
        y = [5., 4., 3., 2., 1.]
        self.assertEqual(self.last(x, y), -1.)

    def test_independent_reference_and_scale_invariance_away_from_endpoints(self):
        x, y = [1., 2., 3., 5., 4.], [5., 2., 4., 1., 3.]
        baseline = _reference_correlation(x, y)
        for scale_x in (1e-200, 1., 1e200):
            for scale_y in (1e-200, 1., 1e200):
                left, right = [value * scale_x for value in x], [value * scale_y for value in y]
                with self.subTest(scale_x=scale_x, scale_y=scale_y):
                    self.assertAlmostEqual(self.last(left, right), baseline, places=14)
                    self.assertAlmostEqual(self.last(left, right), _reference_correlation(left, right), places=14)

    def test_hand_computable_nontrivial_and_zero_correlation(self):
        self.assertAlmostEqual(self.last([-1., 0., 1.], [1., 2., 4.]), math.sqrt(27 / 28), places=14)
        self.assertEqual(self.last([-1., 0., 1.], [1., -2., 1.]), 0.)

    def test_near_zero_true_correlation_is_not_thresholded_to_zero(self):
        x, y = [-1., 0., 1.], [1., -2., np.nextafter(1., 2.)]
        actual, expected = self.last(x, y), _reference_correlation(x, y)
        self.assertGreater(actual, 0.)
        self.assertLess(actual, np.finfo(float).eps)
        self.assertEqual(actual, expected)

    def test_near_perfect_true_differences_retain_rank_order(self):
        x = np.array([0., 1., 2., 3., 4.])
        left = pd.DataFrame({"A": x, "B": x, "C": x})
        right = left.copy()
        right.loc[4, "A"] += 1e-6
        right.loc[4, "B"] += 2e-6
        panels = {"close": left, "volume": right}
        result = compute_expression("ts_corr(close,volume,5)", panels)
        last = result.iloc[-1]
        self.assertLess(last["B"], last["A"])
        self.assertLess(last["A"], last["C"])
        self.assertEqual(last["C"], 1.)
        for asset in left:
            self.assertAlmostEqual(last[asset], _reference_correlation(left[asset].tolist(), right[asset].tolist()), places=15)
        ranks = compute_expression("rank(ts_corr(close,volume,5))", panels).iloc[-1]
        self.assertEqual(ranks.to_dict(), {"A": 2 / 3, "B": 1 / 3, "C": 1.})

    def test_constant_singleton_and_missing_windows_stay_undefined(self):
        self.assertTrue(math.isnan(self.last([1., 1., 1.], [1., 2., 3.])))
        panels = self.panels([1., 2., np.nan, 4., 5., 6.])
        result = compute_expression("ts_corr(close,close,3)", panels)
        self.assertTrue(result.iloc[:5].isna().all().all())
        self.assertEqual(result.iloc[5, 0], 1.)
        self.assertTrue(compute_expression("ts_corr(close,close,1)", panels).isna().all().all())

    def test_reference_does_not_call_production_correlation(self):
        with patch("paper_alpha.expressions._stable_correlation", side_effect=AssertionError("production called")), \
                patch("paper_alpha.expressions._pearson_binary64", side_effect=AssertionError("production called")):
            self.assertEqual(_reference_correlation([1., 2., 3.], [2., 4., 6.]), 1.)
            self.assertIsNone(_reference_correlation([1., 1.], [2., 3.]))

    def test_decimal_global_context_does_not_change_exact_computation(self):
        x, y = [-1., 0., 1.], [1., -2., np.nextafter(1., 2.)]
        baseline = self.last(x, y)
        with localcontext() as context:
            context.prec, context.rounding, context.Emax, context.Emin = 3, ROUND_DOWN, 5, -5
            self.assertEqual(self.last(x, y), baseline)
            self.assertEqual(_reference_correlation(x, y), baseline)

    def test_reference_window_bound_is_explicit(self):
        with self.assertRaises(OracleUnsupported) as caught:
            _reference_correlation([1.] * 253, [2.] * 253)
        self.assertEqual(caught.exception.code, "oracle_resource_bound")


class NumericalPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panels = load_market(ROOT / "examples/alpha101/market.csv", ROOT / "examples/alpha101/metadata.json")
        cls.config = read_json(ROOT / "examples/alpha101/evaluation.json")

    def test_fixture_self_correlation_cannot_create_a_rank_portfolio(self):
        for expression in ("ts_corr(close,close,3)", "rank(ts_corr(close,close,3))",
                           "ts_corr(close/1e-200,close/1e-200,3)",
                           "ts_corr(close*1e-200,close*1e-200,3)"):
            with self.subTest(expression=expression):
                factor = compute_expression(expression, self.panels)
                result = evaluate(factor, self.panels, self.config)
                self.assertEqual(result["status"], "not_evaluable")
                self.assertEqual(result["metrics"]["evaluated_days"], 0)
                self.assertIsNone(result["metrics"]["mean_gross_return"])
                self.assertTrue(all(row["reason"] == "constant_factor" for row in result["daily"][:-2]))
                self.assertTrue(all(not row["assets"] for row in result["daily"]))

    def test_tiny_genuine_factor_differences_still_create_identical_rank_weights(self):
        expression = "(close-open)/(high-low+0.001)"
        baseline = evaluate(compute_expression(expression, self.panels), self.panels, self.config)
        tiny = evaluate(compute_expression(f"({expression})*1e-200", self.panels), self.panels, self.config)
        self.assertEqual(tiny["metrics"], baseline["metrics"])
        self.assertGreater(tiny["metrics"]["evaluated_days"], 0)
        for first, second in zip(baseline["daily"], tiny["daily"]):
            self.assertEqual([a["weight"] for a in first["assets"]], [a["weight"] for a in second["assets"]])

    def test_future_and_prefix_invariance_at_new_numerical_boundaries(self):
        expression = "ts_corr(close/1e-200,volume*1e-200,10)"
        cutoff = 260
        full = compute_expression(expression, self.panels)
        prefix = compute_expression(expression, {k: v.iloc[:cutoff].copy() for k, v in self.panels.items()})
        changed = {k: v.copy() for k, v in self.panels.items()}
        for panel in changed.values():
            panel.iloc[cutoff:] *= 7
        future = compute_expression(expression, changed)
        pd.testing.assert_frame_equal(full.iloc[:cutoff], prefix)
        pd.testing.assert_frame_equal(full.iloc[:cutoff], future.iloc[:cutoff])

    def test_current_oracle_version_is_explicit_and_old_contracts_differ(self):
        self.assertEqual(OPERATOR_SEMANTICS_VERSION, ORACLE_OPERATOR_SEMANTICS)
        self.assertNotEqual(OPERATOR_SEMANTICS_VERSION, OLD_SEMANTICS)
        self.assertEqual(oracle_support("-ts_corr(open,volume,10)")["operator_semantics"], OPERATOR_SEMANTICS_VERSION)
        task = read_json(ROOT / "examples/alpha101/task.json")
        metadata = read_json(ROOT / "examples/alpha101/metadata.json")
        current = freeze_contract(task, "alpha006", "paper", "market", metadata)
        old = deepcopy(current)
        old["operator_semantics"] = OLD_SEMANTICS
        self.assertEqual(compare_contract(old, current), ["operator_semantics"])

    def test_historical_v2_is_readable_but_not_given_a_v3_numerical_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = make_fixture(root, "alpha006")
            self.assertEqual(verify_candidate(root, "alpha006", task)["outcome"], "passed")
            snapshot = root / "source/paper_alpha/expressions.py"
            snapshot.write_text(f"OPERATOR_SEMANTICS_VERSION = {OLD_SEMANTICS!r}\n")
            self.assertEqual(recorded_semantics(root)["operator_semantics"], OLD_SEMANTICS)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            checked = verify_candidate(root, "alpha006", task)
            self.assertEqual(checked["outcome"], "not_comparable")
            self.assertEqual(checked["checks"][-1]["reason_code"], "oracle_unsupported_semantics")
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_result_cannot_claim_another_operator_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = make_fixture(root, "alpha006")
            state = read_json(root / "state.json")
            result = state["candidates"][0]["result"]
            result["expression_diagnostics"]["operator_semantics_version"] = OLD_SEMANTICS
            atomic_json(root / "state.json", state)
            path = root / "candidates/alpha006/attempt-1/result.json"
            atomic_json(path, result)
            checked = verify_candidate(root, "alpha006", task)
            self.assertEqual(checked["outcome"], "failed")
            self.assertIn("operator semantics", checked["checks"][-1]["reason"])


if __name__ == "__main__":
    unittest.main()
