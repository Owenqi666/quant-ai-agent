"""Reviewed-template proposal provider; this is not a general paper-reading LLM."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from .contracts import Candidate, Evidence, Hypothesis, Task, require
from .evidence import normalize


PROVIDER = "reviewed-template proposal provider"

# Match the source label and formula together before emitting any proposal.
_TEMPLATES = (
    {
        "id": "alpha006",
        "label": "Alpha#6:",
        "formula": "(-1 * correlation(open, volume, 10))",
        "fields": ("open", "volume"),
        "mechanism": (
            "A possible price-volume divergence interpretation is a research conjecture. "
            "The cited formula does not establish a causal economic mechanism; human review is required."
        ),
    },
    {
        "id": "alpha101",
        "label": "Alpha#101:",
        "formula": "((close - open) / ((high - low) + .001))",
        "fields": ("close", "open", "high", "low"),
        "mechanism": (
            "A possible interpretation is intraday price pressure relative to the daily range. "
            "This interpretation is a conjecture: the cited formula does not establish a causal "
            "economic mechanism; human review is required."
        ),
    },
    {
        "id": "alpha005",
        "label": "Alpha#5:",
        "formula": "(rank((open - (sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vwap)))))",
        "fields": ("open", "close", "vwap"),
        "mechanism": (
            "A possible interpretation concerns price deviations from volume-weighted prices. "
            "This interpretation is a conjecture: the cited formula does not establish a causal "
            "economic mechanism; human review is required."
        ),
    },
)


def _pages(paper):
    require(isinstance(paper, dict), "paper must be an object")
    pages = paper.get("pages")
    require(isinstance(pages, list) and bool(pages), "paper must contain extracted pages")
    normalized = []
    seen = set()
    for page in pages:
        require(isinstance(page, dict), "Each extracted page must be an object")
        number, text = page.get("page"), page.get("text")
        require(type(number) is int and number > 0, "Extracted page number must be a positive integer")
        require(number not in seen, "Duplicate extracted page number")
        require(isinstance(text, str), "Extracted page text must be a string")
        seen.add(number)
        normalized.append((number, normalize(text)))
    return normalized


def draft_task(paper: dict, *, paper_path: str, pdf_path: str, data_path: str,
               metadata_path: str, evaluation: dict) -> dict:
    """Propose only reviewed formulas found verbatim in normalized extracted pages.

    PDF identity and extracted-text integrity are separately verified by the run
    entry point. This function does not claim general extraction, personal study,
    causal evidence, or reproduction of the paper's backtest.
    """
    pages = _pages(paper)
    evidence, hypotheses, candidates = [], [], []
    for template in _TEMPLATES:
        needle = normalize(f"{template['label']} {template['formula']}")
        matches = [(number, text.index(needle)) for number, text in pages if needle in text]
        require(len(matches) <= 1, f"Ambiguous reviewed formula location: {template['label']}")
        if not matches:
            continue
        number, start = matches[0]
        page_text = next(text for page, text in pages if page == number)
        # Slice the quote from the matched source text, rather than quoting a title or a model guess.
        quote_start = start + len(template["label"]) + 1
        quote = page_text[quote_start:start + len(needle)]
        evidence_id = f"{template['id']}-formula"
        hypothesis_id = f"h-{template['id']}"
        evidence.append(asdict(Evidence(evidence_id, number, quote)))
        assumptions = [
            f"Provider: {PROVIDER}; only exact reviewed formula templates are supported, with no LLM calls.",
            "The user's personal study of this paper is unconfirmed; this is an engineering demonstration.",
            "Higher factor values form the long leg under a local evaluation convention, not a conclusion "
            "established by this formula citation.",
            "Signals using daily OHLCV are available only after those observations are complete; "
            "execution timing must be specified by the local evaluation configuration.",
            "Local data, operator semantics and evaluation settings do not reproduce the paper's backtest "
            "or establish equivalence with WorldQuant BRAIN.",
            "Every required field must be available with documented semantics; no substitute field is authorized.",
        ]
        hypotheses.append(asdict(Hypothesis(
            id=hypothesis_id,
            claim=f"The source lists {template['label']} {quote}",
            attribution="paper_original",
            evidence_ids=[evidence_id],
            economic_mechanism=template["mechanism"],
            mechanism_attribution="model_conjecture",
            signal_direction="Higher values are long under the local evaluation convention; see assumptions.",
            required_fields=list(template["fields"]),
            assumptions=assumptions,
        )))
        candidates.append(asdict(Candidate(
            id=template["id"], hypothesis_id=hypothesis_id, expression=quote,
            origin="paper_original", changes=[],
        )))
    require(bool(candidates),
            "No reviewed formula template matched the extracted paper pages. "
            "This provider is not a general paper-reading Agent; supply reviewed evidence and a supported template.")
    raw = {
        "schema_version": 1,
        "title": "Paper-to-Alpha engineering demonstration; personal study unconfirmed "
                 f"({PROVIDER})",
        "paper": paper_path,
        "paper_pdf": pdf_path,
        "data": data_path,
        "data_metadata": metadata_path,
        "evidence": evidence,
        "hypotheses": hypotheses,
        "candidates": candidates,
        "evaluation": deepcopy(evaluation),
    }
    return Task.parse(raw).to_dict()
