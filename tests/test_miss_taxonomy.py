import unittest

import mainnet_tier_s_shadow_launcher as launcher


class MissTaxonomyTests(unittest.TestCase):
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
