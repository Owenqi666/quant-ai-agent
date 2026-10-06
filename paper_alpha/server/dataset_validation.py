"""Bounded synthetic CSV validation; called in a disposable subprocess."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import platform
import stat
import time

from ..evaluation import FIELDS, load_market
from ..market_diagnostics import DataValidationError, ROW_NUMBERING, SAMPLE_LIMIT, location
from ..storage import atomic_json, digest, read_json

MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_IMPORT_CELLS = 100_000
VALIDATION_TIMEOUT_SECONDS = 30
VALIDATOR_VERSION = "synthetic-dataset-import-v2"


def input_digest(csv_sha256, metadata_sha256):
    return digest({"csv_sha256": csv_sha256, "metadata_sha256": metadata_sha256})


def validator_digest():
    package = Path(__file__).resolve().parents[1]
    files = [Path(__file__).resolve(), package / "evaluation.py", package / "storage.py", package / "contracts.py", package / "market_diagnostics.py"]
    return digest({"version": VALIDATOR_VERSION,
                   "code": {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
                   "python": platform.python_version(),
                   "packages": {name: version(name) for name in ("numpy", "pandas")}})


def bounded_bytes(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DataValidationError("unsafe_input_file", "Input must be an independent regular file")
        if not 0 < info.st_size <= limit:
            raise DataValidationError("input_file_limit", f"Input must contain 1..{limit} bytes")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(limit + 1)
        if len(payload) != info.st_size or len(payload) > limit:
            raise DataValidationError("input_file_changed", "Input changed while being read or exceeds size limit")
        return payload
    finally:
        os.close(descriptor)


def empty_report(expected_input, expected_validator, *, status="interrupted", code="validation_interrupted", message="Validation did not finish"):
    return {"schema_version": 2, "validator": {"version": VALIDATOR_VERSION, "digest": expected_validator},
            "input_digest": expected_input, "status": status,
            "checks": [{"name": "execution", "outcome": "failed", "code": code, "message": message[:1500]}],
            "summary": None, "duration_seconds": 0.0,
            "diagnostics": {"mode": "fail_fast", "detected_error_count": 1, "all_errors_enumerated": False,
                            "samples": [], "sample_limit": SAMPLE_LIMIT, "first_error_location": None,
                            "row_numbering": ROW_NUMBERING,
                            "sample_note": "Only the first failed rule is reported, with at most five samples; this is not an enumeration of all invalid cells. CSV rows count logical records including the header and blank records. Missing grid entries and metadata have no CSV row."},
            "limitations": ["Synthetic data quality checks only; no investment-performance or economic-validity claim.",
                            "Research dates, min_assets, warm-up and evaluation budgets require separate task validation.",
                            "No values are filled, rows dropped, timestamps sorted or input digests repaired."]}


def validate_files(csv_path, metadata_path, expected_input, expected_validator):
    started = time.monotonic()
    report = empty_report(expected_input, expected_validator)
    names = ("input_integrity", "metadata_json", "resource_limits", "engine_loader")
    checks = [{"name": name, "outcome": "not_run", "code": "not_run", "message": "Not reached"} for name in names]
    report["checks"] = checks
    current = 0
    try:
        csv = bounded_bytes(csv_path, MAX_CSV_BYTES)
        metadata_raw = bounded_bytes(metadata_path, MAX_METADATA_BYTES)
        csv_hash, metadata_hash = (hashlib.sha256(value).hexdigest() for value in (csv, metadata_raw))
        if input_digest(csv_hash, metadata_hash) != expected_input:
            raise DataValidationError("input_digest_mismatch", "Frozen uploaded bytes do not match their receipt")
        if validator_digest() != expected_validator:
            raise DataValidationError("validator_changed", "Validator code or environment changed; start a new validation attempt")
        checks[current].update(outcome="passed", code="input_digest_matches", message="Both original input files match the upload receipt")
        current = 1
        try:
            metadata = read_json(metadata_path)
        except (ValueError, TypeError, RecursionError) as exc:
            raise DataValidationError("invalid_metadata_json", "Metadata must contain strict JSON without duplicate keys or nonfinite values") from exc
        if not isinstance(metadata, dict):
            raise DataValidationError("invalid_metadata", "Metadata must be a JSON object")
        if not isinstance(metadata.get("generator"), str) or not metadata["generator"].strip():
            raise DataValidationError("invalid_generator", "Metadata must declare its synthetic-data generator as a nonempty string", at=location(column="generator", reason="metadata_required"))
        checks[current].update(outcome="passed", code="strict_json_valid", message="Strict JSON parsed without duplicate keys or nonfinite values")
        current = 2
        calendar, universe = metadata.get("calendar_dates"), metadata.get("universe")
        if not isinstance(calendar, list) or not isinstance(universe, list) or not calendar or not universe:
            raise DataValidationError("invalid_grid_metadata", "Metadata needs nonempty calendar_dates and universe lists")
        if len(calendar) * len(universe) > MAX_IMPORT_CELLS:
            raise DataValidationError("import_cell_limit", "Declared calendar_dates x universe exceeds the 100,000-cell import limit")
        checks[current].update(outcome="passed", code="within_import_limits", message="Files and declared grid are within bounded import limits")
        current = 3
        panels = load_market(csv_path, metadata_path)
        # load_market is the same strict loader used by actual factor execution.
        checks[current].update(outcome="passed", code="engine_loader_valid", message="Engine loader verified checksum, metadata, complete grid, keys, finite positive OHLCV, OHLC bounds and derived returns")
        report["status"] = "valid"
        report["diagnostics"]["detected_error_count"] = 0
        report["summary"] = {"rows": len(calendar) * len(universe), "sessions": len(calendar), "assets": len(universe),
                             "start_date": calendar[0], "end_date": calendar[-1], "version": metadata["version"],
                             "data_kind": metadata["data_kind"], "fields": [*FIELDS, "returns"],
                             "dataset_sha256": digest({"data": csv_hash, "metadata": metadata})}
        if len(universe) < 3:
            report["limitations"].append("This dataset has fewer than three assets and cannot currently support research evaluation.")
        del panels
    except DataValidationError as exc:
        report["status"] = "invalid"
        checks[current].update(outcome="failed", code=exc.code, message=str(exc)[:1500])
        report["diagnostics"].update(samples=exc.samples, first_error_location=exc.location)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        report["status"] = "invalid"
        # Unknown errors are never classified by matching arbitrary English text.
        # Parser or OS messages may include paths/input, so only a safe stage label
        # crosses the report boundary; no invalid raw traceback is published.
        checks[current].update(outcome="failed", code=names[current] + "_failed",
                               message="Validation could not complete the " + names[current] + " check")
    report["duration_seconds"] = round(time.monotonic() - started, 6)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a frozen synthetic dataset (internal subprocess)")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--input-digest", required=True)
    parser.add_argument("--validator-digest", required=True)
    args = parser.parse_args(argv)
    result = validate_files(args.csv, args.metadata, args.input_digest, args.validator_digest)
    atomic_json(args.out, result)


if __name__ == "__main__":
    main()
