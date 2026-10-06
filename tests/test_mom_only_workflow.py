"""Synthetic persistence/verification faults, not vendor or paper acceptance."""
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from paper_alpha import french_industry as source, mom_only as core
from paper_alpha import mom_only_workflow as workflow
from paper_alpha.storage import atomic_json, digest, read_json

ROOT = Path(__file__).resolve().parents[1]


class WorkflowIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        months = [f"{year:04d}{month:02d}" for year in range(2009, 2012) for month in range(1, 13)]
        text = ["This file was created using the 202608 CRSP database.", source.TITLE, "",
                source.SECTION, "," + ",".join(source.ASSETS)]
        for index, month in enumerate(months):
            text.append(month + "," + ",".join(str(.1 + asset * .02 + index * .001) for asset in range(49)))
        text.extend(["201201," + ",".join("NOT_PARSED" for _ in range(49)), ""])
        self.archive = self.root / "synthetic.zip"
        with ZipFile(self.archive, "w") as output:
            output.writestr("49_Industry_Portfolios.csv", "\n".join(text))
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.config = self.root / "config.json"
        atomic_json(self.config, core.default_config())
        self.method = deepcopy(read_json(ROOT / "docs/research/mom_only_contract.json"))
        self.paper_bytes = b"synthetic test paper; not the original source"
        self.paper_hash = hashlib.sha256(self.paper_bytes).hexdigest()
        self.method["paper"]["document_sha256"] = self.paper_hash
        for item in self.method["evidence"]:
            item["document_sha256"] = self.paper_hash
        for item in self.method["sources"]:
            item["sha256"] = hashlib.sha256(("synthetic author stub " + item["id"]).encode()).hexdigest()
        self.method["illustrative_defaults"]["source_candidate"]["raw_sha256"] = self.sha
        self.method["contract_digest"] = digest({k: v for k, v in self.method.items() if k != "contract_digest"})
        self.method_path = self.root / "method.json"
        atomic_json(self.method_path, self.method)
        # Replace evidence only. The real parser, calculator, oracle, report,
        # snapshot inventory and disk persistence execute in these tests.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(workflow, "PAPER_SHA256", self.paper_hash))
        stack.enter_context(patch.object(workflow, "SUPPORTED_METHOD_DIGEST", self.method["contract_digest"]))
        stack.enter_context(patch.object(workflow, "_evidence_snapshot", side_effect=self._evidence))
        stack.enter_context(patch.object(workflow, "_verify_paper_anchors", return_value=None))

    def _evidence(self, out, method, evidence_dir=None):
        (out / "inputs/paper.pdf").write_bytes(self.paper_bytes)
        for item in method["sources"]:
            path = out / "inputs/author-code" / Path(item["path"]).name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("synthetic author stub " + item["id"]).encode())

    def _run(self, name="run", expected=None):
        return workflow.run(self.archive, self.config, self.method_path,
                            self.root / name, expected or self.sha)

    def _rehash_manifest(self, out):
        state = read_json(out / "state.json")
        workflow._manifest(out, state)

    def test_complete_source_result_reference_report_and_read_only_verification(self):
        result = self._run()
        self.assertTrue(result["calculation_verified"])
        out = self.root / "run"
        before = workflow._inventory(out)
        self.assertEqual(result, workflow.verify(out))
        self.assertEqual(before, workflow._inventory(out))
        state = read_json(out / "state.json")
        self.assertIsNone(state["human_judgments"])
        self.assertIsNone(state["semantic_quality_score"])
        self.assertFalse(result["reserved_evaluated"])

    def test_second_attempt_cannot_overwrite_saved_directory(self):
        self._run()
        before = workflow._inventory(self.root / "run")
        with self.assertRaises(FileExistsError):
            self._run()
        self.assertEqual(before, workflow._inventory(self.root / "run"))

    def _snapshot_subprocess(self, out, program, arguments=(), bytecode_disabled=True):
        # Use a separate interpreter and only the saved source tree as its import
        # path. Environment defaults must not hide the missing -B regression.
        environment = os.environ.copy()
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        environment["PYTHONPATH"] = str(out / "source")
        command = [sys.executable]
        if bytecode_disabled:
            command.append("-B")
        command.extend(["-c", program, *map(str, arguments)])
        completed = subprocess.run(command, cwd=self.root, env=environment,
                                   capture_output=True, text=True, timeout=30,
                                   check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_frozen_source_subprocess_replay_with_b_preserves_original_artifact(self):
        self._run()
        original = self.root / "run"
        before = workflow._inventory(original)
        manifest_bytes = (original / "manifest.json").read_bytes()
        replay = self.root / "snapshot-replay"
        # The subprocess deliberately replaces only paper evidence with the
        # same synthetic stubs used by this test. Its parser, calculation,
        # independent reference, report, code snapshots and verify are real.
        program = """
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from paper_alpha import mom_only_workflow as workflow
from paper_alpha.storage import read_json

original, replay = map(Path, sys.argv[1:])
method = read_json(original / 'inputs/method.json')
paper_bytes = b'synthetic test paper; not the original source'
def evidence(out, method, evidence_dir=None):
    (out / 'inputs/paper.pdf').write_bytes(paper_bytes)
    for item in method['sources']:
        path = out / 'inputs/author-code' / Path(item['path']).name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('synthetic author stub ' + item['id']).encode())

with patch.object(workflow, 'PAPER_SHA256', hashlib.sha256(paper_bytes).hexdigest()), \\
     patch.object(workflow, 'SUPPORTED_METHOD_DIGEST', method['contract_digest']), \\
     patch.object(workflow, '_evidence_snapshot', side_effect=evidence), \\
     patch.object(workflow, '_verify_paper_anchors', return_value=None):
    status = workflow.run(original / 'inputs/source.zip',
                          original / 'inputs/config.json',
                          original / 'inputs/method.json', replay,
                          method['illustrative_defaults']['source_candidate']['raw_sha256'])
    original_status = workflow.verify(original)
print(json.dumps({'module_path': str(Path(workflow.__file__).resolve()),
                  'snapshot_root': str(workflow.ROOT.resolve()),
                  'dont_write_bytecode': sys.dont_write_bytecode,
                  'verification': status, 'original_verification': original_status}))
"""
        completed = self._snapshot_subprocess(original, program, (original, replay))
        self.assertEqual(completed["module_path"],
                         str(original / "source/paper_alpha/mom_only_workflow.py"))
        self.assertEqual(completed["snapshot_root"], str(original / "source"))
        self.assertTrue(completed["dont_write_bytecode"])
        self.assertTrue(completed["verification"]["calculation_verified"])
        self.assertTrue(completed["verification"]["reference_passed"])
        self.assertFalse(completed["verification"]["reserved_evaluated"])
        self.assertTrue(completed["original_verification"]["calculation_verified"])
        self.assertEqual(before, workflow._inventory(original))
        self.assertEqual(manifest_bytes, (original / "manifest.json").read_bytes())
        self.assertEqual(list((original / "source").rglob("__pycache__")), [])
        self.assertEqual(list((original / "source").rglob("*.pyc")), [])
        self.assertTrue(workflow.verify(original)["calculation_verified"])
        self.assertTrue(workflow.verify(replay)["calculation_verified"])
        for name in ("result.json", "reference.json", "report.md"):
            with self.subTest(file=name):
                self.assertEqual((original / name).read_bytes(), (replay / name).read_bytes())

    def test_frozen_source_import_without_b_is_rejected_as_extra_cache_files(self):
        self._run()
        original = self.root / "run"
        before = workflow._inventory(original)
        program = """
import json
from pathlib import Path
import sys
from paper_alpha import mom_only_workflow as workflow
print(json.dumps({'module_path': str(Path(workflow.__file__).resolve()),
                  'dont_write_bytecode': sys.dont_write_bytecode}))
"""
        completed = self._snapshot_subprocess(original, program, bytecode_disabled=False)
        self.assertEqual(completed["module_path"],
                         str(original / "source/paper_alpha/mom_only_workflow.py"))
        self.assertFalse(completed["dont_write_bytecode"])
        self.assertTrue(list((original / "source").rglob("*.pyc")))
        after = workflow._inventory(original)
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertTrue(set(after) - set(before))
        with self.assertRaisesRegex(ValueError, "inventory or bytes changed"):
            workflow.verify(original)

    def test_source_hash_failure_keeps_inspectable_failed_attempt(self):
        with self.assertRaisesRegex(ValueError, "expected SHA256"):
            self._run(expected="0" * 64)
        result = workflow.verify(self.root / "run")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["calculation_verified"])
        self.assertEqual(read_json(self.root / "run/state.json")["attempts"], 1)
        self.assertEqual(read_json(self.root / "run/invocation.json")["expected_source_sha256"], "0" * 64)
        self.assertEqual(read_json(self.root / "run/inputs/config.json"), core.default_config())
        self.assertEqual(read_json(self.root / "run/inputs/method.json"), self.method)

    def test_report_changed_even_with_rehashed_inventory_is_rejected(self):
        self._run()
        out = self.root / "run"
        with (out / "report.md").open("a") as output:
            output.write("Invented profitability statement\n")
        self._rehash_manifest(out)
        with self.assertRaisesRegex(ValueError, "Report differs"):
            workflow.verify(out)

    def test_panel_changed_even_with_rehashed_inventory_is_rejected(self):
        self._run()
        out = self.root / "run"
        panel = read_json(out / "panel.json")
        panel["returns"][0]["value"] += .02
        atomic_json(out / "panel.json", panel)
        self._rehash_manifest(out)
        with self.assertRaisesRegex(ValueError, "conversion does not match"):
            workflow.verify(out)

    def test_result_changed_even_with_rehashed_inventory_is_rejected(self):
        self._run()
        out = self.root / "run"
        result = read_json(out / "result.json")
        result["summary"][0]["mean_gross_return"] = 99.0
        atomic_json(out / "result.json", result)
        self._rehash_manifest(out)
        with self.assertRaisesRegex(ValueError, "numerical verification failed"):
            workflow.verify(out)

    def test_code_changed_during_attempt_is_failed_and_retained(self):
        with patch.object(workflow, "_code_fence", side_effect=ValueError("Computation code changed during the attempt")):
            with self.assertRaisesRegex(ValueError, "code changed"):
                self._run()
        self.assertEqual(workflow.verify(self.root / "run")["status"], "failed")

    def test_recomputed_method_checksum_cannot_change_supported_formula_or_sources(self):
        for mutation in ("formula", "sources", "evidence"):
            with self.subTest(mutation=mutation):
                value = deepcopy(self.method)
                if mutation == "formula":
                    value["paper_signal"]["formula"] = "Return(i,H+1)"
                elif mutation == "sources":
                    value["sources"] = []
                else:
                    value["evidence"] = [value["evidence"][-1]]
                value["contract_digest"] = digest({k: v for k, v in value.items() if k != "contract_digest"})
                with self.assertRaisesRegex(ValueError, "fixed research boundary"):
                    workflow._validate_method(value, core.default_config(), self.sha)

    def test_receipt_must_bind_the_actual_raw_archive(self):
        receipt = {"files": [{"url": core.SOURCE_URL, "sha256": "0" * 64}]}
        receipt_path = self.root / "receipt.json"
        atomic_json(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "exact archive"):
            workflow.run(self.archive, self.config, self.method_path, self.root / "bad-receipt", self.sha, receipt_path)
        self.assertEqual(workflow.verify(self.root / "bad-receipt")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
