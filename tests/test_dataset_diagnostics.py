"""The strict execution loader and immutable import share located failures."""
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pandas.testing import assert_frame_equal

from paper_alpha.evaluation import AVAILABILITY, load_market
from paper_alpha.market_diagnostics import DataValidationError, ROW_NUMBERING
from paper_alpha.server.dataset_imports import DatasetImports
from paper_alpha.server.dataset_validation import input_digest, validate_files, validator_digest
from paper_alpha.server.service import Store


class DatasetDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.csv_path, self.metadata_path = self.root / "market.csv", self.root / "metadata.json"
        self.columns = ["date", "asset", "open", "high", "low", "close", "volume"]
        self.calendar = ["2024-01-02", "2024-01-03", "2024-01-04"]
        self.rows = [[day, asset, 10 + i, 12 + i, 9 + i, 11 + i, 100]
                     for i, day in enumerate(self.calendar) for asset in ["A", "B", "C"]]
        self.metadata = {"calendar_dates": self.calendar, "universe": ["A", "B", "C"],
                         "sessions": 3, "rows": 9, "columns": self.columns,
                         "field_availability": AVAILABILITY, "version": "diagnostic-test-v1",
                         "data_kind": "synthetic", "calendar_kind": "synthetic",
                         "adjustment": "synthetic", "generator": "diagnostic-test"}

    def write(self, *, rows=None, metadata=None, raw=None):
        if raw is None:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerows([self.columns, *(self.rows if rows is None else rows)])
            raw = stream.getvalue().encode()
        metadata = deepcopy(self.metadata if metadata is None else metadata)
        metadata["market_sha256"] = hashlib.sha256(raw).hexdigest()
        self.csv_path.write_bytes(raw)
        self.metadata_path.write_text(json.dumps(metadata))
        return raw

    def load(self):
        return load_market(self.csv_path, self.metadata_path)

    def report(self):
        before = [path.read_bytes() for path in (self.csv_path, self.metadata_path)]
        expected = input_digest(*(hashlib.sha256(value).hexdigest() for value in before))
        report = validate_files(self.csv_path, self.metadata_path, expected, validator_digest())
        self.assertEqual(before, [path.read_bytes() for path in (self.csv_path, self.metadata_path)])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["diagnostics"]["row_numbering"], ROW_NUMBERING)
        self.assertFalse(report["diagnostics"]["all_errors_enumerated"])
        return report

    def failure(self, code):
        with self.assertRaises(DataValidationError) as raised:
            self.load()
        error = raised.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.code, code)
        report = self.report()
        failed = next(check for check in report["checks"] if check["outcome"] == "failed")
        self.assertEqual(failed["code"], code)
        self.assertEqual(report["diagnostics"]["samples"], error.samples)
        self.assertEqual(report["diagnostics"]["first_error_location"], error.location)
        self.assertEqual(report["diagnostics"]["detected_error_count"], 1)
        return error

    def test_valid_data_panels_and_success_report_are_unchanged(self):
        self.write()
        panels = self.load()
        np.testing.assert_array_equal(panels["open"].to_numpy(), [[10] * 3, [11] * 3, [12] * 3])
        np.testing.assert_allclose(panels["returns"].iloc[1:].to_numpy(), [[1 / 11] * 3, [1 / 12] * 3])
        self.assertEqual(self.report()["status"], "valid")
        self.assertEqual(self.report()["diagnostics"]["first_error_location"], None)
        for name, panel in self.load().items():
            assert_frame_equal(panel, panels[name])

    def test_duplicate_key_reports_original_record_reference(self):
        rows = deepcopy(self.rows)
        rows[1] = rows[0]
        self.write(rows=rows)
        sample = self.failure("duplicate_key").samples[0]
        self.assertEqual((sample["row"], sample["related_rows"]), (3, [2]))
        self.assertEqual((sample["date"], sample["asset"], sample["column"]), ("2024-01-02", "A", "date,asset"))

    def test_at_most_five_samples_and_first_rule_only(self):
        rows = deepcopy(self.rows)
        for row in rows:
            row[2] = "not a number"
            row[4] = 0
        self.write(rows=rows)
        error = self.failure("numeric_type")
        self.assertEqual(len(error.samples), 5)
        self.assertTrue(all(sample["column"] == "open" for sample in error.samples))
        self.assertEqual([sample["row"] for sample in error.samples], [2, 3, 4, 5, 6])

    def test_repeated_keys_keep_first_record_even_after_sample_limit(self):
        self.write(rows=[self.rows[0]] * 9)
        error = self.failure("duplicate_key")
        self.assertEqual(len(error.samples), 5)
        self.assertTrue(all(sample["related_rows"] == [2] for sample in error.samples))

    def test_missing_key_reports_field_and_real_record(self):
        for column in (0, 1):
            with self.subTest(column=column):
                rows = deepcopy(self.rows)
                rows[2][column] = ""
                self.write(rows=rows)
                sample = self.failure("missing_key").samples[0]
                self.assertEqual((sample["row"], sample["column"]), (4, self.columns[column]))

    def test_missing_grid_observation_has_no_invented_row(self):
        self.write(rows=self.rows[:-1])
        sample = self.failure("grid_mismatch").samples[0]
        self.assertIsNone(sample["row"])
        self.assertEqual((sample["date"], sample["asset"]), ("2024-01-04", "C"))
        self.assertEqual(sample["reason"], "missing_grid_key")

    def test_unexpected_key_has_real_row_and_missing_key_is_separate(self):
        rows = deepcopy(self.rows)
        rows[0][1] = "UNKNOWN"
        self.write(rows=rows)
        samples = self.failure("grid_mismatch").samples
        self.assertEqual((samples[0]["row"], samples[0]["asset"]), (2, "UNKNOWN"))
        self.assertEqual((samples[1]["row"], samples[1]["asset"]), (None, "A"))

    def test_date_order_links_previous_record(self):
        self.write(rows=self.rows[3:6] + self.rows[:3] + self.rows[6:])
        sample = self.failure("date_order").samples[0]
        self.assertEqual((sample["row"], sample["related_rows"]), (5, [4]))

    def test_invalid_date_is_located_without_echoing_unbounded_value(self):
        rows = deepcopy(self.rows)
        rows[0][0] = "x" * 10_000
        self.write(rows=rows)
        error = self.failure("invalid_date")
        self.assertEqual(error.location["row"], 2)
        self.assertEqual(len(error.location["value"]), 160)
        self.assertLess(len(str(error)), 100)

    def test_numeric_nonfinite_nonpositive_and_bounds_locations(self):
        for field, value, code in (("open", "word", "numeric_type"),
                                   ("low", "NaN", "nonfinite_value"),
                                   ("high", "inf", "nonfinite_value"),
                                   ("volume", 0, "nonpositive_value"),
                                   ("low", 20, "ohlc_bounds")):
            with self.subTest(code=code, field=field):
                rows = deepcopy(self.rows)
                rows[4][self.columns.index(field)] = value
                self.write(rows=rows)
                sample = self.failure(code).samples[0]
                self.assertEqual(sample["row"], 6)
                self.assertIn(field, sample["column"])
                self.assertEqual((sample["date"], sample["asset"]), ("2024-01-03", "B"))

    def test_derived_return_overflow_links_previous_close(self):
        rows = deepcopy(self.rows)
        for index, row in enumerate(rows):
            price = 1e-308 if index < 3 else 1e308
            row[2:6] = [price] * 4
        self.write(rows=rows)
        sample = self.failure("nonfinite_derived_return").samples[0]
        self.assertEqual((sample["row"], sample["related_rows"], sample["column"]), (5, [2], "close"))

    def test_metadata_locations_do_not_claim_csv_rows(self):
        metadata = {**self.metadata, "field_availability": {}}
        self.write(metadata=metadata)
        error = self.failure("unsupported_field_availability")
        self.assertIsNone(error.location["row"])
        self.assertEqual(error.location["column"], "field_availability")

    def test_blank_records_and_quoted_multiline_fields_count_logical_records(self):
        rows = deepcopy(self.rows)
        for row in rows:
            if row[1] == "A":
                row[1] = "A\nquoted"
        rows[1] = rows[0].copy()
        metadata = {**self.metadata, "universe": ["A\nquoted", "B", "C"]}
        raw = self.write(rows=rows, metadata=metadata)
        # Insert a blank logical record after header; physical line numbers no
        # longer equal either data row numbers or our documented record index.
        self.write(raw=raw.replace(b"volume\r\n", b"volume\r\n\r\n", 1), metadata=metadata)
        sample = self.failure("duplicate_key").samples[0]
        self.assertEqual((sample["row"], sample["related_rows"]), (4, [3]))
        self.assertEqual(sample["asset"], "A\nquoted")

    def test_bom_and_blank_records_keep_valid_values(self):
        raw = self.write()
        baseline = self.load()
        self.write(raw=b"\xef\xbb\xbf\r\n" + raw + b"\r\n")
        loaded = self.load()
        for field in baseline:
            assert_frame_equal(loaded[field], baseline[field], check_flags=True)

    def test_csv_field_guard_does_not_add_a_new_identifier_limit_and_is_restored(self):
        asset = "A" * 140_000
        rows = deepcopy(self.rows)
        for row in rows:
            if row[1] == "A":
                row[1] = asset
        self.write(rows=rows, metadata={**self.metadata, "universe": [asset, "B", "C"]})
        before = csv.field_size_limit()
        self.assertEqual(self.load()["open"].columns[0], asset)
        self.assertEqual(csv.field_size_limit(), before)
        self.write(raw=b'"invalid')
        with self.assertRaises(DataValidationError):
            self.load()
        self.assertEqual(csv.field_size_limit(), before)

    def test_csv_structure_errors_are_bounded_and_safe(self):
        header = ",".join(self.columns).encode() + b"\n"
        for raw, code in ((header + b'"unterminated\n', "invalid_csv_structure"),
                          (header + b"2024-01-02,A,10,12\n", "invalid_csv_structure"),
                          (header + b"2024-01-02,A,10,12,9,11,100,extra\n", "invalid_csv_structure"),
                          (header + b'2024-01-02,A,10,12,9,11,"100\n', "invalid_csv_structure"),
                          (header + b"\xff", "invalid_csv_encoding"),
                          (header + b"\x00", "invalid_csv_structure")):
            with self.subTest(raw=raw):
                self.write(raw=raw)
                error = self.failure(code)
                self.assertNotIn(str(self.root), str(error))
                self.assertLess(len(json.dumps(self.report())), 5000)

    def test_quoted_numeric_postquote_whitespace_preserves_pandas_compatibility(self):
        raw = self.write()
        expected = self.load()
        for trailing in (b" ", b"\t"):
            with self.subTest(trailing=trailing):
                self.write(raw=raw.replace(b",10,12", b',"10"' + trailing + b",12", 1))
                actual = self.load()
                for field in expected:
                    assert_frame_equal(actual[field], expected[field])
                self.assertEqual(self.report()["status"], "valid")

    def test_implicit_unheaded_index_column_is_rejected_instead_of_dropped(self):
        raw = self.write()
        lines = raw.splitlines(keepends=True)
        self.write(raw=lines[0] + b"".join(str(index).encode() + b"," + line for index, line in enumerate(lines[1:])))
        error = self.failure("invalid_csv_structure")
        self.assertEqual((error.location["row"], error.location["reason"]), (2, "column_count"))

    def test_parser_does_not_scan_beyond_one_extra_data_record(self):
        raw = self.write(rows=[*self.rows, ["2024-01-05", "A", 10, 12, 9, 11, 100]])
        self.write(raw=raw + b'"unterminated after bounded read')
        error = self.failure("grid_mismatch")
        self.assertEqual(error.samples[0]["row"], 11)

    def test_unknown_io_error_does_not_leak_path(self):
        self.write()
        with patch("paper_alpha.server.dataset_validation.bounded_bytes", side_effect=OSError(str(self.root / "private"))):
            report = self.report()
        self.assertEqual(report["checks"][0]["code"], "input_integrity_failed")
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_real_subprocess_import_retains_exact_samples_and_original_bytes(self):
        rows = deepcopy(self.rows)
        rows[1] = rows[0]
        raw = self.write(rows=rows)
        direct = self.failure("duplicate_key")
        store = Store(self.root / "workspace")
        imports = DatasetImports(store.root, store.db_path)
        uploaded = imports.create("Located duplicate", raw, self.metadata_path.read_bytes(), "upload")
        result = imports.validate(uploaded["id"], "validation")
        report = result["latest_validation"]["report"]
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(report["diagnostics"]["samples"], direct.samples)
        self.assertEqual(report["schema_version"], 2)
        path = store.root / "dataset_imports" / uploaded["id"] / "inputs/market.csv"
        self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
