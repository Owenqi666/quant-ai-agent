"""Integration and fault-injection tests for durable research tasks.

One genuine subprocess run supplies the shared baseline. Budget/error tests
reuse its independently checked preflight response; successful compute calls
still run the real worker against isolated input snapshots.
"""

from copy import deepcopy
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from paper_alpha.contracts import Task
from paper_alpha.proposals import draft_task
from paper_alpha.storage import atomic_json, read_json
from paper_alpha import worker, workflow


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "alpha101"


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="paper-alpha-workflow-")
        cls.addClassCleanup(cls.shared.cleanup)
        cls.shared_root = Path(cls.shared.name)
        cls.base_inputs = cls.shared_root / "inputs"
        cls.base_inputs.mkdir()
        for name in ("paper.json", "paper.pdf", "market.csv", "metadata.json"):
            shutil.copy2(EXAMPLE / name, cls.base_inputs / name)
        cls.raw = draft_task(
            read_json(cls.base_inputs / "paper.json"), paper_path="paper.json",
            pdf_path="paper.pdf", data_path="market.csv", metadata_path="metadata.json",
            evaluation=read_json(EXAMPLE / "evaluation.json"),
        )
        cls.raw["budget"]["max_seconds"] = 120
        atomic_json(cls.base_inputs / "task.json", cls.raw)
        cls.base_output = cls.shared_root / "completed"
        cls.base_state = workflow.run_task(cls.base_inputs / "task.json", cls.base_output)
        if cls.base_state["status"] != "completed":
            raise AssertionError(f"Real integration baseline did not complete: {cls.base_state}")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="paper-alpha-case-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.inputs = self.directory / "inputs"
        shutil.copytree(self.base_inputs, self.inputs)
        self.task_path = self.inputs / "task.json"
        self.output = self.directory / "run"
        self.raw_task = deepcopy(self.raw)

    def run_modified(self, raw=None, **kwargs):
        atomic_json(self.task_path, self.raw_task if raw is None else raw)
        return workflow.run_task(self.task_path, self.output, **kwargs)

    def copy_completed(self, output=None):
        destination = self.output if output is None else output
        shutil.copytree(self.base_output, destination)
        return destination

    def successful_subprocess(self, command, **kwargs):
        """Return real worker computations, caching only verified PDF preflight."""
        self.assertEqual(command[1:3], ["-m", "paper_alpha.worker"])
        job = read_json(command[-2])
        if job["action"] == "preflight":
            value = deepcopy(self.base_state["preflight"])
        else:
            value = worker.execute(job)
        atomic_json(command[-1], {"ok": True, "value": value})
        return subprocess.CompletedProcess(command, 0, "", "")

    def single_candidate(self, candidate_id="alpha101"):
        raw = deepcopy(self.raw_task)
        raw["candidates"] = [c for c in raw["candidates"] if c["id"] == candidate_id]
        return raw

    def test_real_end_to_end_preserves_evidence_results_and_failed_attempt(self):
        state = self.base_state
        candidates = {c["id"]: c for c in state["candidates"]}
        self.assertEqual(candidates["alpha006"]["status"], "evaluated")
        self.assertEqual(candidates["alpha101"]["status"], "evaluated")
        self.assertEqual(candidates["alpha005"]["status"], "blocked")
        self.assertEqual([a["status"] for a in candidates["alpha006"]["attempts"]], ["failed", "evaluated"])
        self.assertEqual(candidates["alpha006"]["attempts"][0]["error"]["code"], "unknown_operator")
        self.assertIn("vwap", candidates["alpha005"]["reason"])
        self.assertTrue(all(e["citation_verified"] for e in state["preflight"]["evidence"]))
        for candidate in (candidates["alpha006"], candidates["alpha101"]):
            result_path = next(path for path in candidate["artifacts"] if path.endswith("/result.json"))
            self.assertEqual(read_json(self.base_output / result_path), candidate["result"])
            self.assertIn("mean_rank_ic", candidate["result"]["metrics"])
        self.assertTrue(workflow.verify_run(self.base_output)["verified"])

    def test_paper_original_requires_relevant_formula_not_an_unrelated_quote(self):
        task = Task.parse(self.raw_task)
        candidate = task.candidates[0]
        hypothesis = next(h for h in task.hypotheses if h.id == candidate.hypothesis_id)
        evidence = {e.id: e for e in task.evidence}
        unrelated = replace(hypothesis, evidence_ids=["alpha101-formula"])
        with self.assertRaisesRegex(ValueError, "differs from its cited formula"):
            workflow.verify_candidate_origin(candidate, unrelated, evidence)
        changed = replace(candidate, expression=candidate.expression.replace(", 10)", ", 20)"))
        with self.assertRaisesRegex(ValueError, "differs from its cited formula"):
            workflow.verify_candidate_origin(changed, hypothesis, evidence)

    def test_documented_alias_keeps_formula_provenance(self):
        task = Task.parse(self.raw_task)
        candidate = task.candidates[0]
        hypothesis = next(h for h in task.hypotheses if h.id == candidate.hypothesis_id)
        evidence = {e.id: e for e in task.evidence}
        canonical = replace(candidate, expression=candidate.expression.replace("correlation", "ts_corr"))
        workflow.verify_candidate_origin(canonical, hypothesis, evidence)

    def test_worker_failure_is_preserved_in_attempt_and_event_history(self):
        def fail_computation(command, **kwargs):
            job = read_json(command[-2])
            if job["action"] == "preflight":
                return self.successful_subprocess(command, **kwargs)
            atomic_json(command[-1], {"ok": False, "error": {
                "type": "ExpressionError", "code": "injected_compute_failure",
                "message": "Controlled computation failure for recovery test",
            }})
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(workflow.subprocess, "run", side_effect=fail_computation):
            state = self.run_modified(self.single_candidate())
        candidate = state["candidates"][0]
        self.assertEqual(candidate["status"], "rejected")
        self.assertEqual(candidate["attempts"][0]["status"], "failed")
        self.assertEqual(candidate["attempts"][0]["error"]["code"], "injected_compute_failure")
        self.assertTrue(any(e["kind"] == "attempt_failed" for e in state["events"]))
        self.assertTrue(workflow.verify_run(self.output)["verified"])

    def test_each_budget_limit_is_enforced(self):
        cases = (
            ("max_tool_calls", 1),
            ("max_attempts_per_candidate", 1),
            ("max_candidates", 1),
        )
        for field, limit in cases:
            with self.subTest(field=field):
                self.output = self.directory / field
                raw = deepcopy(self.raw_task)
                raw["budget"][field] = limit
                if field == "max_attempts_per_candidate":
                    raw["candidates"] = [raw["candidates"][0]]
                with patch.object(workflow.subprocess, "run", side_effect=self.successful_subprocess):
                    state = self.run_modified(raw)
                if field == "max_tool_calls":
                    self.assertEqual(state["status"], "budget_exhausted")
                    self.assertEqual(state["tool_calls"], 1)
                    self.assertTrue(all(not c["attempts"] for c in state["candidates"]))
                elif field == "max_attempts_per_candidate":
                    self.assertEqual(len(state["candidates"][0]["attempts"]), 1)
                    self.assertEqual(state["candidates"][0]["status"], "rejected")
                    self.assertFalse(any(e["kind"] == "repair_selected" for e in state["events"]))
                else:
                    self.assertEqual(state["candidates"][0]["status"], "evaluated")
                    self.assertTrue(all(c["status"] == "budget_stopped" for c in state["candidates"][1:]))

    def test_timeout_uses_subprocess_timeout_and_stops_run(self):
        with patch.object(workflow.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 0.1)) as run:
            state = self.run_modified()
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["tool_calls"], 1)
        self.assertTrue(any(e["kind"] == "tool_timeout" for e in state["events"]))
        timeout = run.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, self.raw_task["budget"]["max_seconds"])
        self.assertTrue(all(c["status"] == "budget_stopped" for c in state["candidates"]))

    def test_completed_resume_never_recomputes_or_spends_calls(self):
        self.copy_completed()
        with patch.object(workflow.subprocess, "run", side_effect=AssertionError("Resume must not execute tools")) as run:
            state = workflow.run_task(self.task_path, self.output, resume=True)
        run.assert_not_called()
        self.assertEqual(state, self.base_state)
        self.assertTrue(workflow.verify_run(self.output)["verified"])

    def test_resume_rejects_input_and_code_drift(self):
        self.copy_completed()
        data_path = self.inputs / "market.csv"
        data_before = data_path.read_bytes()
        data_path.write_bytes(data_before + b"\n")
        with self.assertRaisesRegex(ValueError, "changed; start a new run"):
            workflow.run_task(self.task_path, self.output, resume=True)
        data_path.write_bytes(data_before)
        files = workflow.code_files()
        relative = "paper_alpha/worker.py"
        changed_source = self.directory / "changed-worker.py"
        changed_source.write_bytes(files[relative].read_bytes() + b"\n# Simulated code drift\n")
        files[relative] = changed_source
        with patch.object(workflow, "code_files", return_value=files):
            with self.assertRaisesRegex(ValueError, "changed; start a new run"):
                workflow.run_task(self.task_path, self.output, resume=True)

    def test_verify_detects_report_result_and_state_metric_tampering(self):
        for target in ("report", "result", "state_metrics"):
            with self.subTest(target=target):
                output = self.copy_completed(self.directory / target)
                if target == "report":
                    report = output / "report.md"
                    report.write_text(report.read_text() + "\nFabricated performance claim.\n")
                    expected_error = "Report differs"
                else:
                    state = read_json(output / "state.json")
                    candidate = next(c for c in state["candidates"] if c.get("result"))
                    if target == "result":
                        relative = next(path for path in candidate["artifacts"] if path.endswith("/result.json"))
                        value = read_json(output / relative)
                        value["metrics"]["mean_rank_ic"] = 0.999
                        atomic_json(output / relative, value)
                        expected_error = "Result digest mismatch"
                    else:
                        candidate["result"]["metrics"]["mean_rank_ic"] = 0.999
                        atomic_json(output / "state.json", state)
                        expected_error = "State metrics differ"
                with self.assertRaisesRegex(ValueError, expected_error):
                    workflow.verify_run(output)

    def test_interrupted_running_attempt_spends_attempt_and_tool_budgets(self):
        raw = self.single_candidate()
        raw["budget"]["max_attempts_per_candidate"] = 1

        def interrupt_compute(command, **kwargs):
            if read_json(command[-2])["action"] == "preflight":
                return self.successful_subprocess(command, **kwargs)
            raise KeyboardInterrupt()

        with patch.object(workflow.subprocess, "run", side_effect=interrupt_compute):
            with self.assertRaises(KeyboardInterrupt):
                self.run_modified(raw)
        interrupted = read_json(self.output / "state.json")
        self.assertEqual(interrupted["status"], "running")
        self.assertEqual(interrupted["candidates"][0]["attempts"][0]["status"], "running")
        self.assertEqual(interrupted["tool_calls"], 3)
        with patch.object(workflow.subprocess, "run", side_effect=AssertionError("Attempt budget is consumed")) as run:
            resumed = workflow.run_task(self.task_path, self.output, resume=True)
        run.assert_not_called()
        self.assertEqual(resumed["tool_calls"], 3)
        self.assertEqual(resumed["candidates"][0]["status"], "budget_stopped")
        self.assertEqual([a["status"] for a in resumed["candidates"][0]["attempts"]], ["interrupted"])
        self.assertTrue(any(e["kind"] == "attempt_interrupted" for e in resumed["events"]))

    def test_no_task_python_or_expression_side_effect_is_executed(self):
        sentinel = self.directory / "unauthorized-side-effect"
        raw = self.single_candidate()
        raw["python_code"] = f"open({str(sentinel)!r}, 'w').write('unsafe')"
        with patch.object(workflow.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Unknown task keys"):
                self.run_modified(raw)
        run.assert_not_called()
        raw.pop("python_code")
        candidate = raw["candidates"][0]
        candidate.update(
            expression=f"__import__('pathlib').Path({str(sentinel)!r}).touch()",
            origin="user_modification", changes=["Deliberately invalid code-injection fixture"],
        )
        with patch.object(workflow.subprocess, "run", side_effect=self.successful_subprocess) as run:
            state = self.run_modified(raw)
        self.assertEqual(state["candidates"][0]["status"], "rejected")
        self.assertEqual(state["candidates"][0]["attempts"][0]["error"]["code"], "unsafe_expression")
        self.assertEqual(run.call_count, 1)  # Only input preflight, never the proposed code.
        self.assertFalse(sentinel.exists())

    def test_fixed_baseline_keeps_alias_failure_without_repair(self):
        with patch.object(workflow.subprocess, "run", side_effect=self.successful_subprocess):
            state = self.run_modified(mode="fixed")
        candidates = {c["id"]: c for c in state["candidates"]}
        self.assertEqual(candidates["alpha006"]["status"], "rejected")
        self.assertEqual(len(candidates["alpha006"]["attempts"]), 1)
        self.assertEqual(candidates["alpha101"]["status"], "evaluated")
        self.assertEqual(candidates["alpha005"]["status"], "blocked")
        self.assertFalse(any(e["kind"] == "repair_selected" for e in state["events"]))
        self.assertLess(state["tool_calls"], self.base_state["tool_calls"])

    def test_normalized_baseline_matches_reactive_results_without_failed_alias_attempt(self):
        with patch.object(workflow.subprocess, "run", side_effect=self.successful_subprocess):
            state = self.run_modified(mode="normalized_fixed")
        original = {c["id"]: c for c in self.base_state["candidates"]}
        candidates = {c["id"]: c for c in state["candidates"]}
        self.assertEqual(state["task"], self.raw_task)
        for case in ("alpha006", "alpha101", "alpha005"):
            self.assertEqual(candidates[case]["status"], original[case]["status"])
            self.assertEqual(candidates[case].get("result"), original[case].get("result"))
        self.assertEqual([a["status"] for a in candidates["alpha006"]["attempts"]], ["evaluated"])
        self.assertEqual(candidates["alpha006"]["expression"], original["alpha006"]["expression"])
        self.assertTrue(any(e["kind"] == "alias_normalized" for e in state["events"]))
        self.assertFalse(any(e["kind"] == "repair_selected" for e in state["events"]))
        self.assertTrue(workflow.verify_run(self.output)["verified"])
        with patch.object(workflow.subprocess, "run", side_effect=AssertionError("Completed resume must not run tools")):
            resumed = workflow.run_task(self.task_path, self.output, mode="normalized_fixed", resume=True)
        self.assertEqual(resumed, state)

    def test_normalization_cannot_approve_changed_original_or_substitute_missing_fields(self):
        raw = self.single_candidate("alpha006")
        raw["candidates"][0]["expression"] = "-1 * correlation(open, volume, 11)"
        with patch.object(workflow.subprocess, "run", side_effect=self.successful_subprocess):
            state = self.run_modified(raw, mode="normalized_fixed")
        self.assertEqual(state["candidates"][0]["status"], "rejected")
        self.assertIn("differs from its cited formula", state["candidates"][0]["reason"])
        self.assertEqual(state["candidates"][0]["attempts"], [])
        self.output = self.directory / "missing-field"
        raw = self.single_candidate("alpha006")
        raw["candidates"][0].update(expression="-1 * correlation(open, absent_field, 10)",
                                     origin="user_modification", changes=["Deliberate unavailable-field test"])
        hypothesis = next(h for h in raw["hypotheses"] if h["id"] == raw["candidates"][0]["hypothesis_id"])
        hypothesis["required_fields"].append("absent_field")
        with patch.object(workflow.subprocess, "run", side_effect=self.successful_subprocess):
            state = self.run_modified(raw, mode="normalized_fixed")
        self.assertEqual(state["candidates"][0]["status"], "blocked")
        self.assertIn("absent_field", state["candidates"][0]["reason"])
        self.assertEqual(state["candidates"][0]["attempts"], [])

    def test_invalid_inherited_lock_descriptor_is_rejected_before_creating_run(self):
        for descriptor in ("not-an-integer", "-1", "999999"):
            with self.subTest(descriptor=descriptor), patch.dict(os.environ, {"PAPER_ALPHA_WORKER_LOCK_FD": descriptor}):
                with self.assertRaisesRegex(ValueError, "must identify an open file descriptor"):
                    workflow.run_task(self.task_path, self.output)
                self.assertFalse(self.output.exists())

    def test_compute_descendant_fences_lock_after_supervisor_and_engine_exit(self):
        """Exercise actual subprocess inheritance, including an orphaned compute process."""
        atomic_json(self.task_path, self.single_candidate())
        marker, lock_path = self.directory / "compute.pid", self.directory / "worker.lock"
        # Keep the actual workflow subprocess boundary, replacing only numerical
        # work with a controlled stalled process so both ancestors can be killed.
        engine_script = """
import subprocess, sys
from pathlib import Path
from paper_alpha import workflow
from paper_alpha.storage import read_json
actual_run = subprocess.run
def controlled_run(command, **kwargs):
    if read_json(command[-2])["action"] == "compute_evaluate":
        code = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % sys.argv[3]
        command = [sys.executable, "-c", code]
    return actual_run(command, **kwargs)
workflow.subprocess.run = controlled_run
workflow.run_task(Path(sys.argv[1]), Path(sys.argv[2]))
"""
        engine = None
        try:
            with lock_path.open("a") as supervisor_lock:
                fcntl.flock(supervisor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                descriptor = supervisor_lock.fileno()
                engine = subprocess.Popen(
                    [sys.executable, "-c", engine_script, str(self.task_path), str(self.output), str(marker)],
                    cwd=ROOT, start_new_session=True, pass_fds=(descriptor,),
                    env={**os.environ, "PAPER_ALPHA_WORKER_LOCK_FD": str(descriptor)},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 15
                while not marker.exists() and engine.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "Controlled compute process did not start")
            # The supervisor's descriptor is closed; kill the engine next.
            engine.kill()
            engine.wait(timeout=5)
            os.kill(int(marker.read_text()), 0)
            with lock_path.open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.killpg(engine.pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            with lock_path.open("a") as contender:
                while True:
                    try:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail("Worker lock stayed held after the final descendant exited")
                        time.sleep(.02)
        finally:
            if engine is not None:
                try:
                    os.killpg(engine.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                engine.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
