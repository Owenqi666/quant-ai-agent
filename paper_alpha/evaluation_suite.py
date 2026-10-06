"""Versioned development task suites over the existing deterministic workflow.

Reserved research tasks are inventoried, never executed by this entry point.
Market train/validation/test bounds remain the engine's separate contract.
Expectations, denominators and source hashes are frozen before the first run.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import statistics
import time

from .contracts import Task, require
from .evidence import sha256
from .server.regression import ORACLE_VERSION, oracle_support, verify_candidate
from .storage import atomic_json, digest, read_json
from .workflow import INPUT_NAMES, code_files, environment, run_task, verify_run


MODES = ("fixed", "normalized_fixed", "agent")
STATUSES = {"evaluated", "not_evaluable", "blocked", "failed", "rejected", "budget_stopped"}
SUITE_VERSION = "development-suite-v1"
MAX_RUNS = 60
MAX_SUITE_SECONDS = 3600


def ratio(passed, eligible):
    eligible, passed = sorted(set(eligible)), sorted(set(passed) & set(eligible))
    return {"numerator": len(passed), "denominator": len(eligible),
            "rate": len(passed) / len(eligible) if eligible else None,
            "passed_ids": passed, "eligible_ids": eligible}


def _bounded_json(path, limit=2 * 1024 * 1024):
    require(path.is_file() and not path.is_symlink(), f"Missing or symlinked input: {path.name}")
    require(path.stat().st_size <= limit, f"Input exceeds {limit} bytes: {path.name}")
    return read_json(path)


def _identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value), "Invalid suite/task identifier")


def _task_inputs(manifest_path, entry):
    require(isinstance(entry["task"], str) and entry["task"].strip(), "Task file must be a nonempty local path")
    require(isinstance(entry["input_sha256"], dict), "Input hashes must be an object")
    path = (manifest_path.parent / entry["task"]).resolve()
    raw = _bounded_json(path)
    Task.parse(raw)
    paths = {"task": path, **{key: (path.parent / raw[key]).resolve() for key in INPUT_NAMES}}
    require(set(entry["input_sha256"]) == set(paths), "Every task and input requires a frozen SHA256")
    for key, source in paths.items():
        require(source.is_file() and not source.is_symlink() and source.stat().st_size <= 64 * 1024 * 1024,
                f"Missing or unbounded task input: {key}")
        require(sha256(source) == entry["input_sha256"][key], f"Frozen task input digest mismatch: {entry['id']}/{key}")
    require(_bounded_json(paths["data_metadata"]).get("data_kind") == "synthetic", "Suite v1 accepts synthetic engineering inputs only")
    return raw, paths


def load_manifest(manifest_path: Path) -> dict:
    """Validate declarations and hashes without executing any research task."""
    path = Path(manifest_path).resolve()
    value = _bounded_json(path)
    require(isinstance(value, dict) and value.get("schema_version") == 1, "Unsupported evaluation-suite schema")
    require(set(value) == {"schema_version", "suite_id", "version", "scope", "repetitions", "tasks"}, "Unknown/missing suite fields")
    _identifier(value["suite_id"])
    require(isinstance(value["version"], str) and value["version"].strip(), "Suite version is required")
    require(value["scope"] == "synthetic_engineering", "Real research generalization is not measured by this suite")
    require(type(value["repetitions"]) is int and 1 <= value["repetitions"] <= 3, "Suite repetitions must be 1..3")
    require(isinstance(value["tasks"], list) and 1 <= len(value["tasks"]) <= 20, "Suite requires 1..20 task declarations")
    ids, groups = set(), {"development": set(), "reserved": set()}
    papers = {"development": set(), "reserved": set()}
    runs, total_seconds = 0, 0
    for entry in value["tasks"]:
        require(isinstance(entry, dict), "Task declaration must be an object")
        _identifier(entry.get("id"))
        require(entry["id"] not in ids, "Duplicate evaluation task id")
        ids.add(entry["id"])
        require(entry.get("partition") in groups, "Task partition must be development or reserved")
        require(entry.get("status") in {"ready", "pending_annotation"}, "Task status must be ready or pending_annotation")
        require(entry.get("label_source") in {"human", "automation", "imported", "legacy_unknown"}, "Explicit label source is required")
        require(isinstance(entry.get("label_note"), str) and entry["label_note"].strip(), "Label provenance note is required")
        require(entry.get("human_fidelity") == "unlabelled", "This engineering suite does not accept inferred human semantic labels")
        require(isinstance(entry.get("family_ids"), list) and all(isinstance(g, str) and g for g in entry["family_ids"]), "Formula/lineage families must be explicit")
        groups[entry["partition"]].update(entry["family_ids"])
        common = {"id", "partition", "status", "label_source", "label_note", "human_fidelity", "family_ids"}
        if entry["status"] == "pending_annotation":
            require(set(entry) == common | {"pending_reason"} and bool(entry["pending_reason"]), "Pending task needs a reason and must not invent executable inputs")
            continue
        require(set(entry) == common | {"task", "input_sha256", "expected", "executable_candidate_ids", "numerical_candidate_ids", "formula_groups", "oracle_expectations", "evidence_ids"}, "Unknown/missing ready-task fields")
        raw, _ = _task_inputs(path, entry)
        papers[entry["partition"]].add(entry["input_sha256"]["paper_pdf"])
        require(entry["evidence_ids"] == [item["id"] for item in raw["evidence"]], "Citation denominator differs from frozen task evidence")
        candidates = {item["id"] for item in raw["candidates"]}
        for key in ("executable_candidate_ids", "numerical_candidate_ids"):
            require(isinstance(entry[key], list) and len(set(entry[key])) == len(entry[key]) and set(entry[key]) <= candidates,
                    f"{key} must be unique declared candidate ids")
        require(set(entry["executable_candidate_ids"]) <= set(entry["numerical_candidate_ids"]), "Every executable positive requires numerical verification")
        require(isinstance(entry["formula_groups"], dict) and set(entry["formula_groups"]) == candidates
                and all(isinstance(group, str) for group in entry["formula_groups"].values())
                and set(entry["formula_groups"].values()) <= set(entry["family_ids"]), "All candidates need predeclared formula/lineage groups")
        require(isinstance(entry["oracle_expectations"], dict) and set(entry["oracle_expectations"]) == set(entry["numerical_candidate_ids"])
                and set(entry["oracle_expectations"].values()) <= {"supported", "unsupported"}, "All numerical candidates need frozen reference-coverage expectations")
        require(isinstance(entry["expected"], dict) and set(entry["expected"]) == set(MODES), "All three policies require expected outcomes")
        for mode in MODES:
            expected = entry["expected"][mode]
            require(isinstance(expected, dict) and set(expected) == {"run_status", "candidates"} and expected["run_status"] in {"completed", "failed", "budget_exhausted"}, "Invalid expected task result")
            require(isinstance(expected["candidates"], dict) and set(expected["candidates"]) == candidates
                    and set(expected["candidates"].values()) <= STATUSES, "Every candidate requires an explicit expected terminal state")
        if entry["partition"] == "development":
            runs += len(MODES) * value["repetitions"]
            total_seconds += Task.parse(raw).budget.max_seconds * len(MODES) * value["repetitions"]
    require(not groups["development"] & groups["reserved"], "Development and reserved tasks share a formula/lineage family")
    require(not papers["development"] & papers["reserved"], "Development and reserved tasks share the same paper")
    require(runs <= MAX_RUNS and total_seconds <= MAX_SUITE_SECONDS, "Suite exceeds aggregate run/time budget")
    return deepcopy(value)


def _source_hashes():
    return {name: sha256(path) for name, path in code_files().items()}


def _projection(state, root):
    return {"status": state["status"], "candidates": [
        {"id": candidate["id"], "status": candidate["status"], "result": candidate.get("result"),
         "factor_hashes": [sha256(root / name) for name in sorted(candidate.get("artifacts", {})) if name.endswith("/factor.csv")]}
        for candidate in state["candidates"]]}


def _candidate_record(candidate, expected, record_id, entry, root, raw, verified):
    identity = f"{record_id}/{candidate['id']}"
    result = {"id": identity, "candidate_id": candidate["id"], "actual_status": candidate["status"],
              "expected_status": expected, "status_passed": candidate["status"] == expected,
              "executable_positive": candidate["id"] in entry["executable_candidate_ids"],
              "numerical_required": candidate["id"] in entry["numerical_candidate_ids"],
              "formula_group": entry["formula_groups"][candidate["id"]],
              "oracle_support": oracle_support(candidate["expression"]), "checks": []}
    if verified and candidate.get("result"):
        result["checks"] = verify_candidate(root, candidate["id"], raw)["checks"]
    elif result["numerical_required"]:
        result["checks"] = [{"name": "numerical_oracle", "outcome": "not_comparable",
                             "reason_code": "no_verified_numerical_result", "reason": "No verified numerical output was produced; retained in the denominator."}]
    result["numerical_executed"] = any(item["name"] == "factor_values" for item in result["checks"])
    result["numerical_passed"] = (result["numerical_executed"] and
                                  all(item["outcome"] == "passed" for item in result["checks"] if item["name"] != "evidence_provenance"))
    result["fully_verified"] = (verified and candidate["status"] == "evaluated" and result["numerical_passed"]
                                and any(item["name"] == "evidence_provenance" and item["outcome"] == "passed" for item in result["checks"]))
    return result


def _handling_passed(entry, mode, state, candidates, verified):
    expected = entry["expected"][mode]
    correct = (state["status"] == expected["run_status"] and
               {item["id"] for item in state["candidates"]} == set(expected["candidates"]) and
               all(item["status_passed"] for item in candidates))
    correct = bool(correct and verified and not any(check["outcome"] == "failed" for item in candidates for check in item["checks"]))
    for item in candidates:
        coverage = entry["oracle_expectations"].get(item["candidate_id"])
        if item["actual_status"] in {"evaluated", "not_evaluable"} and coverage:
            matches = (item["numerical_executed"] and item["numerical_passed"]) if coverage == "supported" else not item["oracle_support"]["supported"]
            correct &= matches
    return correct


def summarize(manifest: dict, records: list[dict]) -> dict:
    """Recompute denominators from declarations; missing runs cannot disappear."""
    expected_ids = {f"{entry['id']}/{mode}/{repeat}": (entry, mode, repeat)
                    for entry in manifest["tasks"] if entry["partition"] == "development" and entry["status"] == "ready"
                    for mode in MODES for repeat in range(1, manifest["repetitions"] + 1)}
    indexed = {record["id"]: record for record in records}
    require(len(indexed) == len(records) and set(indexed) <= set(expected_ids), "Duplicate or unexpected suite execution record")
    by_mode = {}
    for mode in MODES:
        eligible_runs = [key for key, (_, policy, _) in expected_ids.items() if policy == mode]
        positive, numerical, negative, citations = [], [], [], []
        evaluated, full, executed, numeric_passed, negative_passed, citations_passed = [], [], [], [], [], []
        handling, reports, recorded = [], [], []
        reasons = Counter()
        times, calls = [], []
        for run_id in eligible_runs:
            entry = expected_ids[run_id][0]
            record = indexed.get(run_id, {})
            if record.get("execution_status") == "completed":
                recorded.append(run_id)
            if record.get("handling_passed"):
                handling.append(run_id)
            if record.get("artifact_verification", {}).get("verified"):
                reports.append(run_id)
            observed = {item["candidate_id"]: item for item in record.get("candidates", [])}
            for candidate_id in entry["expected"][mode]["candidates"]:
                cid = f"{run_id}/{candidate_id}"
                actual = observed.get(candidate_id, {})
                if candidate_id in entry["executable_candidate_ids"]:
                    positive.append(cid)
                    if actual.get("actual_status") == "evaluated":
                        evaluated.append(cid)
                    if actual.get("fully_verified"):
                        full.append(cid)
                else:
                    negative.append(cid)
                    if actual.get("status_passed"):
                        negative_passed.append(cid)
                if candidate_id in entry["numerical_candidate_ids"]:
                    numerical.append(cid)
                    if actual.get("numerical_executed"):
                        executed.append(cid)
                    if actual.get("numerical_passed"):
                        numeric_passed.append(cid)
                    if not actual:
                        reasons["not_run"] += 1
                    for check in actual.get("checks", []):
                        if check["outcome"] != "passed":
                            reasons[check.get("reason_code", "unclassified_check")] += 1
            for citation in record.get("citations", []):
                citations.append(citation["id"])
                if citation["outcome"] == "passed":
                    citations_passed.append(citation["id"])
            # The frozen per-task evidence inventory survives absent run records.
            if not record.get("citations"):
                citations.extend(f"{run_id}/{eid}" for eid in entry.get("evidence_ids", []))
            if record.get("execution_seconds") is not None:
                times.append(record["execution_seconds"])
            if record.get("tool_calls") is not None:
                calls.append(record["tool_calls"])
        by_mode[mode] = {"task_handling": ratio(handling, eligible_runs), "recorded_runs": ratio(recorded, eligible_runs),
                         "positive_execution": ratio(evaluated, positive), "negative_handling": ratio(negative_passed, negative),
                         "numerical_coverage": ratio(executed, numerical), "numerical_agreement": ratio(numeric_passed, executed),
                         "fully_verified_yield": ratio(full, positive), "citation_verification": ratio(citations_passed, citations),
                         "report_consistency": ratio(reports, eligible_runs), "nonpass_reasons": dict(reasons),
                         "execution_seconds": {"samples": len(times), "median": statistics.median(times) if times else None,
                                               "min": min(times) if times else None, "max": max(times) if times else None},
                         "tool_calls": {"observed_total": sum(calls), "observed_runs": len(calls), "planned_runs": len(eligible_runs)},
                         "llm_calls": 0, "external_api_calls": 0, "local_compute_cost": None}
    repeat_eligible, repeat_passed, comparisons = [], [], []
    for identity, (entry, mode, repeat) in expected_ids.items():
        if repeat == 1:
            continue
        reference_id = f"{entry['id']}/{mode}/1"
        left, right = indexed.get(reference_id, {}), indexed.get(identity, {})
        repeat_eligible.append(identity)
        passed = bool(left.get("comparison_digest") and left.get("comparison_digest") == right.get("comparison_digest")
                      and left.get("artifact_verification", {}).get("verified") and right.get("artifact_verification", {}).get("verified"))
        if passed:
            repeat_passed.append(identity)
        comparisons.append({"reference_id": reference_id, "repeat_id": identity, "passed": passed,
                            "reason_code": "reproduced" if passed else "different_or_missing_output"})
    return {"by_mode": by_mode, "reproducibility": ratio(repeat_passed, repeat_eligible), "repeat_comparisons": comparisons,
            "unique_development_tasks": len({entry['id'] for entry in manifest['tasks'] if entry['partition'] == 'development' and entry['status'] == 'ready'}),
            "unique_positive_formula_groups": sorted({entry["formula_groups"][cid] for entry in manifest["tasks"]
                                                      if entry["partition"] == "development" and entry["status"] == "ready"
                                                      for cid in entry["executable_candidate_ids"]}),
            "planned_run_count": len(expected_ids), "human_fidelity": {"numerator": 0, "denominator": 0, "rate": None,
                                                                       "reason_code": "pending_human_annotation"}}


def _report(summary):
    lines = ["# Development engineering task suite", "", summary["scope"], "",
             "Reserved task declarations are not executed. Market final-test intervals remain locked. "
             "These are synthetic engineering tasks; human fidelity, time savings and investment value are unmeasured.", "",
             "| Policy | Expected task handling | Positive execution | Numerical coverage | Numerical agreement | Fully verified yield |",
             "|---|---:|---:|---:|---:|---:|"]
    for mode in MODES:
        metrics = summary["metrics"]["by_mode"][mode]
        cells = [f"{metrics[key]['numerator']}/{metrics[key]['denominator']}" for key in
                 ("task_handling", "positive_execution", "numerical_coverage", "numerical_agreement", "fully_verified_yield")]
        lines.append("| " + " | ".join([mode, *cells]) + " |")
    lines += ["", "Unsupported formulas are coverage gaps, not numerical passes. "
              "Negative inputs handled as expected are not positive research completions. "
              "Alias variants share a declared formula group. Repeats increase execution samples, not independent research tasks.",
              "", f"Acceptance within declared scope: {summary['acceptance_passed']}. Complete numerical coverage: {summary['fully_covered']}.",
              "", "## Per-execution records", ""]
    for record in summary["records"]:
        lines.append(f"- {record['id']}: {record['execution_status']}; expected handling={record.get('handling_passed', False)}; "
                     f"execution seconds={record.get('execution_seconds')}; tool calls={record.get('tool_calls')}.")
        for candidate in record.get("candidates", []):
            for check in candidate["checks"]:
                if check["outcome"] != "passed":
                    lines.append(f"  - {candidate['candidate_id']}: {check['outcome']} / {check.get('reason_code')} — {check['reason']}")
    lines += ["", "All metric numerators, denominators, eligible IDs, references, raw execution records and input/code hashes are in summary.json and manifest.json. "
              "Time includes real local execution without a controlled performance experiment; local compute cost is not measured. "
              "No superiority over normalized_fixed is presumed.", ""]
    return "\n".join(lines)


def run_suite(manifest_path: Path, out: Path) -> dict:
    manifest_path, out = Path(manifest_path).resolve(), Path(out).resolve()
    manifest = load_manifest(manifest_path)
    require(any(entry["partition"] == "development" and entry["status"] == "ready" for entry in manifest["tasks"]),
            "No ready development tasks; pending/reserved declarations cannot produce an evaluation")
    require(not out.exists(), "Suite output already exists; preserve it and use a new directory")
    out.mkdir(parents=True)
    original_hash = sha256(manifest_path)
    shutil.copyfile(manifest_path, out / "manifest.original.json")
    require(sha256(out / "manifest.original.json") == original_hash, "Source manifest changed while snapshotting")
    code = _source_hashes()
    materialized, frozen = {}, deepcopy(manifest)
    for entry in frozen["tasks"]:
        if entry["status"] != "ready" or entry["partition"] != "development":
            continue
        raw, paths = _task_inputs(manifest_path, entry)
        directory = out / "tasks" / entry["id"]
        directory.mkdir(parents=True)
        shutil.copyfile(paths["task"], directory / "source-task.json")
        require(sha256(directory / "source-task.json") == entry["input_sha256"]["task"], "Source task changed while snapshotting")
        for key, name in INPUT_NAMES.items():
            shutil.copyfile(paths[key], directory / name)
            require(sha256(directory / name) == entry["input_sha256"][key], "Input changed while snapshotting")
            raw[key] = name
        atomic_json(directory / "task.json", raw)
        entry["evidence_ids"] = [item["id"] for item in raw["evidence"]]
        entry["materialized_task_sha256"] = sha256(directory / "task.json")
        materialized[entry["id"]] = raw
    atomic_json(out / "manifest.json", frozen)
    plan = []
    for repeat in range(1, manifest["repetitions"] + 1):
        order = MODES[repeat - 1:] + MODES[:repeat - 1]
        for entry in frozen["tasks"]:
            if entry["partition"] == "development" and entry["status"] == "ready":
                plan.extend({"id": f"{entry['id']}/{mode}/{repeat}", "task_id": entry["id"], "mode": mode,
                             "repeat": repeat, "execution_status": "not_run", "reason_code": "not_started"} for mode in order)
    atomic_json(out / "progress.json", {"planned_runs": plan, "complete": False})
    by_task = {entry["id"]: entry for entry in frozen["tasks"]}
    source_changed = False
    for record in plan:
        if _source_hashes() != code:
            source_changed = True
            record["reason_code"] = "source_changed"
            break
        entry, mode = by_task[record["task_id"]], record["mode"]
        raw = materialized[entry["id"]]
        run_dir = out / "runs" / entry["id"] / mode / str(record["repeat"])
        record.update(execution_status="running", reason_code="running", run_dir=str(run_dir.relative_to(out)),
                      started_at=datetime.now(timezone.utc).isoformat())
        atomic_json(out / "progress.json", {"planned_runs": plan, "complete": False})
        start = time.perf_counter()
        try:
            state = run_task(out / "tasks" / entry["id"] / "task.json", run_dir, mode=mode)
            record["execution_seconds"] = time.perf_counter() - start
            verification_start = time.perf_counter()
            verification = verify_run(run_dir)
            record.update(artifact_verification=verification, tool_calls=state["tool_calls"], run_status=state["status"])
            expected = entry["expected"][mode]
            record["candidates"] = [_candidate_record(candidate, expected["candidates"].get(candidate["id"]), record["id"], entry,
                                                        run_dir, raw, verification["verified"]) for candidate in state["candidates"]]
            verified_evidence = {item["id"]: item for item in (state.get("preflight") or {}).get("evidence", [])}
            record["citations"] = [{"id": f"{record['id']}/{eid}", "evidence_id": eid,
                                     "outcome": "passed" if verified_evidence.get(eid, {}).get("citation_verified") is True else "not_run",
                                     "method": "verified_engine_preflight"} for eid in entry["evidence_ids"]]
            record["comparison_digest"] = digest(_projection(state, run_dir))
            record["handling_passed"] = _handling_passed(entry, mode, state, record["candidates"], verification["verified"])
            record.update(execution_status="completed", reason_code="recorded", verification_seconds=time.perf_counter() - verification_start)
        except Exception as exc:
            record.update(execution_status="failed", reason_code="execution_or_verification_error",
                          error={"type": type(exc).__name__, "message": str(exc)},
                          execution_seconds=record.get("execution_seconds", time.perf_counter() - start), handling_passed=False)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(out / "progress.json", {"planned_runs": plan, "complete": False})
    source_changed |= _source_hashes() != code
    metrics = summarize(frozen, plan)
    acceptance = (not source_changed and all(value["task_handling"]["numerator"] == value["task_handling"]["denominator"]
                                            for value in metrics["by_mode"].values())
                  and metrics["reproducibility"]["numerator"] == metrics["reproducibility"]["denominator"])
    fully_covered = all(value["numerical_coverage"]["numerator"] == value["numerical_coverage"]["denominator"] for value in metrics["by_mode"].values())
    summary = {"schema_version": 1, "suite_runner_version": SUITE_VERSION, "oracle_version": ORACLE_VERSION,
               "suite_id": manifest["suite_id"], "suite_version": manifest["version"],
               "scope": "Synthetic engineering development suite; no independent human research or market-value claim.",
               "tool_call_scope": "Engine charged calls, including failed calls; independent suite verification is timed separately. No human active-time measurement.",
               "source_manifest_sha256": original_hash, "frozen_manifest_sha256": sha256(out / "manifest.json"),
               "code_sha256": code, "environment": environment(), "source_unchanged": not source_changed,
               "records": plan, "metrics": metrics, "acceptance_passed": acceptance, "fully_covered": fully_covered,
               "reserved_tasks": [{"id": entry["id"], "status": "not_executed", "reason_code": "reserved_task_locked",
                                   "annotation_status": entry["status"], "label_source": entry["label_source"]}
                                  for entry in manifest["tasks"] if entry["partition"] == "reserved"],
               "unmeasured": ["human_semantic_fidelity", "human_time_saved", "LLM_benefit", "economic_value", "local_compute_cost"]}
    atomic_json(out / "summary.json", summary)
    (out / "report.md").write_text(_report(summary), encoding="utf-8")
    atomic_json(out / "progress.json", {"planned_runs": plan, "complete": True})
    return summary


def verify_suite(out: Path) -> dict:
    """Rebuild declared checks/ratios from immutable local suite/run artifacts.

    This verifies recorded calculations and report projections, not whether a
    person performed the declared annotations or the truth of wall-clock logs.
    A new verifier/oracle version requires an explicit new verification run.
    """
    out = Path(out).resolve()
    summary = _bounded_json(out / "summary.json", 64 * 1024 * 1024)
    require(summary.get("suite_runner_version") == SUITE_VERSION and summary.get("oracle_version") == ORACLE_VERSION,
            "Suite verifier/oracle version differs; preserve this historical report and verify explicitly with its recorded version")
    require(sha256(out / "manifest.original.json") == summary["source_manifest_sha256"], "Original suite manifest digest mismatch")
    require(sha256(out / "manifest.json") == summary["frozen_manifest_sha256"], "Frozen suite manifest digest mismatch")
    original, manifest = read_json(out / "manifest.original.json"), read_json(out / "manifest.json")
    stripped = deepcopy(manifest)
    for entry in stripped["tasks"]:
        entry.pop("materialized_task_sha256", None)
    require(stripped == original, "Materialized manifest changed scientific declarations")
    tasks = {}
    for entry in manifest["tasks"]:
        if entry["partition"] != "development" or entry["status"] != "ready":
            continue
        _identifier(entry["id"])
        directory = out / "tasks" / entry["id"]
        require(sha256(directory / "source-task.json") == entry["input_sha256"]["task"], "Original task snapshot digest mismatch")
        raw = read_json(directory / "source-task.json")
        for key, name in INPUT_NAMES.items():
            require(sha256(directory / name) == entry["input_sha256"][key], "Suite input snapshot digest mismatch")
            raw[key] = name
        require(raw == read_json(directory / "task.json"), "Materialized task differs from its source")
        require(sha256(directory / "task.json") == entry["materialized_task_sha256"], "Materialized task digest mismatch")
        tasks[entry["id"]] = (entry, raw)
    for record in summary["records"]:
        entry, raw = tasks[record["task_id"]]
        require(record["mode"] in MODES and type(record["repeat"]) is int and 1 <= record["repeat"] <= manifest["repetitions"], "Invalid suite execution identity")
        require(record["id"] == f"{entry['id']}/{record['mode']}/{record['repeat']}", "Execution id differs from task/mode/repetition")
        if record["execution_status"] != "completed":
            require(record["execution_status"] in {"not_run", "failed"} and not record.get("handling_passed"), "Unfinished execution cannot pass")
            continue
        run_dir = out / "runs" / entry["id"] / record["mode"] / str(record["repeat"])
        require(record["run_dir"] == str(run_dir.relative_to(out)), "Run directory differs from execution identity")
        verification = verify_run(run_dir)
        state, run_manifest = read_json(run_dir / "state.json"), read_json(run_dir / "manifest.json")
        signature = run_manifest["signature"]
        require(signature["code"] == summary["code_sha256"] and signature["mode"] == record["mode"], "Policy run changed frozen source or mode")
        require(signature["task_sha256"] == entry["materialized_task_sha256"]
                and signature["inputs"] == {key: entry["input_sha256"][key] for key in INPUT_NAMES}, "Policies did not use the same frozen task inputs")
        require(verification == record["artifact_verification"] and state["tool_calls"] == record["tool_calls"]
                and state["status"] == record["run_status"], "Execution summary differs from verified engine state")
        expected = entry["expected"][record["mode"]]
        candidates = [_candidate_record(candidate, expected["candidates"].get(candidate["id"]), record["id"], entry,
                                        run_dir, raw, verification["verified"]) for candidate in state["candidates"]]
        require(candidates == record["candidates"], "Candidate check summary differs from recomputed checks")
        require(_handling_passed(entry, record["mode"], state, candidates, verification["verified"]) == record["handling_passed"], "Handling conclusion differs from declarations")
        require(digest(_projection(state, run_dir)) == record["comparison_digest"], "Repeat comparison digest differs from actual results")
        evidence = {item["id"]: item for item in (state.get("preflight") or {}).get("evidence", [])}
        citations = [{"id": f"{record['id']}/{eid}", "evidence_id": eid,
                      "outcome": "passed" if evidence.get(eid, {}).get("citation_verified") is True else "not_run",
                      "method": "verified_engine_preflight"} for eid in entry["evidence_ids"]]
        require(citations == record["citations"], "Citation summary differs from recorded preflight")
    metrics = summarize(manifest, summary["records"])
    require(len(summary["records"]) == metrics["planned_run_count"], "Suite report omitted planned executions")
    require(metrics == summary["metrics"], "Suite metrics/denominators differ from recorded task outcomes")
    acceptance = (summary["source_unchanged"] and all(value["task_handling"]["numerator"] == value["task_handling"]["denominator"]
                                                   for value in metrics["by_mode"].values())
                  and metrics["reproducibility"]["numerator"] == metrics["reproducibility"]["denominator"])
    covered = all(m["numerical_coverage"]["numerator"] == m["numerical_coverage"]["denominator"] for m in metrics["by_mode"].values())
    require(acceptance == summary["acceptance_passed"] and covered == summary["fully_covered"], "Suite conclusion differs from recorded coverage")
    require((out / "report.md").read_text(encoding="utf-8") == _report(summary), "Suite report differs from recorded results")
    return {"verified": True, "scope": "Frozen suite inputs, engine results, independent checks, ratios and report projection",
            "suite_runner_version": SUITE_VERSION, "oracle_version": ORACLE_VERSION,
            "checked_runs": sum(record["execution_status"] == "completed" for record in summary["records"]),
            "summary_sha256": sha256(out / "summary.json"), "report_sha256": sha256(out / "report.md")}
