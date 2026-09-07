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

    def test_bias_owned_acquisition_wave_is_authoritative(self):
        state = SimpleNamespace(bias_acquisition_handoff={
            "causal_wave_id": "wave-owned", "status": "SEALED",
        })
        row = market_thesis.wave_lifecycle(state, {
            "causal_episode_id": "wave-owned",
            "ignition": {"causal_episode_id": "wave-owned"},
        })
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual(row["reason"], "BIAS_CASH_WAVE_OWNED")


if __name__ == "__main__":
    unittest.main()
