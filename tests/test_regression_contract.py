"""Scientific compatibility and independent numerical regression checks.

The small fixture runs the real production tool. Expected values come from the
separate stdlib oracle and hand-computable rank weights, never a saved copy of
production metrics. Deliberately coherent wrong state/result pairs test what
hash consistency alone cannot establish.
"""
from copy import deepcopy
import csv
from datetime import date, timedelta
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.server.regression import _factors, compare_contract, freeze_contract, oracle_support, recorded_semantics, verify_candidate
from paper_alpha.storage import atomic_json, read_json
from paper_alpha.worker import execute


REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples/alpha101"


def make_fixture(root, candidate_id, expression=None, *, temporal_signal=False):
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    source = root / "source/paper_alpha"
    source.mkdir(parents=True)
    for name in ("expressions.py", "evaluation.py"):
        shutil.copyfile(REPO / "paper_alpha" / name, source / name)
    task = read_json(EXAMPLE / "task.json")
    task["candidates"] = [item for item in task["candidates"] if item["id"] == candidate_id]
    hypothesis_id = task["candidates"][0]["hypothesis_id"]
    task["hypotheses"] = [item for item in task["hypotheses"] if item["id"] == hypothesis_id]
    evidence_ids = task["hypotheses"][0]["evidence_ids"]
    task["evidence"] = [item for item in task["evidence"] if item["id"] in evidence_ids]
    dates = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(18)]
    assets = ["A", "B", "C", "D"]
    task["evaluation"] = {"min_assets": 3, "splits": {
        "train": {"start": dates[0], "end": dates[2]},
        "validation": {"start": dates[3], "end": dates[13]},
        "test": {"start": dates[14], "end": dates[17]}}}
    if expression is not None:
        task["candidates"][0].update(expression=expression, origin="user_modification",
                                     changes=["Intentional engineering expression override"])
        task["hypotheses"][0]["required_fields"] = ["open", "high", "low", "close", "volume"]
    atomic_json(inputs / "task.json", task)
    for name in ("paper.pdf", "paper.json"):
        shutil.copyfile(EXAMPLE / name, inputs / name)
    with (inputs / "market.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "asset", "open", "high", "low", "close", "volume"])
        for t, day in enumerate(dates):
            for a, asset in enumerate(assets):
                opening = 30 + a * 10 + (a + 1) * t + (t % 3) * a / 10
                movement = ((a + 1) * ((t % 5) - 2) * .1) if temporal_signal else 0
                writer.writerow([day, asset, opening, opening + 2, opening - 2,
                                 opening + (a - 1) * .5 + movement, 1000 + (a + 1) * t * t + ((t + a) % 4) * 3])
    metadata = read_json(EXAMPLE / "metadata.json")
    metadata.update(calendar_dates=dates, universe=assets, sessions=len(dates),
                    rows=len(dates) * len(assets),
                    market_sha256=hashlib.sha256((inputs / "market.csv").read_bytes()).hexdigest())
    atomic_json(inputs / "metadata.json", metadata)
    preflight_request = {"action": "preflight", "inputs": str(inputs)}
    preflight = execute(preflight_request)
    tool_dir = root / "tools/0001"
    tool_dir.mkdir(parents=True)
    atomic_json(tool_dir / "request.json", preflight_request)
    atomic_json(tool_dir / "response.json", {"ok": True, "value": preflight})
    expression = task["candidates"][0]["expression"].replace("correlation(", "ts_corr(")
    output = root / "candidates" / candidate_id / "attempt-1"
    result = execute({"action": "compute_evaluate", "inputs": str(inputs),
                      "expression": expression, "output_dir": str(output)})
    artifact_paths = {str((output / name).relative_to(root)): "integrity-checked-by-caller"
                      for name in ("factor.csv", "result.json")}
    state = {"preflight": preflight,
             "tool_artifacts": {"tools/0001/request.json": "integrity-checked-by-caller"},
             "candidates": [{**task["candidates"][0], "status": result["status"],
                             "result": result, "artifacts": artifact_paths}]}
    atomic_json(root / "state.json", state)
    return task


class ScientificContractTests(unittest.TestCase):
    def setUp(self):
        self.task = read_json(EXAMPLE / "task.json")
        self.metadata = read_json(EXAMPLE / "metadata.json")

    def freeze(self, task=None, metadata=None):
        return freeze_contract(task or self.task, "alpha006", "paper-digest", "data-digest",
                               metadata or self.metadata)

    def test_alias_repair_and_display_or_budget_change_remain_comparable(self):
        original = self.freeze()
        edited = deepcopy(self.task)
        edited["title"] = "A new display title"
        edited["paper"] = "different-local-path.json"
        edited["budget"]["max_attempts_per_candidate"] = 4
        edited["candidates"][0]["expression"] = "-1 * ts_corr(open, volume, 10)"
        self.assertEqual(compare_contract(original, self.freeze(edited)), [])

    def test_changed_window_field_hypothesis_evidence_and_split_are_incompatible(self):
        original = self.freeze()
        changes = [
            (lambda task: task["candidates"][0].update(expression="-1 * ts_corr(open, volume, 5)"), "candidate.expression"),
            (lambda task: task["candidates"][0].update(expression="-1 * ts_corr(close, volume, 10)"), "candidate.expression"),
            (lambda task: task["hypotheses"][0].update(signal_direction="Lower values are long"), "hypothesis.signal_direction"),
            (lambda task: task["evidence"][0].update(page=9), "evidence[0].page"),
            (lambda task: task["evaluation"]["splits"]["validation"].update(end="2023-03-30"), "evaluation.splits.validation.end"),
        ]
        for mutate, path in changes:
            with self.subTest(path=path):
                edited = deepcopy(self.task)
                mutate(edited)
                self.assertIn(path, compare_contract(original, self.freeze(edited)))

    def test_metadata_is_frozen_and_returned_contract_has_no_shared_mutable_state(self):
        frozen = self.freeze()
        self.metadata["field_availability"]["open"] = "after session close"
        self.task["hypotheses"][0]["claim"] = "changed later"
        paths = compare_contract(frozen, self.freeze())
        self.assertIn("data_semantics.field_availability.open", paths)
        self.assertIn("hypothesis.claim", paths)
        self.assertEqual(frozen["data_semantics"]["field_availability"]["open"], "session open")

    def test_no_unreviewed_operator_or_algebraic_equivalence_is_assumed(self):
        task = deepcopy(self.task)
        task["candidates"][0]["expression"] = "sum(open, 10)"
        other = deepcopy(task)
        other["candidates"][0]["expression"] = "ts_sum(open, 10)"
        self.assertIn("candidate.expression", compare_contract(self.freeze(task), self.freeze(other)))

    def test_unrelated_candidate_does_not_change_this_case(self):
        expected = self.freeze()
        self.task["candidates"][1]["expression"] = "open"
        self.task["hypotheses"][1]["claim"] = "another hypothesis"
        self.assertEqual(compare_contract(expected, self.freeze()), [])


class IndependentOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.base = Path(cls.fixtures.name)
        cls.tasks = {candidate: make_fixture(cls.base / candidate, candidate)
                     for candidate in ("alpha006", "alpha101")}
        cls.tasks["unsupported"] = make_fixture(cls.base / "unsupported", "alpha101", "open")
        cls.tasks["delta_sign"] = make_fixture(cls.base / "delta_sign", "alpha101", "sign(ts_delta(volume, 1)) * (-1 * ts_delta(close, 1))")
        cls.tasks["cross_rank"] = make_fixture(cls.base / "cross_rank", "alpha101", "rank(-1 * (1 - open / close))")

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "run"
        shutil.copytree(self.base / "alpha101", self.root)
        self.task = deepcopy(self.tasks["alpha101"])

    def tearDown(self):
        self.temp.cleanup()

    def check(self):
        return verify_candidate(self.root, "alpha101", self.task)

    def mutate_result(self, mutate):
        state = read_json(self.root / "state.json")
        result = state["candidates"][0]["result"]
        mutate(result)
        # Keep the duplicate tool-result record coherent: digest verification
        # alone could accept this pair if the production calculation were wrong.
        atomic_json(self.root / "candidates/alpha101/attempt-1/result.json", result)
        atomic_json(self.root / "state.json", state)

    def test_two_supported_formulas_pass_independent_reference(self):
        for candidate in ("alpha006", "alpha101"):
            with self.subTest(candidate=candidate):
                result = verify_candidate(self.base / candidate, candidate, self.tasks[candidate])
                self.assertEqual(result["outcome"], "passed", result)
                self.assertEqual([item["name"] for item in result["checks"]],
                                 ["evidence_provenance", "factor_values", "daily_numerics", "aggregate_metrics"])
        saved = read_json(self.root / "state.json")["candidates"][0]["result"]
        weights = [item["weight"] for item in saved["daily"][0]["assets"]]
        self.assertEqual(weights, [-.375, -.125, .125, .375])
        self.assertEqual(saved["metrics"]["purged_days"], 2)

    def test_wrong_factor_cell_fails_even_if_status_is_evaluated(self):
        path = self.root / "candidates/alpha101/attempt-1/factor.csv"
        with path.open(newline="") as stream:
            rows = list(csv.reader(stream))
        rows[1][1] = str(float(rows[1][1]) + .1)
        with path.open("w", newline="") as stream:
            csv.writer(stream).writerows(rows)
        result = self.check()
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(next(item for item in result["checks"] if item["name"] == "factor_values")["outcome"], "failed")

    def test_wrong_forward_return_weight_date_and_metric_are_independently_detected(self):
        mutations = [
            lambda result: result["daily"][0]["assets"][0].update(forward_return=123.0),
            lambda result: result["daily"][0]["assets"][0].update(weight=.9),
            lambda result: result["daily"][0].update(entry_date=result["daily"][0]["signal_date"]),
            lambda result: result["metrics"].update(mean_rank_ic=.123456789),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                shutil.rmtree(self.root)
                shutil.copytree(self.base / "alpha101", self.root)
                self.mutate_result(mutate)
                result = self.check()
                self.assertEqual(result["outcome"], "failed", result)
                self.assertTrue(any(item["name"] in {"daily_numerics", "aggregate_metrics"}
                                    and item["outcome"] == "failed" for item in result["checks"]))

    def test_missing_numeric_field_cannot_pass(self):
        self.mutate_result(lambda result: result["metrics"].pop("rank_ic_days"))
        self.assertEqual(self.check()["outcome"], "failed")

    def test_discrete_counts_require_exact_integers(self):
        self.mutate_result(lambda result: result["metrics"].update(
            rank_ic_days=float(result["metrics"]["rank_ic_days"])))
        self.assertEqual(self.check()["outcome"], "failed")

    def test_nonfinite_factor_cannot_pass(self):
        path = self.root / "candidates/alpha101/attempt-1/factor.csv"
        with path.open(newline="") as stream:
            rows = list(csv.reader(stream))
        rows[1][1] = "NaN"
        with path.open("w", newline="") as stream:
            csv.writer(stream).writerows(rows)
        self.assertEqual(self.check()["outcome"], "failed")

    def test_future_test_factor_row_is_rejected(self):
        path = self.root / "candidates/alpha101/attempt-1/factor.csv"
        with path.open("a", newline="") as stream:
            csv.writer(stream).writerow(["2024-01-15", 1, 2, 3, 4])
        result = self.check()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("no test signals", result["checks"][-1]["reason"])

    def test_coherently_wrong_evidence_offset_fails_fresh_pdf_check(self):
        state = read_json(self.root / "state.json")
        state["preflight"]["evidence"][0]["normalized_start"] += 1
        atomic_json(self.root / "state.json", state)
        atomic_json(self.root / "tools/0001/response.json", {"ok": True, "value": state["preflight"]})
        result = self.check()
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["checks"][0]["outcome"], "failed")

    def test_missing_actual_preflight_tool_response_is_not_quote_verification(self):
        (self.root / "tools/0001/response.json").unlink()
        self.assertEqual(self.check()["checks"][0]["outcome"], "failed")

    def test_unsupported_formula_is_not_a_numerical_pass(self):
        result = verify_candidate(self.base / "unsupported", "alpha101", self.tasks["unsupported"])
        self.assertEqual(result["outcome"], "not_comparable", result)
        self.assertEqual(result["checks"][0]["outcome"], "passed")
        self.assertEqual(result["checks"][-1]["outcome"], "not_comparable")
        self.assertEqual(result["checks"][-1]["reason_code"], "oracle_unsupported_formula")

    def test_new_small_formulas_match_actual_engine_outputs(self):
        for name in ("delta_sign", "cross_rank"):
            with self.subTest(formula=name):
                result = verify_candidate(self.base / name, "alpha101", self.tasks[name])
                self.assertEqual(result["outcome"], "passed", result)
                self.assertTrue(all(item.get("reason_code") for item in result["checks"]))

    def test_new_formula_hand_calculations_cover_ties_zero_and_warmup(self):
        assets, dates = ["A", "B", "C", "D"], ["2024-01-01", "2024-01-02"]
        rows = {}
        for asset, opening, close in zip(assets, [10, 10, 20, 20], [10, 20, 40, 10]):
            rows[dates[0], asset] = {"open": opening, "close": close, "volume": 100}
        for asset, dc, dv in zip(assets, [1, 2, 3, -2], [2, 0, -2, 1]):
            rows[dates[1], asset] = {**rows[dates[0], asset], "close": rows[dates[0], asset]["close"] + dc, "volume": 100 + dv}
        rank = _factors("alpha033_modified", dates, assets, rows, 0)
        # open/close - 1 is [0, -.5, -.5, 1]; tied average ranks / 4.
        self.assertEqual([rank[dates[0], asset] for asset in assets], [.75, .375, .375, 1.])
        delta = _factors("alpha012", dates, assets, rows, 1)
        self.assertEqual([delta[dates[0], asset] for asset in assets], [None] * 4)
        self.assertEqual([delta[dates[1], asset] for asset in assets], [-1., 0., 3., 2.])
        self.assertTrue(oracle_support("sign(delta(volume, 1)) * (-1 * delta(close, 1))")["supported"])
        for formula in ("alpha012", "alpha033_modified"):
            full = _factors(formula, dates, assets, rows, 1)
            prefix = _factors(formula, dates[:1], assets, rows, 0)
            self.assertEqual({key: value for key, value in full.items() if key[0] == dates[0]}, prefix)

    def test_historical_semantics_are_read_without_using_current_constants(self):
        expected = recorded_semantics(self.root)
        with patch("paper_alpha.server.regression.OPERATOR_SEMANTICS_VERSION", "future-version"), \
                patch("paper_alpha.server.regression.EXECUTION", {"entry": "different"}):
            self.assertEqual(recorded_semantics(self.root), expected)

    def test_unsafe_duplicate_or_missing_historical_semantics_are_rejected_without_execution(self):
        path = self.root / "source/paper_alpha/expressions.py"
        marker = self.root / "must-not-be-created"
        bad_sources = [
            f"OPERATOR_SEMANTICS_VERSION = __import__('pathlib').Path({str(marker)!r}).touch()",
            "OPERATOR_SEMANTICS_VERSION = 'v1'\nOPERATOR_SEMANTICS_VERSION = 'v2'\n",
            "UNRELATED = 'v1'\n",
        ]
        for source in bad_sources:
            with self.subTest(source=source):
                path.write_text(source)
                with self.assertRaises(ValueError):
                    recorded_semantics(self.root)
                self.assertFalse(marker.exists())

    def test_unknown_historical_operator_semantics_require_oracle_review(self):
        path = self.root / "source/paper_alpha/expressions.py"
        path.write_text("OPERATOR_SEMANTICS_VERSION = 'future-version'\n")
        result = self.check()
        self.assertEqual(result["outcome"], "not_comparable")

    def test_reported_execution_semantics_must_match_recorded_source(self):
        self.mutate_result(lambda result: result["execution"].update(entry="session t open"))
        result = self.check()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("execution semantics", result["checks"][-1]["reason"])

    def test_escaping_artifact_path_and_changed_task_are_rejected(self):
        state = read_json(self.root / "state.json")
        state["candidates"][0]["artifacts"] = {"../../factor.csv": "x", "../../result.json": "x"}
        atomic_json(self.root / "state.json", state)
        self.assertEqual(self.check()["outcome"], "failed")
        self.task["hypotheses"][0]["claim"] = "A changed scientific claim"
        result = self.check()
        self.assertEqual(result["checks"][0]["name"], "recorded_inputs")
        self.assertEqual(result["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
