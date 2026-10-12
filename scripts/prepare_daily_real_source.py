"""Create one new source-preparation, review or metadata adaptation artifact."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.daily_real_preparation import (
    VERSION, EXPRESSION_101, assess_preparation, create_independent_review_request,
    inspect_independent_review, prepare_development_plan, source_preparation_template,
)
from paper_alpha.storage import json_text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template", help="Create a no-data draft; every actual source identity is null")
    for name in ("assess", "review-request"):
        item = commands.add_parser(name)
        item.add_argument("preparation")
        item.add_argument("--bundle")
        item.add_argument("--allow-whole-byte-hash", action="store_true")
    review = commands.add_parser("review", help="Submit an assessment; this never authenticates reviewer or source")
    review.add_argument("request")
    review.add_argument("submission")
    review.add_argument("--expected-request-sha256", required=True)
    review.add_argument("--preparation", required=True)
    review.add_argument("--bundle")
    review.add_argument("--allow-whole-byte-hash", action="store_true")
    plan = commands.add_parser("plan", help="Prepare metadata only; no CSV or executable job is accepted")
    plan.add_argument("contract")
    plan.add_argument("--expected-contract-sha256", required=True)
    plan.add_argument("--expression", default=EXPRESSION_101)
    plan.add_argument("--required-field", action="append")
    for command in (template, review, plan, *[commands.choices[n] for n in ("assess", "review-request")]):
        command.add_argument("--out", required=True, help="New output file; no history is overwritten")
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
            if args.command == "template":
                report = source_preparation_template()
                success = True
            elif args.command in ("assess", "review-request"):
                function = assess_preparation if args.command == "assess" else create_independent_review_request
                report = function(args.preparation, bundle_path=args.bundle,
                                  allow_whole_byte_hash=args.allow_whole_byte_hash)
                success = args.command == "review-request" or report["status"] == "prepared_for_external_review"
            elif args.command == "review":
                report = inspect_independent_review(args.request, args.submission,
                                                    expected_request_sha256=args.expected_request_sha256,
                                                    preparation_path=args.preparation, bundle_path=args.bundle,
                                                    allow_whole_byte_hash=args.allow_whole_byte_hash)
                success = True
            else:
                report = prepare_development_plan(args.contract,
                                                  expected_contract_sha256=args.expected_contract_sha256,
                                                  expression=args.expression,
                                                  required_fields=args.required_field or ("open", "high", "low", "close"))
                success = report["status"] == "adaptation_planned"
        except (ValueError, OSError, TypeError, KeyError) as exc:
            report = {"schema_version": 1, "preparation_version": VERSION, "status": "rejected",
                      "code": getattr(exc, "code", "invalid_preparation"), "input_digests": "not_published_on_rejection",
                      "real_source_ready": False, "execution_allowed": False, "independent_authentication": "pending",
                      "review_submitted": False, "numeric_market_rows_parsed": 0, "final_numeric_rows_parsed": 0,
                      "model_calls": 0, "db_mutations": 0}
            success = False
        stream.write(json_text(report))
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"status": report.get("status", "template_created"),
                      "review_submitted": report.get("review_submitted", False),
                      "real_source_ready": False, "execution_allowed": False}))
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
