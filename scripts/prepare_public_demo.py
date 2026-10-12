#!/usr/bin/env python3
"""Prepare original evidence and synthetic inputs for the normal CLI workflow.

This script creates inputs only; paper_alpha performs the real calculation and
verification. No model, third-party paper, or external market data is required.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_alpha.contracts import Task
from paper_alpha.evidence import ingest_pdf, match_evidence, sha256, verify_paper
from paper_alpha.storage import atomic_json, read_json
from scripts.make_fixture import generate


def prepare(output: Path) -> dict:
    source = ROOT / "examples" / "public_demo"
    provenance = read_json(source / "provenance.json")
    pdf = source / "engineering_note.pdf"
    if sha256(pdf) != provenance["document_sha256"]:
        raise ValueError("Original demo PDF digest mismatch")
    task = Task.parse(read_json(source / "task.json"))
    paper = ingest_pdf(pdf, provenance["paper_id"], provenance["title"],
                       provenance["source_url"], provenance["version"])
    verify_paper(paper, pdf)
    for evidence in task.evidence:
        match_evidence(evidence, paper)
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("Demo input directory already exists; choose a new --output-dir")
    output.mkdir(parents=True)
    shutil.copyfile(pdf, output / "paper.pdf")
    atomic_json(output / "paper.json", paper)
    atomic_json(output / "task.json", task.to_dict())
    metadata = generate(output)
    return {"prepared": True, "task": str(output / "task.json"),
            "data_kind": "synthetic", "rows": metadata["rows"],
            "paper_sha256": paper["document_sha256"],
            "market_sha256": metadata["market_sha256"],
            "note": "Inputs only. Run paper_alpha run, then paper_alpha verify."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".demo/inputs"))
    args = parser.parse_args()
    try:
        result = prepare(args.output_dir)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
