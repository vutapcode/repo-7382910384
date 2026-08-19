import unittest

from loi_he_thong import ops_supervisor_safe as safe


class PositionAwareLivenessTests(unittest.TestCase):
    def _heartbeat(self, *, position_active, entry_age=0.1, guardian_age=0.1):
        return {
            "critical_liveness_installed_age_sec": safe.ops.CRITICAL_LOOP_GRACE_SECONDS + 1.0,
            "shadow_position_active": position_active,
            "critical_loops": {
                "bias": {"age_sec": 0.1, "consecutive_errors": 0},
                "entry": {"age_sec": entry_age, "consecutive_errors": 0},
                "guardian": {"age_sec": guardian_age, "consecutive_errors": 0},
            },
        }

    def test_flat_state_requires_entry_liveness(self):
        available, classification = safe._critical_loop_monotonic_classification(
            self._heartbeat(position_active=False, entry_age=safe.ops.BIAS_ENTRY_STALE_SECONDS + 0.1)
        )
        self.assertTrue(available)
        self.assertEqual(classification, "ENTRY_LOOP_STALLED")

    def test_open_position_does_not_require_dormant_entry_loop(self):
        available, classification = safe._critical_loop_monotonic_classification(
            self._heartbeat(position_active=True, entry_age=safe.ops.BIAS_ENTRY_STALE_SECONDS + 60.0)
        )
        self.assertTrue(available)
        self.assertIsNone(classification)

    def test_open_position_still_requires_guardian_liveness(self):
        available, classification = safe._critical_loop_monotonic_classification(
            self._heartbeat(position_active=True, guardian_age=safe.ops.GUARDIAN_STALE_SECONDS + 0.1)
        )
        self.assertTrue(available)
        self.assertEqual(classification, "GUARDIAN_LOOP_STALLED")


if __name__ == "__main__":
    unittest.main()
