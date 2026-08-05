import unittest

from runtime_support import RuntimeMetrics


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
