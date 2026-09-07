import time
import unittest

from loi_he_thong import canonical_opportunity
from loi_he_thong import entry_lifecycle


class DummyState:
    pass


class TimingEconomicsHandoffTests(unittest.TestCase):
    def test_retryable_attempt_contract(self):
        state = DummyState()
        now = time.time()
        first = {
            "decision": "GO",
            "causal_episode_id": "wave_1",
            "side": "LONG",
            "ignition": {
                "current_execution_proof": {
                    "proof_hash": "proof_1",
                    "observed_at_ms": int(now * 1000),
                },
                "clock_quality": {"a": {"epoch": 1}},
            },
            "market_truth_wave_lifecycle": {
                "version": "MARKET_TRUTH_WAVE_LIFECYCLE_V1",
                "owner": "MARKET_THESIS",
                "causal_wave_id": "wave_1",
                "status": "ACTIVE",
            },
        }

        canonical_opportunity.observe(state, first, qualified=True, now=now)
        self.assertEqual(
            getattr(state, "canonical_opportunity_active_episode_id", None),
            "wave_1",
        )
        self.assertTrue(getattr(state, "canonical_opportunity_active", False))

        wait = {"allowed": False, "owner": "TIMING", "reason": "WAIT_SPREAD"}
        entry_lifecycle.observe(state, first, wait, economic_opportunity_id=1)
        self.assertEqual(getattr(state, "entry_timing_attempt_status", ""), "WAIT")

        expired = {
            "allowed": False,
            "owner": "TIMING",
            "reason": "WAIT_STALE_COINBASE",
        }
        entry_lifecycle.observe(state, first, expired, economic_opportunity_id=1)
        self.assertEqual(
            getattr(state, "entry_timing_attempt_status", ""), "EXPIRED"
        )
        self.assertTrue(getattr(state, "canonical_opportunity_active", False))

        allowed, reason = entry_lifecycle.can_create_attempt(state, first)
        self.assertFalse(allowed)
        self.assertEqual(reason, "STALE_PROOF_REUSE")

        second = {
            **first,
            "ignition": {
                **first["ignition"],
                "current_execution_proof": {
                    "proof_hash": "proof_2",
                    "observed_at_ms": int(now * 1000) + 100,
                },
            },
        }
        allowed, reason = entry_lifecycle.can_create_attempt(state, second)
        self.assertTrue(allowed, reason)

        epoch_changed = {
            **second,
            "ignition": {
                **second["ignition"],
                "clock_quality": {"a": {"epoch": 2}},
            },
        }
        allowed, reason = entry_lifecycle.can_create_attempt(state, epoch_changed)
        self.assertFalse(allowed)
        self.assertEqual(reason, "CAUSAL_EPOCH_CHANGED_WITHIN_WAVE")

        entry_lifecycle.observe(state, second, wait, economic_opportunity_id=2)
        self.assertEqual(getattr(state, "entry_timing_attempt_status", ""), "WAIT")

        third = {
            **second,
            "ignition": {
                **second["ignition"],
                "current_execution_proof": {
                    "proof_hash": "proof_3",
                    "observed_at_ms": int(now * 1000) + 200,
                },
            },
        }
        allowed, reason = entry_lifecycle.can_create_attempt(state, third)
        self.assertFalse(allowed)
        self.assertEqual(reason, "PARALLEL_ACTIVE_ATTEMPT")


if __name__ == "__main__":
    unittest.main()
