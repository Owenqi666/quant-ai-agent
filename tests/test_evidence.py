from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from paper_alpha.contracts import Evidence
from paper_alpha.evidence import match_evidence, verify_paper
from paper_alpha.storage import atomic_json, read_json

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "alpha101"


class EvidenceTests(unittest.TestCase):
    def test_original_pdf_and_extracted_pages_match(self):
        verify_paper(read_json(EXAMPLE / "paper.json"), EXAMPLE / "paper.pdf")

    def test_fabricated_page_text_cannot_pass_pdf_verification(self):
        paper = deepcopy(read_json(EXAMPLE / "paper.json"))
        paper["pages"][0]["text"] += "\nInvented research result."
        with self.assertRaisesRegex(ValueError, "differs from PDF"):
            verify_paper(paper, EXAMPLE / "paper.pdf")

    def test_changed_pdf_digest_is_rejected(self):
        paper = deepcopy(read_json(EXAMPLE / "paper.json"))
        paper["document_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            verify_paper(paper, EXAMPLE / "paper.pdf")

    def test_quote_needs_correct_page_and_retains_review_status(self):
        paper = read_json(EXAMPLE / "paper.json")
        quote = "(-1 * correlation(open, volume, 10))"
        result = match_evidence(Evidence("e", 8, quote), paper)
        self.assertTrue(result["citation_verified"])
        self.assertEqual(result["semantic_fidelity"], "requires_human_review")
        with self.assertRaisesRegex(ValueError, "absent from page"):
            match_evidence(Evidence("e", 1, quote), paper)

    def test_json_nonfinite_and_duplicate_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for payload in ('{"metric":NaN}', '{"metric":Infinity}', '{"metric":1e999}', '{"budget":1,"budget":1000}'):
                path.write_text(payload)
                with self.assertRaises(ValueError):
                    read_json(path)
            with self.assertRaises(ValueError):
                atomic_json(path, {"metric": float("nan")})

    def test_fixture_regeneration_is_byte_identical(self):
        from scripts.make_fixture import generate
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            generate(folder)
            for name in ("market.csv", "metadata.json"):
                self.assertEqual((folder / name).read_bytes(), (EXAMPLE / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
