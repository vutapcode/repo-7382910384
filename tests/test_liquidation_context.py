from types import SimpleNamespace
import unittest

from loi_he_thong import liquidation_context as context


def event(side, event_ms, quote=100_000.0, order_id=None):
    price = 100.0
    return {"e": "forceOrder", "E": event_ms, "o": {
        "S": side, "ap": str(price), "q": str(quote / price),
        "T": event_ms, "i": order_id or event_ms,
    }}


def candidate(side="LONG", *, oi="UNWIND", cash=("binance_spot",)):
    return {"decision": "GO", "side": side, "ignition": {
        "cash_venues": list(cash),
        "oi_intent": {"intent": oi, "fresh": True},
    }}


class LiquidationContextTests(unittest.TestCase):
    def test_exchange_side_maps_to_liquidated_position_side(self):
        state = SimpleNamespace()
        self.assertEqual(context.observe_force_order(
            state, event("SELL", 1_000), 1_010), "LIQUIDATION")
        self.assertGreater(state.long_liquidation_quote_total, 0.0)
        self.assertEqual(state.short_liquidation_quote_total, 0.0)
        self.assertEqual(context.observe_force_order(
            state, event("BUY", 1_100), 1_110), "LIQUIDATION")
        self.assertGreater(state.short_liquidation_quote_total, 0.0)

    def test_duplicate_and_out_of_order_do_not_change_aggregate(self):
        state = SimpleNamespace()
        row = event("SELL", 2_000, order_id=7)
        context.observe_force_order(state, row, 2_010)
        total = state.long_liquidation_quote_total
        self.assertEqual(context.observe_force_order(state, row, 2_010), "DUPLICATE")
        self.assertEqual(state.long_liquidation_quote_total, total)
        self.assertEqual(context.observe_force_order(
            state, event("SELL", 1_000), 1_000), "OUT_OF_ORDER")

    def test_active_cascade_is_context_not_a_veto(self):
        state = SimpleNamespace()
        context.observe_force_order(state, event("BUY", 10_000, 200_000), 10_010)
        report = context.assess_entry(state, candidate(), 10.02)
        self.assertEqual(report["phase"], "CASCADE")
        self.assertFalse(report["tail_veto"])
        self.assertFalse(report["can_create_direction"])

    def test_decelerating_unwind_with_single_cash_is_shadow_tail_veto(self):
        state = SimpleNamespace()
        context.observe_force_order(state, event("BUY", 10_000, 200_000), 10_010)
        context.observe_force_order(state, event("BUY", 11_000, 100_000), 11_010)
        report = context.assess_entry(state, candidate(), 12.05)
        self.assertTrue(report["decelerating"])
        self.assertTrue(report["tail_veto"])

    def test_dual_cash_or_position_build_prevents_tail_veto(self):
        state = SimpleNamespace()
        context.observe_force_order(state, event("BUY", 10_000, 200_000), 10_010)
        context.observe_force_order(state, event("BUY", 11_000, 100_000), 11_010)
        dual = context.assess_entry(state, candidate(
            cash=("binance_spot", "coinbase_spot")), 12.05)
        self.assertFalse(dual["tail_veto"])
        build = context.assess_entry(state, candidate(oi="POSITION_BUILD"), 12.05)
        self.assertFalse(build["tail_veto"])


if __name__ == "__main__":
    unittest.main()
