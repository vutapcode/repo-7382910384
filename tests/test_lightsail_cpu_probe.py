import unittest
from datetime import datetime, timedelta, timezone

from ops.lightsail_cpu_probe import averages


class LightsailCpuProbeTests(unittest.TestCase):
    def test_requires_complete_15m_and_1h_datapoint_coverage(self):
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        rows = [
            {"timestamp": now - timedelta(minutes=5 * index), "average": 10 + index}
            for index in range(12)
        ]
        cpu15, cpu1h, count15, count1h = averages(rows, now=now)
        self.assertEqual(count15, 3)
        self.assertEqual(count1h, 12)
        self.assertAlmostEqual(cpu15, 11.0)
        self.assertAlmostEqual(cpu1h, 15.5)

        cpu15, cpu1h, _, count1h = averages(rows[:11], now=now)
        self.assertIsNotNone(cpu15)
        self.assertIsNone(cpu1h)
        self.assertEqual(count1h, 11)


if __name__ == "__main__":
    unittest.main()
