import unittest

from loi_he_thong import entry_exchange_independence_hook as hook


def _result(
    *,
    price_supporters,
    flow_supporters,
    price_strong=(),
    flow_strong=(),
    cb_ts=100.0,
    now=100.5,
    cb_move=0.0,
    cb_flow=0.0,
):
    return {
        "decision": "GO",
        "entry_mode": "NORMAL",
        "phase": "ACCEPTANCE",
        "confidence": 0.80,
        "reason": "CAUSAL_PRICE_FLOW_QUORUM",
        "ts": now,
        "price_threshold_bps": 0.5,
        "freshness": {"coinbase": cb_ts},
        "s_votes": {
            "S1_cross_venue_price_acceptance": {
                "metrics": {
                    "supporters": list(price_supporters),
                    "strong_supporters": list(price_strong),
                    "moves": {"coinbase": cb_move},
                }
            },
            "S2_multi_venue_executed_flow": {
                "metrics": {
                    "supporters": list(flow_supporters),
                    "strong_supporters": list(flow_strong),
                    "venues": {"coinbase": {"signed_imbalance": cb_flow}},
                }
            },
        },
    }



class EntryExchangeIndependenceTest(unittest.TestCase):
    def test_independent_quorum_is_untouched(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "coinbase"},
                flow_supporters={"spot", "coinbase"},
            )
        )
        self.assertTrue(allowed)
        self.assertFalse(meta["applies"])

    def test_strict_window_uses_soft_coinbase_corroboration(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.20,
                cb_ts=100.0,
                now=101.0,
            )
        )
        self.assertTrue(allowed)
        self.assertEqual(meta["mode"], "STRICT_EXTERNAL_SOFT_CORROBORATION")

    def test_degraded_window_requires_strong_native_price_and_flow(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                price_strong={"spot"},
                flow_strong={"futures"},
                cb_ts=100.0,
                now=103.0,
            )
        )
        self.assertTrue(allowed)
        self.assertEqual(meta["mode"], "DEGRADED_EXTERNAL_STRONG_NATIVE")
        self.assertTrue(meta["native_strength_ok"])

    def test_degraded_window_blocks_weak_native_evidence(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                price_strong={"spot"},
                flow_strong=(),
                cb_ts=100.0,
                now=103.0,
            )
        )
        self.assertFalse(allowed)
        self.assertEqual(meta["mode"], "DEGRADED_EXTERNAL_STRONG_NATIVE")
        self.assertFalse(meta["native_strength_ok"])

    def test_unavailable_external_blocks_correlated_binance_only(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                price_strong={"spot"},
                flow_strong={"spot"},
                cb_ts=90.0,
                now=100.0,
            )
        )
        self.assertFalse(allowed)
        self.assertEqual(meta["mode"], "EXTERNAL_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
