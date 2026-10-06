"""Measurement accounting and pagination guards without running a benchmark."""
import unittest

from scripts.measure_workbench import page_round, sample_summary


class PerformanceContractTests(unittest.TestCase):
    def test_sample_statistics_keep_every_observation_and_empty_is_unmeasured(self):
        result = sample_summary([.5, 100, 1])
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["samples"], [.5, 100, 1])
        self.assertEqual((result["median"], result["minimum"], result["maximum"]), (1, .5, 100))
        self.assertIsNone(sample_summary([])["median"])
        self.assertEqual(sample_summary([])["sample_count"], 0)
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ValueError):
                sample_summary([value])

    def test_complete_page_round_uses_fixed_watermark_and_counts_calls(self):
        requests = []
        def fetch(after, limit, through):
            requests.append((after, limit, through))
            items = [{"id": value} for value in (2, 5, 9) if value > after][:limit]
            cursor = items[-1]["id"] if items else after
            return {"items": items, "next_cursor": cursor, "has_more": cursor < 9,
                    "high_watermark": 9, "total_records": 3}
        result = page_round(fetch, expected_count=3, id_field="id", page_size=2)
        self.assertEqual(requests, [(0, 2, None), (5, 2, 9)])
        self.assertEqual(result["record_count"], 3)
        self.assertEqual(result["unique_record_count"], 3)
        self.assertEqual(result["page_count"], len(result["page_samples_seconds"]))

    def test_page_measurement_rejects_duplicates_and_stalled_cursor(self):
        def duplicates(after, limit, through):
            return {"items": [{"id": 1}, {"id": 1}], "next_cursor": 1, "has_more": False,
                    "high_watermark": 1, "total_records": 2}
        with self.assertRaisesRegex(ValueError, "duplicated"):
            page_round(duplicates, expected_count=2, id_field="id", page_size=2)
        def stalled(after, limit, through):
            return {"items": [], "next_cursor": 0, "has_more": True, "high_watermark": 1, "total_records": 2}
        with self.assertRaisesRegex(ValueError, "did not advance"):
            page_round(stalled, expected_count=2, id_field="id", page_size=2)


if __name__ == "__main__":
    unittest.main()
