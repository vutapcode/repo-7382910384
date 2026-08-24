import unittest

from loi_he_thong import ops_supervisor_safe as safe


class FlatTransitionLivenessTest(unittest.TestCase):
    def _heartbeat(self, *, active, entry_age):
        return {
            "critical_liveness_installed_age_sec": safe.ops.CRITICAL_LOOP_GRACE_SECONDS + 1.0,
            "shadow_position_active": active,
            "critical_loops": {
                "bias": {"age_sec": 0.1, "consecutive_errors": 0},
                "entry": {"age_sec": entry_age, "consecutive_errors": 0},
                "guardian": {"age_sec": 0.1, "consecutive_errors": 0},
            },
        }

    def test_active_to_flat_gets_exactly_one_entry_grace_cycle(self):
        stale = safe.ops.BIAS_ENTRY_STALE_SECONDS + 60.0

        available, classification = safe._critical_loop_monotonic_classification(
            self._heartbeat(active=True, entry_age=stale)
        )
        self.assertTrue(available)
        self.assertIsNone(classification)

        available, classification = safe._critical_loop_monotonic_classification(
            self._heartbeat(active=False, entry_age=stale)
        )
        self.assertTrue(available)
        self.assertIsNone(classification)

        available, classification = safe._critical_loop_monotonic_classification(
            self._heartbeat(active=False, entry_age=stale)
        )
        self.assertTrue(available)
        self.assertEqual(classification, "ENTRY_LOOP_STALLED")


if __name__ == "__main__":
    unittest.main()
