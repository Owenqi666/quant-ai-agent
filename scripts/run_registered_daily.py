#!/usr/bin/env python3
"""New isolated registration + fixed #101 run, or pinned read-only history resume."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import stat

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.registered_daily_development import RegisteredDailyError, _hash, _path, _output, _sha, run_registered_daily, verify_registered_run
from paper_alpha.server.daily_snapshots import DailySnapshots
from paper_alpha.server.service import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    new = commands.add_parser("new", help="create an absent workspace; register and execute engineering fixture only")
    for name in ("workspace", "contract", "csv", "source-receipt", "title", "registration-key", "run-key", "out"):
        new.add_argument("--" + name, required=True)
    new.add_argument("--allow-whole-byte-hash", action="store_true", help="byte integrity permission; final numeric permission remains absent")
    resume = commands.add_parser("resume", help="verify original completed effect; compute count zero")
    for name in ("workspace", "registration-id", "run-key", "out", "expected-receipt-sha256"):
        resume.add_argument("--" + name, required=True)
    verify = commands.add_parser("verify", help="verify current authority and pinned original artifact bytes")
    for name in ("workspace", "out", "expected-receipt-sha256"):
        verify.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        root = _path(args.workspace)
        if args.operation == "new":
            if root.exists(): raise RegisteredDailyError("WORKSPACE_EXISTS", "New operation requires an absent isolated workspace")
            if not args.allow_whole_byte_hash: raise RegisteredDailyError("INPUT_PERMISSION_REQUIRED", "Explicit whole-byte integrity permission required")
        else:
            _sha(args.expected_receipt_sha256)
            if not (root / "workbench.sqlite3").is_file(): raise RegisteredDailyError("WORKSPACE_MISSING", "Existing registered workspace required")
        # Check the output boundary before Store initialization or registration.
        class RootOnly: pass
        descriptor = RootOnly(); descriptor.root = root
        _output(descriptor, args.out)
        if args.operation != "new":
            database = _path(root / "workbench.sqlite3")
            info = database.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RegisteredDailyError("UNSAFE_WORKSPACE", "Workspace database must be an independent regular file")
            if _hash(Path(args.out) / "registered-result.json") != args.expected_receipt_sha256:
                raise RegisteredDailyError("OUTER_PIN_DRIFT", "Original host outer pin differs before opening workspace")
        store = Store(root)
        if args.operation == "new":
            registration = DailySnapshots(store).register_files(args.title, args.contract, args.csv, args.source_receipt, args.registration_key, allow_whole_byte_hash=True)
            if registration["status"] != "registered":
                print(json.dumps({"status": registration["status"], "registration_id": registration["id"], "compute_calls": 0, "model_calls": 0, "final_numeric_rows_parsed": 0}, sort_keys=True))
                return 2
            result = run_registered_daily(store, registration["id"], args.out, idempotency_key=args.run_key, allow_whole_byte_hash=True)
        elif args.operation == "resume":
            result = run_registered_daily(store, args.registration_id, args.out, idempotency_key=args.run_key, resume=True, expected_receipt_sha256=args.expected_receipt_sha256)
        else:
            result = verify_registered_run(store, args.out, expected_receipt_sha256=args.expected_receipt_sha256)
        receipt_sha = _hash(Path(args.out) / "registered-result.json")
        print(json.dumps({"status": result["status"], "run_version": result["run_version"], "logical_run_id": result["logical_run_id"], "registration_id": result["registration_identity"]["id"], "dataset_id": result["registration_identity"]["registered_dataset_id"], "outer_receipt_sha256": receipt_sha, "actual_tool_digest": result["inner"]["actual_tool_digest"], "scope": result["scope"], "test_freshness": "unknown", "scientific_score": None, "compute_calls": 1 if args.operation == "new" else 0, "model_calls": 0, "final_numeric_rows_parsed": 0}, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "rejected", "code": getattr(exc, "code", "REGISTERED_CLI_FAILED"), "message": "Registered operation rejected; preserved existing records", "model_calls": 0, "final_numeric_rows_parsed": 0}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
