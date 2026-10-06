"""Isolated, timed tool process. Receives JSON; never executes supplied Python."""
from __future__ import annotations

import sys
from pathlib import Path

from .contracts import Task
from .evidence import match_evidence, verify_paper
from .storage import atomic_json, read_json


def execute(job):
    from .evaluation import evaluate, load_market, validate_config

    inputs = Path(job["inputs"])
    task = Task.parse(read_json(inputs / "task.json"))
    panels = load_market(inputs / "market.csv", inputs / "metadata.json")
    metadata = read_json(inputs / "metadata.json")
    config = validate_config(task.raw["evaluation"], metadata["calendar_dates"])
    if job["action"] == "preflight":
        paper = read_json(inputs / "paper.json")
        verify_paper(paper, inputs / "paper.pdf")
        return {"evidence": [match_evidence(e, paper) for e in task.evidence],
                "available_fields": sorted(panels), "evaluation": config,
                "data_kind": metadata["data_kind"], "data_version": metadata["version"]}
    if job["action"] != "compute_evaluate":
        raise ValueError("Unknown tool action")
    from .expressions import compute_expression
    # Future test data never enters the expression engine. Prior history is kept
    # for trailing windows. Evaluation only has access to validation labels.
    cutoff = config["splits"]["validation"]["end"]
    history = {name: panel.loc[:cutoff].copy() for name, panel in panels.items()}
    factor = compute_expression(job["expression"], history)
    diagnostics = dict(factor.attrs.get("expression_diagnostics", {}))
    output = Path(job["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    factor.to_csv(output / "factor.csv", index_label="date", float_format="%.17g")
    # Reindex only pads held-out rows with NaN; it never computes their signals.
    full_factor = factor.reindex(panels["open"].index)
    result = evaluate(full_factor, panels, config, split="validation")
    result["expression_diagnostics"] = diagnostics
    result["expression"] = job["expression"]
    atomic_json(output / "result.json", result)
    return result


def main():
    job_path, response_path = map(Path, sys.argv[1:3])
    try:
        result = {"ok": True, "value": execute(read_json(job_path))}
    except Exception as exc:
        result = {"ok": False, "error": {"type": type(exc).__name__,
                  "code": getattr(exc, "code", "tool_error"), "message": str(exc)}}
    atomic_json(response_path, result)


if __name__ == "__main__":
    main()
