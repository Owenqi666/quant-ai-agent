"""Measured regression benchmark for a fixed synthetic fixture, not alpha quality."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time

from .contracts import Task, require
from .evidence import sha256
from .storage import atomic_json, digest, read_json
from .workflow import INPUT_NAMES, run_task, verify_run


VALID_EXECUTABLE_IDS = ("alpha006", "alpha101", "canonical_corr")
# Reviewed fixture identities declared before execution, not inferred from
# coincidentally equal metrics. This is not a general expression equivalence solver.
SEMANTIC_GROUPS = {"alpha006": ("alpha006", "canonical_corr"), "alpha101": ("alpha101",)}
_EXPECTED_AGENT = {
    "alpha006": "evaluated", "alpha101": "evaluated", "alpha005": "blocked",
    "canonical_corr": "evaluated", "unsafe_expr": "rejected", "negative_delay": "rejected",
    "changed_original": "rejected", "zero_signal": "not_evaluable",
}
_EXPECTED_FIXED = {**_EXPECTED_AGENT, "alpha006": "rejected"}


def build_cases(raw: dict) -> dict:
    """Declare cases and denominators before observing any run results."""
    Task.parse(raw)
    suite = deepcopy(raw)
    candidates = {item["id"]: item for item in suite["candidates"]}
    require(set(candidates) == {"alpha006", "alpha101", "alpha005"},
            "Benchmark requires exactly the reviewed alpha006, alpha101 and alpha005 example candidates")
    hypotheses = {item["id"]: item for item in suite["hypotheses"]}
    require("correlation(" in candidates["alpha006"]["expression"],
            "Benchmark alpha006 must retain the paper correlation alias")
    require("vwap" in hypotheses[candidates["alpha005"]["hypothesis_id"]]["required_fields"],
            "Benchmark alpha005 must declare required vwap")
    suite["title"] = "Fixed synthetic robustness benchmark; personal study unconfirmed"
    suite["budget"] = {"max_candidates": 10, "max_attempts_per_candidate": 2,
                       "max_tool_calls": 60, "max_seconds": 120}
    canonical = deepcopy(candidates["alpha006"])
    canonical.update(id="canonical_corr", expression="-1 * ts_corr(open, volume, 10)")
    suite["candidates"].append(canonical)
    descriptions = {
        "alpha006": "Paper operator alias requires a documented semantic-preserving repair.",
        "alpha101": "Unmodified executable formula control.",
        "alpha005": "Required vwap is absent; stop without substituting another field.",
        "canonical_corr": "Manually normalized alias control; same formula as alpha006, not an independent alpha.",
        "unsafe_expr": "Reject arbitrary Python function calls without executing them.",
        "negative_delay": "Reject a negative delay that would read a future observation.",
        "changed_original": "Reject a changed lookback falsely attributed to the original formula.",
        "zero_signal": "Compute a constant panel but report not_evaluable rather than inventing an IC.",
    }
    for case_id, expression in (
        ("unsafe_expr", "__import__('os')"),
        ("negative_delay", "delay(close, -1)"),
        ("zero_signal", "close - close"),
    ):
        hypothesis = deepcopy(hypotheses[candidates["alpha101"]["hypothesis_id"]])
        hypothesis.update(
            id=f"h-{case_id}", claim=f"Engineering robustness fixture: {descriptions[case_id]}",
            attribution="model_conjecture", mechanism_attribution="model_conjecture",
            economic_mechanism="No economic mechanism is claimed; this is a deliberately constructed software test.",
            signal_direction="No investment direction is claimed for this robustness test.",
            required_fields=["close"],
        )
        hypothesis["assumptions"].append("Deliberate benchmark mutation, not a formula asserted by the cited paper.")
        suite["hypotheses"].append(hypothesis)
        suite["candidates"].append({
            "id": case_id, "hypothesis_id": hypothesis["id"], "expression": expression,
            "origin": "model_conjecture", "changes": [descriptions[case_id]],
        })
    changed = deepcopy(candidates["alpha006"])
    changed.update(id="changed_original", expression="(-1 * correlation(open, volume, 11))")
    suite["candidates"].append(changed)
    wrong = deepcopy(raw)
    wrong["title"] = "Deliberately incorrect citation preflight case"
    wrong["budget"] = deepcopy(suite["budget"])
    wrong["candidates"] = [deepcopy(candidates["alpha101"])]
    reference = candidates["alpha101"]["hypothesis_id"]
    wrong["hypotheses"] = [deepcopy(hypotheses[reference])]
    evidence_ids = set(wrong["hypotheses"][0]["evidence_ids"])
    wrong["evidence"] = [deepcopy(e) for e in raw["evidence"] if e["id"] in evidence_ids]
    for item in wrong["evidence"]:
        item["quote"] = "DELIBERATELY INCORRECT BENCHMARK QUOTE: this is not a statement in the source."
    return {
        "suite": Task.parse(suite).to_dict(),
        "wrong_quote": Task.parse(wrong).to_dict(),
        "definitions": descriptions,
        "expectations": {
            "fixed": {"run_status": "completed", "candidates": deepcopy(_EXPECTED_FIXED)},
            "normalized_fixed": {"run_status": "completed", "candidates": deepcopy(_EXPECTED_AGENT)},
            "agent": {"run_status": "completed", "candidates": deepcopy(_EXPECTED_AGENT)},
            "wrong_quote": {"run_status": "failed", "candidates": {"alpha101": "blocked"}},
        },
        "valid_executable_candidate_ids": list(VALID_EXECUTABLE_IDS),
        "semantic_groups": deepcopy(SEMANTIC_GROUPS),
        "semantic_denominator_definition": (
            "Two predeclared formula meanings: alpha006 (represented by alpha006 and canonical_corr) and alpha101. "
            "A group completes when at least one of its representations evaluates. These reviewed fixture groups "
            "do not establish independent economic signals or equivalence for arbitrary expressions."
        ),
        "denominator_definition": (
            "Three predeclared formula representations with available data and expected nonconstant evaluation: "
            "alpha006, alpha101 and canonical_corr. canonical_corr duplicates alpha006 semantics; "
            "this is executable representation yield, not the number of independent alphas. "
            "Missing-field, unsafe, future-reading, misattributed and constant-panel negative controls are excluded."
        ),
    }


def _run_summary(state, expected, elapsed, directory, verification, eligible_ids=(), semantic_groups=None):
    statuses = {item["id"]: item["status"] for item in state["candidates"]}
    checks = [{"case": "run_status", "expected": expected["run_status"],
               "actual": state["status"], "passed": state["status"] == expected["run_status"]}]
    checks += [{"case": case, "expected": wanted, "actual": statuses.get(case),
                "passed": statuses.get(case) == wanted} for case, wanted in expected["candidates"].items()]
    checks.append({"case": "exact_case_inventory", "expected": sorted(expected["candidates"]),
                   "actual": sorted(statuses), "passed": set(statuses) == set(expected["candidates"])})
    matched = sum(check["passed"] for check in checks)
    eligible = list(eligible_ids)
    completed = [case for case in eligible if statuses.get(case) == "evaluated"]
    groups = semantic_groups or {}
    completed_groups = [group for group, members in groups.items()
                        if any(statuses.get(case) == "evaluated" for case in members)]
    return {
        "run_dir": str(directory), "status": state["status"], "candidate_statuses": statuses,
        "run_seconds": elapsed, "workflow_seconds": state["elapsed_seconds"],
        "tool_calls": state["tool_calls"], "llm_calls": 0, "external_cost_usd": 0,
        "cost_scope": "No external paid services were called; local CPU/electricity costs are not measured.",
        "valid_executable_yield": {"completed": len(completed), "denominator": len(eligible),
                                   "rate": len(completed) / len(eligible) if eligible else None,
                                   "eligible_ids": eligible, "completed_ids": completed},
        "semantic_group_yield": {"completed": len(completed_groups), "denominator": len(groups),
                                 "rate": len(completed_groups) / len(groups) if groups else None,
                                 "groups": {key: list(members) for key, members in groups.items()},
                                 "completed_ids": completed_groups},
        "correct_handling": {"passed": matched, "total": len(checks), "rate": matched / len(checks),
                             "checks": checks},
        "metrics": {c["id"]: c["result"]["metrics"] for c in state["candidates"] if c.get("result")},
        "artifact_verification": verification,
    }


def _compare(left, left_id, right, right_id):
    left_case = next((c for c in left["candidates"] if c["id"] == left_id), {})
    right_case = next((c for c in right["candidates"] if c["id"] == right_id), {})
    lresult, rresult = left_case.get("result", {}), right_case.get("result", {})
    present = bool(lresult and rresult)
    return {"left_candidate": left_id, "right_candidate": right_id,
            "both_results_present": present,
            "expression_equal": present and lresult.get("expression") == rresult.get("expression"),
            "metrics_equal": present and lresult.get("metrics") == rresult.get("metrics"),
            "daily_equal": present and lresult.get("daily") == rresult.get("daily"),
            "full_result_equal": present and lresult == rresult,
            "left_metrics_sha256": digest(lresult["metrics"]) if present else None,
            "right_metrics_sha256": digest(rresult["metrics"]) if present else None}


def _comparison_passes(check):
    return all(check[key] for key in ("both_results_present", "expression_equal", "metrics_equal", "daily_equal", "full_result_equal"))


def _report(summary):
    lines = [
        "# Fixed-fixture policy benchmark", "",
        "This is a measured software regression comparison on one synthetic fixture. "
        "The agent policy performs bounded alias repair; it uses no LLM. "
        "normalized_fixed applies the identical alias transform once upfront with no retry.", "",
        summary["denominator_definition"], "",
        summary["semantic_denominator_definition"], "",
        "| Run | Wall seconds | Tool calls | Representation yield | Formula groups | Expected handling checks |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, run in summary["runs"].items():
        yield_, correct = run["valid_executable_yield"], run["correct_handling"]
        yield_text = f"{yield_['completed']}/{yield_['denominator']}" if yield_["denominator"] else "not applicable"
        groups = run["semantic_group_yield"]
        groups_text = f"{groups['completed']}/{groups['denominator']}" if groups["denominator"] else "not applicable"
        lines.append(f"| {name} | {run['run_seconds']:.6f} | {run['tool_calls']} | {yield_text} | "
                     f"{groups_text} | {correct['passed']}/{correct['total']} |")
    lines += ["", "Expected handling is checked against declarations written before execution. "
              "Fixed-mode rejection of the alias is expected, while its smaller valid evaluation yield remains visible.",
              "", "| Candidate | Fixed | Normalized fixed | Agent | Agent expected |", "|---|---|---|---|---|"]
    for case, wanted in _EXPECTED_AGENT.items():
        lines.append(f"| {case} | {summary['runs']['fixed']['candidate_statuses'].get(case)} | "
                     f"{summary['runs']['normalized_fixed']['candidate_statuses'].get(case)} | "
                     f"{summary['runs']['agent']['candidate_statuses'].get(case)} | {wanted} |")
    checks = summary["consistency"]
    lines += ["", f"All predefined checks passed: **{summary['all_checks_passed']}**.", "",
              f"Shared-expression and repaired-alias comparisons passed: **{checks['cross_policy_passed']}**. "
              f"Normalized fixed and agent statuses and complete results agree: **{checks['normalized_baseline_passed']}**. "
              f"Fresh agent rerun reproduced statuses, metrics and daily records: **{checks['fresh_repeat_passed']}**.",
              "", f"All {summary['run_count']} runs retain input/code snapshots, state, attempts, reports and result artifacts. "
              "JSON summary includes exact metrics and their hashes; full daily rows remain in each run.",
              "", "Tool counts include one batch alias-normalization call for normalized_fixed and each reactive repair "
              "call for agent. Wall time includes real local execution but is not a controlled performance study. "
              "Shared outcomes in this fixture do not demonstrate a research advantage for reactive repair over upfront normalization.",
              "", "LLM calls: 0. External API cost: USD 0; local compute cost is unmeasured. "
              "Human baseline time, semantic fidelity and economic validity are not measured. "
              "This selected fixture does not establish statistical generalization, profitability, "
              "real-world time savings or LLM benefits. The final test interval remains reserved.", ""]
    return "\n".join(lines)


def benchmark(task_path: Path, output_dir: Path) -> dict:
    task_path, output_dir = Path(task_path).resolve(), Path(output_dir).resolve()
    raw = read_json(task_path)
    # Written benchmark tasks retain explicit resolved references to the input fixture.
    for key in INPUT_NAMES:
        raw[key] = str((task_path.parent / raw[key]).resolve())
    metadata = read_json(raw["data_metadata"])
    require(metadata.get("data_kind") == "synthetic", "This benchmark is restricted to the declared synthetic fixture")
    cases = build_cases(raw)
    require(not output_dir.exists(), "Benchmark output already exists; preserve it and use a fresh directory")
    output_dir.mkdir(parents=True)
    task_dir = output_dir / "tasks"
    atomic_json(task_dir / "suite.json", cases["suite"])
    atomic_json(task_dir / "wrong_quote.json", cases["wrong_quote"])
    declarations = {key: value for key, value in cases.items() if key not in {"suite", "wrong_quote"}}
    atomic_json(output_dir / "case_definitions.json", declarations)
    plans = (("fixed", "suite", "fixed"), ("normalized_fixed", "suite", "normalized_fixed"), ("agent", "suite", "agent"),
             ("agent_repeat", "suite", "agent"), ("wrong_quote_fixed", "wrong_quote", "fixed"),
             ("wrong_quote_normalized_fixed", "wrong_quote", "normalized_fixed"),
             ("wrong_quote_agent", "wrong_quote", "agent"))
    states, runs = {}, {}
    for name, case_name, mode in plans:
        run_dir = output_dir / "runs" / name
        started = time.perf_counter()
        state = run_task(task_dir / f"{case_name}.json", run_dir, mode=mode)
        elapsed = time.perf_counter() - started
        verification = verify_run(run_dir)
        expectation = cases["expectations"][mode if case_name == "suite" else "wrong_quote"]
        states[name] = state
        runs[name] = _run_summary(state, expectation, elapsed, run_dir, verification,
                                 VALID_EXECUTABLE_IDS if case_name == "suite" else (),
                                 SEMANTIC_GROUPS if case_name == "suite" else None)
        # Preserve partial benchmark progress if a later run is interrupted.
        atomic_json(output_dir / "progress.json", {"completed_runs": runs, "planned_runs": len(plans)})
    cross = [_compare(states["fixed"], case, states["agent"], case)
             for case in ("alpha101", "canonical_corr")]
    cross.append(_compare(states["fixed"], "canonical_corr", states["agent"], "alpha006"))
    normalized = [_compare(states["normalized_fixed"], case, states["agent"], case)
                  for case in (*VALID_EXECUTABLE_IDS, "zero_signal")]
    normalized_statuses = runs["normalized_fixed"]["candidate_statuses"] == runs["agent"]["candidate_statuses"]
    repeated = [_compare(states["agent"], case, states["agent_repeat"], case)
                for case in (*VALID_EXECUTABLE_IDS, "zero_signal")]
    repeat_statuses = runs["agent"]["candidate_statuses"] == runs["agent_repeat"]["candidate_statuses"]
    consistency = {"cross_policy": cross, "fresh_repeat": repeated,
                   "normalized_baseline": normalized,
                   "normalized_baseline_passed": normalized_statuses and all(_comparison_passes(c) for c in normalized),
                   "normalized_baseline_statuses_equal": normalized_statuses,
                   "cross_policy_passed": all(_comparison_passes(c) for c in cross),
                   "fresh_repeat_passed": repeat_statuses and all(_comparison_passes(c) for c in repeated),
                   "fresh_repeat_statuses_equal": repeat_statuses}
    summary = {
        "schema_version": 2, "scope": "fixed synthetic engineering fixture; not statistical or financial evidence",
        "source_task_sha256": sha256(task_path), "data_kind": metadata["data_kind"],
        "data_version": metadata["version"], "data_sha256": metadata["market_sha256"],
        "run_count": len(plans), "denominator_definition": cases["denominator_definition"],
        "semantic_denominator_definition": cases["semantic_denominator_definition"],
        "runs": runs, "consistency": consistency,
        "unmeasured": ["human_baseline_time", "semantic_fidelity", "economic_validity", "LLM_benefit", "local_compute_cost"],
        "all_checks_passed": all(r["correct_handling"]["passed"] == r["correct_handling"]["total"]
                                 and r["artifact_verification"]["verified"] for r in runs.values())
                             and consistency["cross_policy_passed"] and consistency["fresh_repeat_passed"]
                             and consistency["normalized_baseline_passed"],
    }
    atomic_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(_report(summary), encoding="utf-8")
    return summary
