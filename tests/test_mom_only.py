from copy import deepcopy
from fractions import Fraction
import ast
import math
from pathlib import Path
import unittest

from paper_alpha import mom_only as core, mom_only_reference as oracle


def panel():
    """Synthetic unit-test values only, carrying the required parser contract."""
    months = [f"{year}-{month:02d}" for year in range(2009, 2012) for month in range(1, 13)]
    return {
        "schema_version": 1, "data_kind": "market_derived_portfolio_returns",
        "source_id": core.SOURCE_ID, "asset_kind": "industry_portfolio",
        "return_semantics": "monthly_total_return_decimal", "months": months,
        "assets": list(core.ASSETS),
        "returns": [{"month": month, "asset": asset,
                     "value": .001 * (number - 24) + .0001 * (position % 3)}
                    for position, month in enumerate(months) for number, asset in enumerate(core.ASSETS)],
        "source": {"archive_sha256": "a" * 64, "csv_sha256": "b" * 64,
                   "download_url": core.SOURCE_URL, "section": core.SOURCE_SECTION,
                   "source_contract": core.SOURCE_CONTRACT,
                   "header_preamble": ["SYNTHETIC UNIT-TEST PANEL: not downloaded market data."]},
    }


def set_return(value, month, asset, number):
    for row in value["returns"]:
        if row["month"] == month and row["asset"] == asset:
            row["value"] = number
            return
    raise AssertionError("fixture cell not found")


def formation(value):
    return {key: value[key] for key in ("formation_months", "skipped_month", "as_of", "eligible_assets", "exclusions", "signals")}


class MomOnlyTests(unittest.TestCase):
    def checked(self, value, config=None):
        config = config or core.default_config()
        result = core.evaluate(value, config)
        check = oracle.check(value, config, result)
        self.assertTrue(check["supported"], check["issues"])
        self.assertTrue(check["passed"], check["issues"])
        return result

    def test_exact_window_hand_signal_and_portfolio_weights(self):
        value = panel()
        result = self.checked(value)
        month = result["months"][0]
        self.assertEqual(month["month"], "2010-01")
        self.assertEqual(month["formation_months"], [f"2009-{m:02d}" for m in range(1, 12)])
        self.assertEqual(month["skipped_month"], "2009-12")
        self.assertEqual(month["as_of"], "2009-12-31")
        sample = next(row for row in month["signals"] if row["asset"] == "Agric")
        product = Fraction(1)
        for row in value["returns"]:
            if row["asset"] == "Agric" and row["month"] in month["formation_months"]:
                product *= 1 + Fraction.from_float(row["value"])
        self.assertEqual(sample["momentum"], float(product - 1))
        candidate, baseline = month["strategies"]
        self.assertAlmostEqual(candidate["gross_exposure"], 1)
        self.assertAlmostEqual(candidate["net_exposure"], 0)
        self.assertAlmostEqual(baseline["gross_exposure"], 1)
        self.assertAlmostEqual(baseline["net_exposure"], 1)
        self.assertEqual(len(candidate["weights"]), 32)
        self.assertEqual(len(baseline["weights"]), 49)
        self.assertEqual(result["paired_comparison"]["months_evaluated"], 24)
        self.assertIsNone(result["human_judgment"])
        self.assertFalse(result["reserved_evaluated"])

    def test_current_skipped_and_future_labels_cannot_change_formation(self):
        original = panel()
        before = self.checked(original)["months"][0]
        for date in ("2009-12", "2010-01", "2011-12"):
            changed = deepcopy(original)
            for row in changed["returns"]:
                if row["month"] == date:
                    row["value"] = .8
            after = self.checked(changed)["months"][0]
            with self.subTest(date=date):
                self.assertEqual(formation(before), formation(after))
                for old, new in zip(before["strategies"], after["strategies"]):
                    self.assertEqual(old["weights"], new["weights"])
                if date != "2010-01":
                    self.assertEqual(before, after)
                else:
                    self.assertNotEqual(before["labels"], after["labels"])

    def test_missing_and_null_history_have_distinct_reasons(self):
        value = panel()
        value["returns"] = [row for row in value["returns"] if not (row["month"] == "2009-04" and row["asset"] == "Agric")]
        set_return(value, "2009-05", "Food", None)
        result = self.checked(value)["months"][0]
        excluded = {row["asset"]: row for row in result["exclusions"]}
        self.assertEqual(excluded["Agric"]["reasons"], ["formation_missing_row"])
        self.assertEqual(excluded["Food"]["reasons"], ["formation_null_value"])
        self.assertEqual(excluded["Agric"]["observed_months"], 10)
        self.assertEqual(len(result["eligible_assets"]), 47)
        self.assertEqual(len(result["strategies"][1]["weights"]), 47)

    def test_missing_whole_source_month_is_not_a_compressed_window(self):
        value = panel()
        value["returns"] = [row for row in value["returns"] if row["month"] != "2009-07"]
        first = self.checked(value)["months"][0]
        self.assertEqual(len(first["formation_months"]), 11)
        self.assertEqual(first["eligible_assets"], [])
        self.assertEqual(len(first["exclusions"]), 49)
        self.assertTrue(all(row["missing_months"] == ["2009-07"] for row in first["exclusions"]))
        self.assertTrue(all(strategy["gross_return"] is None for strategy in first["strategies"]))

    def test_holding_label_does_not_reweight_and_breaks_cumulative_path(self):
        original = panel()
        before = self.checked(original)["months"][0]
        held = before["strategies"][0]["weights"][0]["asset"]
        changed = deepcopy(original)
        set_return(changed, "2010-01", held, None)
        result = self.checked(changed)
        after = result["months"][0]
        self.assertEqual(formation(before), formation(after))
        for old, new in zip(before["strategies"], after["strategies"]):
            self.assertEqual(old["weights"], new["weights"])
            self.assertIsNone(new["gross_return"])
        self.assertTrue(all(strategy["gross_return_index"] is None for month in result["months"] for strategy in month["strategies"]))
        self.assertEqual(result["paired_comparison"]["months_evaluated"], 23)
        self.assertTrue(all(not row["cumulative_complete"] for row in result["summary"]))

    def test_middle_group_holding_gap_preserves_candidate_and_pair_denominator(self):
        original = panel()
        before = self.checked(original)["months"][0]
        middle = next(row["asset"] for row in before["signals"] if row["mom_group"] == 2)
        changed = deepcopy(original)
        changed["returns"] = [row for row in changed["returns"] if not (row["month"] == "2010-01" and row["asset"] == middle)]
        result = self.checked(changed)
        after = result["months"][0]
        self.assertEqual(formation(before), formation(after))
        self.assertEqual(before["strategies"][0], after["strategies"][0])
        self.assertIsNone(after["strategies"][1]["gross_return"])
        self.assertIsNone(after["difference_gross_return"])
        label = next(row for row in after["labels"] if row["asset"] == middle)
        self.assertEqual(label["reasons"], ["holding_missing_row"])
        self.assertEqual(result["summary"][0]["months_evaluated"], 24)
        self.assertEqual(result["summary"][1]["months_evaluated"], 23)

    def test_ties_never_split_by_industry_name_and_all_ties_keep_baseline(self):
        value = panel()
        for row in value["returns"]:
            row["value"] = 0.
        result = self.checked(value)
        first = result["months"][0]
        self.assertEqual({row["mom_group"] for row in first["signals"]}, {2})
        self.assertEqual(first["strategies"][0]["weights"], [])
        self.assertEqual(first["strategies"][0]["reasons"][0], "empty_required_group:mom=3,id=None")
        self.assertEqual(first["strategies"][1]["gross_return"], 0.)
        self.assertEqual(result["paired_comparison"]["months_evaluated"], 0)
        self.assertEqual(result["summary"][1]["annualized_sample_volatility"], 0.)
        self.assertIsNone(result["summary"][1]["annualized_mean_over_volatility_zero_rf"])

    def test_new_sample_gate_does_not_relax_to_satisfy_execution(self):
        value = panel()
        for asset in core.ASSETS[:20]:
            set_return(value, "2009-07", asset, None)
        first = self.checked(value)["months"][0]
        self.assertEqual(len(first["eligible_assets"]), 29)
        self.assertTrue(all(strategy["weights"] == [] for strategy in first["strategies"]))
        self.assertTrue(all("insufficient_eligible_assets" in strategy["reasons"] for strategy in first["strategies"]))

    def test_complete_loss_valid_signal_and_extreme_compound_diagnostic(self):
        value = panel()
        set_return(value, "2009-07", "Agric", -1.)
        first = self.checked(value)["months"][0]
        self.assertEqual(next(row["momentum"] for row in first["signals"] if row["asset"] == "Agric"), -1.)
        changed = panel()
        for month in [f"2009-{number:02d}" for number in range(1, 12)]:
            set_return(changed, month, "Agric", 1e308)
        first = self.checked(changed)["months"][0]
        self.assertEqual(next(row["reasons"] for row in first["exclusions"] if row["asset"] == "Agric"), ["nonfinite_formation_signal"])

    def test_tiny_returns_do_not_collapse_all_formation_signals_to_zero(self):
        value = panel()
        for row in value["returns"]:
            row["value"] = (core.ASSETS.index(row["asset"]) + 1) * 1e-100
        first = self.checked(value)["months"][0]
        self.assertEqual(len(first["strategies"][0]["weights"]), 32)
        self.assertTrue(all(row["momentum"] > 0 for row in first["signals"]))

    def test_return_below_minus_one_stops_index_but_keeps_monthly_gross(self):
        value = panel()
        original = self.checked(value)["months"][0]
        shorts = [row["asset"] for row in original["strategies"][0]["weights"] if row["weight"] < 0]
        for asset in shorts:
            set_return(value, "2010-01", asset, 4.)
        result = self.checked(value)
        self.assertLess(result["months"][0]["strategies"][0]["gross_return"], -1)
        self.assertIsNone(result["summary"][0]["terminal_gross_return_index"])
        self.assertEqual(result["summary"][0]["months_evaluated"], 24)

    def test_inputs_unchanged_and_output_order_deterministic(self):
        value, config = panel(), core.default_config()
        before = deepcopy((value, config))
        result = self.checked(value, config)
        self.assertEqual((value, config), before)
        changed = deepcopy(value)
        changed["returns"].reverse()
        second = self.checked(changed, config)
        self.assertEqual(result["months"], second["months"])
        self.assertNotEqual(result["input_digest"], second["input_digest"])

    def test_closed_fixed_config_and_reserved_cannot_be_reconfigured(self):
        config = core.default_config()
        variants = []
        for key, value in (("study_id", "new"), ("min_formation_assets", 29), ("schema_version", True),
                           ("source_contract", "author"), ("research_scope", "paper")):
            variants.append({**config, key: value})
        for key, child, value in (("development", "end", "2012-01"), ("development", "start", "2011-01"),
                                  ("reserved", "start", "2014-01"), ("signal", "window_months", 10),
                                  ("signal", "skip_months", 0), ("signal", "fill_policy", "zero")):
            changed = deepcopy(config)
            changed[key][child] = value
            variants.append(changed)
        variants.append({**config, "open_final_test": True})
        for changed in variants:
            with self.subTest(config=changed):
                with self.assertRaises(core.MomOnlyError):
                    core.evaluate(panel(), changed)
                rejected = oracle.check(panel(), changed, {})
                self.assertFalse(rejected["supported"])
                self.assertIsNone(rejected["passed"])

    def test_source_identity_unit_universe_axes_and_reserved_rows_rejected(self):
        variants = []
        for key, value in (("data_kind", "controlled_fixture"), ("asset_kind", "stock"),
                           ("return_semantics", "monthly_percent"), ("schema_version", True)):
            changed = panel()
            changed[key] = value
            variants.append(changed)
        for key, value in (("section", "Average Equal Weighted Returns -- Monthly"),
                           ("archive_sha256", "no"), ("header_preamble", ["201201,1.0"]),
                           ("download_url", "https://example.com/a.zip")):
            changed = panel()
            changed["source"][key] = value
            variants.append(changed)
        changed = panel()
        changed["assets"][0] = "OTHER_STOCK"
        variants.append(changed)
        changed = panel()
        changed["months"][1] = changed["months"][0]
        variants.append(changed)
        changed = panel()
        changed["months"].remove("2009-07")
        variants.append(changed)
        changed = panel()
        changed["returns"].pop()
        changed["returns"].append({"month": "2012-01", "asset": "Agric", "value": .1})
        variants.append(changed)
        changed = panel()
        changed["returns"][-1] = deepcopy(changed["returns"][0])
        variants.append(changed)
        for changed in variants:
            with self.subTest(panel=changed.get("return_semantics")):
                with self.assertRaises(core.MomOnlyError):
                    core.evaluate(changed, core.default_config())
                self.assertFalse(oracle.check(changed, core.default_config(), {})["supported"])

    def test_invalid_value_rejected_without_becoming_missing(self):
        for value in (True, float("nan"), float("inf"), -1.01, "0.1", {}, 10 ** 1000):
            changed = panel()
            changed["returns"][0]["value"] = value
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(core.MomOnlyError):
                    core.evaluate(changed, core.default_config())
                rejected = oracle.check(changed, core.default_config(), {})
                self.assertFalse(rejected["supported"])
                self.assertIsNone(rejected["passed"])

    def test_independent_reference_has_no_production_calculation_imports(self):
        tree = ast.parse(Path(oracle.__file__).read_text())
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(any(any(word in name for word in ("mom_only", "monthly_evaluation", "research_protocol")) for name in imports))

    def test_independent_reference_rejects_each_layer_and_order_tampering(self):
        value, config = panel(), core.default_config()
        result = self.checked(value, config)
        paths = [
            ("semantics_version",), ("human_judgment",), ("reserved_evaluated",),
            ("config", "development", "end"), ("source", "archive_sha256"),
            ("months", 0, "formation_months", 0), ("months", 0, "eligible_assets", 0),
            ("months", 0, "signals", 0, "momentum"), ("months", 0, "signals", 0, "mom_group"),
            ("months", 0, "labels", 0, "return_value"),
            ("months", 0, "strategies", 0, "weights", 0, "weight"),
            ("months", 0, "strategies", 0, "gross_return"),
            ("months", 0, "difference_gross_return"),
            ("summary", 0, "mean_gross_return"), ("summary", 0, "months_evaluated"),
            ("paired_comparison", "mean_gross_difference"),
        ]
        for path in paths:
            changed = deepcopy(result)
            parent = changed
            for key in path[:-1]:
                parent = parent[key]
            key = path[-1]
            current = parent[key]
            parent[key] = current + 1 if type(current) in (int, float) else "tampered"
            with self.subTest(path=path):
                rejected = oracle.check(value, config, changed)
                self.assertTrue(rejected["supported"])
                self.assertFalse(rejected["passed"])
                self.assertTrue(rejected["issues"])
        for key in ("months", "summary"):
            changed = deepcopy(result)
            changed[key].reverse()
            self.assertFalse(oracle.check(value, config, changed)["passed"])
        self.assertFalse(oracle.check(value, config, None)["passed"])


if __name__ == "__main__":
    unittest.main()
