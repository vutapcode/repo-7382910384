import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loi_he_thong import verified_cost_model


class FakeAPI:
    def __init__(self, credentials=True, maker="0.0002", taker="0.0005"):
        self.has_private_credentials = credentials
        self.maker = maker
        self.taker = taker

    async def get_commission_rate(self, _symbol):
        return {
            "makerCommissionRate": self.maker,
            "takerCommissionRate": self.taker,
        }, 200


class VerifiedCostModelTests(unittest.IsolatedAsyncioTestCase):
    SHADOW_PROFILE_ENV = {
        "WSTRADE_MODE": "SHADOW",
        "SMC_ENABLE_TRADING": "false",
        "SMC_MAINNET_ARMED": "false",
        "SMC_MAINNET_EXCLUSIVE_ACCOUNT": "false",
        "SMC_SHADOW_COMMISSION_PROFILE": "BINANCE_USDM_STANDARD",
        "SMC_SHADOW_MAKER_FEE_BPS": "2.0",
        "SMC_SHADOW_TAKER_FEE_BPS": "5.0",
    }

    async def test_reads_account_commission_and_estimates_maker_roundtrip(self):
        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        report = await verified_cost_model.refresh_account_commission(
            FakeAPI(), state
        )
        self.assertTrue(report["verified"])
        self.assertAlmostEqual(state.mainnet_maker_fee_bps, 2.0)
        self.assertAlmostEqual(state.mainnet_taker_fee_bps, 5.0)
        costs = verified_cost_model.estimate({"phase": "ACCEPTANCE"}, state)
        self.assertEqual(costs["execution_style"], "MAKER")
        self.assertTrue(costs["commission_verified"])
        self.assertAlmostEqual(costs["half_spread_bps"], 1.0)
        self.assertAlmostEqual(costs["total_cost_bps"], 9.5)
        self.assertEqual(costs["minimum_net_edge_bps"], 2.0)

    async def test_release_uses_taker_cost_on_both_sides(self):
        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        await verified_cost_model.refresh_account_commission(FakeAPI(), state)
        costs = verified_cost_model.estimate({"phase": "RELEASE"}, state)
        self.assertEqual(costs["execution_style"], "TAKER")
        self.assertAlmostEqual(costs["total_cost_bps"], 15.0)
        self.assertEqual(costs["minimum_net_edge_bps"], 6.0)

    async def test_no_credentials_is_explicit_conservative_fallback(self):
        state = SimpleNamespace(execution_best_bid=0.0, execution_best_ask=0.0)
        report = await verified_cost_model.refresh_account_commission(
            FakeAPI(credentials=False), state, fallback_per_side=9.0
        )
        self.assertFalse(report["verified"])
        with patch.dict(os.environ, {"SMC_SHADOW_FEE_BPS_PER_SIDE": "9.0"}):
            costs = verified_cost_model.estimate({"phase": "ACCEPTANCE"}, state)
        self.assertFalse(costs["commission_verified"])
        self.assertEqual(costs["commission_source"], "CONSERVATIVE_CONFIG_FALLBACK")
        self.assertEqual(
            costs["commission_verification_reason"],
            "PRIVATE_CREDENTIALS_UNAVAILABLE",
        )
        self.assertGreaterEqual(costs["total_cost_bps"], 18.0)

    async def test_private_commission_http_failure_is_visible_without_message(self):
        class RejectedAPI:
            has_private_credentials = True

            async def get_commission_rate(self, _symbol):
                return {"code": -2015, "msg": "secret-bearing exchange text"}, 401

        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        report = await verified_cost_model.refresh_account_commission(
            RejectedAPI(), state, fallback_per_side=9.0
        )
        self.assertFalse(report["verified"])
        self.assertEqual(
            report["reason"], "COMMISSION_HTTP_401_BINANCE_-2015"
        )
        self.assertEqual(
            state.mainnet_commission_verification_reason,
            "COMMISSION_HTTP_401_BINANCE_-2015",
        )
        self.assertNotIn("secret-bearing", str(report))

    async def test_explicit_shadow_profile_skips_locked_private_api(self):
        class LockedAPI:
            has_private_credentials = True

            async def get_commission_rate(self, _symbol):
                raise AssertionError("shadow profile must not query private API")

        state = SimpleNamespace(
            execution_best_bid=99.99, execution_best_ask=100.01,
            wstrade_live_armed=False,
        )
        with patch.dict(os.environ, self.SHADOW_PROFILE_ENV, clear=False):
            report = await verified_cost_model.refresh_account_commission(
                LockedAPI(), state,
            )
            maker = verified_cost_model.estimate(
                {"phase": "ACCEPTANCE"}, state,
            )
            taker = verified_cost_model.estimate(
                {"phase": "RELEASE"}, state,
            )

        self.assertFalse(report["verified"])
        self.assertTrue(report["simulation_cost_usable"])
        self.assertEqual(report["maker_fee_bps"], 2.0)
        self.assertEqual(report["taker_fee_bps"], 5.0)
        self.assertEqual(
            report["reason"],
            "SHADOW_PROFILE_ACTIVE_ACCOUNT_QUERY_SKIPPED",
        )
        self.assertFalse(maker["commission_verified"])
        self.assertTrue(maker["simulation_cost_usable"])
        self.assertAlmostEqual(maker["total_cost_bps"], 9.5)
        self.assertAlmostEqual(taker["total_cost_bps"], 15.0)

    async def test_shadow_profile_revalidates_cost_but_never_grants_live_truth(self):
        state = SimpleNamespace(
            execution_best_bid=99.99, execution_best_ask=100.01,
            wstrade_live_armed=False,
        )
        with patch.dict(os.environ, self.SHADOW_PROFILE_ENV, clear=False):
            await verified_cost_model.refresh_account_commission(
                FakeAPI(), state,
            )
            result = {"phase": "ACCEPTANCE"}
            result["execution_cost_contract"] = (
                verified_cost_model.freeze_execution_cost_contract(
                    result, state,
                )
            )
            ok, reason, _detail = (
                verified_cost_model.validate_execution_cost_contract(
                    result, state, "TAKER",
                )
            )
            state.wstrade_live_armed = True
            live_ok, _live_reason, _live_detail = (
                verified_cost_model.validate_execution_cost_contract(
                    result, state, "TAKER",
                )
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "SHADOW_SIMULATED_COST_CONTRACT_PASS")
        self.assertFalse(live_ok)

    async def test_shadow_fee_plan_matches_actual_maker_fill(self):
        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        await verified_cost_model.refresh_account_commission(FakeAPI(), state)
        plan = verified_cost_model.shadow_execution_plan(
            {"phase": "ACCEPTANCE"}, state, "MAKER_TRADE_THROUGH"
        )
        self.assertEqual(plan["execution_style"], "MAKER")
        self.assertEqual(plan["entry_fee_bps"], 2.0)
        self.assertEqual(plan["exit_fee_bps"], 5.0)
        self.assertEqual(plan["roundtrip_fee_bps"], 7.0)
        self.assertAlmostEqual(plan["total_cost_bps"], 9.5)
        self.assertAlmostEqual(plan["roundtrip_cost_bps"], 9.5)
        self.assertAlmostEqual(plan["remaining_recovery_cost_bps"], 9.5)

    async def test_shadow_market_fallback_uses_taker_entry_fee(self):
        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        await verified_cost_model.refresh_account_commission(FakeAPI(), state)
        plan = verified_cost_model.shadow_execution_plan(
            {"phase": "ACCEPTANCE"}, state, "MARKET_FALLBACK"
        )
        self.assertEqual(plan["execution_style"], "TAKER")
        self.assertEqual(plan["roundtrip_fee_bps"], 10.0)
        self.assertAlmostEqual(plan["decision_total_cost_bps"], 15.0)
        self.assertAlmostEqual(plan["total_cost_bps"], 12.5)
        self.assertAlmostEqual(plan["roundtrip_cost_bps"], 15.0)
        self.assertAlmostEqual(plan["ledger_fee_bps"], 10.0)
        self.assertTrue(plan["entry_execution_cost_embedded_in_fill"])

    async def test_frozen_plan_is_one_cost_truth_for_risk_and_ledger(self):
        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        await verified_cost_model.refresh_account_commission(FakeAPI(), state)
        contract = verified_cost_model.freeze_execution_cost_contract(
            {"phase": "RELEASE"}, state
        )
        plan = verified_cost_model.shadow_execution_plan(
            {"phase": "RELEASE"}, state, "MARKET"
        )
        position = SimpleNamespace(execution_cost_plan=plan)
        self.assertEqual(plan["version"], verified_cost_model.FROZEN_COST_PLAN_VERSION)
        self.assertAlmostEqual(
            plan["roundtrip_cost_bps"], contract["budgets_bps"]["TAKER"]
        )
        self.assertAlmostEqual(
            verified_cost_model.position_total_cost_bps(position),
            plan["remaining_recovery_cost_bps"],
        )
        self.assertAlmostEqual(
            verified_cost_model.position_roundtrip_cost_bps(position),
            plan["roundtrip_cost_bps"],
        )
        self.assertEqual(
            verified_cost_model.position_fee_components(position), (5.0, 5.0)
        )

    async def test_cost_contract_compares_the_same_execution_style(self):
        state = SimpleNamespace(execution_best_bid=99.99, execution_best_ask=100.01)
        await verified_cost_model.refresh_account_commission(FakeAPI(), state)
        result = {"phase": "ACCEPTANCE"}
        contract = verified_cost_model.freeze_execution_cost_contract(result, state)
        result["execution_cost_contract"] = contract

        # Taker is naturally dearer than maker, but must pass its own frozen
        # budget rather than being compared with the maker budget.
        ok, reason, detail = verified_cost_model.validate_execution_cost_contract(
            result, state, "TAKER"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "EXECUTION_COST_CONTRACT_PASS")
        self.assertGreater(
            detail["current_cost_bps"], contract["budgets_bps"]["MAKER"]
        )

        state.execution_best_bid = 99.95
        state.execution_best_ask = 100.05
        original_contract = contract
        result.update({
            "canonical_opportunity_id": 7,
            "causal_episode_id": "episode-7",
        })
        state.canonical_reserved_context = {
            "opportunity_id": 7,
            "causal_episode_id": "episode-7",
            "execution_cost_contract": original_contract,
        }
        # A downstream mutation cannot relax the immutable reservation budget.
        result["execution_cost_contract"] = (
            verified_cost_model.freeze_execution_cost_contract(result, state)
        )
        ok, reason, detail = verified_cost_model.validate_execution_cost_contract(
            result, state, "TAKER"
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "EXECUTION_COST_WORSE_THAN_DECISION")
        self.assertGreater(detail["current_cost_bps"], detail["budget_bps"])


if __name__ == "__main__":
    unittest.main()
