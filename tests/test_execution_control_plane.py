import unittest

from loi_he_thong import execution_control_plane


class Clock:
    def __init__(self):
        self.mono = 0.0
        self.wall = 1_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class ExecutionControlPlaneTests(unittest.TestCase):
    def test_no_measurement_cannot_authorize_entry(self):
        clock = Clock()
        monitor = execution_control_plane.Monitor(
            clock.monotonic, clock.time
        )
        snapshot = monitor.snapshot(opportunity_budget_ms=500.0)
        self.assertEqual(snapshot["health"], "UNKNOWN")
        self.assertFalse(snapshot["entry_allowed"])

    def test_successful_measured_path_fits_opportunity_budget(self):
        clock = Clock()
        monitor = execution_control_plane.Monitor(
            clock.monotonic, clock.time
        )
        token = monitor.begin("NEW_ORDER")
        clock.advance(0.08)
        monitor.complete(token, 200)
        snapshot = monitor.snapshot(opportunity_budget_ms=500.0)
        self.assertEqual(snapshot["health"], "HEALTHY")
        self.assertTrue(snapshot["entry_allowed"])
        self.assertAlmostEqual(snapshot["latency_p95_ms"], 80.0)

    def test_uncalibrated_latency_budget_is_telemetry_only(self):
        clock = Clock()
        monitor = execution_control_plane.Monitor(
            clock.monotonic, clock.time
        )
        token = monitor.begin("QUERY_ORDER")
        clock.advance(0.4)
        monitor.complete(token, 200)
        snapshot = monitor.snapshot(opportunity_budget_ms=250.0)
        self.assertEqual(snapshot["health"], "DEGRADED")
        self.assertEqual(
            snapshot["reason"], "LATENCY_BUDGET_EXCEEDED_TELEMETRY_ONLY"
        )
        self.assertTrue(snapshot["entry_allowed"])

    def test_approved_latency_authority_can_block_new_entry(self):
        clock = Clock()
        monitor = execution_control_plane.Monitor(
            clock.monotonic, clock.time, latency_authority_enabled=True
        )
        token = monitor.begin("QUERY_ORDER")
        clock.advance(0.4)
        monitor.complete(token, 200)
        snapshot = monitor.snapshot(opportunity_budget_ms=250.0)
        self.assertEqual(snapshot["health"], "UNSAFE_FOR_NEW_ENTRY")
        self.assertFalse(snapshot["entry_allowed"])

    def test_latest_failure_is_exit_only_with_exposure(self):
        clock = Clock()
        monitor = execution_control_plane.Monitor(
            clock.monotonic, clock.time
        )
        token = monitor.begin("CANCEL_ORDER")
        clock.advance(0.1)
        monitor.complete(token, 599)
        snapshot = monitor.snapshot(has_exposure=True)
        self.assertEqual(snapshot["health"], "EXIT_ONLY")
        self.assertFalse(snapshot["entry_allowed"])


if __name__ == "__main__":
    unittest.main()
