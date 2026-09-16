from __future__ import annotations

import unittest

from tests.performance.budget import (
    ANY_FEATURE_ABSOLUTE_MAX,
    NEWSLETTER_TARGET_BUDGET,
    NEWSLETTER_TARGET_CEILING,
    NORMAL_FEATURE_BUDGET,
    PerformanceBudgetExceeded,
    RuntimeBudget,
    assert_runtime_budget,
)


class PerformanceBudgetTests(unittest.TestCase):
    def test_platform_budget_constants(self) -> None:
        self.assertEqual(NORMAL_FEATURE_BUDGET.seconds, 45.0)
        self.assertEqual(ANY_FEATURE_ABSOLUTE_MAX.seconds, 300.0)
        self.assertEqual(NEWSLETTER_TARGET_BUDGET.seconds, 90.0)
        self.assertEqual(NEWSLETTER_TARGET_CEILING.seconds, 180.0)

    def test_assert_runtime_budget_passes_when_under_budget(self) -> None:
        readings = iter([10.0, 12.5])

        with assert_runtime_budget(RuntimeBudget("fast", 3.0), clock=lambda: next(readings)):
            pass

    def test_assert_runtime_budget_fails_when_over_budget(self) -> None:
        readings = iter([10.0, 14.1])

        with self.assertRaises(PerformanceBudgetExceeded):
            with assert_runtime_budget(RuntimeBudget("slow", 3.0), clock=lambda: next(readings)):
                pass


if __name__ == "__main__":
    unittest.main()
