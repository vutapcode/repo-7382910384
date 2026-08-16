import importlib.util
import json
import tempfile
import unittest
from collections import deque
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
    'dynamic_path_fee_tests',
    '2_suy_luan_mapping/tong_ket_chi_huy/dynamic_path_fee.py',
)
risk = load(
    'dynamic_path_risk_tests',
    '3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py',
)
journal = load(
    'dynamic_path_journal_tests',
    '3_thuc_thi/quan_ly_vi_the/nhat_ky_giao_dich.py',
)
executor = load('dynamic_path_executor_tests', '3_thuc_thi/dat_lenh.py')


def snapshot(**overrides):
    values = dict(
        snapshot_time=1000.0, atr_1m=40.0,
        poc=64032.0, vah=64128.0, val=63800.0,
        swing_high_m15=64256.0, swing_low_m15=63700.0,
        closed_m1_extrema=[
            {'timeframe': 'M1', 'high': 64128.0, 'low': 0.0},
        ],
        closed_m15_extrema=[],
        current_cvd_buy_3s=9.0, current_cvd_sell_3s=1.0,
        obi_top3=0.70, obi_top10=0.60, obi=0.50,
        price_progress_atr_3s=0.08, price_progress_coverage_3s=3.0,
        flow_price_trap={}, zone_acceptance_trap={},
        continuous_sweep_m1={'ts': 999.0, 'ttl': 120.0, 'quality': 0.90},
        continuous_breakout_m1={},
        continuous_footprint={'ts': 999.0, 'ttl': 15.0, 'quality': 0.80},
        continuous_persistent_flow={'ts': 999.0, 'ttl': 5.0, 'quality': 0.90},
        continuous_zone_reaction={'ts': 999.0, 'ttl': 15.0, 'quality': 0.90},
        continuous_absorption_reaction={},
        trend_m15='BULLISH',
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def signal(side='LONG', entry_style='PASSIVE_RETEST'):
    return {
        'bias': side, 'setup_kind': 'zone', 'entry_style': entry_style,
        'continuous_score': {
            'score': 90.0, 'confidence': 0.95, 'activation': 0.95,
            'sides': {
                side: {'score': 90.0, 'confidence': 0.95, 'activation': 0.95},
            },
        },
    }


class DynamicPathTests(unittest.TestCase):
    def test_meta_bootstrap_recovers_strong_trend_continuation_as_passive(self):
        miss_snapshot = snapshot(
            atr_1m=7.557,
            poc=0.0, vah=63612.7, val=0.0,
            swing_high_m15=63694.9, swing_low_m15=0.0,
            closed_m1_extrema=[
                {'timeframe': 'M1', 'high': value, 'low': 0.0}
                for value in (63617.6, 63618.7, 63621.1, 63629.2, 63660.5)
            ],
            closed_m15_extrema=[
                {'timeframe': 'M15', 'high': value, 'low': 0.0}
                for value in (63611.4, 63620.1, 63629.8, 63670.1, 63694.9)
            ],
            price_progress_atr_3s=0.31758,
        )
        strong = signal(entry_style=None)
        strong.update({'mode': 'TREND-PULLBACK'})
        strong['continuous_score'].update(
            score=76.258, confidence=0.7757, activation=0.7384,
        )
        strong['continuous_score']['sides']['LONG'].update(
            score=76.258, confidence=0.7757, activation=0.7384,
            impulse_conflict=0.0,
        )
        plan = path_engine.plan_exit(
            miss_snapshot, strong, 0.001, 63601.9, 63560.9, 0.1,
            filters={'step_size': 0.0001, 'min_qty': 0.0001, 'min_notional': 50.0},
        )
        projected = [
            item for item in plan['target_candidates']
            if 'CAUSAL_MEASURED_CONTINUATION' in item['sources']
        ]
        self.assertEqual(len(projected), 1)
        self.assertTrue(plan['available'])
        self.assertGreater(plan['realizable_edge_lcb'], 0.0)
        self.assertEqual(plan['entry_policy'], 'PASSIVE_RETEST_ONLY')
        self.assertEqual(plan['tp1_allocation'], 0.0)
        self.assertEqual(
            projected[0]['path_normalization_basis'], 'CAUSAL_MEASURED_LEG'
        )
        self.assertFalse(
            projected[0]['continuation_meta']['future_fields_used']
        )

    def test_meta_bootstrap_does_not_project_weak_or_misaligned_setup(self):
        weak = signal(entry_style=None)
        weak.update({'mode': 'TREND-PULLBACK'})
        weak['continuous_score'].update(
            score=71.9, confidence=0.90, activation=0.90,
        )
        weak['continuous_score']['sides']['LONG'].update(
            score=71.9, confidence=0.90, activation=0.90,
        )
        weak_candidates = path_engine.build_path_candidates(
            snapshot(), weak, 64000.0, 0.1,
        )
        self.assertNotIn('CAUSAL_MEASURED_CONTINUATION', {
            source for item in weak_candidates for source in item['sources']
        })
        misaligned = signal(entry_style=None)
        misaligned.update({'mode': 'TREND-PULLBACK'})
        candidates = path_engine.build_path_candidates(
            snapshot(trend_m15='BEARISH'), misaligned, 64000.0, 0.1,
        )
        self.assertNotIn('CAUSAL_MEASURED_CONTINUATION', {
            source for item in candidates for source in item['sources']
        })

    def test_candidates_are_favorable_deduped_and_never_atr_targets(self):
        candidates = path_engine.build_path_candidates(
            snapshot(), signal(), 64000.0, 0.1,
        )
        prices = [item['price'] for item in candidates]
        self.assertEqual(len(prices), len(set(prices)))
        self.assertTrue(all(price > 64000.0 for price in prices))
        self.assertNotIn('ATR', {
            source for item in candidates for source in item['sources']
        })
        target = next(item for item in candidates if item['price'] == 64128.0)
        self.assertIn('VAH', target['sources'])
        self.assertIn('M1_EXTREMUM', target['sources'])

    def test_reachability_decreases_as_path_gets_farther(self):
        plan = path_engine.plan_exit(
            snapshot(swing_high_m15=64128.0), signal(),
            0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        probabilities = [
            item['p_hit_before_stop'] for item in plan['target_candidates']
        ]
        self.assertEqual(probabilities, sorted(probabilities, reverse=True))

    def test_near_target_below_fees_gets_zero_allocation_but_runner_can_pass(self):
        moderate = signal()
        moderate['continuous_score'].update(
            score=60.0, confidence=0.60, activation=0.60,
        )
        moderate['continuous_score']['sides']['LONG'].update(
            score=60.0, confidence=0.60, activation=0.60,
        )
        plan = path_engine.plan_exit(
            snapshot(swing_high_m15=64128.0), moderate,
            0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        self.assertTrue(plan['available'])
        self.assertGreater(plan['net_edge_lcb'], 0.0)
        self.assertEqual(plan['tp1'], 64032.0)
        self.assertEqual(plan['tp1_allocation'], 0.0)
        self.assertGreater(plan['runner_target'], plan['tp1'])
        self.assertEqual(plan['entry_policy'], 'PASSIVE_RETEST_ONLY')
        self.assertEqual(plan['runner_dependency'], 1.0)
        self.assertLess(plan['local_path_lcb'], 0.0)
        self.assertGreater(plan['runner_dependency_buffer_bps'], 0.0)

    def test_far_intermediate_target_cannot_masquerade_as_local_confirmation(self):
        near = {
            'price': 63975.0, 'distance_bps': 4.0,
            'p_hit_lcb': 0.80, 'p_stop_ucb': 0.12,
            'stop_distance_bps': 10.0, 'net_edge_lcb': -4.0,
            'uncertainty': 0.10,
        }
        runner = {
            'price': 63616.0, 'distance_bps': 60.0,
            'p_hit_lcb': 0.60, 'p_stop_ucb': 0.15,
            'stop_distance_bps': 10.0, 'net_edge_lcb': 20.0,
            'uncertainty': 0.15,
        }
        best = {
            'tp1_allocation': 0.0, 'near': near, 'runner': runner,
            'quantity': 0.001, 'filters': {}, 'epistemic_buffer_bps': 0.0,
            'path_candidates': [
                near,
                {**near, 'distance_bps': 8.0, 'net_edge_lcb': -1.0},
                {**near, 'distance_bps': 10.0, 'net_edge_lcb': 0.0},
                # This was incorrectly selected by max() in the old code.
                {**near, 'distance_bps': 33.0, 'net_edge_lcb': 13.0},
                runner,
            ],
        }
        result = path_engine._realizable_plan(best, 7.0)
        self.assertLess(result['local_path_lcb'], 1.0)
        self.assertLess(result['local_path_confirmation'], 0.63)
        self.assertGreater(result['runner_dependency_buffer_bps'], 1.5)
        self.assertAlmostEqual(result['local_path_anchor_bps'], 9.0)

    def test_recent_confirmed_adverse_flow_reduces_runner_edge(self):
        clean = path_engine.plan_exit(
            snapshot(swing_high_m15=64128.0), signal(),
            0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        adverse_snapshot = snapshot(swing_high_m15=64128.0)
        adverse_snapshot.adverse_flow_memory_by_bias = {
            'LONG': {
                'ts': 998.3, 'blocked_bias': 'LONG', 'severity': 1.0,
            },
            'SHORT': {},
        }
        adverse = path_engine.plan_exit(
            adverse_snapshot, signal(),
            0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        self.assertLess(
            adverse['realizable_edge_lcb'], clean['realizable_edge_lcb']
        )
        self.assertGreater(
            adverse['target_candidates'][0]['context']['adverse_flow_memory'],
            0.75,
        )

    def test_future_adverse_memory_is_not_used_by_path_gate(self):
        snap = snapshot(swing_high_m15=64128.0)
        snap.adverse_flow_memory_by_bias = {
            'LONG': {
                'ts': snap.snapshot_time + 1.0,
                'blocked_bias': 'LONG', 'severity': 1.0,
            },
            'SHORT': {},
        }
        plan = path_engine.plan_exit(
            snap, signal(), 0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        self.assertEqual(
            plan['target_candidates'][0]['context']['adverse_flow_memory'], 0.0
        )

    def test_adverse_flow_cannot_improve_reachability(self):
        supportive = path_engine.plan_exit(
            snapshot(), signal(), 0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        adverse = path_engine.plan_exit(
            snapshot(
                current_cvd_buy_3s=1.0, current_cvd_sell_3s=9.0,
                obi_top3=-0.70, obi_top10=-0.60, obi=-0.50,
                price_progress_atr_3s=-0.08,
                flow_price_trap={'active': True, 'blocked_bias': 'LONG'},
            ),
            signal(), 0.010, 64000.0, 63959.0, 0.1,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        support_p = supportive['target_candidates'][1]['p_hit_before_stop']
        adverse_p = adverse['target_candidates'][1]['p_hit_before_stop']
        self.assertLess(adverse_p, support_p)

    def test_tp1_below_minimum_becomes_zero_not_full_close(self):
        result = risk.calculate_tp1_close_qty(
            current_qty=0.001, initial_qty=0.001, allocation=0.70,
            current_price=64000.0,
            filters={'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
        )
        self.assertFalse(result['executable'])
        self.assertEqual(result['quantity'], 0.0)

    def test_venue_sign_flip_is_excluded_from_calibration(self):
        state = SimpleNamespace(journal_events=deque(maxlen=20))
        cycle = {
            'position_cycle_id': 'pc-venue',
            'strategy_mainnet': {
                'net_pnl_bps': 5.0, 'valid_for_calibration': True,
            },
            'execution_testnet': {},
            'actual': {
                'net_pnl_bps': -3.0,
                'cross_venue_entry_gap_bps': 4.0,
                'cross_venue_exit_gap_bps': 4.0,
            },
            'venue_attribution': {'classification': 'PENDING'},
        }
        journal._update_venue_attribution(state, cycle)
        self.assertEqual(
            cycle['venue_attribution']['classification'], 'VENUE_ARTIFACT'
        )
        self.assertTrue(cycle['venue_attribution']['exclude_from_calibration'])
        self.assertFalse(cycle['strategy_mainnet']['valid_for_calibration'])

    def test_executor_bundle_uses_one_dynamic_plan_for_levels_and_economics(self):
        state = snapshot(
            best_bid=63999.9, best_ask=64000.0,
            bids_top_10=[[63999.9, 2.0]], asks_top_10=[[64000.0, 2.0]],
            exchange_filters={
                'tick_size': 0.1, 'step_size': 0.001,
                'min_qty': 0.001, 'min_notional': 5.0,
            },
            sweep_m1={}, klines_m1=[], klines_m15=[],
            active_setups={'zone': {
                'setup_id': 'dynamic-setup', 'generation': 1,
                'zone': 64000.0, 'kind': 'zone',
            }},
        )
        sig = signal()
        sig.update({
            'setup_id': 'dynamic-setup', 'setup_generation': 1,
            'mode': 'NEUTRAL-FADE', 'setup_zone': 64000.0,
            'decision_poc': 64032.0, 'decision_vah': 64128.0,
            'decision_val': 63800.0, 'score_poc_modifier': 0.0,
        })
        entry, price, levels, _, plan, economic = executor._dynamic_bundle(
            state, sig, 0.010, 64000.0, 0.1,
        )
        self.assertTrue(entry['available'])
        self.assertEqual(price, 64000.0)
        self.assertTrue(plan['available'])
        self.assertEqual(levels['soft_tp1'], plan['tp1'])
        self.assertEqual(levels['soft_tp2'], plan['runner_target'])
        self.assertEqual(economic['mode'], 'DYNAMIC_PATH_ENFORCED')
        self.assertEqual(economic['net_edge_lcb'], plan['net_edge_lcb'])

    def test_mainnet_full_exit_bundle_never_selects_split_sl(self):
        state = snapshot(
            best_bid=63999.9, best_ask=64000.0,
            bids_top_10=[[63999.9, 2.0]], asks_top_10=[[64000.0, 2.0]],
            exchange_filters={
                'tick_size': 0.1, 'step_size': 0.001,
                'min_qty': 0.001, 'min_notional': 5.0,
            },
            sweep_m1={}, klines_m1=[], klines_m15=[],
            active_setups={'zone': {
                'setup_id': 'mainnet-full-exit', 'generation': 1,
                'zone': 64000.0, 'kind': 'zone',
            }},
        )
        sig = signal()
        sig.update({
            'setup_id': 'mainnet-full-exit', 'setup_generation': 1,
            'mode': 'NEUTRAL-FADE', 'setup_zone': 64000.0,
            'decision_poc': 64032.0, 'decision_vah': 64128.0,
            'decision_val': 63800.0, 'score_poc_modifier': 0.7,
        })
        _, _, levels, split, plan, _ = executor._dynamic_bundle(
            state, sig, 0.001, 64000.0, 0.1, allow_split_sl=False,
        )
        self.assertFalse(split['enabled'])
        self.assertEqual(split['version'], 'MAINNET_FULL_EXIT_V1')
        self.assertNotIn('standard_hard_sl', levels)
        self.assertEqual(plan['tp1_allocation'], 0.0)

    def test_mainnet_actual_fill_is_strategy_truth_not_testnet_artifact(self):
        state = SimpleNamespace(journal_events=deque(maxlen=10))
        cycle = {
            'position_cycle_id': 'pc-mainnet',
            'execution_venue': 'BINANCE_FUTURES_MAINNET',
            'strategy_mainnet': {'status': 'PENDING'},
            'execution_mainnet': {'status': 'PENDING'},
            'actual': {'net_pnl_bps': 7.25},
        }
        journal._update_venue_attribution(state, cycle)
        self.assertNotIn('execution_testnet', cycle)
        self.assertEqual(cycle['execution_mainnet']['net_pnl_bps'], 7.25)
        self.assertEqual(cycle['strategy_mainnet']['net_pnl_bps'], 7.25)
        self.assertEqual(
            cycle['venue_attribution']['classification'], 'LIVE_MAINNET'
        )
        self.assertFalse(cycle['venue_attribution']['exclude_from_calibration'])

    def test_minimal_mainnet_active_audit_prunes_rows_older_than_24h(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_text(
                json.dumps({'ts': 100.0, 'event': 'OLD'}) + '\n'
                + json.dumps({'ts': 190.0, 'event': 'FRESH'}) + '\n',
                encoding='utf-8',
            )
            with mock.patch.object(journal, 'EVENT_PATH', path), mock.patch.object(
                journal, 'JOURNAL_EVENT_RETENTION_SECONDS', 50.0
            ):
                journal._prune_active_event_log(now=200.0)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['FRESH'])

    def test_minimal_mainnet_drain_drops_direct_decision_chatter(self):
        self.assertFalse(journal._keep_minimal_mainnet_event({
            'event': 'DECISION_EVALUATED', 'position_cycle_id': None,
        }))
        self.assertFalse(journal._keep_minimal_mainnet_event({
            'event': 'RADAR_WATCH', 'position_cycle_id': None,
        }))
        self.assertTrue(journal._keep_minimal_mainnet_event({
            'event': 'ACTUAL_FILL_SYNCED', 'position_cycle_id': 'pc-live-1',
        }))
        self.assertTrue(journal._keep_minimal_mainnet_event({
            'event': 'MAINNET_BREAKER', 'position_cycle_id': None,
        }))
        self.assertFalse(journal._keep_minimal_mainnet_event(None))

    def test_minimal_mainnet_compacts_decision_forensics(self):
        raw = {
            'ts': 123.0,
            'event': 'DECISION_EVALUATED',
            'run_id': 'run-1',
            'position_cycle_id': None,
            'payload': {
                'setup_id': 'setup-1',
                'opportunity_id': 'opp-1',
                'generation': 3,
                'mode': 'TRANSITION-BREAKOUT',
                'bias': 'SHORT',
                'kind': 'breakout',
                'entry_style': 'PASSIVE_RETEST',
                'result': 'CONTINUOUS_V2_POWER_OR_DIRECTION_BELOW_ENTRY',
                'score': {
                    'version': 'CONTINUOUS_V2',
                    'score': 71.0,
                    'confidence': 0.8,
                    'activation': 0.7,
                    'trade_power': 39.76,
                    'activation_floor': 40.0,
                    'activated': False,
                    'selected_bias': 'SHORT',
                    'target_notional_pct': 1.2,
                    'large_blob': {'must_not_survive': list(range(100))},
                },
                'context': {
                    'best_bid': 63360.0,
                    'best_ask': 63360.1,
                    'atr_1m': 42.0,
                    'depth_top10': list(range(100)),
                },
            },
        }
        compact = journal._compact_minimal_mainnet_event(raw)
        self.assertEqual(compact['event'], 'DECISION_AUDIT')
        self.assertTrue(journal._keep_minimal_mainnet_event(compact))
        self.assertEqual(compact['payload']['opportunity_id'], 'opp-1')
        self.assertEqual(compact['payload']['trade_power'], 39.76)
        self.assertNotIn('large_blob', compact['payload'])
        self.assertNotIn('depth_top10', compact['payload'])


if __name__ == '__main__':
    unittest.main()
