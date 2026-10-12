#!/usr/bin/env python3
"""Run fixed Alpha101 #101 on a new engineering fixture development attempt."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.daily_research import run_daily_development


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--source-receipt", required=True)
    parser.add_argument("--out", required=True, help="new attempt directory; never overwritten")
    parser.add_argument("--allow-whole-byte-hash", action="store_true",
                        help="authorize whole-file byte integrity hashing, not final numeric parsing")
    args = parser.parse_args(argv)
    try:
        result = run_daily_development(args.contract, args.csv, args.source_receipt, args.out,
                                       allow_whole_byte_hash=args.allow_whole_byte_hash)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": result["status"], "manifest_digest": result["manifest_digest"],
                      "scope": result["scope"], "test_freshness": "unknown",
                      "real_execution_readiness": "pending", "out": str(Path(args.out).absolute())},
                     sort_keys=True))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
