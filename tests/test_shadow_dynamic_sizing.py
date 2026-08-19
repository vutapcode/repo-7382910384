import unittest
from types import SimpleNamespace

from loi_he_thong import shadow_dynamic_sizing_hook as hook


class ShadowDynamicSizingTest(unittest.TestCase):
    def _shadow(self, balance=5.4):
        state = SimpleNamespace(mainnet_shadow_balance_usdt=balance)
        app = SimpleNamespace(state=state)
        calls = {"close_qty": None}

        def feasibility(price):
            qty = shadow.QTY_BTC
            required = qty * float(price) / shado.LEVERAGE
            fees = qty * float(price) * (2.0 * shadow.FEE_BPS_PER_SIDE / 10000.0)
            return {"ok": required + fees <= state.mainnet_shadow_balance_usdt}

        def close(pos, guardian_result, now):
            calls["close_qty"] = shadow.QTY_BTC
            return "closed"

        shadow = SimpleNamespace(
            app=app,
            START_BALANCE_USDT=balance,
            LEVERAGE=20.0,
            FEE_BPS_PER_SIDE=9.0,
            QTY_BTC=0.001,
            _entry_feasibility=feasibility,
            _close_shadow=close,
        )
        return shadow, calls

    def test_keeps_target_when_affordable(self):
        shadow, _ = self._shadow(balance=20.0)
        hook.install(shadow)
        result = shadow._entry_feasibility(100000.0)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["adaptive_qty_btc"], 0.001)
        self.assertEqual(result["sizing_mode"], "TARGET")

    def test_scales_down_instead_of_bankroll_skip(self):
        shadow, _ = self._shadow(balance=5.4)
        hook.install(shadow)
        result = shadow._entry_feasibility(120000.0)
        self.assertTrue(result["ok"])
        self.assertGreater(result["adaptive_qty_btc"], 0.0)
        self.assertLess(result["adaptive_qty_btc"], 0.001)
        self.assertEqual(result["sizing_mode"], "BALANCE_ADAPTIVE")

    def test_close_uses_persisted_position_qty(self):
        shadow, calls = self._shadow(balance=5.4)
        hook.install(shadow)
        pos = SimpleNamespace(qty=0.00073)
        self.assertEqual(shadow._close_shadow(pos, {}, 1.0), "closed")
        self.assertAlmostEqual(calls["close_qty"], 0.00073)
        self.assertAlmostEqual(shadow.QTY_BTC, 0.001)


if __name__ == "__main__":
    unittest.main()
