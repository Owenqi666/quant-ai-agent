from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import require
from .evidence import ingest_pdf
from .storage import atomic_json, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evidence-linked, bounded local alpha research")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Extract PDF pages, preserve its checksum")
    ingest.add_argument("pdf", type=Path)
    for flag in ("id", "title", "url", "version", "out"):
        ingest.add_argument("--" + flag, required=True)
    draft = commands.add_parser("draft", help="Generate reviewable proposals from supported formula templates")
    draft.add_argument("--paper", type=Path, required=True)
    draft.add_argument("--pdf", type=Path, required=True)
    draft.add_argument("--data", type=Path, required=True)
    draft.add_argument("--metadata", type=Path, required=True)
    draft.add_argument("--evaluation", type=Path, required=True)
    draft.add_argument("--out", type=Path, required=True)
    run = commands.add_parser("run", help="Run or resume a local validation experiment")
    run.add_argument("task", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--mode", choices=("agent", "fixed", "normalized_fixed"), default="agent")
    run.add_argument("--resume", action="store_true")
    verify = commands.add_parser("verify", help="Verify snapshots, result hashes and report consistency")
    verify.add_argument("run", type=Path)
    bench = commands.add_parser("benchmark", help="Compare bounded repair with raw and alias-normalized fixed workflows")
    bench.add_argument("--task", type=Path, default=Path("examples/alpha101/task.json"))
    bench.add_argument("--out", type=Path, required=True)
    suite = commands.add_parser("suite", help="Run the versioned engineering development suite; held-out material stays reserved")
    suite.add_argument("--manifest", type=Path, default=Path("evaluation_suites/v04/manifest.json"))
    suite.add_argument("--out", type=Path, required=True)
    suite_verify = commands.add_parser('verify-suite', help='Recompute saved suite checks, denominators and report consistency')
    suite_verify.add_argument('run', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            require(not Path(args.out).exists(), "Output already exists")
            result = ingest_pdf(args.pdf, args.id, args.title, args.url, args.version)
            atomic_json(args.out, result)
            result = {"out": str(args.out), "pages": len(result["pages"]), "sha256": result["document_sha256"]}
        elif args.command == "draft":
            import os
            from .proposals import draft_task
            from .evidence import verify_paper
            require(not args.out.exists(), "Output already exists")
            paper = read_json(args.paper)
            verify_paper(paper, args.pdf)
            base = args.out.resolve().parent
            relative = lambda p: os.path.relpath(p.resolve(), base)
            task = draft_task(paper, paper_path=relative(args.paper), pdf_path=relative(args.pdf),
                              data_path=relative(args.data), metadata_path=relative(args.metadata),
                              evaluation=read_json(args.evaluation))
            atomic_json(args.out, task)
            result = {"out": str(args.out), "candidates": len(task["candidates"]), "human_review_required": True}
        elif args.command == "run":
            from .workflow import run_task
            state = run_task(args.task, args.out, args.mode, args.resume)
            result = {"out": str(args.out), "status": state["status"], "tool_calls": state["tool_calls"],
                      "candidates": {c["id"]: c["status"] for c in state["candidates"]}}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if state["status"] == "completed" else 2
        elif args.command == "verify":
            from .workflow import verify_run
            result = verify_run(args.run)
        elif args.command == 'suite':
            from .evaluation_suite import run_suite
            result = run_suite(args.manifest, args.out)
        elif args.command == 'verify-suite':
            from .evaluation_suite import verify_suite
            result = verify_suite(args.run)
        else:
            from .benchmark import benchmark
            result = benchmark(args.task, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if (args.command == "benchmark" and not result["all_checks_passed"]) or (args.command == 'suite' and not result['acceptance_passed']) else 0
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
