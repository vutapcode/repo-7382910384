import unittest
from types import SimpleNamespace

from loi_he_thong import canonical_opportunity as opportunity


def go(side="LONG", mode="NORMAL", phase="ACCEPTANCE"):
    return {
        "decision": "GO", "side": side,
        "entry_mode": mode, "phase": phase,
    }


class CanonicalOpportunityTests(unittest.TestCase):
    def test_same_go_episode_is_claimed_once_across_exit_and_restart_state(self):
        state = SimpleNamespace()
        first = opportunity.observe(state, go(), qualified=True, now=100.0)
        self.assertTrue(first["new"])
        self.assertTrue(first["qualified_now"])
        self.assertTrue(first["qualified_ever"])
        self.assertTrue(first["qualification_transition"])
        self.assertTrue(opportunity.claim(state, first["opportunity_id"]))
        self.assertFalse(opportunity.claim(state, first["opportunity_id"]))

        continued = opportunity.observe(state, go(), qualified=True, now=101.0)
        self.assertFalse(continued["new"])
        self.assertTrue(continued["qualified_now"])
        self.assertTrue(continued["qualified_ever"])
        self.assertFalse(continued["qualification_transition"])
        self.assertEqual(continued["opportunity_id"], first["opportunity_id"])
        self.assertFalse(opportunity.claim(state, continued["opportunity_id"]))

        wait = opportunity.observe(state, {
            "decision": "WAIT", "side": "LONG", "phase": "ACCEPTANCE",
        }, qualified=False, now=102.0)
        self.assertFalse(wait["qualified_now"])
        self.assertTrue(wait["qualified_ever"])

    def test_short_wait_flicker_stays_in_same_causal_episode(self):
        state = SimpleNamespace()
        first = opportunity.observe(state, go(), qualified=False, now=100.0)
        wait = opportunity.observe(state, {"decision": "WAIT"}, qualified=False, now=101.0)
        second = opportunity.observe(state, go(phase="RELEASE"), qualified=True, now=103.0)

        self.assertTrue(wait["grace_active"])
        self.assertEqual(second["opportunity_id"], first["opportunity_id"])
        self.assertEqual(state.canonical_opportunity_qualified, 1)

    def test_pressure_wait_starts_episode_and_later_go_reuses_it(self):
        state = SimpleNamespace()
        pressure = opportunity.observe(state, {
            "decision": "WAIT", "side": "LONG", "phase": "PRESSURE_BUILDING",
            "reason": "CAUSAL_SEQUENCE_NOT_READY",
        }, qualified=False, now=100.0)
        entered = opportunity.observe(state, go(), qualified=True, now=103.0)
        self.assertTrue(pressure["new"])
        self.assertIsNotNone(pressure["causal_episode_id"])
        self.assertFalse(entered["new"])
        self.assertEqual(entered["causal_episode_id"], pressure["causal_episode_id"])

    def test_data_gap_resets_episode_immediately(self):
        state = SimpleNamespace()
        first = opportunity.observe(state, go(), qualified=False, now=100.0)
        gap = opportunity.observe(state, {
            "decision": "WAIT", "side": "LONG", "phase": "PRESSURE_BUILDING",
            "reason": "DATA_GAP",
        }, qualified=False, now=101.0)
        self.assertFalse(gap["active"])
        second = opportunity.observe(state, go(), qualified=False, now=102.0)
        self.assertGreater(second["opportunity_id"], first["opportunity_id"])

    def test_wait_beyond_grace_creates_new_opportunity(self):
        state = SimpleNamespace()
        first = opportunity.observe(state, go(), qualified=False, now=100.0)
        opportunity.observe(state, {"decision": "WAIT"}, qualified=False, now=106.0)
        second = opportunity.observe(state, go(), qualified=True, now=106.1)
        self.assertEqual(second["opportunity_id"], first["opportunity_id"] + 1)

    def test_calibration_capture_is_once_per_qualified_opportunity(self):
        state = SimpleNamespace()
        row = opportunity.observe(state, go(), qualified=True, now=100.0)
        self.assertTrue(opportunity.mark_captured(state, row["opportunity_id"]))
        self.assertFalse(opportunity.mark_captured(state, row["opportunity_id"]))
        self.assertEqual(state.canonical_opportunity_captured, 1)


if __name__ == "__main__":
    unittest.main()
