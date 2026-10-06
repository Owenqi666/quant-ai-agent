"""Independent small examples for monthly windows and declared coverage."""
from copy import deepcopy
from fractions import Fraction
import json
import math
import unittest
from unittest.mock import patch

from paper_alpha import research_protocol as protocol


def config(**changes):
    value = protocol.presets()[1]["config"]
    value.update(mom_window_months=1, id_window_months=1)
    value.update(changes)
    return value


def fixture(values, *, sessions=None, history_start="2026-01-01"):
    if sessions is None:
        sessions = [f"2026-05-{day:02d}" for day in range(1, len(values) + 1)]
    rows = [{"date": day, "asset": "A", "value": value}
            for day, value in zip(sessions, values) if value != "absent"]
    return {"schema_version": 1, "data_kind": "controlled_fixture", "source_id": "hand-calculated-fixture",
            "return_semantics": "daily_total_return_decimal",
            "calendar": {"id": "explicit-fictional-sessions", "version": "1",
                         "start": "2026-01-01", "end": "2026-07-31", "sessions": sessions},
            "assets": [{"id": "A", "history_start": history_start}], "returns": rows}


def asset_report(bundle, **changes):
    return protocol.preview(config(**changes), "2026-07", bundle)["assets"][0]


class ProtocolConfigTests(unittest.TestCase):
    def assert_code(self, expected, function, *args):
        with self.assertRaises(protocol.ProtocolError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, expected)

    def test_presets_are_independent_and_paper_is_unresolved(self):
        presets = protocol.presets()
        self.assertEqual(len(presets), 2)
        for preset in presets:
            self.assertEqual(set(preset), {"id", "title", "description", "config", "sources", "unresolved"})
            self.assertTrue(all(isinstance(s, str) for s in preset["sources"]))
            self.assertEqual(protocol.validate_config(preset["config"]), preset["config"])
        presets[0]["sources"].clear()
        presets[1]["config"]["mom_window_months"] = 1
        self.assertTrue(protocol.presets()[0]["sources"])
        self.assertEqual(protocol.presets()[1]["config"]["mom_window_months"], 11)

    def test_unknown_and_missing_fields_rejected(self):
        for value in ({**config(), "surprise": 1}, {k: v for k, v in config().items() if k != "zero_policy"}):
            self.assert_code("invalid_fields", protocol.validate_config, value)

    def test_public_digest_normalizes_config_and_matches_preview(self):
        value = config(min_coverage=1)
        expected = protocol.config_digest(value)
        self.assertEqual(expected, protocol.config_digest(config(min_coverage=1.0)))
        self.assertEqual(expected, protocol.preview(value, "2026-07", fixture([0.1]))["config_digest"])
        self.assertNotEqual(expected, protocol.config_digest(config(mom_window_months=2)))

    def test_numeric_types_and_limits(self):
        for field, values in {
            "schema_version": [True, 1.0, 2],
            "mom_window_months": [True, 0, 37, 1.0],
            "id_window_months": [None, False, 37],
            "mom_skip_months": [-1, 13, True],
            "id_skip_months": [-1, 13, True],
            "max_missing_run": [-1, 367, True, 1.0],
            "min_coverage": [True, float("nan"), float("inf"), 0, -1, 1.01, "1"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(protocol.ProtocolError):
                    protocol.validate_config(config(missing_policy="available", **{field: value}))
        self.assertEqual(protocol.validate_config(config(min_coverage=1))["min_coverage"], 1.0)

    def test_policy_rules_not_generic_fill_options(self):
        for change in ({"zero_policy": "exclude"}, {"fill_policy": "zero"},
                       {"missing_policy": "unresolved"}, {"min_coverage": 0.5},
                       {"max_missing_run": 1}):
            with self.subTest(change=change), self.assertRaises(protocol.ProtocolError):
                protocol.validate_config(config(**change))

    def test_paper_rules_cannot_be_forged(self):
        paper = protocol.presets()[0]["config"]
        for field, value in (("id_window_months", 11), ("missing_policy", "complete"),
                             ("min_coverage", 1), ("mom_window_months", 12),
                             ("mom_skip_months", 0)):
            modified = {**paper, field: value}
            self.assert_code("paper_profile_modified", protocol.validate_config, modified)
        result = protocol.preview(paper, "2026-07")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["windows"]["id"])
        for row in result["assets"]:
            self.assertEqual(row["status"], "blocked")
            self.assertTrue(row["reasons"])
            for name in ("momentum", "pret", "id", "momentum_coverage", "id_coverage"):
                self.assertIsNone(row[name])

    def test_literal_calendar_windows_and_asof(self):
        value = protocol.presets()[1]["config"]
        result = protocol.resolve_windows(value, "2026-07")
        self.assertEqual(result, {"target_month": "2026-07", "as_of": "2026-06-30",
                                 "momentum": {"start": "2025-07-01", "end": "2026-05-31"},
                                 "id": {"start": "2025-07-01", "end": "2026-05-31"}})
        leap = protocol.resolve_windows(config(mom_skip_months=0, id_skip_months=0), "2024-03")
        self.assertEqual(leap["as_of"], "2024-02-29")
        self.assertEqual(leap["momentum"], {"start": "2024-02-01", "end": "2024-02-29"})
        new_year = protocol.resolve_windows(config(), "2026-01")
        self.assertEqual(new_year["momentum"], {"start": "2025-11-01", "end": "2025-11-30"})
        maximum = protocol.resolve_windows(config(mom_window_months=36, mom_skip_months=12), "1905-01")
        self.assertEqual(maximum["momentum"], {"start": "1901-01-01", "end": "1903-12-31"})

    def test_month_shape_and_bounds(self):
        for month in (None, 202607, "2026-7", "2026-13", "2026-07-01", "1904-12", "2100-01"):
            self.assert_code("invalid_month", protocol.resolve_windows, config(), month)


class ProtocolCalculationTests(unittest.TestCase):
    def test_hand_calculation_and_zero_denominator(self):
        row = asset_report(fixture([0.1, 0.1, -0.05, 0.0]))
        # Independent exact decimal arithmetic: (11/10)^2 * 19/20 - 1.
        expected = Fraction(11, 10) ** 2 * Fraction(19, 20) - 1
        self.assertEqual(row["status"], "ready")
        self.assertAlmostEqual(row["momentum"], float(expected), places=14)
        self.assertEqual(row["pret"], row["momentum"])
        self.assertEqual(row["id"], -0.25)
        self.assertEqual(row["id_coverage"]["zero"], 1)

    def test_negative_pret_reverses_count_sign_not_momentum_direction(self):
        row = asset_report(fixture([0.1, 0.1, -0.5, 0.0]))
        self.assertAlmostEqual(row["pret"], -0.395, places=14)
        self.assertEqual(row["id"], 0.25)

    def test_zero_returns_are_observations_not_missing(self):
        row = asset_report(fixture([0.0, -0.0, 0.0]))
        self.assertEqual((row["momentum"], row["pret"], row["id"]), (0.0, 0.0, 0.0))
        self.assertEqual(row["id_coverage"]["valid"], 3)
        self.assertEqual(row["id_coverage"]["zero"], 3)
        self.assertEqual(row["id_coverage"]["coverage"], 1)

    def test_missing_row_and_null_have_distinct_reasons_same_complete_failure(self):
        row = asset_report(fixture([0.1, "absent", None, 0.0]))
        self.assertEqual(row["status"], "unavailable")
        self.assertIsNone(row["momentum"])
        self.assertIsNone(row["pret"])
        self.assertIsNone(row["id"])
        cov = row["id_coverage"]
        self.assertEqual((cov["expected"], cov["valid"], cov["missing_rows"], cov["null_values"]), (4, 2, 1, 1))
        self.assertEqual(cov["missing_dates"], [{"date": "2026-05-02", "reason": "missing_row"},
                                                {"date": "2026-05-03", "reason": "null_value"}])

    def test_teaching_example_available_is_explicit_observed_product(self):
        bundle = fixture([0.1] * 6 + [-0.01] * 2 + [0.0, "absent"])
        result = protocol.preview(config(missing_policy="available", min_coverage=0.9, max_missing_run=1), "2026-07", bundle)
        row = result["assets"][0]
        expected = Fraction(11, 10) ** 6 * Fraction(99, 100) ** 2 - 1
        self.assertAlmostEqual(row["pret"], float(expected), places=14)
        self.assertEqual(row["id"], -4 / 9)
        self.assertEqual(row["id_coverage"]["expected"], 10)
        self.assertTrue(any("observed_returns_only" in w and "not the complete-window return" in w for w in result["warnings"]))
        strict = asset_report(bundle)
        self.assertIsNone(strict["id"])
        # A real observed zero changes the denominator. This is a changed input,
        # not an allowed silent imputation policy.
        bundle["returns"].append({"date": "2026-05-10", "asset": "A", "value": 0.0})
        self.assertEqual(asset_report(bundle)["id"], -4 / 10)

    def test_missing_run_follows_sessions_across_weekend(self):
        bundle = fixture([0.1, None, "absent", 0.0], sessions=["2026-05-07", "2026-05-08", "2026-05-11", "2026-05-12"])
        row = asset_report(bundle, missing_policy="available", min_coverage=0.5, max_missing_run=1)
        self.assertEqual(row["id_coverage"]["max_missing_run"], 2)
        self.assertIn("id:missing_run_exceeded", row["reasons"])
        self.assertIsNone(row["id"])
        self.assertEqual(asset_report(bundle, missing_policy="available", min_coverage=0.5, max_missing_run=2)["status"], "ready")

    def test_available_never_shortens_history(self):
        bundle = fixture([0.1, 0.0], sessions=["2026-05-11", "2026-05-12"], history_start="2026-05-11")
        row = asset_report(bundle, missing_policy="available", min_coverage=0.01, max_missing_run=366)
        self.assertEqual(row["id_coverage"]["coverage"], 1.0)
        self.assertIn("id:history_insufficient", row["reasons"])
        self.assertIsNone(row["id"])

    def test_separate_window_failures_do_not_erase_valid_signal(self):
        bundle = fixture([0.1, 0.0], history_start="2026-05-01")
        result = protocol.preview(config(id_window_months=2), "2026-07", bundle)
        row = result["assets"][0]
        self.assertEqual(result["status"], "partial")
        self.assertAlmostEqual(row["momentum"], 0.1)
        self.assertIsNone(row["pret"])
        self.assertIsNone(row["id"])
        self.assertIn("id:history_insufficient", row["reasons"])
        self.assertFalse(any(reason.startswith("momentum:") for reason in row["reasons"]))

    def test_empty_and_entirely_missing_windows_never_produce_zero(self):
        for bundle in (fixture([None, "absent"]), fixture([0.1], sessions=["2026-06-01"])):
            row = asset_report(bundle, missing_policy="available", min_coverage=0.01, max_missing_run=366)
            self.assertIsNone(row["id"])
            self.assertIsNone(row["momentum"])
            self.assertIn("id:no_valid_returns", row["reasons"])

    def test_future_and_skipped_month_do_not_change_earlier_signals(self):
        bundle = fixture([0.1, 0.0, 0.2, 0.3], sessions=["2026-05-01", "2026-05-04", "2026-06-01", "2026-07-01"])
        before = protocol.preview(config(), "2026-07", bundle)
        later = deepcopy(bundle)
        later["returns"][-1]["value"] = -0.8
        later["returns"][-2]["value"] = None
        after = protocol.preview(config(), "2026-07", later)
        self.assertEqual(before["assets"], after["assets"])
        self.assertEqual(before["config_digest"], after["config_digest"])
        self.assertNotEqual(before["input_digest"], after["input_digest"])

    def test_overflow_is_explicit_and_total_loss_is_valid(self):
        row = asset_report(fixture([1e308, 1e308]))
        self.assertIsNone(row["momentum"])
        self.assertIsNone(row["id"])
        self.assertIn("momentum:nonfinite_compound", row["reasons"])
        json.dumps(row, allow_nan=False)
        loss = asset_report(fixture([-1.0, 1e308, 1e308]))
        self.assertEqual(loss["pret"], -1.0)
        self.assertTrue(math.isfinite(loss["id"]))

    def test_small_nonzero_returns_are_not_rounded_to_zero(self):
        row = asset_report(fixture([1e-20, 0.0]))
        self.assertGreater(row["pret"], 0)
        self.assertEqual(row["id"], -0.5)

    def test_exact_zero_boundary_does_not_invent_id_direction(self):
        # 8 * 1/2 * 1/2 * 1/2 == 1, with three negative days and one positive.
        # A log-only reduction produces about -1.11e-16 on this fixture.
        row = asset_report(fixture([7.0, -0.5, -0.5, -0.5]))
        self.assertEqual(row["pret"], 0.0)
        self.assertEqual(row["id"], 0.0)
        above = asset_report(fixture([math.nextafter(7.0, math.inf), -0.5, -0.5, -0.5]))
        below = asset_report(fixture([math.nextafter(7.0, -math.inf), -0.5, -0.5, -0.5]))
        self.assertGreater(above["pret"], 0)
        self.assertEqual(above["id"], 0.5)
        self.assertLess(below["pret"], 0)
        self.assertEqual(below["id"], -0.5)

    def test_unrepresentable_nonzero_pret_is_explicitly_unavailable(self):
        row = asset_report(fixture([1e-200, -1e-200]))
        self.assertIsNone(row["pret"])
        self.assertIsNone(row["id"])
        self.assertIn("id:compound_precision_underflow", row["reasons"])
        with patch.object(protocol, "EXACT_COMPOUND_MAX_BITS", 2):
            row = asset_report(fixture([0.1, -0.1 / 1.1]))
        self.assertIsNone(row["id"])
        self.assertIn("id:compound_precision_budget_exceeded", row["reasons"])

    def test_demo_bounds_reproducibility_and_report_shape(self):
        default = protocol.presets()[1]["config"]
        demo = protocol.demo_bundle("2026-07")
        before = deepcopy(demo)
        result = protocol.preview(default, "2026-07", demo)
        self.assertEqual(result, protocol.preview(default, "2026-07", demo))
        self.assertEqual(demo, before)
        self.assertEqual(result["status"], "partial")
        expected = float(Fraction(209, 200) ** 11 - 1)
        complete = next(r for r in result["assets"] if r["asset"] == "COMPLETE")
        self.assertAlmostEqual(complete["momentum"], expected, places=13)
        asymmetric = next(r for r in result["assets"] if r["asset"] == "ASYMMETRIC")
        self.assertEqual(asymmetric["id"], -11 / asymmetric["id_coverage"]["expected"])
        self.assertAlmostEqual(asymmetric["pret"], float((Fraction(209, 200) * Fraction(101, 100)) ** 11 - 1), places=13)
        self.assertEqual(set(result), {"schema_version", "semantics_version", "config", "config_digest", "input_digest",
                                       "data_kind", "source_id", "windows", "status", "warnings", "assets"})
        self.assertEqual(set(complete["id_coverage"]), {"expected", "valid", "positive", "negative", "zero", "missing_rows",
                                                       "null_values", "coverage", "max_missing_run", "missing_dates"})
        protocol.preview(config(mom_window_months=36, id_window_months=36, mom_skip_months=12, id_skip_months=12), "1905-01")
        json.dumps(result, allow_nan=False)


class ProtocolInputTests(unittest.TestCase):
    def assert_bad(self, bundle, code):
        with self.assertRaises(protocol.ProtocolError) as raised:
            protocol.preview(config(), "2026-07", bundle)
        self.assertEqual(raised.exception.code, code)

    def test_reject_real_market_and_unstated_return_semantics(self):
        for field, value, code in (("data_kind", "real_market", "data_kind_not_supported"),
                                   ("return_semantics", "close", "return_semantics_not_supported"),
                                   ("schema_version", True, "invalid_integer")):
            bundle = fixture([0.1])
            bundle[field] = value
            self.assert_bad(bundle, code)

    def test_calendar_coverage_cannot_disappear_with_return_rows(self):
        bundle = fixture([0.1])
        bundle["calendar"]["start"] = "2026-05-02"
        bundle["calendar"]["sessions"] = ["2026-05-04"]
        bundle["returns"][0]["date"] = "2026-05-04"
        self.assert_bad(bundle, "calendar_coverage_insufficient")

    def test_duplicates_non_session_and_unknown_asset(self):
        bundle = fixture([0.1, 0.0])
        bundle["returns"].append(dict(bundle["returns"][0]))
        self.assert_bad(bundle, "duplicate_return")
        for field, value, code in (("date", "2026-05-30", "non_session_return"),
                                   ("asset", "B", "unknown_asset"),
                                   ("date", "2026-2-1", "invalid_date")):
            bundle = fixture([0.1])
            bundle["returns"][0][field] = value
            self.assert_bad(bundle, code)

    def test_calendar_sessions_must_be_ordered_unique_bounded_dates(self):
        for sessions in (["2026-05-02", "2026-05-01"], ["2026-05-01"] * 2, ["2025-05-01"]):
            bundle = fixture([0.1] * len(sessions), sessions=sessions)
            self.assert_bad(bundle, "invalid_sessions")
        bundle = fixture([0.1])
        bundle["calendar"]["end"] = "9999-12-31"
        self.assert_bad(bundle, "calendar_bounds")

    def test_bad_returns_not_reclassified_as_missing(self):
        for value in (True, float("nan"), float("inf"), -float("inf"), "0.1", 10 ** 1000):
            self.assert_bad(fixture([value]), "invalid_number")
        self.assert_bad(fixture([-1.01]), "invalid_return")
        bundle = fixture([0.1])
        bundle["assets"][0]["history_start"] = "2026-05-02"
        self.assert_bad(bundle, "return_before_history")

    def test_limits_are_checked_without_computing_huge_panels(self):
        bundle = fixture([0.1])
        bundle["assets"] = [{"id": str(i), "history_start": "2026-01-01"} for i in range(protocol.MAX_ASSETS + 1)]
        self.assert_bad(bundle, "asset_limit")
        for name, bound, expected in (("MAX_RETURNS", 0, "return_limit"),
                                       ("MAX_SESSIONS", 0, "session_limit"),
                                       ("MAX_PANEL_CELLS", 0, "panel_limit"),
                                       ("MAX_BUNDLE_BYTES", 1, "bundle_size_limit")):
            with patch.object(protocol, name, bound):
                self.assert_bad(fixture([0.1]), expected)

    def test_return_row_order_does_not_change_signal_or_mutate_input(self):
        bundle = fixture([0.1, -0.02, 0.0])
        before = asset_report(bundle)
        bundle["returns"].reverse()
        snapshot = deepcopy(bundle)
        self.assertEqual(asset_report(bundle), before)
        self.assertEqual(bundle, snapshot)


if __name__ == "__main__":
    unittest.main()
