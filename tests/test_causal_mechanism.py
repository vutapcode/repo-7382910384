import unittest

from loi_he_thong import causal_mechanism


class CausalMechanismTests(unittest.TestCase):
    @staticmethod
    def converting_cash(side="LONG"):
        return {
            "dual_cash_independent": True,
            "dual_cash_flow_price_conversion": True,
            "fresh": True,
            "side": side,
            "max_age_ms": 600,
            "venues": {
                "binance_spot": {
                    "age_ms": 80, "price_conversion_bps": 0.3,
                },
                "coinbase_spot": {
                    "age_ms": 150, "price_conversion_bps": 0.2,
                },
            },
        }

    def test_position_build(self):
        oi = {"status": "FRESH_POSITION_BUILD", "intent": "POSITION_BUILD"}
        self.assertEqual(causal_mechanism.classify(oi, {}, {}), "POSITION_BUILD")

    def test_unwind_no_liquidation(self):
        oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
        self.assertEqual(causal_mechanism.classify(oi, {}, {}), "UNWIND")

    def test_forced_closing(self):
        oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
        liq = {"burst": True, "decelerating": False}
        self.assertEqual(
            causal_mechanism.classify(
                oi, liq, {"dual_cash_independent": True}
            ),
            "FORCED_CLOSING",
        )
        self.assertEqual(causal_mechanism.classify(oi, liq, {}), "FORCED_CLOSING")

    def test_cash_control_after_unwind(self):
        oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
        liq = {"burst": True, "decelerating": True}
        self.assertEqual(
            causal_mechanism.classify(
                oi, liq, self.converting_cash()
            ),
            "CASH_CONTROL_AFTER_UNWIND",
        )

    def test_cash_presence_without_fresh_conversion_is_not_control(self):
        oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
        liq = {"burst": True, "decelerating": True}
        for cash in (
            {"dual_cash_independent": True},
            {
                **self.converting_cash(),
                "dual_cash_flow_price_conversion": False,
            },
            {
                **self.converting_cash(),
                "fresh": False,
            },
        ):
            with self.subTest(cash=cash):
                self.assertEqual(
                    causal_mechanism.classify(oi, liq, cash),
                    "FORCED_CLOSING",
                )

    def test_forced_closing_decelerating_no_cash(self):
        oi = {"status": "FRESH_UNWIND", "intent": "UNWIND"}
        liq = {"burst": True, "decelerating": True}
        self.assertEqual(
            causal_mechanism.classify(
                oi, liq, {"dual_cash_independent": False}
            ),
            "FORCED_CLOSING",
        )

    def test_unresolved(self):
        liq = {"burst": False, "decelerating": False}
        for status in (
            "STALE_UNKNOWN", "UNCHANGED_UNKNOWN", "UNAVAILABLE", "FRESH_CONFLICT"
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    causal_mechanism.classify({"status": status}, liq, {}),
                    "UNRESOLVED",
                )

    def test_stale_oi_preserves_cash_control_without_guessing_positioning(self):
        result = causal_mechanism.classify(
            {"status": "STALE_UNKNOWN"},
            {"burst": False, "decelerating": False},
            self.converting_cash(),
        )
        self.assertEqual(result, "CASH_CONTROL_POSITIONING_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
