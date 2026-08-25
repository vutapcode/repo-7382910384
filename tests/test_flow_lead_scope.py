from types import SimpleNamespace
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
