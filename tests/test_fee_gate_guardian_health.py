import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


path_engine = load(
    'realizable_path_tests',
    '2_suy_luan_mapping/tong_ket_chi_huy/dynamic_path_fee.py',
)
guardian = load(
    'realizable_guardian_tests',
    '3_thuc_thi/ve_si_lenh/bao_ve_khan_cap.py',
)
watchdog = load(
    'realizable_watchdog_tests', '3_thuc_thi/giam_sat_he_thong.py'
)
ram = load('realizable_ram_tests', 'loi_he_thong/bo_nho_ram.py')
reconcile = load(
    'realizable_reconcile_tests',
    '3_thuc_thi/quan_ly_vi_the/dong_bo_trang_thai.py',
)


def saved_zero_allocation_case(case):
    if case == '20:16':
        near = {
            'target_id': 'near-2016', 'price': 64284.7,
            'distance_bps': 0.3889101493, 'p_hit_lcb': 0.8452270686,
            'p_stop_ucb': 0.0801191154, 'stop_distance_bps': 6.3781264487,
        }
        runner = {
            'target_id': 'runner-2016', 'price': 64470.0,
            'distance_bps': 29.2149304162, 'p_hit_lcb': 0.5562287922,
            'p_stop_ucb': 0.2329666855, 'stop_distance_bps': 6.3781264487,
        }
        epistemic = 0.6490084435
    else:
        near = {
            'target_id': 'near-2026', 'price': 64150.0,
            'distance_bps': 0.1558870754, 'p_hit_lcb': 0.7984529429,
            'p_stop_ucb': 0.1062127611, 'stop_distance_bps': 7.3594288298,
        }
        runner = {
            'target_id': 'runner-2026', 'price': 64366.0,
            'distance_bps': 33.8274953624, 'p_hit_lcb': 0.4484668672,
            'p_stop_ucb': 0.2934757178, 'stop_distance_bps': 7.3594288298,
        }
        epistemic = 0.6675859767
    return {
        'tp1_allocation': 0.0, 'all_in_cost_bps': 8.0,
        'epistemic_buffer_bps': epistemic,
        'selected_target_ids': [near['target_id'], runner['target_id']],
        'target_candidates': [near, runner],
    }


class RealizableFeeGateTests(unittest.TestCase):
    def test_recent_long_regressions_are_passive_not_market(self):
        filters = {'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0}
        for case, qty in (('20:16', 0.001), ('20:26', 0.0011)):
            with self.subTest(case=case):
                result = path_engine.reassess_saved_plan(
                    saved_zero_allocation_case(case), qty, filters
                )
                self.assertTrue(result['available'])
                self.assertGreater(result['realizable_edge_lcb'], 0.0)
                self.assertFalse(result['checkpoint_monetizable'])
                self.assertEqual(result['entry_policy'], 'PASSIVE_RETEST_ONLY')
                self.assertFalse(result['trailing_applies_after_tp1'])

    def test_unmonetized_tp1_never_activates_trailing_or_moves_stop(self):
        position = SimpleNamespace(
            tp1_done=False, trailing_active=False,
            soft_sl=99.0, entry_price=100.0,
        )
        guardian.mark_unmonetized_tp1(position)
        self.assertTrue(position.tp1_done)
        self.assertFalse(position.trailing_active)
        self.assertEqual(position.soft_sl, 99.0)

    def test_split_sl_tail_is_reserved_for_sl2(self):
        position = SimpleNamespace(split_sl_enabled=True, split_sl1_done=True)
        self.assertEqual(guardian.split_sl_soft_action(position), 'TAIL_TO_SL2')
        position.split_sl1_done = False
        self.assertEqual(guardian.split_sl_soft_action(position), 'EXECUTE_SL1')


class MainnetFullExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_early_protection_closes_full_mainnet_step(self):
        position = SimpleNamespace(
            qty=0.001, initial_qty=0.001,
            protection_reasons_done=[], split_sl_enabled=False,
        )
        state = SimpleNamespace(vi_the_hien_tai=position)
        api = SimpleNamespace(testnet=False)
        with mock.patch.object(
            guardian, 'close_position', new=mock.AsyncMock(return_value=True)
        ) as close:
            result = await guardian.close_early_protection(
                api, 'BTCUSDT', 'LONG', state, 'TIME_STOP'
            )
        self.assertTrue(result)
        close.assert_awaited_once_with(
            api, 'BTCUSDT', 'LONG', 0.001, state, 'TIME_STOP'
        )


class HealthTests(unittest.TestCase):
    def test_out_of_band_sample_fail_closes_stalled_loop(self):
        state = SimpleNamespace(
            event_loop_heartbeat_mono=10.0, event_loop_stalled=False,
            system_ready=True, trading_enabled=True,
        )
        self.assertTrue(watchdog._sample_out_of_band(
            state, now_mono=15.0, cpu_ratio=0.95, stall_seconds=3.0
        ))
        self.assertFalse(state.system_ready)
        self.assertFalse(state.trading_enabled)
        self.assertGreater(state.event_loop_lag_seconds, 3.0)

    def test_sustained_cpu_runaway_fail_closes_but_short_spike_does_not(self):
        state = SimpleNamespace(system_ready=True, trading_enabled=True)
        hot_since, runaway = watchdog._sample_cpu_runaway(
            state, 10.0, 0.95, ratio_threshold=0.85, sustain_seconds=5.0,
        )
        self.assertFalse(runaway)
        hot_since, runaway = watchdog._sample_cpu_runaway(
            state, 16.0, 0.95, hot_since,
            ratio_threshold=0.85, sustain_seconds=5.0,
        )
        self.assertTrue(runaway)
        self.assertFalse(state.trading_enabled)
        _, runaway = watchdog._sample_cpu_runaway(
            state, 16.5, 0.20, hot_since,
            ratio_threshold=0.85, sustain_seconds=5.0,
        )
        self.assertFalse(runaway)

    def test_stale_journal_fail_closes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_text('{}\n', encoding='utf-8')
            state = SimpleNamespace(system_ready=True, trading_enabled=True)
            self.assertTrue(watchdog._sample_journal_health(
                state, path, now=path.stat().st_mtime + 91,
                stale_seconds=90,
            ))
            self.assertTrue(state.journal_stalled)
            self.assertFalse(state.trading_enabled)

    def test_idle_minimal_journal_uses_task_and_persist_heartbeats(self):
        state = SimpleNamespace(
            system_ready=True, trading_enabled=True,
            journal_loop_heartbeat_mono=100.0,
            journal_last_persist_mono=95.0,
        )
        self.assertFalse(watchdog._sample_journal_health(
            state, path='/missing/events.jsonl', now=1000.0,
            now_mono=120.0, stale_seconds=90.0,
        ))
        self.assertFalse(state.journal_stalled)

    def test_dead_journal_task_still_fail_closes_with_minimal_audit(self):
        state = SimpleNamespace(
            system_ready=True, trading_enabled=True,
            journal_loop_heartbeat_mono=100.0,
            journal_last_persist_mono=100.0,
        )
        self.assertTrue(watchdog._sample_journal_health(
            state, path='/missing/events.jsonl', now=1000.0,
            now_mono=191.0, stale_seconds=90.0,
        ))
        self.assertTrue(state.journal_stalled)
        self.assertFalse(state.trading_enabled)

    def test_stale_recorder_health_is_persisted_as_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.json'
            path.write_text(json.dumps({
                'status': 'RUNNING', 'current_status': 'OK',
                'updated_at_ms': 1000, 'component_health': {},
            }), encoding='utf-8')
            self.assertTrue(watchdog.mark_recorder_stale(
                path, now_ms=22000, stale_seconds=20.0
            ))
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['status'], 'ERROR')
            self.assertEqual(payload['current_status'], 'ERROR')
            self.assertEqual(
                payload['last_error']['component'], 'recorder_process'
            )


class ReconcileTerminalFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_sl2_fill_closes_cycle_and_reconciles_all_fees(self):
        state = ram.SharedState()
        journal = reconcile.journal_mod
        cycle_id = journal.create_cycle(state, {
            'setup_id': 'split-fill', 'setup_generation': 1,
            'bias': 'LONG', 'mode': 'TRANSITION-PULLBACK',
        }, 0.0011, 64149.0, {'mode': 'DYNAMIC_PATH_ENFORCED'})
        journal.record_actual_order(
            state, cycle_id, 'ENTRY',
            {'orderId': 1, 'clientOrderId': 'entry', 'executedQty': '0.0011'},
            0.0011, 64248.8,
        )
        journal.record_actual_order(
            state, cycle_id, 'SL1',
            {'orderId': 2, 'clientOrderId': 'sl1', 'executedQty': '0.0009'},
            0.0009, 64208.8, reason='SL1_90',
        )
        journal.mark_actual_open(state, cycle_id, 64248.8, 64149.0)
        trades = [
            {'id': 11, 'orderId': 1, 'side': 'BUY', 'positionSide': 'LONG',
             'price': '64248.8', 'qty': '0.0011', 'quoteQty': '70.67368',
             'commission': '0.01413473', 'commissionAsset': 'USDT',
             'realizedPnl': '0', 'time': 1000, 'maker': True},
            {'id': 12, 'orderId': 2, 'side': 'SELL', 'positionSide': 'LONG',
             'price': '64208.8', 'qty': '0.0009', 'quoteQty': '57.78792',
             'commission': '0.02311516', 'commissionAsset': 'USDT',
             'realizedPnl': '-0.036', 'time': 2000, 'maker': False},
            {'id': 13, 'orderId': 3, 'side': 'SELL', 'positionSide': 'LONG',
             'price': '64138.3', 'qty': '0.0002', 'quoteQty': '12.82766',
             'commission': '0.00513106', 'commissionAsset': 'USDT',
             'realizedPnl': '-0.0221', 'time': 3000, 'maker': False},
        ]
        journal._apply_trade_fills(state, trades[:2])

        class FakeAPI:
            async def get_account_trades(self, symbol, start_time=None):
                return trades, 200

        position = state.vi_the_hien_tai
        position.opened_at = time.time() - 60
        position.split_sl_enabled = True
        position.split_sl1_done = True
        position.hard_sl_algo_id = 3
        position.hard_sl_client_algo_id = 'sl2-exact'
        cycle = await reconcile._sync_terminal_exchange_fills(
            state, FakeAPI(), cycle_id, position
        )
        self.assertEqual(cycle['status'], 'CLOSED')
        self.assertEqual(cycle['exit_reason'], 'SL2_EXCHANGE_FILL')
        self.assertAlmostEqual(cycle['actual']['net_pnl_quote'], -0.10048095)
        self.assertTrue(cycle['actual']['integrity']['all_orders_quantity_reconciled'])


if __name__ == '__main__':
    unittest.main()
