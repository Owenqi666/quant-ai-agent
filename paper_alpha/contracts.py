"""Small explicit JSON contracts; no executable code is accepted in task files."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value),
            f"Invalid identifier: {value!r}")
    return value


def nonempty(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"{field} must be nonempty text")
    return value


@dataclass(frozen=True)
class Evidence:
    id: str
    page: int
    quote: str

    def __post_init__(self):
        identifier(self.id)
        require(type(self.page) is int and self.page > 0, "page must be a positive integer")
        nonempty(self.quote, "quote")


@dataclass(frozen=True)
class Hypothesis:
    id: str
    claim: str
    attribution: str
    evidence_ids: list[str]
    economic_mechanism: str
    mechanism_attribution: str
    signal_direction: str
    required_fields: list[str]
    assumptions: list[str]

    def __post_init__(self):
        identifier(self.id)
        for key in ("claim", "economic_mechanism", "signal_direction"):
            nonempty(getattr(self, key), key)
        for key in ("attribution", "mechanism_attribution"):
            require(getattr(self, key) in {"paper_original", "user_modification", "model_conjecture"},
                    f"Invalid {key}")
        for key in ("evidence_ids", "required_fields", "assumptions"):
            value = getattr(self, key)
            require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value),
                    f"{key} must be a text list")
        require(bool(self.evidence_ids), "Hypothesis requires evidence")


@dataclass(frozen=True)
class Candidate:
    id: str
    hypothesis_id: str
    expression: str
    origin: str
    changes: list[str]

    def __post_init__(self):
        identifier(self.id)
        identifier(self.hypothesis_id)
        nonempty(self.expression, "expression")
        require(self.origin in {"paper_original", "user_modification", "model_conjecture"}, "Invalid candidate origin")
        require(isinstance(self.changes, list) and all(isinstance(v, str) and v for v in self.changes),
                "changes must be a text list")
        if self.origin != "paper_original":
            require(bool(self.changes), "Modified or conjectured candidates must describe changes")
        else:
            require(not self.changes, "A changed implementation must not be labelled paper_original")


@dataclass(frozen=True)
class Budget:
    max_candidates: int = 4
    max_attempts_per_candidate: int = 2
    max_tool_calls: int = 24
    max_seconds: float = 60

    def __post_init__(self):
        for key, ceiling in (("max_candidates", 20), ("max_attempts_per_candidate", 5), ("max_tool_calls", 200)):
            value = getattr(self, key)
            require(type(value) is int and 1 <= value <= ceiling, f"Invalid budget {key}")
        require(type(self.max_seconds) in (float, int) and math.isfinite(self.max_seconds)
                and 0 < self.max_seconds <= 600, "Invalid max_seconds")


@dataclass
class Task:
    raw: dict
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    candidates: list[Candidate]
    budget: Budget

    @classmethod
    def parse(cls, raw):
        require(isinstance(raw, dict) and raw.get("schema_version") == 1, "Unsupported task schema")
        allowed = {"schema_version", "title", "paper", "paper_pdf", "data", "data_metadata",
                   "evidence", "hypotheses", "candidates", "evaluation", "budget"}
        require(not set(raw) - allowed, f"Unknown task keys: {set(raw) - allowed}")
        for key in ("title", "paper", "paper_pdf", "data", "data_metadata"):
            nonempty(raw.get(key), key)
        try:
            evidence = [Evidence(**x) for x in raw["evidence"]]
            hypotheses = [Hypothesis(**x) for x in raw["hypotheses"]]
            candidates = [Candidate(**x) for x in raw["candidates"]]
            budget = Budget(**raw.get("budget", {}))
        except (TypeError, KeyError) as e:
            raise ValueError(f"Invalid task fields: {e}") from e
        for name, objects in (("evidence", evidence), ("hypotheses", hypotheses), ("candidates", candidates)):
            require(0 < len(objects) <= 100, f"{name} must contain 1..100 objects")
            require(len({x.id for x in objects}) == len(objects), f"Duplicate {name} id")
        evidence_ids, hypothesis_ids = {x.id for x in evidence}, {x.id for x in hypotheses}
        for hypothesis in hypotheses:
            require(set(hypothesis.evidence_ids) <= evidence_ids, "Unknown evidence reference")
        for candidate in candidates:
            require(candidate.hypothesis_id in hypothesis_ids, "Unknown hypothesis reference")
        require(isinstance(raw.get("evaluation"), dict), "evaluation must be an object")
        return cls(raw, evidence, hypotheses, candidates, budget)

    def to_dict(self):
        return {**self.raw, "evidence": [asdict(x) for x in self.evidence],
                "hypotheses": [asdict(x) for x in self.hypotheses],
                "candidates": [asdict(x) for x in self.candidates], "budget": asdict(self.budget)}
