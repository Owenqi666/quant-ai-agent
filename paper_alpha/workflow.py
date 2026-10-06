"""A bounded, resumable policy over explicit tools; no implicit financial decisions."""
from __future__ import annotations

import ast
from dataclasses import asdict
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

from .contracts import Task, require
from .evidence import sha256
from .expressions import ExpressionError, repair_expression, validate_expression
from .reporting import render_report
from .storage import atomic_json, digest, read_json, run_lock


INPUT_NAMES = {"paper": "paper.json", "paper_pdf": "paper.pdf",
               "data": "market.csv", "data_metadata": "metadata.json"}


class BudgetExhausted(ValueError):
    pass


def code_files():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "paper_alpha").rglob("*.py"))
    paths += [root / "pyproject.toml"]
    paths += [root / "requirements-lock.txt", root / "paper_alpha/vendor/PROVENANCE.json"]
    paths += sorted((root / "scripts").glob("*.py"))
    return {str(p.relative_to(root)): p for p in paths}


def environment():
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": {p: version(p) for p in ("numpy", "pandas", "pypdf", "python-dateutil", "six")}}


def fingerprint(task_path, task, mode):
    paths = {k: (task_path.parent / task.raw[k]).resolve() for k in INPUT_NAMES}
    for path in [task_path, *paths.values()]:
        require(path.is_file(), f"Missing input file: {path}")
        require(path.stat().st_size <= 64 * 1024 * 1024, f"Input exceeds 64 MiB limit: {path}")
    files = code_files()
    value = {"task_sha256": sha256(task_path), "inputs": {k: sha256(p) for k, p in paths.items()},
             "code": {k: sha256(p) for k, p in files.items()}, "environment": environment(), "mode": mode}
    return value, paths, files


def _error(exc):
    return {"type": type(exc).__name__, "code": getattr(exc, "code", "validation_error"), "message": str(exc)}


def _formula_tree(expression):
    require(len(expression) <= 4096, "Formula exceeds provenance parser length limit")
    tree = ast.parse(expression, mode="eval")
    aliases = {"correlation": "ts_corr", "delta": "ts_delta", "stddev": "ts_std", "sum": "ts_sum"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            node.func.id = aliases.get(node.func.id, node.func.id)
    return ast.dump(tree, include_attributes=False)


def verify_candidate_origin(candidate, hypothesis, evidence):
    """Exact formula provenance for paper_original; prose derivations must declare changes."""
    if candidate.origin != "paper_original":
        return
    original = _formula_tree(candidate.expression)
    for reference in hypothesis.evidence_ids:
        try:
            if _formula_tree(evidence[reference].quote) == original:
                return
        except (SyntaxError, ValueError):
            continue
    raise ValueError("paper_original expression differs from its cited formula; declare a modification or conjecture")


def _write_projections(out, state):
    atomic_json(out / "state.json", state)
    events = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                     for e in state["events"])
    # state.json is the durable source of truth; these derived views can be rebuilt.
    (out / "events.jsonl").write_text(events, encoding="utf-8")
    (out / "report.md").write_text(render_report(state), encoding="utf-8")


def verify_run(out):
    out = Path(out).resolve()
    manifest, state = read_json(out / "manifest.json"), read_json(out / "state.json")
    require(digest(manifest["signature"]) == manifest["signature_sha256"], "Manifest signature checksum mismatch")
    for relative, expected in manifest["snapshot_files"].items():
        require(sha256(out / relative) == expected, f"Snapshot digest mismatch: {relative}")
    for relative, expected in state.get("tool_artifacts", {}).items():
        require(sha256(out / relative) == expected, f"Tool record digest mismatch: {relative}")
    require(read_json(out / "inputs/task.json") == state["task"], "Saved task differs from input snapshot")
    for candidate in state["candidates"]:
        for relative, expected in candidate.get("artifacts", {}).items():
            require(sha256(out / relative) == expected, f"Result digest mismatch: {relative}")
        if candidate.get("result"):
            result_path = next(p for p in candidate["artifacts"] if p.endswith("/result.json"))
            require(read_json(out / result_path) == candidate["result"], "State metrics differ from tool result")
    expected_events = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                              for e in state["events"])
    require((out / "events.jsonl").read_text() == expected_events, "Event projection mismatch")
    require((out / "report.md").read_text() == render_report(state), "Report differs from saved tool results")
    return {"verified": True, "status": state["status"], "snapshot_files": len(manifest["snapshot_files"]),
            "result_files": sum(len(c.get("artifacts", {})) for c in state["candidates"])}


def run_task(task_path, output_dir, mode="agent", resume=False):
    require(mode in {"agent", "fixed", "normalized_fixed"}, "Unknown policy")
    inherited_lock = os.environ.get("PAPER_ALPHA_WORKER_LOCK_FD")
    inherited_fds = ()
    if inherited_lock is not None:
        try:
            descriptor = int(inherited_lock)
            require(descriptor >= 0, "Negative worker lock descriptor")
            os.fstat(descriptor)
        except (ValueError, OSError) as exc:
            raise ValueError("PAPER_ALPHA_WORKER_LOCK_FD must identify an open file descriptor") from exc
        inherited_fds = (descriptor,)
    task_path, out = Path(task_path).resolve(), Path(output_dir).resolve()
    require(task_path.stat().st_size <= 1024 * 1024, "Task JSON exceeds 1 MiB limit")
    raw = read_json(task_path)
    task = Task.parse(raw)
    signature, paths, files = fingerprint(task_path, task, mode)
    with run_lock(out):
        if (out / "state.json").exists():
            require(resume, "Run already exists; use --resume or a new output directory")
            manifest, state = read_json(out / "manifest.json"), read_json(out / "state.json")
            require(signature == manifest["signature"], "Inputs, code, environment or policy changed; start a new run")
            for relative, expected in manifest["snapshot_files"].items():
                require(sha256(out / relative) == expected, f"Snapshot digest mismatch: {relative}")
            # A crash between state commit and projection write may leave old views.
            _write_projections(out, state)
            verify_run(out)
            if state["status"] != "running":
                return state
        else:
            require(not resume, "Cannot resume: no saved state")
            require(not (out / "manifest.json").exists(), "Incomplete initialization; preserve this directory and start a new run")
            inputs = out / "inputs"
            inputs.mkdir(exist_ok=True)
            shutil.copy2(task_path, inputs / "task.json")
            for key, name in INPUT_NAMES.items():
                shutil.copy2(paths[key], inputs / name)
            for relative, source in files.items():
                destination = out / "source" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            snapshots = [p for folder in (inputs, out / "source") for p in folder.rglob("*") if p.is_file()]
            require(sha256(inputs / "task.json") == signature["task_sha256"], "Task changed while snapshotting")
            for key, name in INPUT_NAMES.items():
                require(sha256(inputs / name) == signature["inputs"][key], "Input changed while snapshotting")
            for relative, expected in signature["code"].items():
                require(sha256(out / "source" / relative) == expected, "Code changed while snapshotting")
            manifest = {"schema_version": 1, "signature": signature, "signature_sha256": digest(signature),
                        "snapshot_files": {str(p.relative_to(out)): sha256(p) for p in snapshots},
                        "source_note": "Source-file SHA256 inventory; no Git commit is invented."}
            atomic_json(out / "manifest.json", manifest)
            state = {"schema_version": 1, "task": raw, "mode": mode, "status": "running",
                     "tool_calls": 0, "elapsed_seconds": 0.0, "events": [],
                     "candidates": [{**asdict(c), "status": "pending", "attempts": []} for c in task.candidates]}
            _write_projections(out, state)

        # Conservatively charge downtime after an unclean process death as well:
        # restarting must never reset time already spent by an in-flight tool.
        inflight = state.pop("inflight_since", None)
        if inflight is not None:
            state["elapsed_seconds"] += max(0.0, time.time() - inflight)
        started, previous_seconds = time.monotonic(), state["elapsed_seconds"]

        def save():
            state["elapsed_seconds"] = previous_seconds + time.monotonic() - started
            _write_projections(out, state)

        def event(kind, **payload):
            state["events"].append({"sequence": len(state["events"]) + 1, "kind": kind, **payload})

        def remaining():
            return task.budget.max_seconds - previous_seconds - (time.monotonic() - started)

        def charge(tool, **payload):
            if state["tool_calls"] >= task.budget.max_tool_calls or remaining() <= 0:
                raise BudgetExhausted("Tool-call or cumulative wall-time budget exhausted")
            state["tool_calls"] += 1
            event("tool_started", tool=tool, call=state["tool_calls"], **payload)
            save()  # Charge before work so interruption does not grant free retries.

        def external_tool(action, **payload):
            charge(action, candidate_id=payload.get("candidate_id"))
            tool_dir = out / "tools" / f"{state['tool_calls']:04d}"
            tool_dir.mkdir(parents=True, exist_ok=True)
            job = {"action": action, "inputs": str(out / "inputs"), **payload}
            atomic_json(tool_dir / "request.json", job)
            state.setdefault("tool_artifacts", {})[str((tool_dir / "request.json").relative_to(out))] = sha256(tool_dir / "request.json")
            state["inflight_since"] = time.time()
            save()
            timeout = remaining()
            if timeout <= 0:
                raise BudgetExhausted("Wall-time budget exhausted before tool execution")
            try:
                process = subprocess.run([sys.executable, "-m", "paper_alpha.worker",
                                         str(tool_dir / "request.json"), str(tool_dir / "response.json")],
                                         capture_output=True, text=True, timeout=timeout, check=False,
                                         cwd=out / "source", pass_fds=inherited_fds)
            except subprocess.TimeoutExpired as exc:
                state.pop("inflight_since", None)
                event("tool_timeout", tool=action)
                raise BudgetExhausted("Tool process terminated at wall-time budget") from exc
            state.pop("inflight_since", None)
            (tool_dir / "stdout.txt").write_text(process.stdout or "", encoding="utf-8")
            (tool_dir / "stderr.txt").write_text(process.stderr or "", encoding="utf-8")
            for path in tool_dir.iterdir():
                if path.is_file():
                    state["tool_artifacts"][str(path.relative_to(out))] = sha256(path)
            require(process.returncode == 0, f"Tool process failed: {process.stderr[-2000:]}")
            response = read_json(tool_dir / "response.json")
            event("tool_finished", tool=action, ok=response["ok"])
            if not response["ok"]:
                error = response["error"]
                raise ExpressionError(error["message"], error["code"])
            return response["value"]

        try:
            # Resume consumes any previously running attempt, keeping all history.
            for item in state["candidates"]:
                for attempt in item["attempts"]:
                    if attempt["status"] == "running":
                        attempt["status"] = "interrupted"
                        event("attempt_interrupted", candidate_id=item["id"], attempt=attempt["number"])
                if item["status"] == "running":
                    item["status"] = "pending"
            save()
            if "preflight" not in state:
                state["preflight"] = external_tool("preflight")
                event("evidence_and_data_verified", evidence_count=len(state["preflight"]["evidence"]))
                save()
            available = set(state["preflight"]["available_fields"])
            hypotheses = {h.id: h for h in task.hypotheses}
            evidence = {e.id: e for e in task.evidence}
            if mode == "normalized_fixed" and "normalized_expressions" not in state:
                # The baseline gets the exact same finite alias transform as
                # the reactive policy, applied once before any attempt. Charge
                # this deterministic batch and preserve all original inputs.
                charge("normalize_operator_aliases")
                state["normalized_expressions"] = {}
                for candidate in task.candidates:
                    normalized = repair_expression(candidate.expression, {"code": "unknown_operator"})
                    state["normalized_expressions"][candidate.id] = normalized or candidate.expression
                    if normalized is not None:
                        event("alias_normalized", candidate_id=candidate.id,
                              original_expression=candidate.expression, expression=normalized,
                              semantic_change=False, reason="Documented operator alias normalization before execution")
                save()
            for index, (candidate, item) in enumerate(zip(task.candidates, state["candidates"])):
                if item["status"] != "pending":
                    continue
                if index >= task.budget.max_candidates:
                    item.update(status="budget_stopped", reason="Candidate-count budget exhausted")
                    event("candidate_stopped", candidate_id=item["id"], reason=item["reason"])
                    save()
                    continue
                hypothesis = hypotheses[candidate.hypothesis_id]
                try:
                    verify_candidate_origin(candidate, hypothesis, evidence)
                except (SyntaxError, ValueError) as exc:
                    item.update(status="rejected", reason=str(exc))
                    event("provenance_rejected", candidate_id=item["id"], error=_error(exc))
                    save()
                    continue
                missing = sorted(set(hypothesis.required_fields) - available)
                if missing:
                    item.update(status="blocked", reason=f"Missing data fields: {missing}. Substitution requires a new, explicitly modified hypothesis.")
                    event("data_unavailable", candidate_id=item["id"], fields=missing)
                    save()
                    continue
                expression = item.get("next_expression", state.get("normalized_expressions", {}).get(
                    candidate.id, candidate.expression))
                cap = 1 if mode in {"fixed", "normalized_fixed"} else task.budget.max_attempts_per_candidate
                while len(item["attempts"]) < cap:
                    charge("validate_expression", candidate_id=item["id"])
                    attempt = {"number": len(item["attempts"]) + 1, "expression": expression, "status": "running"}
                    item["attempts"].append(attempt)
                    item["status"] = "running"
                    save()
                    try:
                        validation = validate_expression(expression, available)
                        require(set(validation["used_fields"]) <= set(hypothesis.required_fields),
                                "Expression uses fields absent from the hypothesis contract")
                        attempt["validation"] = validation
                        artifact_dir = out / "candidates" / item["id"] / f"attempt-{attempt['number']}"
                        result = external_tool("compute_evaluate", expression=expression,
                                               output_dir=str(artifact_dir), candidate_id=item["id"])
                        attempt["status"] = result["status"]
                        item["status"] = result["status"]
                        item["result"] = result
                        item["artifacts"] = {str(p.relative_to(out)): sha256(p)
                                             for p in sorted(artifact_dir.glob("*")) if p.is_file()}
                        event("candidate_finished", candidate_id=item["id"], status=item["status"])
                        save()
                        break
                    except BudgetExhausted:
                        attempt["status"] = "budget_stopped"
                        raise
                    except (ValueError, SyntaxError) as exc:
                        attempt.update(status="failed", error=_error(exc))
                        event("attempt_failed", candidate_id=item["id"], attempt=attempt["number"], error=_error(exc))
                        save()
                        repair = None
                        if mode == "agent" and len(item["attempts"]) < cap:
                            charge("repair_expression", candidate_id=item["id"])
                            repair = repair_expression(expression, exc)
                        if repair is None:
                            item.update(status="rejected", reason="No permitted semantic-preserving repair; retain this attempt for review")
                            save()
                            break
                        attempt["repair"] = repair
                        item["next_expression"] = repair
                        expression = repair
                        event("repair_selected", candidate_id=item["id"], expression=repair,
                              semantic_change=False, reason="Documented operator alias normalization")
                        save()
                if item["status"] in {"pending", "running"}:
                    item.update(status="budget_stopped", reason="Per-candidate attempt budget exhausted")
                    save()
            state["status"] = "completed"
        except BudgetExhausted as exc:
            state.update(status="budget_exhausted", error=_error(exc))
            for item in state["candidates"]:
                if item["status"] in {"pending", "running"}:
                    item.update(status="budget_stopped", reason=str(exc))
            event("budget_exhausted", error=_error(exc))
        except (ValueError, OSError) as exc:
            state.update(status="failed", error=_error(exc))
            for item in state["candidates"]:
                if item["status"] in {"pending", "running"}:
                    item.update(status="blocked", reason="Run preconditions or tool process failed")
            event("run_failed", error=_error(exc))
        except KeyboardInterrupt:
            state.pop("inflight_since", None)
            event("run_interrupted", reason="Resume with identical inputs, code and environment")
            save()
            raise
        save()
        return state
