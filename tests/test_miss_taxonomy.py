import unittest
from types import SimpleNamespace

import mainnet_tier_s_shadow_launcher as launcher


class MissTaxonomyTests(unittest.TestCase):
    def test_frozen_bias_mismatch_has_distinct_taxonomy(self):
        result = {
            "decision": "WAIT",
            "reason": "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS",
            "side": "LONG",
            "s_votes": {},
        }
        primary, failed = launcher._miss_taxonomy(result, {}, False)
        self.assertEqual(primary, "BIAS_ALIGNMENT_FAIL")
        self.assertIn("BIAS_ALIGNMENT_FAIL", failed)
        self.assertNotIn("BIAS_NOT_READY", failed)

    def test_snapshot_oi_freshness_uses_poll_aware_contract(self):
        state = SimpleNamespace(
            open_interest=100.0, thoi_gian_vi_mo_cuoi=83.0,
            oi_poll_interval_effective_seconds=15.0,
            bias_council={}, bias_state="ABSTAIN", bias_confidence=0.0,
            execution_best_bid=100.0, execution_best_ask=100.1,
        )
        snapshot = launcher._decision_snapshot(
            state, {"side": "ABSTAIN", "s_votes": {}}, {}, False,
            "cycle-test", 100.0,
        )
        oi = snapshot["inputs"]["open_interest"]
        self.assertTrue(oi["fresh"])
        self.assertEqual(oi["max_age_seconds"], 18.0)

    def test_accepted_bootstrap_shadow_trade_is_not_a_miss(self):
        result = {
            "decision": "GO", "reason": "IGNITION_METAORDER_CONTINUATION",
            "side": "LONG", "s_votes": {},
        }
        edge = {
            "cost_ok": False, "bootstrap_shadow_allowed": True,
            "live_empirical_ok": False, "price_impact": {},
            "spot_perp_basis": {},
        }
        self.assertEqual(launcher._miss_taxonomy(result, edge, True), (None, []))


if __name__ == "__main__":
    unittest.main()
