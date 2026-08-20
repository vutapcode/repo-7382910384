import unittest
from types import SimpleNamespace

from loi_he_thong import entry_exchange_independence_hook as hook


class ExternalConfidencePenaltyTest(unittest.TestCase):
    def test_degraded_external_window_reduces_confidence_without_blocking_strong_native_go(self):
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
                        "strong_supporters": ["spot"],
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

        class Council:
            _exchange_independence_installed = False

            @staticmethod
            def evaluate(state, now=None, side=None):
                return dict(result)

        council = Council()
        hook.install(council)
        out = council.evaluate(SimpleNamespace())

        self.assertEqual(out["decision"], "GO")
        self.assertLess(out["confidence"], result["confidence"])
        meta = out["exchange_independence"]
        self.assertEqual(meta["mode"], "DEGRADED_EXTERNAL_STRONG_NATIVE")
        self.assertTrue(meta["confidence_degraded"])
        self.assertTrue(meta["native_strength_ok"])


if __name__ == "__main__":
    unittest.main()
