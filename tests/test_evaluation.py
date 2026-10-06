from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from paper_alpha.evaluation import AVAILABILITY, evaluate, load_market, validate_config
from paper_alpha.vendor.factors import factors


ROOT = Path(__file__).resolve().parents[1]


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calendar = pd.bdate_range("2024-01-01", periods=12).strftime("%Y-%m-%d").tolist()
        self.universe = ["A", "B", "C", "D"]
        rows = []
        for t, session in enumerate(self.calendar):
            for j, asset in enumerate(self.universe):
                opening = 100 + t * (j + 1) + ((t + j) % 3)
                rows.append({"date": session, "asset": asset, "open": opening,
                             "high": opening + 3, "low": opening - 2,
                             "close": opening + 1, "volume": 1000 + t * 10 + j})
        self.frame = pd.DataFrame(rows)
        self.metadata = {
            "calendar_dates": self.calendar, "universe": self.universe,
            "sessions": 12, "rows": 48, "columns": list(self.frame.columns),
            "field_availability": deepcopy(AVAILABILITY), "version": "unit-fixture-v1",
            "data_kind": "synthetic", "calendar_kind": "synthetic weekdays",
            "adjustment": "none; synthetic fixture",
        }
        self.config = {"splits": {
            "train": {"start": self.calendar[0], "end": self.calendar[2]},
            "validation": {"start": self.calendar[3], "end": self.calendar[8]},
            "test": {"start": self.calendar[9], "end": self.calendar[11]},
        }, "min_assets": 3}
        self._write()

    def _write(self, frame=None, metadata=None):
        frame = self.frame if frame is None else frame
        metadata = deepcopy(self.metadata if metadata is None else metadata)
        payload = frame.to_csv(index=False).encode()
        metadata["market_sha256"] = hashlib.sha256(payload).hexdigest()
        (self.directory / "market.csv").write_bytes(payload)
        (self.directory / "metadata.json").write_text(json.dumps(metadata))

    def _load(self):
        return load_market(self.directory / "market.csv", self.directory / "metadata.json")

    def _factor(self, panels):
        return pd.DataFrame(np.tile([1., 2., 3., 4.], (len(self.calendar), 1)),
                            index=panels["open"].index, columns=panels["open"].columns)

    def test_loader_preserves_metadata_and_derived_returns(self):
        panels = self._load()
        self.assertEqual(set(panels), {"open", "high", "low", "close", "volume", "returns"})
        self.assertEqual(panels["close"].attrs["field_availability"], "after session close")
        self.assertEqual(panels["open"].attrs["market_metadata"]["calendar_dates"], self.calendar)
        self.assertTrue(panels["returns"].iloc[0].isna().all())
        self.assertAlmostEqual(panels["returns"].iloc[1, 0],
                               panels["close"].iloc[1, 0] / panels["close"].iloc[0, 0] - 1)

    def test_checksum_is_verified_before_loading(self):
        with (self.directory / "market.csv").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "market_sha256"):
            self._load()

    def test_file_and_declared_grid_resource_limits(self):
        with patch("paper_alpha.evaluation.MAX_FILE_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "64 MiB"):
                self._load()
        with patch("paper_alpha.evaluation.MAX_PANEL_CELLS", 47):
            with self.assertRaisesRegex(ValueError, "panel-cell limit"):
                self._load()

    def test_real_market_data_is_not_silently_labeled_as_synthetic(self):
        metadata = deepcopy(self.metadata)
        metadata["data_kind"] = "real"
        self._write(metadata=metadata)
        with self.assertRaisesRegex(ValueError, "synthetic fixtures only"):
            self._load()

    def test_evaluation_output_size_is_bounded_before_record_construction(self):
        panels = self._load()
        with patch("paper_alpha.evaluation.MAX_EVALUATION_CELLS", 15):
            with self.assertRaisesRegex(ValueError, "evaluation-cell limit"):
                evaluate(self._factor(panels), panels, self.config)

    def test_missing_endpoint_interior_and_asset_are_rejected(self):
        for positions in ([0], list(range(4)), list(range(20, 24)), list(range(44, 48))):
            with self.subTest(positions=positions):
                self._write(self.frame.drop(positions))
                with self.assertRaisesRegex(ValueError, "coverage"):
                    self._load()

    def test_duplicate_keys_rejected_even_when_total_count_matches(self):
        frame = self.frame.copy()
        frame.iloc[1] = frame.iloc[0]
        self._write(frame)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self._load()

    def test_out_of_order_time_is_rejected_instead_of_sorted(self):
        self._write(pd.concat([self.frame.iloc[4:8], self.frame.iloc[:4], self.frame.iloc[8:]]))
        with self.assertRaisesRegex(ValueError, "increasing time order"):
            self._load()

    def test_extra_session_or_asset_rejected(self):
        for column, value in (("date", "2023-12-31"), ("asset", "UNKNOWN")):
            with self.subTest(column=column):
                frame = self.frame.copy()
                frame.loc[0, column] = value
                self._write(frame)
                with self.assertRaisesRegex(ValueError, "coverage"):
                    self._load()

    def test_invalid_numeric_ohlcv_and_bounds_rejected(self):
        for column, value, message in (("volume", 0., "positive"), ("open", -1., "positive"),
                                       ("close", np.inf, "finite"), ("high", np.nan, "finite"),
                                       ("low", 9999., "OHLC bounds"), ("high", 1., "OHLC bounds")):
            with self.subTest(column=column, value=value):
                frame = self.frame.astype({column: float})
                frame.loc[0, column] = value
                self._write(frame)
                with self.assertRaisesRegex(ValueError, message):
                    self._load()

    def test_independent_metadata_is_validated(self):
        mutations = [
            ("calendar_dates", [*self.calendar[:-1], self.calendar[0]], "Duplicate"),
            ("calendar_dates", self.calendar[::-1], "increasing"),
            ("universe", ["A", "B", "C", "C"], "Duplicate"),
            ("rows", 47, "rows mismatch"),
            ("field_availability", {}, "field_availability"),
        ]
        for key, value, message in mutations:
            with self.subTest(key=key):
                metadata = deepcopy(self.metadata)
                metadata[key] = value
                self._write(metadata=metadata)
                with self.assertRaisesRegex(ValueError, message):
                    self._load()

    def test_next_open_alignment_exact_labels_and_weights(self):
        panels = self._load()
        panels["open"].iloc[3] = [300., 100., 200., 400.]
        panels["open"].iloc[4] = [100., 100., 100., 100.]
        panels["open"].iloc[5] = [110., 120., 90., 100.]
        result = evaluate(self._factor(panels), panels, self.config)
        first = result["daily"][0]
        self.assertEqual(first["signal_date"], self.calendar[3])
        self.assertEqual(first["entry_date"], self.calendar[4])
        self.assertEqual(first["exit_date"], self.calendar[5])
        self.assertAlmostEqual(first["gross_return"], -0.075)
        self.assertAlmostEqual(first["rank_ic"], -0.6)
        np.testing.assert_allclose([a["weight"] for a in first["assets"]], [-.375, -.125, .125, .375])
        np.testing.assert_allclose([a["forward_return"] for a in first["assets"]], [.1, .2, -.1, 0])
        for row in result["daily"]:
            if row["status"] == "evaluated":
                self.assertAlmostEqual(sum(a["weight"] for a in row["assets"]), 0)
                self.assertAlmostEqual(sum(abs(a["weight"]) for a in row["assets"]), 1)
                self.assertAlmostEqual(sum(a["gross_contribution"] for a in row["assets"]), row["gross_return"])

    def test_purge_keeps_all_label_timestamps_inside_validation(self):
        panels = self._load()
        result = evaluate(self._factor(panels), panels, self.config)
        self.assertEqual(result["metrics"]["eligible_days"], 4)
        self.assertEqual(result["metrics"]["purged_days"], 2)
        self.assertEqual(len(result["daily"]), 6)
        self.assertEqual([d["status"] for d in result["daily"][-2:]], ["purged", "purged"])
        for row in result["daily"][:-2]:
            self.assertLessEqual(row["exit_date"], self.config["splits"]["validation"]["end"])
            self.assertGreaterEqual(row["signal_date"], self.config["splits"]["validation"]["start"])

    def test_final_test_and_train_are_locked(self):
        panels = self._load()
        for split in ("test", "train", "unknown"):
            with self.subTest(split=split), self.assertRaisesRegex(ValueError, "test is locked"):
                evaluate(self._factor(panels), panels, self.config, split=split)

    def test_config_rejects_overlap_reversal_and_noncalendar_bounds(self):
        for split, key, value in (("validation", "start", self.calendar[2]),
                                  ("test", "start", self.calendar[7]),
                                  ("validation", "end", self.calendar[0]),
                                  ("validation", "start", "2024-01-06")):
            with self.subTest(split=split, key=key, value=value):
                config = deepcopy(self.config)
                config["splits"][split][key] = value
                with self.assertRaises(ValueError):
                    validate_config(config, self.calendar)
        config = deepcopy(self.config)
        config["allow_test"] = True
        with self.assertRaisesRegex(ValueError, "Unknown"):
            validate_config(config, self.calendar)

    def test_misaligned_factor_and_market_panels_rejected(self):
        panels = self._load()
        factor = self._factor(panels)
        for invalid in (factor.iloc[::-1], factor.iloc[:, ::-1], factor.iloc[:-1]):
            with self.subTest(shape=invalid.shape), self.assertRaisesRegex(ValueError, "not aligned"):
                evaluate(invalid, panels, self.config)
        panels["volume"] = panels["volume"].iloc[:, ::-1]
        with self.assertRaisesRegex(ValueError, "Misaligned volume"):
            evaluate(factor, panels, self.config)

    def test_constant_factor_has_explicit_undefined_metrics_and_strict_json(self):
        panels = self._load()
        result = evaluate(self._factor(panels) * 0 + 1, panels, self.config)
        self.assertEqual(result["status"], "not_evaluable")
        self.assertIsNone(result["metrics"]["mean_rank_ic"])
        self.assertIsNone(result["metrics"]["mean_gross_return"])
        self.assertEqual(result["metrics"]["skipped_days"], 4)
        self.assertTrue(all(row["reason"] == "constant_factor" for row in result["daily"][:-2]))
        json.dumps(result, allow_nan=False)

    def test_nan_coverage_and_expression_diagnostics_are_preserved(self):
        panels = self._load()
        factor = self._factor(panels)
        factor.iloc[3, :2] = np.nan
        factor.attrs["expression_diagnostics"] = {"infinite_count": 0, "nan_count": 2}
        result = evaluate(factor, panels, self.config)
        self.assertEqual(result["daily"][0]["reason"], "insufficient_finite_assets")
        self.assertEqual(result["daily"][0]["missing_assets"], ["A", "B"])
        self.assertEqual(result["metrics"]["factor_coverage"], 14 / 16)
        self.assertEqual(result["factor_diagnostics"]["nan_count"], 2)
        json.dumps(result, allow_nan=False)

    def test_constant_labels_leave_ic_null_but_gross_return_defined(self):
        panels = self._load()
        panels["open"].iloc[:] = 100.
        result = evaluate(self._factor(panels), panels, self.config)
        self.assertIsNone(result["metrics"]["mean_rank_ic"])
        self.assertEqual(result["metrics"]["mean_gross_return"], 0.)
        self.assertEqual(result["daily"][0]["rank_ic_state"], "constant_forward_returns")
        json.dumps(result, allow_nan=False)

    def test_test_prices_and_factors_cannot_affect_validation_metrics(self):
        panels = self._load()
        factor = self._factor(panels)
        original = evaluate(factor, panels, self.config)
        panels["open"].iloc[9:] *= 700
        factor.iloc[9:] = -factor.iloc[9:] * 999
        changed = evaluate(factor, panels, self.config)
        self.assertEqual(original["metrics"], changed["metrics"])
        self.assertEqual(original["daily"], changed["daily"])

    def test_bundled_fixture_all_vendor_factors_are_prefix_invariant(self):
        panels = load_market(ROOT / "examples/alpha101/market.csv", ROOT / "examples/alpha101/metadata.json")
        cutoff = 329
        altered = {name: panel.copy() for name, panel in panels.items()}
        for name in ("open", "high", "low", "close", "volume"):
            altered[name].iloc[cutoff + 1:] *= np.arange(1, 13) * 7
        altered["returns"] = altered["close"].pct_change(fill_method=None)
        args = [panels[k] for k in ("open", "high", "low", "close", "volume", "returns")]
        other_args = [altered[k] for k in ("open", "high", "low", "close", "volume", "returns")]
        for name, function in factors.items():
            with self.subTest(factor=name):
                original = function(*args)
                changed = function(*other_args)
                prefix = function(*(arg.iloc[:cutoff + 1] for arg in args))
                assert_frame_equal(original.iloc[:cutoff + 1], changed.iloc[:cutoff + 1])
                assert_frame_equal(original.iloc[:cutoff + 1], prefix)

    def test_repeat_evaluation_is_identical(self):
        panels = self._load()
        factor = self._factor(panels)
        first = json.dumps(evaluate(factor, panels, self.config), allow_nan=False, sort_keys=True)
        second = json.dumps(evaluate(factor, panels, self.config), allow_nan=False, sort_keys=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
