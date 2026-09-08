import unittest
from types import SimpleNamespace

from loi_he_thong import market_thesis


class MarketTruthWaveLifecycleTests(unittest.TestCase):
    def test_wait_and_elapsed_time_do_not_kill_wave(self):
        state = SimpleNamespace()
        result = {
            "decision": "WAIT",
            "reason": "WAIT_IMPULSE_ALREADY_CONSUMED",
            "side": "LONG",
            "causal_episode_id": "wave-1",
            "ignition": {"causal_episode_id": "wave-1"},
        }
        row = market_thesis.wave_lifecycle(state, result)
        self.assertEqual(row["status"], "ACTIVE")
        self.assertFalse(row["time_alone_falsifies"])

    def test_source_gap_is_unknown_not_falsified(self):
        state = SimpleNamespace()
        row = market_thesis.wave_lifecycle(state, {
            "decision": "WAIT", "reason": "DATA_GAP",
            "causal_episode_id": "wave-gap",
            "ignition": {"causal_episode_id": "wave-gap"},
        })
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertIsNone(row["falsifier"])

    def test_opposite_dual_cash_control_falsifies(self):
        state = SimpleNamespace()
        row = market_thesis.wave_lifecycle(state, {
            "decision": "WAIT", "causal_episode_id": "wave-2",
            "ignition": {
                "causal_episode_id": "wave-2",
                "causal_wave_snapshot": {
                    "causal_wave_id": "wave-2",
                    "contradictions": {"opposing_cash_control": True},
                },
            },
        })
        self.assertEqual(row["status"], "FALSIFIED")
        self.assertEqual(row["falsifier"], "OPPOSITE_DUAL_CASH_CONTROL")

    def test_falsified_wave_cannot_resurrect_after_handoff_replacement(self):
        state = SimpleNamespace()
        terminated = {
            "causal_wave_id": "wave-dead",
            "status": "TERMINATED_CONTRADICTION",
            "termination_reason": "EXECUTED_FLOW_STOPPED_CONVERTING",
        }
        dead = market_thesis.wave_lifecycle(state, {
            "causal_episode_id": "wave-dead",
            "ignition": {"causal_episode_id": "wave-dead"},
            "bias_acquisition_handoff": terminated,
        })
        self.assertEqual(dead["status"], "FALSIFIED")

        state.bias_acquisition_handoff = {
            "causal_wave_id": "wave-new", "status": "SEALED",
        }
        replayed = market_thesis.wave_lifecycle(state, {
            "decision": "GO", "causal_episode_id": "wave-dead",
            "ignition": {"causal_episode_id": "wave-dead"},
        })
        self.assertEqual(replayed["status"], "FALSIFIED")
        self.assertEqual(
            replayed["falsifier"], "EXECUTED_FLOW_STOPPED_CONVERTING",
        )

    def test_unknown_source_does_not_create_terminal_tombstone(self):
        state = SimpleNamespace()
        unknown = market_thesis.wave_lifecycle(state, {
            "decision": "WAIT", "reason": "DATA_GAP",
            "causal_episode_id": "wave-unknown",
            "ignition": {"causal_episode_id": "wave-unknown"},
        })
        self.assertEqual(unknown["status"], "UNKNOWN")
        later = market_thesis.wave_lifecycle(state, {
            "decision": "GO", "causal_episode_id": "wave-unknown",
            "ignition": {"causal_episode_id": "wave-unknown"},
        })
        self.assertEqual(later["status"], "ACTIVE")

    def test_bias_owned_acquisition_wave_is_authoritative(self):
        state = SimpleNamespace()
        handoff = {
            "causal_wave_id": "wave-owned", "status": "SEALED",
            "side": "LONG",
        }
        row = market_thesis.wave_lifecycle(state, {
            "side": "LONG", "causal_episode_id": "timing-attempt-2",
            "ignition": {"causal_episode_id": "timing-attempt-2"},
            "bias_acquisition_handoff": handoff,
        })
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual(row["reason"], "BIAS_CASH_WAVE_OWNED")
        self.assertEqual(row["market_wave_id"], "wave-owned")
        self.assertEqual(row["timing_episode_id"], "timing-attempt-2")

    def test_frozen_handoff_wins_over_newer_mutable_state(self):
        state = SimpleNamespace(bias_acquisition_handoff={
            "causal_wave_id": "wave-new", "status": "SEALED",
            "side": "SHORT",
        })
        row = market_thesis.wave_lifecycle(state, {
            "side": "LONG", "causal_episode_id": "timing-old",
            "ignition": {"causal_episode_id": "timing-old"},
            "bias_acquisition_handoff": {
                "causal_wave_id": "wave-old",
                "status": "TERMINATED_CAUSAL_FALSIFIER",
                "termination_reason": "NO_RECENT_OLD_SIDE_CONVERSION",
                "side": "LONG",
            },
        })
        self.assertEqual(row["market_wave_id"], "wave-old")
        self.assertEqual(row["status"], "FALSIFIED")
        self.assertEqual(row["falsifier"], "NO_RECENT_OLD_SIDE_CONVERSION")

    def test_unowned_timing_episode_is_not_market_identity_authority(self):
        row = market_thesis.wave_lifecycle(SimpleNamespace(), {
            "side": "LONG", "causal_episode_id": "timing-only",
            "ignition": {"causal_episode_id": "timing-only"},
        })
        self.assertEqual(row["identity_authority"], "TIMING_EPISODE_FALLBACK")


if __name__ == "__main__":
    unittest.main()
