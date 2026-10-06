import json
from pathlib import Path
import unittest

from paper_alpha.contracts import Task
from paper_alpha.evidence import match_evidence, normalize
from paper_alpha.proposals import PROVIDER, draft_task


ROOT = Path(__file__).resolve().parents[1]


class ProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paper = json.loads((ROOT / "examples/alpha101/paper.json").read_text())

    def draft(self, paper=None, **kwargs):
        options = {
            "paper_path": "paper.json",
            "pdf_path": "paper.pdf",
            "data_path": "market.csv",
            "metadata_path": "metadata.json",
            "evaluation": {"validation_start": "2020-01-01"},
        }
        options.update(kwargs)
        return draft_task(self.paper if paper is None else paper, **options)

    def test_real_paper_formulas_are_found_at_exact_pages(self):
        task = Task.parse(self.draft())
        self.assertEqual([c.id for c in task.candidates], ["alpha006", "alpha101", "alpha005"])
        self.assertEqual({e.id: e.page for e in task.evidence}, {
            "alpha006-formula": 8, "alpha101-formula": 15, "alpha005-formula": 8,
        })
        for evidence in task.evidence:
            matched = match_evidence(evidence, self.paper)
            text = normalize(next(p["text"] for p in self.paper["pages"] if p["page"] == evidence.page))
            self.assertEqual(text[matched["normalized_start"]:matched["normalized_end"]], evidence.quote)
        self.assertEqual(task.candidates[0].expression, "(-1 * correlation(open, volume, 10))")
        self.assertEqual(task.candidates[1].expression, "((close - open) / ((high - low) + .001))")

    def test_title_alone_cannot_create_proposals(self):
        paper = {"title": "101 Formulaic Alphas", "pages": [{"page": 8, "text": "No formula here."}]}
        with self.assertRaisesRegex(ValueError, "No reviewed formula template matched"):
            self.draft(paper)

    def test_no_match_is_explicit_not_general_agent_output(self):
        paper = {"title": "Unreviewed research", "pages": [{"page": 1, "text": "A new hypothesis."}]}
        with self.assertRaisesRegex(ValueError, "not a general paper-reading Agent"):
            self.draft(paper)

    def test_formula_must_have_correct_source_label(self):
        paper = {"pages": [{"page": 8, "text": "Alpha#60: (-1 * correlation(open, volume, 10))"}]}
        with self.assertRaisesRegex(ValueError, "No reviewed formula"):
            self.draft(paper)

    def test_changed_formula_is_not_silently_repaired(self):
        paper = {"pages": [{"page": 8, "text": "Alpha#6: (-1 * correlation(open, volume, 20))"}]}
        with self.assertRaisesRegex(ValueError, "No reviewed formula"):
            self.draft(paper)

    def test_partial_match_uses_actual_page_and_normalized_source(self):
        paper = {"title": "An irrelevant title", "pages": [{
            "page": 23, "text": "Alpha#101:\n ((close - open) / ((high - low) + .001))\n",
        }]}
        task = self.draft(paper)
        self.assertEqual([c["id"] for c in task["candidates"]], ["alpha101"])
        self.assertEqual(task["evidence"][0]["page"], 23)
        self.assertIn(task["evidence"][0]["quote"], normalize(paper["pages"][0]["text"]))

    def test_attribution_missing_fields_and_no_substitutions(self):
        task = self.draft()
        fields = {h["id"]: h["required_fields"] for h in task["hypotheses"]}
        self.assertEqual(fields["h-alpha006"], ["open", "volume"])
        self.assertEqual(fields["h-alpha101"], ["close", "open", "high", "low"])
        self.assertEqual(fields["h-alpha005"], ["open", "close", "vwap"])
        self.assertIn("engineering demonstration", task["title"])
        self.assertIn("personal study unconfirmed", task["title"])
        for hypothesis in task["hypotheses"]:
            self.assertEqual(hypothesis["attribution"], "paper_original")
            self.assertEqual(hypothesis["mechanism_attribution"], "model_conjecture")
            self.assertIn("human review", hypothesis["economic_mechanism"])
            assumptions = " ".join(hypothesis["assumptions"])
            self.assertIn(PROVIDER, assumptions)
            self.assertIn("no LLM calls", assumptions)
            self.assertIn("local evaluation convention", assumptions)
            self.assertIn("no substitute field", assumptions)
            self.assertIn("do not reproduce the paper's backtest", assumptions)
        for candidate in task["candidates"]:
            self.assertEqual(candidate["origin"], "paper_original")
            self.assertEqual(candidate["changes"], [])

    def test_ambiguous_page_match_is_rejected(self):
        text = "Alpha#6: (-1 * correlation(open, volume, 10))"
        paper = {"pages": [{"page": 8, "text": text}, {"page": 9, "text": text}]}
        with self.assertRaisesRegex(ValueError, "Ambiguous reviewed formula location"):
            self.draft(paper)

    def test_duplicate_page_numbers_are_rejected(self):
        paper = {"pages": [{"page": 8, "text": "a"}, {"page": 8, "text": "b"}]}
        with self.assertRaisesRegex(ValueError, "Duplicate extracted page number"):
            self.draft(paper)

    def test_invalid_page_numbers_are_rejected(self):
        for page in [True, 0, "8"]:
            with self.subTest(page=page), self.assertRaisesRegex(ValueError, "positive integer"):
                self.draft({"pages": [{"page": page, "text": "No formula"}]})

    def test_evaluation_is_copied_and_task_contract_is_valid(self):
        evaluation = {"bounds": {"validation": ["2020-01-01", "2020-12-31"]}}
        task = self.draft(evaluation=evaluation)
        evaluation["bounds"]["validation"][0] = "MUTATED"
        self.assertEqual(task["evaluation"]["bounds"]["validation"][0], "2020-01-01")
        self.assertEqual(Task.parse(task).to_dict(), task)


if __name__ == "__main__":
    unittest.main()
