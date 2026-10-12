#!/usr/bin/env python3
"""Prepare a user-supplied Alpha101 PDF without distributing the document.

No download is performed. The maintained fixture requires an exact edition,
PDF digest, and physical-page citations. A different document is rejected.
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

PAPER_ID = "arxiv:1601.00991v3"
PDF_SHA256 = "1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9"
SOURCE_URL = "https://arxiv.org/abs/1601.00991v3"


def prepare(pdf: Path) -> dict:
    pdf = pdf.expanduser().resolve(strict=True)
    fixture = ROOT / "examples" / "alpha101"
    actual = sha256(pdf)
    if actual != PDF_SHA256:
        raise ValueError(f"Wrong PDF edition: expected SHA256 {PDF_SHA256}; got {actual}")
    source = read_json(fixture / "source_evidence.json")
    if (source["paper_id"], source["document_sha256"]) != (PAPER_ID, PDF_SHA256):
        raise ValueError("Maintained source-evidence identity differs from the pinned edition")
    task = Task.parse(read_json(fixture / "task.json"))
    paper = ingest_pdf(pdf, PAPER_ID, "101 Formulaic Alphas", SOURCE_URL, "v3")
    paper["authors"] = ["Zura Kakushadze"]
    verify_paper(paper, pdf)
    citations = [match_evidence(evidence, paper) for evidence in task.evidence]
    for maintained in source["evidence"]:
        if not any((citation["id"], citation["page"], citation["quote"]) ==
                   (maintained["id"], maintained["page"], maintained["quote"])
                   for citation in citations):
            raise ValueError("Maintained formula citation differs from the task")
    destination = fixture / "paper.pdf"
    extracted = fixture / "paper.json"
    if destination.exists() and sha256(destination) != actual:
        raise ValueError("Refusing to overwrite an existing different paper.pdf")
    if extracted.exists() and read_json(extracted) != paper:
        raise ValueError("Refusing to overwrite existing different extracted paper.json")
    if not destination.exists():
        shutil.copyfile(pdf, destination)
    if not extracted.exists():
        atomic_json(extracted, paper)
    verify_paper(read_json(extracted), destination)
    return {"prepared": True, "paper_id": PAPER_ID, "document_sha256": actual,
            "pages": len(paper["pages"]), "citations_verified": len(citations),
            "semantic_fidelity": "requires_human_review"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path,
                        help="Your independently obtained arXiv 1601.00991v3 PDF")
    args = parser.parse_args()
    try:
        result = prepare(args.pdf)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
