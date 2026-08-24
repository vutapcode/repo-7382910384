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
    def test_absorption_cannot_use_coinbase_and_futures_without_spot_handoff(self):
        result = _result(
            price_supporters={"coinbase", "futures"},
            flow_supporters={"spot", "coinbase", "futures"},
            cb_move=0.5,
            cb_flow=0.5,
        )
        result["causal"] = {"handoff": {"status": "NEUTRAL"}}

        class Council:
            _exchange_independence_installed = False

            @staticmethod
            def evaluate(state, now=None, side=None):
                return dict(result)

        state = type("State", (), {
            "bias_council": {
                "story": {"name": "BUY_FLOW_ABSORBED_BY_SHORT_BUILD"}
            }
        })()
        council = Council()
        hook.install(council)
        out = council.evaluate(state)
        self.assertEqual(out["decision"], "WAIT")
        self.assertEqual(
            out["reason"], "WAIT_ABSORPTION_BINANCE_SPOT_HANDOFF"
        )
        self.assertFalse(
            out["absorption_cash_authority"]["binance_spot_price_support"]
        )

    def test_absorption_passes_only_with_spot_price_and_spot_handoff(self):
        result = _result(
            price_supporters={"spot", "coinbase", "futures"},
            flow_supporters={"spot", "coinbase", "futures"},
            cb_move=0.5,
            cb_flow=0.5,
        )
        result["causal"] = {"handoff": {"status": "SPOT_HANDOFF"}}

        class Council:
            _exchange_independence_installed = False

            @staticmethod
            def evaluate(state, now=None, side=None):
                return dict(result)

        state = type("State", (), {
            "bias_council": {
                "story": {"name": "SELL_FLOW_ABSORBED_BY_LONG_BUILD"}
            }
        })()
        council = Council()
        hook.install(council)
        out = council.evaluate(state)
        self.assertEqual(out["decision"], "GO")
        self.assertTrue(out["absorption_cash_authority"]["allowed"])

    def test_independent_quorum_is_untouched(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "coinbase"},
                flow_supporters={"spot", "coinbase"},
            )
        )
        self.assertTrue(allowed)
        self.assertFalse(meta["applies"])

    def test_strict_window_accepts_two_soft_coinbase_axes(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.20,
                cb_flow=0.05,
                cb_ts=100.0,
                now=101.0,
            )
        )
        self.assertTrue(allowed)
        self.assertEqual(meta["mode"], "STRICT_EXTERNAL_SOFT_CORROBORATION")

    def test_strict_window_blocks_one_weak_external_axis(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.20,
                cb_flow=0.0,
                cb_ts=100.0,
                now=101.0,
            )
        )
        self.assertFalse(allowed)
        self.assertTrue(meta["price_ok"])
        self.assertFalse(meta["flow_ok"])
        self.assertFalse(meta["corroborated"])

    def test_incidental_coinbase_supporter_cannot_bypass_correlated_pair_check(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "coinbase", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.20,
                cb_flow=0.0,
                cb_ts=100.0,
                now=101.0,
            )
        )
        self.assertFalse(allowed)
        self.assertTrue(meta["applies"])
        self.assertEqual(meta["mode"], "STRICT_EXTERNAL_SOFT_CORROBORATION")

    def test_strict_window_accepts_one_material_external_axis(self):
        allowed, meta = hook._soft_external_ok(
            _result(
                price_supporters={"spot", "futures"},
                flow_supporters={"spot", "futures"},
                cb_move=0.40,
                cb_flow=0.0,
                cb_ts=100.0,
                now=101.0,
            )
        )
        self.assertTrue(allowed)
        self.assertTrue(meta["strong_price_ok"])

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

    def test_fast_lane_cannot_use_degraded_coinbase_window(self):
        result = _result(
            price_supporters={"spot", "coinbase", "futures"},
            flow_supporters={"spot", "futures"},
            price_strong={"spot", "coinbase", "futures"},
            flow_strong={"spot", "futures"},
            cb_ts=97.0,
            now=100.0,
        )
        result["entry_mode"] = "FAST"

        class Council:
            _exchange_independence_installed = False

            @staticmethod
            def evaluate(state, now=None, side=None):
                return dict(result)

        council = Council()
        hook.install(council)
        out = council.evaluate(object())
        self.assertEqual(out["decision"], "WAIT")
        self.assertEqual(out["reason"], "WAIT_EXTERNAL_FAST_REQUIRES_FRESH_COINBASE")

    def test_fast_lane_records_strict_fresh_external_authority(self):
        result = _result(
            price_supporters={"spot", "coinbase", "futures"},
            flow_supporters={"spot", "futures"},
            price_strong={"spot", "coinbase", "futures"},
            flow_strong={"spot", "futures"},
            cb_ts=99.0,
            now=100.0,
        )
        result["entry_mode"] = "FAST"

        class Council:
            _exchange_independence_installed = False

            @staticmethod
            def evaluate(state, now=None, side=None):
                return dict(result)

        council = Council()
        hook.install(council)
        out = council.evaluate(object())
        self.assertEqual(out["decision"], "GO")
        self.assertEqual(out["exchange_independence"]["mode"], "FAST_STRICT_EXTERNAL")


if __name__ == "__main__":
    unittest.main()
