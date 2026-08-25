import unittest
from types import SimpleNamespace

from loi_he_thong import opportunity_research_matrix as matrix


def runtime_state(confidence=0.60):
    return SimpleNamespace(
        bias_state="LONG", bias_confidence=confidence,
        bias_council={
            "raw_bias": "LONG", "raw_confidence": confidence,
            "hysteresis": "STABLE", "s_votes": {},
        },
        persistent_metaorder_shadow={},
    )


def frozen(confidence):
    return {
        "direction": "LONG", "confidence": confidence,
        "raw_direction": "LONG", "raw_confidence": confidence - 0.01,
        "direction_context": {"hysteresis": "STABLE", "story": "TEST"},
        "s_votes": {},
    }


class OpportunityResearchMatrixTests(unittest.TestCase):
    def test_borderline_pre_bias_is_telemetry_only(self):
        result = {
            "decision": "WAIT", "reason": "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS",
            "side": "LONG", "ignition": {
                "bias_snapshot": frozen(0.5238),
                "research_candidate_id": "candidate-1",
                "research_candidate_transition": True,
            },
        }
        report = matrix.build(runtime_state(), result)
        self.assertEqual(report["pre_bias"]["band"], "BORDERLINE")
        self.assertTrue(report["pre_bias"]["borderline"])
        self.assertFalse(report["authority"])
        self.assertTrue(report["transition"])

    def test_current_bias_is_not_mislabeled_pre_impulse(self):
        report = matrix.build(runtime_state(0.80), {
            "decision": "WAIT", "reason": "WAIT_IGNITION", "side": "LONG",
        })
        self.assertEqual(report["pre_bias"]["band"], "UNOBSERVED")
        self.assertEqual(
            report["pre_bias"]["snapshot_source"], "CURRENT_NOT_PRE_IMPULSE"
        )

    def test_simultaneous_cash_acceptance_never_becomes_authority(self):
        persistent = {
            "sides": {"LONG": {
                "cash_candidates": ["binance_spot", "coinbase_spot"],
                "futures_follow": True,
            }}
        }
        report = matrix.build(runtime_state(), {
            "decision": "WAIT", "reason": "WAIT_CAUSAL_LEADER_UNCERTAIN",
            "side": "LONG", "persistent_metaorder_shadow": persistent,
            "ignition": {
                "leader": "SIMULTANEOUS", "proposer": "binance_spot",
                "bias_snapshot": frozen(0.70),
                "oi_intent": {"intent": "UNWIND", "fresh": True},
            },
        })
        self.assertEqual(
            report["simultaneous_cash_acceptance"],
            "SIMULTANEOUS_CASH_ACCEPTANCE",
        )
        self.assertEqual(report["oi_class"], "UNWIND_CASH_ACCEPTED")
        self.assertFalse(report["authority"])

    def test_futures_unwind_remains_distinct(self):
        report = matrix.build(runtime_state(), {
            "decision": "WAIT", "reason": "WAIT_FUTURES_PROPOSER_OI_UNWIND",
            "side": "SHORT", "ignition": {
                "leader": "futures", "proposer": "futures",
                "bias_snapshot": frozen(0.70),
                "oi_intent": {"intent": "UNWIND", "fresh": True},
            },
        })
        self.assertEqual(report["oi_class"], "UNWIND_FUTURES_LED")
        self.assertIn(
            "FUTURES_LED_UNWIND", report["observed_secondary_conditions"]
        )


if __name__ == "__main__":
    unittest.main()
