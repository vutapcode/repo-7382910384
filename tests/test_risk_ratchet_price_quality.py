import unittest
from types import SimpleNamespace
from loi_he_thong import risk_ratchet_price_quality_hook as hook

class T(unittest.TestCase):
    def _risk(self):
        def tier_mode(_):
            return "PROTECT", 0, 0, ["NEUTRAL", "NEUTRAL", "NEUTRAL"]
        def candidate(best_r, fee_r, mode):
            return (None, "INITIAL") if best_r < 1.0 else (max(0.25, fee_r + 0.05), "LOCK_1")
        def assess(p, px, guardian=None, market_state=None, now=None):
            p.best = max(p.best, float(px))
            p.best_r = max(0.0, (p.best - p.entry_price) / p.r)
            if float(px) <= p.hard_sl:
                return {"decision": "EXIT", "reason": "HARD_SL", "ts": float(now or 0.0)}
            floor_r, stage = candidate(p.best_r, p.fee_r, "PROTECT")
            if floor_r is not None:
                p.floor_r = floor_r if p.floor_r is None else max(p.floor_r, floor_r)
                p.floor = p.entry_price + p.floor_r * p.r
                p.stage = stage
            return {"decision": "HOLD", "reason": "TIER_S_PROTECT", "ts": float(now or 0.0),
                    "best_r": p.best_r, "profit_floor": p.floor, "floor_r": p.floor_r, "stage": p.stage}
        return SimpleNamespace(assess=assess, tier_mode=tier_mode, _candidate=candidate)

    def _p(self):
        return SimpleNamespace(side="LONG", entry_price=100.0, r=0.55, hard_sl=99.45,
                               best=100.0, best_rZ0.0, floor=None, floor_r=None,
                               stage="INITIAL", fee_r=0.1)

    def _m(self):
        return SimpleNamespace(best_bid=100.99, best_ask=101.01, thoi_gian_tick_cuoi=100.0,
                               coinbase_price=101.0, thoi_gian_coinbase_ticker_cuoi=100.0, atr_1m=0.20)

    def test_caps_isolated_futures_wick(self):
        risk, p = self._risk(), self._p()
        hook.install(risk)
        out = risk.assess(p, 102.0, market_state=self._m(), now=100.0)
        self.assertEqual(out["decision"], "HOLD")
        self.assertAlmostEqual(p.best, 101.0, places=6)
        self.assertEqual(out["ratchet_price_quality"]["mode"], "CROSS_VENUE_CAPPED")

    def test_hard_sl_passthrough(self):
        risk, p = self._risk(), self._p()
        hook.install(risk)
        out = risk.assess(p, 99.0, market_state=self._m(), now=100.0)
        self.assertEqual((out["decision"], out["reason"]), ("EXIT", "HARD_SL"))
        self.assertNotIn("ratchet_price_quality", out)

if __name__ == "__main__":
    unittest.main()
