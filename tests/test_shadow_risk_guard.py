import unittest
from types import SimpleNamespace

from loi_he_thong import shadow_risk_guard as risk


def guardian(*statuses, **metadata):
    keys = (
        "S1_price_acceptance",
        "S2_executed_flow",
        "S3_price_x_oi",
    )
    return {"votes": {
        key: {"status": status} for key, status in zip(keys, statuses)
    }, **metadata}


class ShadowRiskGuardTests(unittest.TestCase):
    def position(self):
        p = SimpleNamespace(side="LONG", entry_price=100.0)
        risk.arm(p, 100.0)
        return p

    def test_profit_floor_is_not_overridden_by_retired_whale_snapshot(self):
        p = self.position()
        market = SimpleNamespace(whale_intent_snapshot={
            "state": "SUPPORT", "side": "LONG",
        })
        risk.assess(
            p, 101.0, guardian("NEUTRAL", "NEUTRAL", "NEUTRAL"),
            market_state=market, now=1.0,
        )
        self.assertIsNotNone(p.floor)

        result = risk.assess(
            p, p.floor - 0.01,
            guardian("NEUTRAL", "NEUTRAL", "NEUTRAL"),
            market_state=market, now=1.1,
        )

        self.assertEqual(result["decision"], "EXIT")
        self.assertEqual(result["reason"], "PROFIT_FLOOR")

    def test_lost_support_and_retrace_cannot_exit_without_guardian(self):
        p = self.position()
        risk.assess(
            p, 100.94,
            guardian("SUPPORTIVE", "SUPPORTIVE", "NEUTRAL"), now=1.0,
        )
        first = risk.assess(
            p, 100.61,
            guardian("NEUTRAL", "NEUTRAL", "NEUTRAL"), now=2.0,
        )
        second = risk.assess(
            p, 100.61,
            guardian("NEUTRAL", "NEUTRAL", "NEUTRAL"), now=3.0,
        )
        self.assertEqual(first["decision"], "HOLD")
        self.assertEqual(second["decision"], "HOLD")

    def test_hard_stop_still_wins_over_whale_support(self):
        p = self.position()
        market = SimpleNamespace(whale_intent_snapshot={
            "state": "SUPPORT", "side": "LONG",
        })

        result = risk.assess(
            p, p.hard_sl - 0.01,
            guardian("SUPPORTIVE", "SUPPORTIVE", "SUPPORTIVE"),
            market_state=market, now=1.0,
        )

        self.assertEqual(result["decision"], "EXIT")
        self.assertEqual(result["reason"], "HARD_SL")

    def test_support_widens_runner_but_floor_never_moves_back(self):
        p = self.position()
        supported = guardian("SUPPORTIVE", "SUPPORTIVE", "SUPPORTIVE")
        risk.assess(p, 102.20, supported, now=1.0)
        supported_floor = p.floor_r
        self.assertEqual(p.tier_mode, "MAX_RIDE")

        risk.assess(
            p, 102.10,
            guardian("NEUTRAL", "NEUTRAL", "NEUTRAL"), now=2.0,
        )
        self.assertGreaterEqual(p.floor_r, supported_floor)

    def test_runner_shield_does_not_tighten_profit_floor_early(self):
        p = self.position()
        report = risk.assess(
            p, 102.20,
            guardian(
                "ADVERSE", "ADVERSE", "NEUTRAL",
                decision="DETERIORATING", runner_shield_active=True,
                kill_fast=False,
            ),
            now=1.0,
        )
        self.assertEqual(report["decision"], "HOLD")
        self.assertEqual(report["tier_mode"], "RIDE")

    def test_trend_shield_does_not_tighten_profit_floor_early(self):
        p = self.position()
        report = risk.assess(
            p, 102.20,
            guardian(
                "ADVERSE", "ADVERSE", "NEUTRAL",
                decision="DETERIORATING", trend_shield_active=True,
                kill_fast=False,
            ),
            now=1.0,
        )
        self.assertEqual(report["decision"], "HOLD")
        self.assertEqual(report["tier_mode"], "RIDE")


if __name__ == "__main__":
    unittest.main()
