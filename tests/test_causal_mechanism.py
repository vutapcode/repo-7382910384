import unittest

from loi_he_thong import causal_mechanism


class CausalMechanismTests(unittest.TestCase):
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
                oi, liq, {"dual_cash_independent": True}
            ),
            "CASH_CONTROL_AFTER_UNWIND",
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


if __name__ == "__main__":
    unittest.main()
