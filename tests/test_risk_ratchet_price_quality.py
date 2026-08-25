import unittest
from types import SimpleNamespace
from loi_he_thong import risk_ratchet_price_quality_hook as h

class T(unittest.TestCase):
    def test_outlier(self):
        p=SimpleNamespace(side="LONG")
        self.assertTrue(h._is_favorable_outlier(p,102.0,101.0,6.0))

    def test_spot_fallback_is_available_with_wider_tolerance(self):
        state=SimpleNamespace(
            best_bid=100.0,best_ask=100.2,thoi_gian_tick_cuoi=100.0,
            coinbase_price=0.0,thoi_gian_coinbase_ticker_cuoi=0.0,atr_1m=0.1,
            atr_1m_updated_at=100.0,
        )
        fair,tol,source=h._fair_price(state,100.5)
        self.assertAlmostEqual(fair,100.1)
        self.assertEqual(source,"SPOT_FALLBACK")
        self.assertGreater(tol,4.0)

    def test_stale_atr_uses_neutral_fallback_tolerance(self):
        market = dict(
            best_bid=99.99, best_ask=100.01, thoi_gian_tick_cuoi=200.0,
            coinbase_price=100.0, thoi_gian_coinbase_ticker_cuoi=200.0,
        )
        fresh = SimpleNamespace(
            **market, atr_1m=1.0, atr_1m_updated_at=80.0,
        )
        stale_high = SimpleNamespace(
            **market, atr_1m=100.0, atr_1m_updated_at=79.0,
        )
        stale_low = SimpleNamespace(
            **market, atr_1m=0.0001, atr_1m_updated_at=79.0,
        )

        _, fresh_tolerance, _ = h._fair_price(fresh, 200.0)
        _, stale_high_tolerance, _ = h._fair_price(stale_high, 200.0)
        _, stale_low_tolerance, _ = h._fair_price(stale_low, 200.0)

        self.assertEqual(fresh_tolerance, 15.0)
        self.assertEqual(stale_high_tolerance, 6.0)
        self.assertEqual(stale_low_tolerance, 6.0)

    def test_exit_passthrough(self):
        def assess(*a,**k): return {"decision":"EXIT","reason":"HARD_SL"}
        r=SimpleNamespace(assess=assess)
        h.install(r)
        out=r.assess(SimpleNamespace(),99.0)
        self.assertEqual(out,{"decision":"EXIT","reason":"HARD_SL"})

    def test_isolated_futures_wick_cannot_trip_long_profit_floor(self):
        def assess(p,px,**kwargs):
            return {"decision":"EXIT","reason":"PROFIT_FLOOR","ts":100.0}
        r=SimpleNamespace(assess=assess)
        h.install(r)
        p=SimpleNamespace(side="LONG",floor=100.0)
        market=SimpleNamespace(
            best_bid=100.19,best_ask=100.21,thoi_gian_tick_cuoi=100.0,
            coinbase_price=100.20,thoi_gian_coinbase_ticker_cuoi=100.0,
            atr_1m=0.1,atr_1m_updated_at=100.0,
        )
        out=r.assess(p,99.90,market_state=market,now=100.0)
        self.assertEqual(out["decision"],"HOLD")
        self.assertEqual(out["reason"],"PROFIT_FLOOR_AWAITING_CASH_CONFIRMATION")

    def test_cash_confirmed_profit_floor_exit_is_not_suppressed(self):
        def assess(p,px,**kwargs):
            return {"decision":"EXIT","reason":"PROFIT_FLOOR","ts":100.0}
        r=SimpleNamespace(assess=assess)
        h.install(r)
        p=SimpleNamespace(side="LONG",floor=100.0)
        market=SimpleNamespace(
            best_bid=99.89,best_ask=99.91,thoi_gian_tick_cuoi=100.0,
            coinbase_price=99.90,thoi_gian_coinbase_ticker_cuoi=100.0,
            atr_1m=0.1,atr_1m_updated_at=100.0,
        )
        out=r.assess(p,99.90,market_state=market,now=100.0)
        self.assertEqual(out["decision"],"EXIT")
        self.assertEqual(out["reason"],"PROFIT_FLOOR")

if __name__=="__main__":
    unittest.main()
