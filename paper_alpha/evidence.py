from __future__ import annotations

from dataclasses import asdict
import hashlib
import re
from pathlib import Path
import unicodedata

from .contracts import require


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def ingest_pdf(path, paper_id, title, url, version):
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = [{"page": i + 1, "text": page.extract_text() or ""} for i, page in enumerate(reader.pages)]
    require(any(p["text"].strip() for p in pages), "PDF has no extractable text; OCR/human review required")
    return {"id": paper_id, "title": title, "url": url, "version": version,
            "document_sha256": sha256(path), "pages": pages,
            "extraction": "pypdf; text extraction is not semantic verification"}


def verify_paper(paper, pdf_path):
    require(paper.get("document_sha256") == sha256(pdf_path), "Paper PDF digest mismatch")
    for field in ("id", "title", "url", "version"):
        require(isinstance(paper.get(field), str) and paper[field].strip(), f"Missing paper {field}")
    # Re-extract from the PDF to prevent a fabricated pages JSON passing quote matching.
    extracted = ingest_pdf(pdf_path, paper["id"], paper["title"], paper["url"], paper["version"])
    expected = [(p["page"], normalize(p["text"])) for p in extracted["pages"]]
    supplied = [(p["page"], normalize(p["text"])) for p in paper.get("pages", [])]
    require(expected == supplied, "Paper text differs from PDF extraction; re-ingest with this environment")


def match_evidence(evidence, paper):
    pages = {p["page"]: normalize(p["text"]) for p in paper["pages"]}
    quote = normalize(evidence.quote)
    text = pages.get(evidence.page, "")
    require(quote in text, f"Evidence {evidence.id}: quote absent from page {evidence.page}")
    start = text.index(quote)
    return {**asdict(evidence), "paper_id": paper["id"], "document_sha256": paper["document_sha256"],
            "normalized_start": start, "normalized_end": start + len(quote),
            "citation_verified": True, "semantic_fidelity": "requires_human_review"}
