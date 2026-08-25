from types import SimpleNamespace
from collections import deque
import time
import unittest
from unittest.mock import patch

from loi_he_thong import flow_lead_engine, ignition_signals


class FlowLeadScopeTests(unittest.TestCase):
    def test_warmup_cannot_claim_objective_spot_discovery(self):
        report = flow_lead_engine.analyze(SimpleNamespace(), "LONG")
        self.assertEqual(report["lead_scope"], "TRADE_RELATIVE_CASH_VS_PERP")
        self.assertEqual(report["spot_price_discovery"], "NOT_MEASURED_HERE")
        self.assertEqual(report["lead"], "UNKNOWN")

    def test_active_ignition_buckets_feed_flow_lead_without_legacy_council(self):
        now = time.time()
        state = SimpleNamespace(
            thoi_gian_tick_cuoi=now,
            thoi_gian_coinbase_ticker_cuoi=now,
            execution_price_time=now,
        )
        base_ms = int(now * 1000) - 1_300
        for index in range(12):
            receive_ms = base_ms + index * 100
            for venue, gain in (
                ("binance_spot", 0.004),
                ("coinbase_spot", 0.0035),
                ("futures", 0.001),
            ):
                ignition_signals.observe_trade(
                    state, venue, receive_time_ms=receive_ms,
                    event_time_ms=receive_ms - 10,
                    price=100.0 + gain * index, qty=0.1,
                    aggressive_buy=True,
                )
        report = flow_lead_engine.analyze(state, "LONG")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["history_source"], "ACTIVE_IGNITION_100MS")
        self.assertFalse(hasattr(state, "entry_causal_flow_history"))
        self.assertFalse(hasattr(state, "entry_shadow_price_history"))

    def test_short_sell_flow_is_persistence_not_opposition(self):
        state = SimpleNamespace(
            thoi_gian_tick_cuoi=2.0,
            thoi_gian_coinbase_ticker_cuoi=2.0,
            execution_price_time=2.0,
        )
        flow = deque(({
            "ts": 1.0 + index * 0.1,
            "venues": {
                "spot": {"signed_imbalance": -0.70, "volume_btc": 1.0},
                "coinbase": {"signed_imbalance": -0.55, "volume_btc": 1.0},
                "futures": {"signed_imbalance": -0.65, "volume_btc": 1.0},
            },
        } for index in range(12)), maxlen=64)
        prices = deque((
            {"ts": 1.0, "spot": 100.0, "coinbase": 100.0, "futures": 100.0},
            {"ts": 1.4, "spot": 99.8, "coinbase": 99.8, "futures": 99.9},
            {"ts": 1.9, "spot": 99.6, "coinbase": 99.6, "futures": 99.8},
        ), maxlen=96)
        with patch.object(
            flow_lead_engine, "_active_histories", return_value=(flow, prices)
        ):
            report = flow_lead_engine.analyze(state, "SHORT")
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["persistence"], 1.0)
        self.assertEqual(report["oppose_ratio"], 0.0)
        self.assertLess(report["flow_mean_raw"], 0.0)
        self.assertGreater(report["flow_mean"], 0.0)

    def test_missing_side_cannot_be_treated_as_short(self):
        state = SimpleNamespace(
            thoi_gian_tick_cuoi=2.0,
            thoi_gian_coinbase_ticker_cuoi=2.0,
            execution_price_time=2.0,
        )
        with patch.object(
            flow_lead_engine, "_active_histories",
            return_value=(({"ts": 1.0, "venues": {}},), (
                {"ts": 1.0, "spot": 100.0, "coinbase": 100.0, "futures": 100.0},
            )),
        ):
            report = flow_lead_engine.analyze(state, "ABSTAIN")
        self.assertEqual(report["status"], "DIRECTION_UNAVAILABLE")
        self.assertEqual(report["lead"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
