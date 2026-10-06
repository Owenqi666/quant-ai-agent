"""Cross-family scheduling invariants and typed HTTP domain boundaries."""
import unittest
from unittest.mock import patch

from paper_alpha.server.runner import Worker
from tests.test_monthly_runner import QueueStore


class IndustryMomSchedulingTests(unittest.TestCase):
    def test_three_busy_families_do_not_starve_and_keep_fifo(self):
        from paper_alpha.server import monthly_runner, industry_mom_runner
        worker = Worker(QueueStore('daily', 3), -1)
        worker.monthly = QueueStore('monthly', 3)
        worker.industry_mom = QueueStore('industry', 3)
        seen = []
        with patch.object(worker, 'execute', side_effect=lambda job: seen.append(job['id'])), \
                patch.object(monthly_runner, 'execute', side_effect=lambda _, job: seen.append(job['id'])), \
                patch.object(industry_mom_runner, 'execute', side_effect=lambda _, job: seen.append(job['id'])):
            for _ in range(9):
                self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
        self.assertEqual(seen, [f'{kind}-{number}' for number in range(3)
                                for kind in ('daily', 'monthly', 'industry')])

    def test_once_recovers_all_families_but_consumes_only_one(self):
        from paper_alpha.server import monthly_runner, industry_mom_runner
        daily, monthly, industry = [QueueStore(name, 2) for name in ('daily', 'monthly', 'industry')]
        worker = Worker(daily, -1)
        worker.monthly, worker.industry_mom = monthly, industry
        with patch.object(worker, 'execute') as daily_execute, \
                patch.object(monthly_runner, 'execute') as monthly_execute, \
                patch.object(industry_mom_runner, 'execute') as industry_execute:
            worker.run(once=True)
        daily_execute.assert_called_once()
        monthly_execute.assert_not_called()
        industry_execute.assert_not_called()
        self.assertEqual([daily.recovered, monthly.recovered, industry.recovered], [1, 1, 1])
        self.assertEqual([len(daily.jobs), len(monthly.jobs), len(industry.jobs)], [1, 2, 2])


if __name__ == '__main__':
    unittest.main()
