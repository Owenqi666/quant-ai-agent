"""Independent temporal reference checks; no saved production output as oracle."""
from copy import deepcopy
import csv
import math
from pathlib import Path
import shutil
import tempfile
import unittest

import pandas as pd

from paper_alpha.expressions import compute_expression
from paper_alpha.evaluation_suite import load_manifest, summarize
from paper_alpha.server.oracle_temporal import TEMPORAL_FORMULAS, temporal_factors
from paper_alpha.server.regression import _factors, oracle_support, verify_candidate
from paper_alpha.storage import atomic_json, read_json
from tests.test_regression_contract import make_fixture


class TemporalSuiteDeclarationTests(unittest.TestCase):
    def test_frozen_inputs_and_denominators_include_the_unsupported_executable_control(self):
        manifest = load_manifest(Path(__file__).resolve().parents[1] / "evaluation_suites/v06/manifest.json")
        summary = summarize(manifest, [])
        self.assertEqual(summary["planned_run_count"], 24)
        self.assertEqual(summary["unique_positive_formula_groups"], ["alpha101"])
        for mode in summary["by_mode"].values():
            self.assertEqual(mode["task_handling"]["denominator"], 8)
            self.assertEqual(mode["positive_execution"]["denominator"], 8)
            self.assertEqual(mode["numerical_coverage"]["denominator"], 8)
            self.assertEqual(mode["fully_verified_yield"]["denominator"], 8)
            self.assertEqual(mode["negative_handling"]["denominator"], 4)
            self.assertEqual(mode["numerical_coverage"]["numerator"], 0)
        self.assertTrue(all(task["human_fidelity"] == "unlabelled" for task in manifest["tasks"]))


class TemporalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.dates = [f"2024-01-{i + 1:02d}" for i in range(12)]
        self.assets = ["A", "B", "C"]
        # high-low+.001 is exactly 1, making each ratio hand-computable.
        self.rows = {(day, asset): {"open": 10., "close": 10. + index + offset,
                                   "high": .999, "low": 0., "volume": 100.}
                     for index, day in enumerate(self.dates)
                     for offset, asset in enumerate(self.assets)}

    def panels(self, rows):
        return {field: pd.DataFrame([[rows[day, asset][field] for asset in self.assets]
                                    for day in self.dates], index=self.dates, columns=self.assets)
                for field in ("open", "close", "high", "low", "volume")}

    def assert_panels_match(self, name, rows, last=11):
        reference = _factors(name, self.dates, self.assets, rows, last)
        production = compute_expression(TEMPORAL_FORMULAS[name], self.panels(rows))
        for (day, asset), expected in reference.items():
            actual = production.loc[day, asset]
            if expected is None:
                self.assertTrue(math.isnan(actual), (name, day, asset, actual))
            else:
                self.assertAlmostEqual(actual, expected, places=10)
        return reference

    def test_hand_calculated_mean_sample_std_delay_and_warmup(self):
        expected = {"alpha101_mean5_modified": 2.,
                    "alpha101_std5_modified": math.sqrt(2.5),
                    "alpha101_delay1_modified": 3.}
        for name, value in expected.items():
            with self.subTest(formula=name):
                result = self.assert_panels_match(name, self.rows)
                self.assertAlmostEqual(result[self.dates[4], "A"], value)
                warmup = 1 if "delay" in name else 4
                self.assertTrue(all(result[day, "A"] is None for day in self.dates[:warmup]))
        # Sample standard deviation of a constant signal is exactly zero.
        constant = deepcopy(self.rows)
        for item in constant.values():
            item["close"] = 11.
        self.assertEqual(self.assert_panels_match("alpha101_std5_modified", constant)[self.dates[4], "A"], 0.)

    def test_missing_input_propagates_then_recovers_at_exact_window_boundary(self):
        rows = deepcopy(self.rows)
        rows[self.dates[4], "B"]["close"] = float("nan")
        for name in TEMPORAL_FORMULAS:
            with self.subTest(formula=name):
                result = self.assert_panels_match(name, rows)
                missing_indices = [5] if "delay" in name else list(range(4, 9))
                for index in missing_indices:
                    self.assertIsNone(result[self.dates[index], "B"])
                recovered = 6 if "delay" in name else 9
                self.assertIsNotNone(result[self.dates[recovered], "B"])

    def test_prefix_and_future_perturbation_leave_past_reference_and_production_unchanged(self):
        changed = deepcopy(self.rows)
        for (day, _asset), row in changed.items():
            if day > self.dates[7]:
                row["close"] *= 10
                row["open"] *= 3
        for name, expression in TEMPORAL_FORMULAS.items():
            with self.subTest(formula=name):
                full = self.assert_panels_match(name, self.rows)
                prefix = temporal_factors(name, self.dates[:8], self.assets, self.rows, 7)
                self.assertEqual(prefix, {key: value for key, value in full.items() if key[0] <= self.dates[7]})
                self.assertEqual(prefix, _factors(name, self.dates, self.assets, changed, 7))
                pd.testing.assert_frame_equal(
                    compute_expression(expression, self.panels(self.rows)).iloc[:8],
                    compute_expression(expression, self.panels(changed)).iloc[:8])

    def test_only_explicit_formulas_registered_and_unsupported_changes_stay_unsupported(self):
        for name, expression in TEMPORAL_FORMULAS.items():
            self.assertEqual(oracle_support(expression)["formula"], name)
            altered = expression.replace(", 5)", ", 6)").replace(", 1)", ", 2)")
            self.assertFalse(oracle_support(altered)["supported"])
        with self.assertRaises(ValueError):
            temporal_factors("unregistered", self.dates, self.assets, self.rows, 11)


class TemporalArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.tasks = {name: make_fixture(cls.base / name, "alpha101", expression, temporal_signal=True)
                     for name, expression in TEMPORAL_FORMULAS.items()}
        cls.tasks["unsupported"] = make_fixture(
            cls.base / "unsupported", "alpha101", TEMPORAL_FORMULAS["alpha101_mean5_modified"].replace(", 5)", ", 6)"),
            temporal_signal=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_worker_factor_labels_weights_and_aggregate_match_all_three_references(self):
        for name in TEMPORAL_FORMULAS:
            with self.subTest(formula=name):
                result = verify_candidate(self.base / name, "alpha101", self.tasks[name])
                self.assertEqual(result["outcome"], "passed", result)
                self.assertEqual([item["name"] for item in result["checks"]],
                                 ["evidence_provenance", "factor_values", "daily_numerics", "aggregate_metrics"])

    def test_changed_window_executes_but_is_not_a_numerical_pass(self):
        result = verify_candidate(self.base / "unsupported", "alpha101", self.tasks["unsupported"])
        self.assertEqual(result["outcome"], "not_comparable", result)
        self.assertEqual(result["checks"][-1]["reason_code"], "oracle_unsupported_formula")

    def test_coherent_wrong_labels_weights_aggregate_and_lookahead_are_detected(self):
        mutations = {
            "label": lambda result: result["daily"][1]["assets"][0].update(forward_return=.789),
            "weight": lambda result: result["daily"][1]["assets"][0].update(weight=.789),
            "aggregate": lambda result: result["metrics"].update(mean_rank_ic=.789),
            "lookahead_entry": lambda result: result["daily"][1].update(entry_date=result["daily"][1]["signal_date"]),
        }
        for name in TEMPORAL_FORMULAS:
            for kind, mutation in mutations.items():
                with self.subTest(formula=name, corruption=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "run"
                    shutil.copytree(self.base / name, root)
                    state = read_json(root / "state.json")
                    result = state["candidates"][0]["result"]
                    mutation(result)
                    atomic_json(root / "state.json", state)
                    atomic_json(root / "candidates/alpha101/attempt-1/result.json", result)
                    checked = verify_candidate(root, "alpha101", self.tasks[name])
                    self.assertEqual(checked["outcome"], "failed", checked)
                    self.assertTrue(any(item["name"] in {"daily_numerics", "aggregate_metrics"}
                                        and item["outcome"] == "failed" for item in checked["checks"]))

    def test_future_shifted_factor_series_is_detected_for_every_new_formula(self):
        for name in TEMPORAL_FORMULAS:
            with self.subTest(formula=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "run"
                shutil.copytree(self.base / name, root)
                path = root / "candidates/alpha101/attempt-1/factor.csv"
                with path.open(newline="") as stream:
                    rows = list(csv.reader(stream))
                # Keep dates valid, but falsely use tomorrow's factor values.
                shifted = [rows[0]] + [[row[0], *following[1:]] for row, following in zip(rows[1:-1], rows[2:])] + [rows[-1]]
                with path.open("w", newline="") as stream:
                    csv.writer(stream).writerows(shifted)
                result = verify_candidate(root, "alpha101", self.tasks[name])
                self.assertEqual(result["outcome"], "failed", result)
                self.assertEqual(next(item for item in result["checks"] if item["name"] == "factor_values")["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
