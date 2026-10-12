"""Run the user-selected, pinned local industry MOM development example."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_alpha.mom_only_workflow import run
from paper_alpha.storage import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New output directory; existing attempts are never overwritten")
    parser.add_argument("--source", type=Path, default=ROOT / "artifacts/mom-only-source-assessment-01/49_Industry_Portfolios_CSV.zip")
    parser.add_argument("--source-receipt", type=Path, default=ROOT / "artifacts/mom-only-source-assessment-01/download_receipt.json")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    method_path = ROOT / "docs/research/mom_only_contract.json"
    expected = read_json(method_path)["illustrative_defaults"]["source_candidate"]["raw_sha256"]
    try:
        result = run(args.source, ROOT / "examples/mom_only_industry/config.json", method_path,
                     args.out, expected, args.source_receipt, args.evidence_dir)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
