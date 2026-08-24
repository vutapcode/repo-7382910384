import unittest

from loi_he_thong import entry_exchange_independence_hook as hook


class EntryCashAnchorTest(unittest.TestCase):
    def test_degraded_external_rejects_futures_only_strong_evidence(self):
        result = {
            "decision": "GO",
            "entry_mode": "NORMAL",
            "confidence": 0.80,
            "ts": 100.0,
            "price_threshold_bps": 0.5,
            "freshness": {"coinbase": 97.0},
            "s_votes": {
                "S1_cross_venue_price_acceptance": {
                    "metrics": {
                        "supporters": ["spot", "futures"],
                        "strong_supporters": ["futures"],
                        "moves": {"coinbase": 0.0},
                    }
                },
                "S2_multi_venue_executed_flow": {
                    "metrics": {
                        "supporters": ["spot", "futures"],
                        "strong_supporters": ["futures"],
                        "venues": {"coinbase": {"signed_imbalance": 0.0}},
                    }
                },
            },
        }

        allowed, meta = hook._soft_external_ok(result)

        self.assertFalse(allowed)
        self.assertEqual(meta["mode"], "DEGRADED_EXTERNAL_STRONG_NATIVE")
        self.assertFalse(meta["cash_anchor_strong"])
        self.assertFalse(meta["native_strength_ok"])


if __name__ == "__main__":
    unittest.main()
