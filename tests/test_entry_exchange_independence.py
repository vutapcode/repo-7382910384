import unittest

from loi_he_thong import entry_exchange_independence_hook as hook


def _result(*, price_supporters, flow_supporters, cb_ts=100.0, now=100.5, cb_move=0.0, cb_flow=0.0):
    return {
        "decision": "GO",
        "entry_mode": "NORMAL",
        "phase": "ACCEPTANCE",
        "confidence": 0.8,
        "reason": "CAUSAL_PRICE_FLOW_QUORUM",
        "ts": now,
        "price_threshold_bps": 0.5,
        "freshness": {"coinbase": cb_ts},
        "s_votes": {
            "S1_cross_venue_price_acceptance": {
                "metrics": {
                    "supporters": list(price_supporters),
                    "moves": {"coinbase": cb_move},
                }
            },
            "S2_multi_venue_executed_flow": {
                "metrics": {
                    "supporters": list(flow_supporters),
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

    def test_soft_coinbase_price_corroboration_preserves_normal_go(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.20,
            )
        )
        self.assertTrue(allowed)
        self.assertTrue(meta["price_ok"])

    def test_fresh_neutral_coinbase_blocks_correlated_binance_only_go(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.0,
                cb_flow=0.0,
            )
        )
        self.assertFalse(allowed)
        self.assertTrue(meta["coinbase_fresh"])

    def test_stale_coinbase_is_availability_neutral(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_ts=90.0,
                now=100.0,
            )
        )
        self.assertTrue(allowed)
        self.assertTrue(meta["availability_neutral"])


if __name__ == "__main__":
    unittest.main()
