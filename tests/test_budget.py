"""Native budgets count sleep: a Grok writer outlived its 7200 seconds by 13 hours of macOS maintenance sleep (#137)."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
import unittest
from unittest.mock import patch

import factory


class Budget(unittest.TestCase):
    def test_sleep_the_loop_clock_missed_still_ends_the_attempt(self):
        readings = iter([1.0, 10_000.0])  # A check while running, then the first check after the Mac woke.
        cancelled = []
        async def worker():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        with patch.object(factory, "BUDGET_CHECK_SECONDS", 0.01), self.assertRaisesRegex(TimeoutError, "allowance expired"):
            asyncio.run(factory.within_budget(worker(), deadline=7200, clock=lambda: next(readings)))
        self.assertEqual(cancelled, [True])

    def test_a_finished_or_failing_worker_returns_or_raises_as_itself(self):
        async def done():
            return 0
        async def failing():
            raise TimeoutError("fixture")
        self.assertEqual(asyncio.run(factory.within_budget(done(), deadline=factory.native_clock() + 30)), 0)
        with self.assertRaisesRegex(TimeoutError, "fixture"):
            asyncio.run(factory.within_budget(failing(), deadline=factory.native_clock() + 30))

    def test_the_budget_clock_counts_sleep(self):
        import time
        self.assertEqual(factory.BUDGET_CLOCK, getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


if __name__ == "__main__":
    unittest.main()
