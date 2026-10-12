"""Write one new offline source-bundle inspection report."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.daily_source_onboarding_v1 import DailySourceOnboardingError, VERSION, inspect_source_bundle
from paper_alpha.storage import json_text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--out", required=True, help="New JSON report; existing files are refused")
    parser.add_argument("--allow-whole-byte-hash", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out).absolute()
    try:
        if out.resolve() != out or any(p.is_symlink() for p in (out, *out.parents)):
            raise OSError("unsafe output")
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.parent.resolve() != out.parent:
            raise OSError("unsafe output parent")
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        print(json.dumps({"status": "rejected", "code": "output_unavailable", "inputs_read": False}))
        return 2
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        try:
            report = inspect_source_bundle(args.bundle, allow_whole_byte_hash=args.allow_whole_byte_hash)
        except (DailySourceOnboardingError, OSError, ValueError, TypeError) as exc:
            report = {
                "schema_version": 1, "bundle_version": VERSION, "status": "rejected",
                "code": getattr(exc, "code", "unsafe_input"), "role": getattr(exc, "role", "bundle"),
                "checks_complete": False, "retained_evidence_verified": False,
                "raw_byte_identity_verified": False, "onboarding_complete": False,
                "input_digests": "not_published_on_rejection", "independent_authentication": "pending",
                "real_source_ready": False, "execution_allowed": False,
                "numeric_market_rows_parsed": 0, "final_numeric_rows_parsed": 0,
            }
        stream.write(json_text(report)); stream.flush(); os.fsync(stream.fileno())
    print(json.dumps({"status": report["status"], "onboarding_complete": report["onboarding_complete"],
                      "real_source_ready": False, "execution_allowed": False}))
    return 0 if report["onboarding_complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
