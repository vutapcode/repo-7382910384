import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loi_he_thong import mainnet_safety as safety


class FakeAPI:
    testnet = False

    def __init__(self):
        self.income = []
        self.positions = [{
            'symbol': 'BTCUSDT', 'positionSide': 'LONG', 'positionAmt': '0',
            'marginType': 'isolated', 'leverage': '20',
        }]

    async def get_all_orders(self, *args, **kwargs):
        return [], 200

    async def get_income_history(self, *args, **kwargs):
        return self.income, 200

    async def get_positions(self, *args, **kwargs):
        return self.positions, 200

    async def get_open_orders(self, *args, **kwargs):
        return [], 200

    async def get_open_algo_orders(self, *args, **kwargs):
        return [], 200

    async def get_balance_details(self):
        return {'availableBalance': '5.71'}, 200

    async def change_position_mode(self, *args, **kwargs):
        return {'code': 200}, 200

    async def get_multi_asset_mode(self, *args, **kwargs):
        return {'multiAssetsMargin': False}, 200

    async def change_multi_asset_mode(self, *args, **kwargs):
        return {'code': 200}, 200

    async def change_margin_type(self, *args, **kwargs):
        return {'code': 200}, 200

    async def change_leverage(self, *args, **kwargs):
        return {'leverage': 20}, 200

    async def get_commission_rate(self, *args, **kwargs):
        return {
            'makerCommissionRate': '0.0002',
            'takerCommissionRate': '0.0005',
        }, 200

    async def cancel_all_open_orders(self, *args, **kwargs):
        return {'code': 200}, 200

    async def cancel_all_algo_orders(self, *args, **kwargs):
        return {'code': 200}, 200


class MainnetSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.safety_path = str(Path(self.temporary.name) / 'safety.json')

    def tearDown(self):
        self.temporary.cleanup()

    def env(self, **overrides):
        base = {
            'SMC_EXECUTION_VENUE': 'MAINNET',
            'SMC_MAINNET_ARMED': 'true',
            'SMC_MAINNET_EXCLUSIVE_ACCOUNT': 'true',
            'SMC_FIXED_QTY_BTC': '0.001',
            'SMC_LEVERAGE': '20',
            'SMC_MARGIN_TYPE': 'ISOLATED',
            'SMC_PASSIVE_ENTRY_FEE_BPS': '2',
            'SMC_SHADOW_ENTRY_FEE_BPS': '5',
            'SMC_SHADOW_EXIT_FEE_BPS': '5',
            'SMC_DAILY_NET_LOSS_USDT': '0.30',
            'SMC_MAX_PLANNED_LOSS_USDT': '0.12',
            'SMC_MAX_CONSECUTIVE_LOSSES': '2',
            'SMC_LOSS_STREAK_COOLDOWN_SECONDS': '36000',
            'SMC_MAINNET_SAFETY_STATE_PATH': self.safety_path,
        }
        base.update(overrides)
        return mock.patch.dict(os.environ, base, clear=False)

    @staticmethod
    def risk_plan(planned=0.12):
        return {
            'eligible': True,
            'planned_worst_loss_usdt': planned,
            'bounded_hard_sl': 63650.0,
        }

    def test_mainnet_credentials_never_fall_back_to_dotenv(self):
        with self.env(), mock.patch.dict(
            os.environ, {'BINANCE_API_KEY': 'unsafe-env-value'}, clear=False
        ):
            os.environ.pop('CREDENTIALS_DIRECTORY', None)
            self.assertEqual(safety.credential('binance_api_key', 'BINANCE_API_KEY'), '')

    def test_fixed_quantity_must_match_exchange_step(self):
        filters = {'step_size': 0.001, 'min_qty': 0.001}
        with self.env():
            self.assertEqual(safety.validate_static_config(filters), (True, 'PASS'))
        with self.env(SMC_FIXED_QTY_BTC='0.0015'):
            ok, reason = safety.validate_static_config(filters)
            self.assertFalse(ok)
            self.assertEqual(reason, 'MAINNET_FIXED_QTY_MUST_BE_0_001')

    def test_fixed_quantity_preflight_uses_real_mainnet_lot(self):
        filters = {
            'step_size': 0.001, 'min_qty': 0.001,
            'max_qty': 1000.0, 'min_notional': 50.0,
        }
        with self.env():
            result = safety.fixed_quantity_feasibility(
                5.519, 63360.0, filters
            )
        self.assertTrue(result['executable'], result)
        self.assertEqual(result['quantity'], 0.001)
        self.assertEqual(result['allocation_unit'], 'FIXED_BASE_ASSET_QTY')
        self.assertTrue(result['risk_and_economics_pending_executor'])
        self.assertAlmostEqual(result['initial_margin_usdt'], 3.168, places=3)

    def test_fixed_quantity_preflight_still_fails_closed(self):
        filters = {
            'step_size': 0.001, 'min_qty': 0.001,
            'max_qty': 1000.0, 'min_notional': 50.0,
        }
        with self.env():
            poor = safety.fixed_quantity_feasibility(3.0, 63360.0, filters)
            too_small = safety.fixed_quantity_feasibility(
                5.519, 63360.0, {**filters, 'min_notional': 100.0}
            )
        self.assertFalse(poor['executable'])
        self.assertEqual(poor['reason'], 'MAINNET_PROVISIONAL_MARGIN_INSUFFICIENT')
        self.assertFalse(too_small['executable'])
        self.assertEqual(too_small['reason'], 'MAINNET_MIN_NOTIONAL_REJECTED')

    async def test_exchange_gate_accepts_5_71_wallet_with_reserve(self):
        state = SimpleNamespace(
            trade_cycles={}, mainnet_worst_roundtrip_fee_bps=10.0,
            exchange_filters={
                'step_size': 0.001, 'min_qty': 0.001,
                'max_qty': 1000.0, 'min_notional': 50.0,
            },
        )
        with self.env():
            ok, reason, details = await safety.exchange_entry_gate(
                FakeAPI(), state, 63700.0, 63650.0, self.risk_plan()
            )
        self.assertTrue(ok, details)
        self.assertEqual(reason, 'PASS')
        self.assertLess(details['required_balance'], 5.71)

    async def test_exchange_gate_rechecks_live_min_notional(self):
        state = SimpleNamespace(
            trade_cycles={}, mainnet_worst_roundtrip_fee_bps=10.0,
            exchange_filters={
                'step_size': 0.001, 'min_qty': 0.001,
                'max_qty': 1000.0, 'min_notional': 100.0,
            },
        )
        with self.env():
            ok, reason, _ = await safety.exchange_entry_gate(
                FakeAPI(), state, 63700.0, 63650.0, self.risk_plan()
            )
        self.assertFalse(ok)
        self.assertEqual(reason, 'MAINNET_MIN_NOTIONAL_REJECTED')

    async def test_exchange_gate_rejects_other_symbol_activity(self):
        api = FakeAPI()

        async def positions(*args, **kwargs):
            return api.positions + [{
                'symbol': 'ETHUSDT', 'positionSide': 'LONG',
                'positionAmt': '0.01', 'marginType': 'isolated',
                'leverage': '20',
            }], 200

        api.get_positions = positions
        state = SimpleNamespace(
            trade_cycles={}, mainnet_worst_roundtrip_fee_bps=10.0,
            exchange_filters={
                'step_size': 0.001, 'min_qty': 0.001,
                'max_qty': 1000.0, 'min_notional': 50.0,
            },
        )
        with self.env():
            ok, reason, _ = await safety.exchange_entry_gate(
                api, state, 63700.0, 63650.0, self.risk_plan()
            )
        self.assertFalse(ok)
        self.assertEqual(reason, 'MAINNET_EXCLUSIVE_ACCOUNT_CONTAMINATED')

    async def test_daily_cap_uses_unique_filled_entry_orders(self):
        api = FakeAPI()

        async def orders(*args, **kwargs):
            return [
                {'orderId': i, 'clientOrderId': f'smc_entry_{i}', 'executedQty': '0.001'}
                for i in range(8)
            ], 200

        api.get_all_orders = orders
        with self.env():
            ok, reason, snapshot = await safety.refresh_daily_gate(
                api, SimpleNamespace(trade_cycles={})
            )
        self.assertFalse(ok)
        self.assertEqual(reason, 'MAINNET_DAILY_ENTRY_CAP')
        self.assertEqual(snapshot['filled_entries'], 8)

    async def test_prepare_configures_hedge_isolated_x20(self):
        state = SimpleNamespace(run_id='run-test')
        with self.env():
            ok, reason = await safety.prepare_mainnet_account(FakeAPI(), state)
        self.assertTrue(ok)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(state.mainnet_taker_fee_bps, 5.0)

    async def test_prepare_disables_multi_asset_before_isolated(self):
        api = FakeAPI()
        calls = []
        modes = iter((True, False))

        async def get_mode(*args, **kwargs):
            return {'multiAssetsMargin': next(modes)}, 200

        async def change_mode(enabled=False):
            calls.append(enabled)
            return {'code': 200}, 200

        api.get_multi_asset_mode = get_mode
        api.change_multi_asset_mode = change_mode
        state = SimpleNamespace(run_id='run-test')
        with self.env():
            ok, reason = await safety.prepare_mainnet_account(api, state)
        self.assertTrue(ok)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(calls, [False])

    def test_loss_budget_bounds_hard_sl_and_keeps_soft_outside(self):
        levels = {'soft_sl': 63655.0, 'hard_sl': 63500.0}
        with self.env():
            adjusted, plan = safety.apply_mainnet_loss_budget(
                levels, 'LONG', 63700.0, 0.001, 50.0, 0.1,
                0.02, 2.0, 5.0,
            )
        self.assertTrue(plan['eligible'], plan)
        self.assertLess(adjusted['hard_sl'], levels['soft_sl'])
        self.assertGreater(adjusted['hard_sl'], levels['hard_sl'])
        self.assertLessEqual(plan['planned_worst_loss_usdt'], 0.12 + 1e-9)

    def test_loss_budget_rejects_soft_sl_outside_budget(self):
        levels = {'soft_sl': 63600.0, 'hard_sl': 63500.0}
        with self.env():
            _, plan = safety.apply_mainnet_loss_budget(
                levels, 'LONG', 63700.0, 0.001, 50.0, 0.1,
                0.02, 2.0, 5.0,
            )
        self.assertFalse(plan['eligible'])
        self.assertEqual(plan['reason'], 'SOFT_SL_OUTSIDE_SAFE_BUDGET')

    def test_loss_budget_expands_too_close_short_hard_sl_within_cap(self):
        levels = {'soft_sl': 62926.0, 'hard_sl': 62926.1}
        with self.env():
            adjusted, plan = safety.apply_mainnet_loss_budget(
                levels, 'SHORT', 62881.2, 0.001, 14.97857, 0.1,
                0.02, 2.0, 5.0,
            )
        self.assertTrue(plan['eligible'], plan)
        self.assertTrue(plan['hard_sl_expanded_for_geometry'])
        self.assertGreaterEqual(
            adjusted['hard_sl'],
            plan['soft_sl'] + plan['minimum_hard_soft_gap'] - 1e-9,
        )
        self.assertLessEqual(plan['planned_worst_loss_usdt'], 0.12 + 1e-9)

    def test_loss_budget_cannot_expand_hard_sl_past_budget(self):
        levels = {'soft_sl': 63600.0, 'hard_sl': 63600.1}
        with self.env(SMC_MAX_PLANNED_LOSS_USDT='0.09'):
            _, plan = safety.apply_mainnet_loss_budget(
                levels, 'SHORT', 63590.0, 0.001, 50.0, 0.1,
                0.02, 5.0, 5.0,
            )
        self.assertFalse(plan['eligible'])
        self.assertEqual(plan['reason'], 'SOFT_SL_OUTSIDE_SAFE_BUDGET')

    def test_taker_entry_has_less_stop_room_than_maker(self):
        levels = {'soft_sl': 63660.0, 'hard_sl': 63500.0}
        with self.env():
            _, maker = safety.apply_mainnet_loss_budget(
                levels, 'LONG', 63700.0, 0.001, 50.0, 0.1,
                0.02, 2.0, 5.0,
            )
            _, taker = safety.apply_mainnet_loss_budget(
                levels, 'LONG', 63700.0, 0.001, 50.0, 0.1,
                0.02, 5.0, 5.0,
            )
        self.assertLess(taker['maximum_stop_distance'], maker['maximum_stop_distance'])

    async def test_exchange_gate_requires_verified_risk_plan(self):
        state = SimpleNamespace(
            trade_cycles={}, mainnet_worst_roundtrip_fee_bps=10.0,
            exchange_filters={
                'step_size': 0.001, 'min_qty': 0.001,
                'max_qty': 1000.0, 'min_notional': 50.0,
            },
        )
        with self.env():
            ok, reason, _ = await safety.exchange_entry_gate(
                FakeAPI(), state, 63700.0, 63650.0
            )
        self.assertFalse(ok)
        self.assertEqual(reason, 'MAINNET_PLANNED_RISK_UNVERIFIED')

    async def test_daily_remaining_room_rejects_full_risk(self):
        api = FakeAPI()
        api.income = [{
            'incomeType': 'REALIZED_PNL', 'income': '-0.25'
        }]
        state = SimpleNamespace(
            trade_cycles={}, mainnet_worst_roundtrip_fee_bps=10.0,
            exchange_filters={
                'step_size': 0.001, 'min_qty': 0.001,
                'max_qty': 1000.0, 'min_notional': 50.0,
            },
        )
        with self.env():
            ok, reason, details = await safety.exchange_entry_gate(
                api, state, 63700.0, 63650.0, self.risk_plan()
            )
        self.assertFalse(ok)
        self.assertEqual(reason, 'MAINNET_DAILY_RISK_BUDGET_INSUFFICIENT')
        self.assertAlmostEqual(details['daily_risk_room_usdt'], 0.05)

    def test_two_losses_persist_a_ten_hour_cooldown(self):
        state = SimpleNamespace(trade_cycles={})
        with self.env():
            safety._register_outcome(state, 'cycle-1', -0.01, 1000.0)
            result = safety._register_outcome(state, 'cycle-2', -0.01, 1001.0)
            duplicate = safety._register_outcome(
                state, 'cycle-2', -0.01, 1002.0
            )
            restored = safety._load_safety_state(state)
        self.assertEqual(result['loss_streak'], 2)
        self.assertEqual(duplicate['loss_streak'], 2)
        self.assertEqual(restored['loss_streak'], 2)
        self.assertEqual(restored['cooldown_until'], 37001.0)

    def test_reconciliation_dedupes_more_than_the_last_cycle(self):
        state = SimpleNamespace(trade_cycles={})
        with self.env():
            safety._register_outcome(state, 'cycle-1', -0.01, 1000.0)
            safety._register_outcome(state, 'cycle-2', 0.01, 1001.0)
            replay = safety._register_outcome(state, 'cycle-1', -0.01, 1002.0)
        self.assertEqual(replay['loss_streak'], 0)
        self.assertEqual(replay['last_cycle_id'], 'cycle-2')


if __name__ == '__main__':
    unittest.main()
