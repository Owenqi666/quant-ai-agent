"""Bounded formula spans in explicitly labelled Alpha citations.

This adds syntax provenance for contextual quotations, not semantic verification
of surrounding prose. Only a complete parenthesized expression immediately
following ``Alpha#N:`` can supply formula provenance. A single target number must
be declared in the adopted hypothesis claim; candidate identifiers never supply
that authority. Expressions are compared by the caller's existing AST policy.
"""
from __future__ import annotations

import re
from typing import Callable

MAX_CLAIM_CHARS = 4000
MAX_QUOTE_CHARS = 4096
MAX_LABELS_PER_QUOTE = 32
MAX_FORMULA_DEPTH = 64

_ALPHA_NUMBER = r"(?<!\w)Alpha\s*#\s*([0-9]{1,6})(?![0-9])"
_TARGET = re.compile(_ALPHA_NUMBER, re.IGNORECASE)
_LABEL = re.compile(_ALPHA_NUMBER + r"\s*:", re.IGNORECASE)
# Besides quote end or the next explicit Alpha label, the observed source has
# exactly this section boundary. No arbitrary prose/punctuation suffix is used.
_CONTEXT_HEADING = re.compile(r"A\.1\.\s+Functions\s+and\s+Operators\b")


def claim_alpha_number(claim: str) -> int | None:
    """Return one explicit Alpha target or fail closed on ambiguous claims."""
    if len(claim) > MAX_CLAIM_CHARS:
        raise ValueError("Hypothesis exceeds labelled-formula provenance length limit")
    targets = {int(match.group(1)) for match in _TARGET.finditer(claim)}
    if 0 in targets or len(targets) > 1:
        raise ValueError("Ambiguous or invalid Alpha number in hypothesis claim")
    return next(iter(targets)) if targets else None


def _complete_formula(quote: str, start: int, end: int) -> str:
    while start < end and quote[start].isspace():
        start += 1
    if start >= end or quote[start] != "(":
        raise ValueError("Labelled Alpha formula must start with complete parentheses")
    depth = 0
    for position in range(start, end):
        char = quote[position]
        if char == "(":
            depth += 1
            if depth > MAX_FORMULA_DEPTH:
                raise ValueError("Labelled Alpha formula exceeds provenance depth limit")
        elif char == ")":
            depth -= 1
            if depth == 0:
                trailer = quote[position + 1:end].lstrip()
                if trailer and not _CONTEXT_HEADING.match(trailer):
                    raise ValueError("Labelled Alpha formula has an unsupported boundary or expression continuation")
                return quote[start:position + 1]
    raise ValueError("Labelled Alpha formula is truncated or unbalanced")


def labelled_alpha_tree(
    quotes: list[str], target: int | None, formula_tree: Callable[[str], str]
) -> str | None:
    """Get one complete target formula; conflicting occurrences are refused.

    Other Alpha labels bound the source span but cannot supply the selected
    hypothesis's formula. Bare prose/anonymous embedded expressions are never
    searched for matching substrings or descendant AST nodes.
    """
    if target is None:
        return None
    trees: set[str] = set()
    for quote in quotes:
        if len(quote) > MAX_QUOTE_CHARS:
            raise ValueError("Citation exceeds labelled-formula provenance length limit")
        labels = list(_LABEL.finditer(quote))
        if len(labels) > MAX_LABELS_PER_QUOTE:
            raise ValueError("Citation exceeds labelled-formula provenance label limit")
        for index, label in enumerate(labels):
            if int(label.group(1)) != target:
                continue
            end = labels[index + 1].start() if index + 1 < len(labels) else len(quote)
            formula = _complete_formula(quote, label.end(), end)
            try:
                trees.add(formula_tree(formula))
            except (SyntaxError, ValueError) as exc:
                raise ValueError("Labelled Alpha formula is not a complete supported expression") from exc
    if len(trees) > 1:
        raise ValueError("Conflicting formulas for the same cited Alpha number")
    return next(iter(trees)) if trees else None
