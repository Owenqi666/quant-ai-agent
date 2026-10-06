from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.benchmark import SEMANTIC_GROUPS, VALID_EXECUTABLE_IDS, benchmark, build_cases
from paper_alpha.contracts import Task
from paper_alpha.proposals import draft_task
from paper_alpha.storage import atomic_json, read_json


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "alpha101"


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.raw = draft_task(
            read_json(EXAMPLE / "paper.json"), paper_path=str(EXAMPLE / "paper.json"),
            pdf_path=str(EXAMPLE / "paper.pdf"), data_path=str(EXAMPLE / "market.csv"),
            metadata_path=str(EXAMPLE / "metadata.json"), evaluation=read_json(EXAMPLE / "evaluation.json"),
        )

    def test_fixed_cases_are_declared_before_results(self):
        before = deepcopy(self.raw)
        cases = build_cases(self.raw)
        self.assertEqual(self.raw, before)
        suite = Task.parse(cases["suite"])
        self.assertEqual(len(suite.candidates), 8)
        self.assertEqual(set(c.id for c in suite.candidates), set(cases["expectations"]["agent"]["candidates"]))
        self.assertEqual(cases["valid_executable_candidate_ids"], list(VALID_EXECUTABLE_IDS))
        self.assertEqual(cases["expectations"]["fixed"]["candidates"]["alpha006"], "rejected")
        self.assertEqual(cases["expectations"]["agent"]["candidates"]["alpha006"], "evaluated")
        self.assertEqual(cases["expectations"]["normalized_fixed"], cases["expectations"]["agent"])
        self.assertEqual(cases["semantic_groups"], SEMANTIC_GROUPS)
        self.assertIn("not an independent alpha", cases["definitions"]["canonical_corr"])
        candidates = {c.id: c for c in suite.candidates}
        self.assertEqual(candidates["canonical_corr"].origin, "paper_original")
        self.assertEqual(candidates["changed_original"].origin, "paper_original")
        self.assertIn("11", candidates["changed_original"].expression)
        for case in ("unsafe_expr", "negative_delay", "zero_signal"):
            self.assertEqual(candidates[case].origin, "model_conjecture")
            self.assertTrue(candidates[case].changes)
        self.assertEqual(cases["expectations"]["wrong_quote"]["run_status"], "failed")
        self.assertIn("DELIBERATELY INCORRECT", cases["wrong_quote"]["evidence"][0]["quote"])

    def _fake_run(self, task_path, directory, mode):
        raw = read_json(task_path)
        cases = build_cases(self.raw)
        wrong = task_path.name == "wrong_quote.json"
        expected = cases["expectations"]["wrong_quote" if wrong else mode]
        records = []
        for item in raw["candidates"]:
            record = {**item, "status": expected["candidates"][item["id"]], "attempts": []}
            if record["status"] in {"evaluated", "not_evaluable"}:
                expression = item["expression"]
                if item["id"] in ("alpha006", "canonical_corr"):
                    expression = "-1 * ts_corr(open, volume, 10)"
                record["result"] = {"expression": expression, "metrics": {"mean_rank_ic": 0.125},
                                    "daily": [{"synthetic_mock_test_marker": item["id"] in ("alpha006", "canonical_corr")} ]}
            records.append(record)
        return {"status": expected["run_status"], "candidates": records, "elapsed_seconds": 1.25,
                "tool_calls": 1 if wrong else {"fixed": 10, "normalized_fixed": 12, "agent": 14}[mode]}

    @patch("paper_alpha.benchmark.verify_run", return_value={"verified": True})
    def test_summary_uses_observed_runs_and_has_no_daily_payload(self, verify):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            atomic_json(base / "task.json", self.raw)
            with patch("paper_alpha.benchmark.run_task", side_effect=self._fake_run) as run:
                summary = benchmark(base / "task.json", base / "benchmark")
            self.assertEqual(run.call_count, 7)
            self.assertEqual(verify.call_count, 7)
            self.assertEqual(summary["run_count"], 7)
            self.assertTrue(summary["all_checks_passed"])
            self.assertEqual(summary["runs"]["fixed"]["valid_executable_yield"]["completed"], 2)
            self.assertEqual(summary["runs"]["agent"]["valid_executable_yield"]["completed"], 3)
            self.assertEqual(summary["runs"]["agent"]["valid_executable_yield"]["denominator"], 3)
            self.assertEqual(summary["runs"]["normalized_fixed"]["valid_executable_yield"]["completed"], 3)
            for policy in ("fixed", "normalized_fixed", "agent"):
                groups = summary["runs"][policy]["semantic_group_yield"]
                self.assertEqual(groups["completed"], 2)
                self.assertEqual(groups["denominator"], 2)
            self.assertTrue(summary["consistency"]["normalized_baseline_passed"])
            self.assertEqual(summary["runs"]["agent"]["tool_calls"], 14)
            self.assertEqual(summary["runs"]["agent"]["llm_calls"], 0)
            self.assertGreaterEqual(summary["runs"]["agent"]["run_seconds"], 0)
            self.assertEqual(summary["runs"]["agent"]["workflow_seconds"], 1.25)
            self.assertNotIn('"daily":', json.dumps(summary))
            self.assertIn("semantic_fidelity", summary["unmeasured"])
            self.assertEqual(read_json(base / "benchmark/summary.json"), summary)
            self.assertIn("no LLM", (base / "benchmark/report.md").read_text())
            self.assertTrue((base / "benchmark/case_definitions.json").is_file())
            with self.assertRaisesRegex(ValueError, "already exists"):
                benchmark(base / "task.json", base / "benchmark")

    @patch("paper_alpha.benchmark.verify_run", return_value={"verified": True})
    def test_fresh_repeat_metric_drift_fails_instead_of_claiming_reproduction(self, verify):
        def altered(*args, **kwargs):
            state = self._fake_run(*args, **kwargs)
            if args[1].name == "agent_repeat":
                next(c for c in state["candidates"] if c["id"] == "alpha101")["result"]["metrics"]["mean_rank_ic"] = 0.5
            return state
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            atomic_json(base / "task.json", self.raw)
            with patch("paper_alpha.benchmark.run_task", side_effect=altered):
                summary = benchmark(base / "task.json", base / "benchmark")
            self.assertFalse(summary["all_checks_passed"])
            self.assertFalse(summary["consistency"]["fresh_repeat_passed"])

    @patch("paper_alpha.benchmark.verify_run", return_value={"verified": True})
    def test_normalized_baseline_diagnostic_drift_fails_even_when_metrics_match(self, verify):
        def altered(*args, **kwargs):
            state = self._fake_run(*args, **kwargs)
            if args[1].name == "normalized_fixed":
                next(c for c in state["candidates"] if c["id"] == "alpha101")["result"]["diagnostics"] = {"changed": True}
            return state
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            atomic_json(base / "task.json", self.raw)
            with patch("paper_alpha.benchmark.run_task", side_effect=altered):
                summary = benchmark(base / "task.json", base / "benchmark")
            self.assertFalse(summary["all_checks_passed"])
            self.assertFalse(summary["consistency"]["normalized_baseline_passed"])
            check = next(c for c in summary["consistency"]["normalized_baseline"] if c["left_candidate"] == "alpha101")
            self.assertTrue(check["metrics_equal"])
            self.assertTrue(check["daily_equal"])
            self.assertFalse(check["full_result_equal"])

    @patch("paper_alpha.benchmark.verify_run", return_value={"verified": True})
    def test_unexpected_status_is_visible_and_fails_handling_check(self, verify):
        def altered(*args, **kwargs):
            state = self._fake_run(*args, **kwargs)
            if args[1].name == "agent":
                next(c for c in state["candidates"] if c["id"] == "negative_delay")["status"] = "evaluated"
            return state
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            atomic_json(base / "task.json", self.raw)
            with patch("paper_alpha.benchmark.run_task", side_effect=altered):
                summary = benchmark(base / "task.json", base / "benchmark")
            self.assertFalse(summary["all_checks_passed"])
            checks = summary["runs"]["agent"]["correct_handling"]["checks"]
            self.assertFalse(next(c for c in checks if c["case"] == "negative_delay")["passed"])

    def test_benchmark_rejects_already_repaired_source_case(self):
        self.raw["candidates"][0]["expression"] = "-1 * ts_corr(open, volume, 10)"
        with self.assertRaisesRegex(ValueError, "retain the paper correlation alias"):
            build_cases(self.raw)


if __name__ == "__main__":
    unittest.main()
