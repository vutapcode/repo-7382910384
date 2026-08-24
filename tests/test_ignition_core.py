from types import SimpleNamespace
import unittest

from loi_he_thong import ignition_core, ignition_signals, entry_edge_tier


def state(now=3.0):
    return SimpleNamespace(
        bias_state="LONG", bias_confidence=0.80, bias_updated_at=now - 1.2,
        bias_version="TEST", bias_council={"s_votes": {}},
        best_bid=99.99, best_ask=100.01, thoi_gian_tick_cuoi=now,
        thoi_gian_dong_tien_cuoi=now,
        coinbase_price=100.0, thoi_gian_coinbase_ticker_cuoi=now,
        coinbase_flow_3s_ts=now,
        execution_best_bid=99.99, execution_best_ask=100.01,
        execution_price_time=now, thoi_gian_dong_tien_futures_cuoi=now,
        open_interest_updated_at=now, wstrade_live_armed=False,
        mainnet_commission_verified=True, mainnet_maker_fee_bps=2.0,
        mainnet_taker_fee_bps=5.0, mainnet_shadow_trades=0,
        mainnet_shadow_stress_25bps_pnl=0.0, atr_1m=0.1,
    )


def bucket(s, venue, start_ms, *, qty=0.10, side="LONG", base=100.0):
    buy = side == "LONG"
    p1 = base
    p2 = base + (0.003 if buy else -0.003)
    ignition_signals.observe_trade(
        s, venue, receive_time_ms=start_ms + 1,
        event_time_ms=start_ms - 9, price=p1, qty=qty / 2,
        aggressive_buy=buy,
    )
    ignition_signals.observe_trade(
        s, venue, receive_time_ms=start_ms + 51,
        event_time_ms=start_ms + 41, price=p2, qty=qty / 2,
        aggressive_buy=buy,
    )


def warm(s, venue, start_ms=0):
    for index in range(ignition_signals.WARMUP_BUCKETS):
        begin = start_ms + index * 100
        ignition_signals.observe_trade(
            s, venue, receive_time_ms=begin + 1,
            event_time_ms=begin - 9, price=100.0, qty=0.01,
            aggressive_buy=True,
        )
        ignition_signals.observe_trade(
            s, venue, receive_time_ms=begin + 51,
            event_time_ms=begin + 41, price=100.001, qty=0.01,
            aggressive_buy=False,
        )
    ignition_signals.snapshot(s, start_ms + ignition_signals.WARMUP_BUCKETS * 100 + 1)


class IgnitionCoreTests(unittest.TestCase):
    def test_cash_metaorder_proves_early_and_only_once(self):
        s = state()
        warm(s, "binance_spot")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "binance_spot", 3_000)
        first = ignition_core.evaluate(s, now=3.101)
        self.assertEqual(first["phase"], "PROBE")
        self.assertEqual(first["decision"], "WAIT")
        bucket(s, "binance_spot", 3_100, base=100.003)
        proved = ignition_core.evaluate(s, now=3.201)
        self.assertEqual(proved["decision"], "GO")
        self.assertEqual(proved["ignition"]["proof_type"], "METAORDER_CONTINUATION")
        self.assertLessEqual(proved["ignition"]["consumed_fraction"], 0.35)
        repeated = ignition_core.evaluate(s, now=3.202)
        self.assertNotEqual(repeated["decision"], "GO")

    def test_futures_alert_never_self_opens(self):
        s = state()
        warm(s, "futures")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "futures", 3_000, qty=1.0)
        result = ignition_core.evaluate(s, now=3.101)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_FUTURES_ALERT_CASH_RESPONSE")

    def test_futures_lead_can_prove_only_after_independent_cash_response(self):
        s = state()
        warm(s, "futures")
        warm(s, "binance_spot")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "futures", 3_000, qty=1.0)
        ignition_core.evaluate(s, now=3.101)
        bucket(s, "binance_spot", 3_300)
        waiting = ignition_core.evaluate(s, now=3.401)
        self.assertEqual(waiting["decision"], "WAIT")
        bucket(s, "binance_spot", 3_400, base=100.003)
        proved = ignition_core.evaluate(s, now=3.501)
        self.assertEqual(proved["decision"], "GO")
        self.assertEqual(proved["ignition"]["leader"], "futures")
        self.assertTrue(proved["ignition"]["futures_cash_response_ok"])

    def test_bbo_without_executed_flow_cannot_start_episode(self):
        s = state()
        ignition_signals.observe_bbo(
            s, "binance_spot", bid=99.0, ask=101.0,
            bid_qty=100.0, ask_qty=0.1, receive_time_ms=3_000,
        )
        result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "WAIT")
        self.assertIsNone(result.get("causal_episode_id"))

    def test_epoch_reset_does_not_bridge_an_episode(self):
        s = state()
        warm(s, "binance_spot")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "binance_spot", 3_000)
        first = ignition_core.evaluate(s, now=3.101)
        self.assertIsNotNone(first.get("causal_episode_id"))
        ignition_signals.reset_venue(s, "binance_spot", 2)
        reset = ignition_core.evaluate(s, now=3.102)
        self.assertEqual(reset["decision"], "WAIT")
        self.assertEqual(reset["reason"], "EXECUTED_FLOW_EPOCH_RESET")

    def test_late_exchange_event_cannot_rewrite_prior_receive_time_decision(self):
        s = state()
        # Keep the frozen Bias older than the first impulse while all feed ages
        # remain valid at the later receive time.
        s.bias_updated_at = 1.8
        warm(s, "binance_spot")
        warm(s, "coinbase_spot")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "binance_spot", 3_000)
        ignition_core.evaluate(s, now=3.101)
        bucket(s, "binance_spot", 3_100, base=100.003)
        proved = ignition_core.evaluate(s, now=3.201)
        self.assertEqual(proved["decision"], "GO")
        episode_id = proved["causal_episode_id"]

        for name in (
            "thoi_gian_tick_cuoi", "thoi_gian_dong_tien_cuoi",
            "thoi_gian_coinbase_ticker_cuoi", "coinbase_flow_3s_ts",
            "execution_price_time", "thoi_gian_dong_tien_futures_cuoi",
        ):
            setattr(s, name, 4.0)

        # Coinbase event-time predates the entry, but it arrives afterwards.
        # It may affect only a future decision, never the already emitted one.
        ignition_signals.observe_trade(
            s, "coinbase_spot", receive_time_ms=4_001,
            event_time_ms=3_050, price=100.0, qty=0.10,
            aggressive_buy=False,
        )
        ignition_signals.observe_trade(
            s, "coinbase_spot", receive_time_ms=4_051,
            event_time_ms=3_060, price=99.997, qty=0.10,
            aggressive_buy=False,
        )
        later = ignition_core.evaluate(s, now=4.101)
        self.assertEqual(proved["causal_episode_id"], episode_id)
        self.assertNotEqual(later["decision"], "GO")

    def test_shadow_bootstrap_collects_but_live_fails_closed(self):
        s = state()
        result = {
            "decision": "GO", "side": "LONG", "entry_mode": "IGNITION",
            "phase": "RELEASE", "s_votes": {},
            "ignition": {
                "state": "PROVE", "proof_type": "METAORDER_CONTINUATION",
                "cash_venues": ["binance_spot"], "proposer": "binance_spot",
                "consumed_fraction": 0.20, "residual_edge_proxy_bps": 1.0,
                "venue_moves_bps": {"binance_spot": 0.5, "futures": 0.2},
            },
        }
        allowed, report = entry_edge_tier.authorize(result, s)
        self.assertTrue(allowed)
        self.assertTrue(report["bootstrap_shadow_allowed"])
        self.assertFalse(report["cost_ok"])
        s.wstrade_live_armed = True
        allowed, report = entry_edge_tier.authorize(result, s)
        self.assertFalse(allowed)
        self.assertFalse(report["live_empirical_ok"])


if __name__ == "__main__":
    unittest.main()
