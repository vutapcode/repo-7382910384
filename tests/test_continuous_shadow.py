import asyncio
import copy
import importlib.util
import os
import time
import unittest
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scorer = load(
    'continuous_shadow_scorer_tests',
    '2_suy_luan_mapping/tong_ket_chi_huy/cham_diem_continuous.py',
)
footprint = load(
    'continuous_shadow_footprint_tests',
    '2_suy_luan_mapping/map_dong_tien/footprint.py',
)
economic = load(
    'continuous_shadow_economic_tests',
    '2_suy_luan_mapping/tong_ket_chi_huy/kinh_te_lenh.py',
)
risk = load(
    'continuous_shadow_risk_tests',
    '3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py',
)
journal = load(
    'continuous_shadow_journal_tests',
    '3_thuc_thi/quan_ly_vi_the/nhat_ky_giao_dich.py',
)
radar = load(
    'continuous_shadow_radar_tests',
    '2_suy_luan_mapping/map_gia_tick.py',
)
commander = load(
    'continuous_shadow_commander_tests',
    '2_suy_luan_mapping/tong_ket_chi_huy/chi_huy_truong.py',
)
executor = load(
    'continuous_shadow_executor_tests', '3_thuc_thi/dat_lenh.py',
)


NOW = 1000.0


def evidence(direction, strength, family, quality=0.9, ts=NOW, **extra):
    value = {
        'active': True, 'direction': direction, 'strength': strength,
        'quality': quality, 'ts': ts, 'ttl': 20.0,
        'source_event_id': f'{family}:{direction}:{strength}',
        'source_family': family, 'dependency_families': [family],
    }
    value.update(extra)
    return value


def snapshot(strong=True):
    strength = 0.9 if strong else 0.1
    return SimpleNamespace(
        snapshot_time=NOW, snapshot_mono=10.0,
        best_bid=99.9, best_ask=100.1, atr_1m=10.0,
        current_cvd_buy_3s=8.0 if strong else 0.0,
        current_cvd_sell_3s=2.0 if strong else 0.0,
        vol_pct90=5.0, obi=0.4 if strong else 0.0,
        obi_top3=0.5 if strong else 0.0,
        obi_top10=0.4 if strong else 0.0,
        obi_history=[(NOW - 1 + i * 0.1, 0.4) for i in range(10)],
        price_progress_atr_3s=0.05 if strong else 0.0,
        poc=105.0, flow_price_trap={}, zone_acceptance_trap={},
        continuous_m15=evidence(1.0, strength, 'M15_CLOSED', ttl=3600.0),
        continuous_sweep_m1=evidence(
            1.0, strength, 'PRICE_REACTION',
            zone_precision=0.9, reclaim_distance_atr=0.2,
        ) if strong else {},
        continuous_breakout_m1={},
        continuous_footprint=evidence(1.0, strength, 'AGGTRADE') if strong else {},
        continuous_persistent_flow=evidence(1.0, strength, 'AGGTRADE') if strong else {},
        continuous_zone_reaction=evidence(1.0, strength, 'PRICE_REACTION') if strong else {},
        continuous_flow_divergence={},
        continuous_absorption_reaction={},
        continuous_value_area_sweep={},
        bids_top_10=[[99.9, 100.0]], asks_top_10=[[100.1, 100.0]],
        balance_usdt=100000.0,
        exchange_filters={
            'tick_size': 0.1, 'step_size': 0.001,
            'min_qty': 0.001, 'min_notional': 5.0,
        },
        vah=110.0, val=90.0, swing_high_m15=120.0,
        swing_low_m15=80.0, sweep_m1={},
    )


def setup():
    return {
        'setup_id': 'setup-1', 'generation': 1,
        'semantic_key': 'opportunity-1', 'opportunity_id': None,
        'mode': 'TREND-PULLBACK', 'bias': 'LONG', 'zone': 100.0,
        'zone_id': 'POC', 'kind': 'zone',
        'activation_reason': 'RETEST_CONFIRMED',
    }


class ContinuousScorerTests(unittest.TestCase):
    def test_aligned_evidence_is_continuous_and_directional(self):
        result = scorer.score_continuous(snapshot(), setup(), {})
        self.assertEqual(result['version'], 'CONTINUOUS_SHADOW_V1')
        self.assertTrue(result['activated'])
        self.assertGreater(result['score'], 85.0)
        self.assertGreater(result['trade_power'], result['activation_floor'])
        self.assertGreater(result['sides']['LONG']['score'], result['sides']['SHORT']['score'])
        self.assertLessEqual(result['target_notional_pct'], 9.0)
        self.assertEqual(
            result['allocation_unit'], 'TARGET_NOTIONAL_PCT_OF_EQUITY'
        )

    def test_context_without_activation_cannot_request_economics(self):
        source = snapshot(False)
        source.continuous_m15 = evidence(1.0, 0.9, 'M15_CLOSED', ttl=3600.0)
        result = scorer.score_continuous(source, setup(), {})
        self.assertEqual(result['activation'], 0.0)
        self.assertFalse(result['activated'])
        self.assertEqual(result['target_notional_pct'], 0.0)

    def test_future_event_is_rejected_without_mutating_inputs(self):
        source = snapshot(False)
        source.continuous_sweep_m1 = evidence(
            1.0, 1.0, 'PRICE_REACTION', ts=NOW + 10.0,
            zone_precision=1.0, reclaim_distance_atr=0.3,
        )
        frozen_source = copy.deepcopy(source.__dict__)
        frozen_setup = copy.deepcopy(setup())
        result = scorer.score_continuous(source, frozen_setup, {})
        self.assertIn('FUTURE_TIMESTAMP_REJECTED', result['evidence_quality_flags'])
        sweep_effect = next(
            item for item in result['sides']['LONG']['evidence_effects']
            if item['name'] == 'SWEEP_M1'
        )
        self.assertEqual(sweep_effect['freshness'], 0.0)
        self.assertEqual(source.__dict__, frozen_source)
        self.assertEqual(frozen_setup, setup())

    def test_same_aggtrade_family_is_downweighted(self):
        result = scorer.score_continuous(snapshot(), setup(), {})
        effects = {
            item['name']: item
            for item in result['sides']['LONG']['evidence_effects']
        }
        self.assertLess(effects['FOOTPRINT']['independence'], 1.0)
        self.assertEqual(
            effects['FOOTPRINT']['independence'],
            effects['PERSISTENT_FLOW']['independence'],
        )

    def test_live_version_uses_separate_hysteresis(self):
        source = snapshot()
        item = setup()
        result = scorer.score_continuous(source, item, {}, live=True)
        self.assertEqual(result['version'], 'CONTINUOUS_V1')
        self.assertTrue(result['live_authority'])
        ready, reason = scorer.entry_ready(item, result, 10.0)
        self.assertFalse(ready)
        self.assertTrue(reason.startswith('CONTINUOUS_PERSISTENCE_WAIT_'))
        ready, reason = scorer.entry_ready(item, result, 12.0)
        self.assertTrue(ready)
        self.assertEqual(reason, 'CONTINUOUS_PERSISTENCE_PASS')

    def test_adaptive_floor_has_no_old_spread_or_atr_cliff(self):
        left = snapshot()
        right = snapshot()
        left.best_bid, left.best_ask = 99.99, 100.01
        right.best_bid, right.best_ask = 99.9899, 100.0101
        floor_left = scorer.score_continuous(left, setup(), {})['activation_floor']
        floor_right = scorer.score_continuous(right, setup(), {})['activation_floor']
        self.assertLess(abs(floor_right - floor_left), 0.10)

        left.atr_1m = 0.0199
        right.atr_1m = 0.0201
        floor_left = scorer.score_continuous(left, setup(), {})['activation_floor']
        floor_right = scorer.score_continuous(right, setup(), {})['activation_floor']
        self.assertLess(abs(floor_right - floor_left), 0.10)

    def test_marginal_power_gets_small_continuous_notional(self):
        at_floor = scorer._target_notional_pct(33.0, 33.0)
        slightly_above = scorer._target_notional_pct(38.0, 33.0)
        self.assertAlmostEqual(at_floor, 0.30)
        self.assertGreater(slightly_above, at_floor)
        self.assertLess(slightly_above, 1.0)
        self.assertGreater(
            scorer._persistence_required_ms(33.0, 33.0, 0.8, 0.75),
            scorer._persistence_required_ms(75.0, 33.0, 0.9, 0.9),
        )


class ContinuousProducerTests(unittest.TestCase):
    def test_footprint_strength_updates_without_live_rescore(self):
        state = SimpleNamespace(
            fp_current_candle={'open_time': 60_000, 'rows': defaultdict(lambda: [0.0, 0.0])},
            fp_last_imbalance={'dir': None, 'ts': 0.0, 'used': False},
            continuous_footprint={}, decision_revision=0,
            continuous_evidence_revision=0,
        )
        rows = state.fp_current_candle['rows']
        for tick in (20, 21, 22):
            rows[tick][0] = 1.0
            rows[tick - 1][1] = 0.1
        stacks = footprint._tinh_stacked_imbalances(rows)
        footprint._publish_strongest_stack(state, stacks, 105.0, NOW, True)
        live_revision = state.decision_revision
        continuous_revision = state.continuous_evidence_revision
        first_strength = state.continuous_footprint['strength']

        for tick in (20, 21, 22):
            rows[tick][0] = 3.0
        stacks = footprint._tinh_stacked_imbalances(rows)
        footprint._publish_strongest_stack(state, stacks, 105.0, NOW + 0.1, True)
        self.assertEqual(state.decision_revision, live_revision)
        self.assertGreater(state.continuous_evidence_revision, continuous_revision)
        self.assertGreater(state.continuous_footprint['strength'], first_strength)


class ContinuousEconomicsTests(unittest.TestCase):
    def test_venue_minimum_never_upsizes_probe(self):
        result = risk.quantity_feasibility(
            4888.6337, 0.77, 64000.0,
            {'step_size': 0.0001, 'min_qty': 0.0001, 'min_notional': 50.0},
        )
        self.assertFalse(result['executable'])
        self.assertEqual(result['quantity'], 0.0)
        self.assertAlmostEqual(
            result['minimum_executable_notional_pct'], 1.0473, places=3
        )

    def test_snapshot_economics_matches_live_wrapper(self):
        source = snapshot()
        direct = economic.observe_snapshot(
            'LONG', 1.0, 110.0, source.bids_top_10, source.asks_top_10,
            source.best_bid, source.best_ask,
        )
        wrapped = economic.observe(source, 'LONG', 1.0, 110.0)
        self.assertEqual(direct, wrapped)

    def test_risk_sweep_freshness_uses_evaluation_time(self):
        source = snapshot()
        source.sweep_m1 = {'ts': 900.0, 'direction': 'LONG', 'level': 20.0}
        # Hàm chấp nhận clock replay mà không đọc wall clock hiện tại.
        levels = risk.calculate_levels(
            source, 100.0, 'LONG', 0.1, 'TREND-PULLBACK',
            setup_zone=100.0, setup_kind='zone', evaluation_time=950.0,
        )
        self.assertIn('soft_tp1', levels)

    def test_background_economics_has_no_live_authority(self):
        source = snapshot()
        data = radar._shadow_economic_input(
            source, setup(), scorer.score_continuous(source, setup(), {}),
        )
        result = journal._evaluate_continuous_shadow_economics(data)
        self.assertTrue(result['available'])
        self.assertFalse(result['live_authority'])
        self.assertIn('economic_pass', result)


class ContinuousLifecycleTests(unittest.TestCase):
    def test_snapshot_registry_keeps_terminal_but_bounds_history(self):
        source = {
            'opp': {
                'terminal': {'state': 'EXPIRED'},
                'history': [{'ts': float(index)} for index in range(100)],
            },
        }
        compact = journal._compact_shadow_registry(source)
        self.assertEqual(compact['opp']['terminal']['state'], 'EXPIRED')
        self.assertEqual(len(compact['opp']['history']), 3)
        self.assertEqual(compact['opp']['history_count'], 100)

    def test_shadow_record_does_not_mutate_live_setup_or_queue(self):
        source = snapshot()
        item = setup()
        before = copy.deepcopy(item)
        result = scorer.score_continuous(source, item, {})
        state = SimpleNamespace(
            continuous_shadow_events=deque(maxlen=20),
            continuous_shadow_registry={}, continuous_shadow_drop_count=0,
        )
        radar._record_continuous_shadow_score(
            state, item, source, result, NOW, 10.0,
        )
        self.assertEqual(item, before)
        self.assertEqual(len(state.continuous_shadow_events), 1)
        self.assertNotIn('last_score', item)
        self.assertEqual(len(getattr(state, 'journal_events', ())), 0)

    def test_terminal_links_future_labels_by_opportunity(self):
        source = snapshot()
        item = setup()
        state = SimpleNamespace(
            continuous_shadow_events=deque(maxlen=20),
            continuous_shadow_registry={}, continuous_shadow_drop_count=0,
            continuous_shadow_schedule={'opportunity-1': {}},
        )
        result = scorer.score_continuous(source, item, {})
        radar._record_continuous_shadow_score(
            state, item, source, result, NOW, 10.0,
        )
        radar._finalize_continuous_shadow(
            state, item, {
                'setup_id': 'setup-1', 'terminal_state': 'EXPIRED',
                'reason': 'TTL',
            }, NOW + 60.0,
        )
        terminal = state.continuous_shadow_registry['opportunity-1']['terminal']
        self.assertEqual(terminal['state'], 'EXPIRED')
        self.assertNotIn('mfe_bps', result)
        self.assertNotIn('opportunity-1', state.continuous_shadow_schedule)

    def test_commander_continuous_claims_without_old_reference(self):
        now = time.time()
        source = snapshot()
        source.snapshot_time = now
        source.snapshot_mono = time.monotonic()
        source.best_bid = 99.999
        source.best_ask = 100.001
        for field in (
            'continuous_m15', 'continuous_sweep_m1', 'continuous_footprint',
            'continuous_persistent_flow', 'continuous_zone_reaction',
        ):
            getattr(source, field)['ts'] = now
        item = setup()
        item.update({
            'state': 'WATCH', 'score_count': 0, 'evaluation_count': 0,
            'core_reject_count': 0, 'veto_count': 0, 'max_core': 0,
            'max_shark': 0, 'best_score': None,
            'seen_score_details': [],
        })
        live_score = scorer.score_continuous(source, item, {}, live=True)
        item['continuous_eligible_since_mono'] = time.monotonic() - 2.0
        state = SimpleNamespace(
            system_ready=True, trading_enabled=True, co_lenh_mo=False,
            execution_in_flight=False, last_execution_release_mono=0.0,
            setup_cooldowns={}, journal_events=deque(maxlen=100),
            code_version='test', strategy_config_version='test',
            hang_doi_tin_hieu=asyncio.Queue(maxsize=2),
            execution_setup_id=None, execution_generation=0,
            execution_client_order_id=None, execution_unknown=False,
        )
        with mock.patch.dict(os.environ, {'SMC_SCORER_VERSION': 'CONTINUOUS_V1'}):
            signal = commander.phan_tich_va_ra_lenh(
                state, {'modes': ['TREND-PULLBACK']},
                'TREND-PULLBACK', 'LONG', setup=item,
                decision_snapshot=source, continuous_score=live_score,
            )
        self.assertIsNotNone(signal)
        self.assertEqual(signal['score_version'], 'CONTINUOUS_V1')
        self.assertTrue(signal['continuous_score']['live_authority'])
        self.assertNotIn('score_v2', signal)
        self.assertNotIn('reference_score_v2', signal)
        self.assertLessEqual(signal['size_pct'], 9.0)
        self.assertEqual(item['state'], 'EXECUTING')

    def test_commander_keeps_unexecutable_probe_in_watch(self):
        now = time.time()
        source = snapshot()
        source.snapshot_time = now
        source.snapshot_mono = time.monotonic()
        source.balance_usdt = 1000.0
        source.best_bid, source.best_ask = 99.999, 100.001
        source.exchange_filters = {
            'tick_size': 0.1, 'step_size': 0.001,
            'min_qty': 0.001, 'min_notional': 50.0,
        }
        for field in (
            'continuous_m15', 'continuous_sweep_m1', 'continuous_footprint',
            'continuous_persistent_flow', 'continuous_zone_reaction',
        ):
            getattr(source, field)['ts'] = now
        item = setup()
        item.update({
            'state': 'WATCH', 'score_count': 0, 'evaluation_count': 0,
            'core_reject_count': 0, 'veto_count': 0, 'max_core': 0,
            'max_shark': 0, 'best_score': None, 'seen_score_details': [],
            'continuous_eligible_since_mono': time.monotonic() - 2.0,
        })
        live_score = scorer.score_continuous(source, item, {}, live=True)
        live_score['target_notional_pct'] = 0.5
        state = SimpleNamespace(
            system_ready=True, trading_enabled=True, co_lenh_mo=False,
            execution_in_flight=False, last_execution_release_mono=0.0,
            setup_cooldowns={}, journal_events=deque(maxlen=100),
            code_version='test', strategy_config_version='test',
            hang_doi_tin_hieu=asyncio.Queue(maxsize=2),
        )
        with mock.patch.dict(os.environ, {'SMC_SCORER_VERSION': 'CONTINUOUS_V1'}):
            signal = commander.phan_tich_va_ra_lenh(
                state, {'modes': ['TREND-PULLBACK']},
                'TREND-PULLBACK', 'LONG', setup=item,
                decision_snapshot=source, continuous_score=live_score,
            )
        self.assertIsNone(signal)
        self.assertEqual(item['state'], 'WATCH')
        self.assertEqual(
            state.journal_events[-1]['payload']['result'],
            'CONTINUOUS_VENUE_MIN_WAIT',
        )

    def test_commander_mainnet_claims_exact_fixed_lot_then_defers_edge(self):
        now = time.time()
        source = snapshot()
        source.snapshot_time = now
        source.snapshot_mono = time.monotonic()
        for field in (
            'continuous_m15', 'continuous_sweep_m1', 'continuous_footprint',
            'continuous_persistent_flow', 'continuous_zone_reaction',
        ):
            getattr(source, field)['ts'] = now
        item = setup()
        item.update({
            'state': 'WATCH', 'score_count': 0, 'evaluation_count': 0,
            'core_reject_count': 0, 'veto_count': 0, 'max_core': 0,
            'max_shark': 0, 'best_score': None, 'seen_score_details': [],
            'continuous_eligible_since_mono': time.monotonic() - 2.0,
        })
        live_score = scorer.score_continuous(source, item, {}, live=True)
        live_score['target_notional_pct'] = 0.5
        # The scorer percentage is deliberately too small for Binance's lot.
        # Mainnet must instead preflight the exact configured 0.001 BTC, while
        # leaving stop-risk and fee-edge authority to the Executor.
        source.balance_usdt = 5.519
        source.best_bid, source.best_ask = 63359.9, 63360.0
        source.exchange_filters = {
            'tick_size': 0.1, 'step_size': 0.001,
            'min_qty': 0.001, 'max_qty': 1000.0,
            'min_notional': 50.0,
        }
        state = SimpleNamespace(
            system_ready=True, trading_enabled=True, co_lenh_mo=False,
            execution_in_flight=False, last_execution_release_mono=0.0,
            setup_cooldowns={}, journal_events=deque(maxlen=100),
            code_version='test', strategy_config_version='test',
            hang_doi_tin_hieu=asyncio.Queue(maxsize=2),
            execution_setup_id=None, execution_generation=0,
            execution_client_order_id=None, execution_unknown=False,
        )
        environment = {
            'SMC_SCORER_VERSION': 'CONTINUOUS_V1',
            'SMC_EXECUTION_VENUE': 'MAINNET',
            'SMC_MAINNET_ARMED': 'true',
            'SMC_MAINNET_EXCLUSIVE_ACCOUNT': 'true',
            'SMC_FIXED_QTY_BTC': '0.001',
            'SMC_LEVERAGE': '20', 'SMC_MARGIN_TYPE': 'ISOLATED',
            'SMC_MAINNET_MARGIN_RESERVE_USDT': '0.50',
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            signal = commander.phan_tich_va_ra_lenh(
                state, {'modes': ['TREND-PULLBACK']},
                'TREND-PULLBACK', 'LONG', setup=item,
                decision_snapshot=source, continuous_score=live_score,
            )
        self.assertIsNotNone(signal)
        policy = signal['size_policy']
        self.assertEqual(policy['allocation_unit'], 'FIXED_BASE_ASSET_QTY')
        self.assertEqual(policy['fixed_qty_btc'], 0.001)
        self.assertTrue(
            policy['venue_size_feasibility'][
                'risk_and_economics_pending_executor'
            ]
        )
        self.assertEqual(item['state'], 'EXECUTING')

    def test_continuous_small_size_keeps_six_bps_edge_buffer(self):
        evaluation = executor.continuous_economic_evaluation({
            'score_version': 'CONTINUOUS_V1', 'size_pct': 1.5,
            'size_policy': {'tier': 'WATCH'},
        }, {
            'structural_fee_floor_pass': True,
            'expected_net_edge_bps': 5.5,
        })
        self.assertTrue(evaluation['applies'])
        self.assertFalse(evaluation['qualified'])
        self.assertEqual(evaluation['minimum_expected_net_edge_bps'], 6.0)


if __name__ == '__main__':
    unittest.main()
