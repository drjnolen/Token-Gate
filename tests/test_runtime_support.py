import unittest
from unittest.mock import Mock, patch
from concurrent.futures import Future, ThreadPoolExecutor

from runtime_support import RuntimeMetrics, SlidingWindowRateLimiter, bounded_executor_map


class BoundedMapTests(unittest.TestCase):
    def test_output_matches_map_for_empty_and_nonempty_inputs(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(bounded_executor_map(executor, abs, [])), [])
            self.assertEqual(
                list(bounded_executor_map(executor, abs, range(-50, 50), max_pending=3)),
                list(map(abs, range(-50, 50))),
            )

    def test_window_is_bounded_and_close_cancels_queued_work(self):
        executor = Mock()
        futures = []

        def submit(function, value):
            future = Future()
            if not futures:
                future.set_result(function(value))
            futures.append(future)
            return future

        executor.submit.side_effect = submit
        results = bounded_executor_map(executor, abs, range(1000), max_pending=4)
        self.assertEqual(next(results), 0)
        self.assertEqual(executor.submit.call_count, 4)
        results.close()
        self.assertTrue(all(future.cancelled() for future in futures[1:]))

    def test_worker_exception_cancels_remaining_queued_work(self):
        executor = Mock()
        failed, queued = Future(), Future()
        failed.set_exception(ValueError("lookup failed"))
        executor.submit.side_effect = [failed, queued]
        with self.assertRaisesRegex(ValueError, "lookup failed"):
            list(bounded_executor_map(executor, abs, [1, 2], max_pending=2))
        self.assertTrue(queued.cancelled())


class RateLimiterTests(unittest.TestCase):
    def test_capacity_does_not_evict_live_quotas_and_expires(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10, max_keys=100)
        with patch("runtime_support.time.monotonic", return_value=0):
            for i in range(100):
                self.assertTrue(limiter.allow(str(i)))
            for i in range(100, 1000):
                self.assertFalse(limiter.allow(str(i)))
            self.assertEqual(len(limiter._events), 100)
            self.assertTrue(limiter.allow("0"))
            self.assertFalse(limiter.allow("0"))
        with patch("runtime_support.time.monotonic", return_value=10):
            self.assertTrue(limiter.allow("new"))
            self.assertEqual(len(limiter._events), 1)

    def test_recently_used_key_survives_older_keys_expiring(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10)
        with patch("runtime_support.time.monotonic", return_value=0):
            limiter.allow("active")
            limiter.allow("idle")
        with patch("runtime_support.time.monotonic", return_value=5):
            self.assertTrue(limiter.allow("active"))
            self.assertFalse(limiter.allow("active"))
        with patch("runtime_support.time.monotonic", return_value=10):
            self.assertTrue(limiter.allow("active"))
            self.assertNotIn("idle", limiter._events)
            self.assertFalse(limiter.allow("active"))


class RuntimeMetricsTests(unittest.TestCase):
    def test_snapshot_is_detached_and_aggregates_durations(self):
        metrics = RuntimeMetrics()
        metrics.increment("checks")
        metrics.increment("checks", 2)
        metrics.observe("latency", 1.5)
        metrics.observe("latency", 0.5)

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["counters"]["checks"], 3)
        self.assertEqual(snapshot["durations"]["latency"]["count"], 2)
        self.assertEqual(snapshot["durations"]["latency"]["total_seconds"], 2.0)
        self.assertEqual(snapshot["durations"]["latency"]["max_seconds"], 1.5)

        snapshot["counters"]["checks"] = 99
        self.assertEqual(metrics.snapshot()["counters"]["checks"], 3)


if __name__ == "__main__":
    unittest.main()
