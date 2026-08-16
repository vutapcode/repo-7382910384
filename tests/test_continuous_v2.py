import asyncio
import importlib.util
import time
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


scorer = load(
    'continuous_v2_scorer',
    '2_suy_luan_mapping/tong_ket_chi_huy/cham_diem_continuous_v2.py',
)
snapshot_mod = load(
    'continuous_v2_snapshot',
    '2_suy_luan_mapping/tong_ket_chi_huy/decision_snapshot.py',
)
radar = load('continuous_v2_radar', '2_suy_luan_mapping/map_gia_tick.py')
executor = load('continuous_v2_executor', '3_thuc_thi/dat_lenh.py')


FIELDS = (
    'continuous_m15', 'continuous_sweep_m1', 'continuous_breakout_m1',
    'continuous_footprint', 'continuous_persistent_flow',
    'continuous_zone_reaction', 'continuous_flow_divergence',
    'continuous_absorption_reaction', 'continuous_value_area_sweep',
)


def event(direction=0.0, strength=0.0, quality=0.0, ts=1000.0,
          source='none', dependencies=(), parent=None, ttl=20.0):
    return {
        'active': bool(strength), 'direction': direction,
        'strength': strength, 'quality': quality, 'ts': ts, 'ttl': ttl,
        'source_event_id': source, 'parent_event_id': parent,
        'dependency_families': list(dependencies),
    }


def momentum_rows(direction=1.0, acceptance=0.90):
    progress = {15: 0.35, 60: 0.80, 180: 1.30}
    return {
        str(horizon): {
            'price_progress_atr': direction * progress[horizon],
            'range_expansion_atr': progress[horizon],
            'price_efficiency': 0.90,
            'price_coverage_seconds': horizon,
            'flow_imbalance': direction * 0.75,
            'flow_total': 100.0 * horizon / 15.0,
            'flow_coverage_seconds': horizon,
            'acceptance_long': acceptance if direction > 0 else 0.0,
            'acceptance_short': acceptance if direction < 0 else 0.0,
        }
        for horizon in (15, 60, 180)
    }


def decision(direction=1.0, price=64000.0, atr=20.0, acceptance=0.90):
    values = {
        'snapshot_time': 1000.0, 'best_bid': price - 0.05,
        'best_ask': price + 0.05, 'atr_1m': atr,
        'poc': price - direction * atr, 'vah': price,
        'val': price, 'vol_pct90': 5.0,
        'current_cvd_buy_3s': 9.0 if direction > 0 else 1.0,
        'current_cvd_sell_3s': 1.0 if direction > 0 else 9.0,
        'momentum_horizons': momentum_rows(direction, acceptance),
    }
    for field in FIELDS:
        values[field] = event()
    return SimpleNamespace(**values)


def setup(side='LONG', mode='NEUTRAL-MOMENTUM', zone=64000.0):
    return {
        'setup_id': 'v2-setup', 'generation': 1,
        'semantic_key': 'v2-opportunity', 'mode': mode,
        'bias': side, 'zone': zone, 'kind': 'zone',
    }


class ContinuousV2ScorerTests(unittest.TestCase):
    def test_snapshot_uses_only_rows_available_at_decision_time(self):
        state = SimpleNamespace(
            trend_price_history=[
                {'ts': 990.0, 'price': 100.0},
                {'ts': 1000.0, 'price': 101.0},
                {'ts': 1001.0, 'price': 999.0},
            ],
            flow_1s_buffer=[
                {'ts': 990, 'buy': 1.0, 'sell': 0.0},
                {'ts': 1001, 'buy': 1000.0, 'sell': 0.0},
            ],
            best_bid=100.9, best_ask=101.1, atr_1m=1.0,
            vah=101.0, val=99.0, klines_m1=[], klines_m15=[],
        )
        snap = snapshot_mod.capture(state, wall_time=1000.0, monotonic_time=50.0)
        self.assertAlmostEqual(
            snap.momentum_horizons['15']['price_progress_atr'], 1.0
        )
        self.assertEqual(snap.momentum_horizons['15']['flow_buy'], 1.0)

    def test_location_alone_cannot_activate(self):
        snap = decision(direction=0.0)
        result = scorer.score_continuous(snap, setup(), {})
        self.assertEqual(result['activation'], 0.0)
        self.assertFalse(result['activated'])
        self.assertEqual(result['target_notional_pct'], 0.0)

    def test_confirmed_flash_memory_decays_continuously_without_hard_cooldown(self):
        snap = decision(direction=1.0)
        item = setup()
        baseline = scorer.score_continuous(snap, item, {})
        self.assertGreater(baseline['trade_power'], baseline['activation_floor'])

        powers = []
        for age in (1.7, 5.0, 10.0, 20.0):
            snap.adverse_flow_memory_by_bias = {
                'LONG': {
                    'ts': snap.snapshot_time - age,
                    'blocked_bias': 'LONG', 'severity': 1.0,
                    'source_event_id': 'flash-loss-case',
                    'contract_version': 'FLASH_ADVERSE_MEMORY_V1',
                },
                'SHORT': {},
            }
            result = scorer.score_continuous(snap, item, {})
            powers.append(result['trade_power'])
            if age == 1.7:
                self.assertLess(
                    result['trade_power'], result['activation_floor']
                )
                self.assertGreater(
                    result['recent_adverse_flow_memory']['active_strength'], 0.75
                )
        self.assertEqual(powers, sorted(powers))
        self.assertLess(powers[-1], baseline['trade_power'])

    def test_same_source_reversal_is_one_causal_component(self):
        snap = decision(direction=0.0)
        for field, name in (
            ('continuous_sweep_m1', 'sweep'),
            ('continuous_zone_reaction', 'zone'),
            ('continuous_value_area_sweep', 'va'),
        ):
            setattr(snap, field, event(
                1.0, 1.0, 1.0, source=name,
                dependencies=('PRICE_REACTION',), parent='price-parent',
            ))
        result = scorer.score_continuous(
            snap, setup(mode='NEUTRAL-FADE'), {}
        )
        components = result['causal_components']
        reaction = next(row for row in components if 'SWEEP_M1' in row['members'])
        self.assertEqual(
            set(reaction['members']),
            {'SWEEP_M1', 'ZONE_REACTION', 'VALUE_AREA_SWEEP'},
        )
        # Strongest direct evidence plus at most 20%, never three full votes.
        self.assertLessEqual(reaction['support'], 1.32 + 1e-9)

    def test_interactions_require_disjoint_dependency_families_and_are_capped(self):
        snap = decision(direction=0.0)
        snap.continuous_sweep_m1 = event(
            1.0, 1.0, 1.0, source='reaction',
            dependencies=('PRICE_REACTION',),
        )
        snap.continuous_footprint = event(
            1.0, 1.0, 1.0, source='footprint',
            dependencies=('AGGTRADE',),
        )
        result = scorer.score_continuous(
            snap, setup(mode='NEUTRAL-FADE'), {}
        )
        side = result['sides']['LONG']
        total = sum(row['effect'] for row in side['interactions'])
        positive = sum(
            max(0.0, row['effect']) for row in side['causal_components']
        )
        self.assertLessEqual(total, 0.25 * positive + 1e-9)

    def test_neutral_momentum_is_passive_and_capped_at_one_percent(self):
        snap = decision(direction=-1.0, price=63607.0, atr=12.0)
        item = setup(side='SHORT', zone=63609.0)
        result = scorer.score_continuous(snap, item, {})
        self.assertTrue(result['activated'])
        self.assertEqual(result['entry_style_policy'], 'PASSIVE_RETEST')
        self.assertGreaterEqual(result['target_notional_pct'], 0.30)
        self.assertLessEqual(result['target_notional_pct'], 1.0)
        self.assertLess(result['momentum_state'], 0.0)

    def test_long_impulse_and_buy_footprint_block_duplicate_short_fade(self):
        snap = decision(direction=1.0, price=63568.0, atr=28.557)
        # Actual 03:01 shape: reversal labels share one price parent while BUY
        # footprint remains a full independent counterargument.
        for field, name in (
            ('continuous_sweep_m1', 'sweep'),
            ('continuous_zone_reaction', 'zone'),
            ('continuous_value_area_sweep', 'va'),
        ):
            setattr(snap, field, event(
                -1.0, 1.0, 0.9, source=name,
                dependencies=('PRICE_REACTION',), parent='same-reversal',
            ))
        snap.continuous_absorption_reaction = event(
            -1.0, 1.0, 1.0, source='absorption',
            dependencies=('DEPTH', 'AGGTRADE', 'PRICE_REACTION'),
            parent='same-reversal',
        )
        snap.continuous_footprint = event(
            1.0, 1.0, 1.0, source='buy-footprint',
            dependencies=('AGGTRADE',),
        )
        result = scorer.score_continuous(
            snap, setup(side='SHORT', mode='NEUTRAL-FADE', zone=63580.95), {}
        )
        self.assertFalse(result['activated'])
        self.assertEqual(result['target_notional_pct'], 0.0)
        self.assertLess(result['trade_power'], result['activation_floor'])
        reaction_components = [
            row for row in result['causal_components']
            if 'SWEEP_M1' in row['members']
        ]
        self.assertEqual(len(reaction_components), 1)

    def test_false_breakout_against_impulse_is_not_traded(self):
        snap = decision(direction=-1.0)
        snap.continuous_breakout_m1 = event(
            1.0, 1.0, 1.0, source='false-break',
            dependencies=('M1_CLOSED',),
        )
        result = scorer.score_continuous(
            snap, setup('LONG', 'TREND-BREAKOUT'), {}
        )
        self.assertFalse(result['activated'])
        self.assertEqual(result['target_notional_pct'], 0.0)
        self.assertGreater(result['impulse_conflict'], 0.75)

    def test_independent_fade_can_trade_against_only_weak_momentum(self):
        snap = decision(direction=-0.15, acceptance=0.20)
        snap.continuous_sweep_m1 = event(
            1.0, 1.0, 1.0, source='sweep',
            dependencies=('PRICE_REACTION',), parent='sweep-parent',
        )
        snap.continuous_footprint = event(
            1.0, 1.0, 1.0, source='footprint',
            dependencies=('AGGTRADE',), parent='footprint-parent',
        )
        result = scorer.score_continuous(
            snap, setup('LONG', 'NEUTRAL-FADE'), {}
        )
        self.assertTrue(result['activated'])
        self.assertGreater(result['trade_power'], result['activation_floor'])
        self.assertLess(result['impulse_conflict'], 0.10)

    def test_aligned_pullback_remains_tradeable(self):
        snap = decision(direction=1.0)
        snap.continuous_zone_reaction = event(
            1.0, 1.0, 1.0, source='zone',
            dependencies=('PRICE_REACTION',),
        )
        snap.continuous_footprint = event(
            1.0, 0.80, 0.90, source='footprint',
            dependencies=('AGGTRADE',),
        )
        result = scorer.score_continuous(
            snap, setup('LONG', 'TREND-PULLBACK'), {}
        )
        self.assertTrue(result['activated'])
        self.assertGreater(result['target_notional_pct'], 0.30)

    def test_retest_fit_decays_smoothly_without_distance_cliff(self):
        near = scorer.score_continuous(
            decision(-1.0, 63607.0, 12.0),
            setup('SHORT', zone=63609.0), {},
        )
        far = scorer.score_continuous(
            decision(-1.0, 63597.0, 12.0, acceptance=0.65),
            setup('SHORT', zone=63609.0), {},
        )
        self.assertGreater(near['retest_fit'], far['retest_fit'])
        self.assertGreater(far['retest_fit'], 0.0)

    def test_neutral_candidates_add_acceptance_lanes_only_under_v2(self):
        mode = {
            'modes': ['NEUTRAL-FADE'], 'bias': 'NEUTRAL',
            'zone_long': 63609.0, 'zone_short': 64470.0,
        }
        with mock.patch.dict('os.environ', {'SMC_SCORER_VERSION': 'CONTINUOUS_V2'}):
            candidates = radar.build_candidates(mode)
        momentum = [row for row in candidates if row['mode'] == 'NEUTRAL-MOMENTUM']
        self.assertEqual({row['bias'] for row in momentum}, {'LONG', 'SHORT'})
        self.assertTrue(all(row['entry_style'] == 'PASSIVE_RETEST' for row in momentum))

    def test_value_migration_location_does_not_activate_without_causal_force(self):
        snap = decision(direction=0.0)
        item = setup('SHORT', 'NEUTRAL-MOMENTUM', zone=64000.0)
        item.update({
            'value_migration_retest': True,
            'value_boundary': 'VAL',
            'location_role': 'VALUE_MIGRATION_RETEST',
        })
        result = scorer.score_continuous(snap, item, {})
        self.assertFalse(result['activated'])
        self.assertEqual(result['activation'], 0.0)
        self.assertTrue(result['value_migration_retest'])
        self.assertEqual(result['entry_style_policy'], 'PASSIVE_RETEST')

    def test_value_migration_uses_existing_causal_momentum_floor(self):
        snap = decision(direction=-1.0, price=62775.0, atr=18.0)
        snap.continuous_persistent_flow = event(
            -1.0, 0.85, 0.90, source='sell-flow',
            dependencies=('AGGTRADE',), ttl=5.0,
        )
        item = setup('SHORT', 'NEUTRAL-MOMENTUM', zone=62775.0)
        item.update({
            'value_migration_retest': True,
            'value_boundary': 'VAL',
        })
        result = scorer.score_continuous(snap, item, {})
        self.assertTrue(result['activated'], result)
        self.assertGreater(result['trade_power'], result['activation_floor'])
        self.assertLessEqual(result['target_notional_pct'], 1.0)


class PassiveLifecycleV2Tests(unittest.TestCase):
    def state_and_signal(self):
        item = {'setup_id': 'passive-v2', 'generation': 1, 'state': 'EXECUTING'}
        state = SimpleNamespace(
            best_bid=99.9, best_ask=100.1,
            execution_best_bid=99.9, execution_best_ask=100.1,
            execution_price_time=time.time(), system_ready=True,
            trading_enabled=True, active_setups={'x': item},
        )
        signal = {
            'passive_entry_price': 100.0,
            'client_order_id': 'smc-passive-v2',
            'setup_id': 'passive-v2', 'setup_generation': 1,
            'continuous_score': {
                'confidence': 0.5, 'activation': 0.5,
                'trade_power': 50.0, 'activation_floor': 30.0,
                'impulse_conflict': 0.0,
            },
            'score_version': 'CONTINUOUS_V2', 'bias': 'LONG',
        }
        return state, signal, item

    def test_intent_ttl_is_confidence_activation_curve(self):
        _, signal, _ = self.state_and_signal()
        self.assertEqual(executor._passive_intent_ttl(signal), 45.0)
        signal['continuous_score'].update({'confidence': 1.0, 'activation': 1.0})
        self.assertEqual(executor._passive_intent_ttl(signal), 90.0)

    def test_retention_hysteresis_replaces_two_second_cliff(self):
        state, signal, item = self.state_and_signal()
        signal['initial_trade_power'] = 60.0
        signal['initial_floor'] = 30.0
        item['passive_live_score'] = {
            **signal['continuous_score'], 'trade_power': 14.0,
        }
        with mock.patch.dict(
            'os.environ', {'SMC_ENTRY_LIFECYCLE': 'OPPORTUNITY_RETENTION_V1'}
        ):
            self.assertIsNone(executor._passive_thesis_reason(state, signal, item, 100.0))
            self.assertIsNone(executor._passive_thesis_reason(state, signal, item, 144.9))
            self.assertEqual(
                executor._passive_thesis_reason(state, signal, item, 145.0),
                'RETENTION_POWER_EXPIRED',
            )
        self.assertEqual(item['passive_retention_floor'], 15.0)

    def test_toxic_maker_fill_shortens_retention_without_hard_veto(self):
        state, signal, item = self.state_and_signal()
        signal['initial_trade_power'] = 60.0
        signal['initial_floor'] = 30.0
        item['passive_live_score'] = {
            **signal['continuous_score'], 'trade_power': 50.0,
            'momentum_breakdown': {
                'flow': -0.45, 'acceptance': -0.95,
            },
        }
        state.adverse_flow_memory_by_bias = {
            'LONG': {
                'ts': time.time(), 'blocked_bias': 'LONG',
                'severity': 1.0,
            },
            'SHORT': {},
        }
        with mock.patch.dict(
            'os.environ', {'SMC_ENTRY_LIFECYCLE': 'OPPORTUNITY_RETENTION_V1'}
        ):
            self.assertIsNone(
                executor._passive_thesis_reason(state, signal, item, 100.0)
            )
            self.assertTrue(executor._passive_quote_should_wait(item))
            self.assertEqual(
                executor._passive_thesis_reason(state, signal, item, 100.6),
                'PASSIVE_TOXICITY_EXPIRED',
            )
        self.assertGreater(item['passive_toxicity']['score'], 0.90)
        self.assertLess(item['passive_effective_grace_seconds'], 1.0)
        self.assertLess(
            item['passive_current_trade_power'],
            item['passive_retention_floor'],
        )

    def test_retention_minimum_is_bounded_by_two_percent(self):
        filters = {'step_size': 0.0001, 'min_qty': 0.0001, 'min_notional': 5.0}
        result = executor._retention_size_feasibility(
            4888.0, 0.30, 63400.0, filters,
        )
        self.assertTrue(result['executable'])
        self.assertTrue(result['policy_minimum_applied'])
        self.assertEqual(result['quantity'], 0.001)
        self.assertLessEqual(result['effective_target_notional_pct'], 2.0)

    def test_market_conversion_requires_positive_taker_lcb_and_price_progress(self):
        state, signal, item = self.state_and_signal()
        state.best_ask = 100.4
        state.execution_best_ask = 101.0
        state.atr_1m = 1.0
        signal.update({
            'decision_price': 100.0, 'initial_trade_power': 50.0,
            'initial_floor': 30.0,
        })
        item['retention_policy'] = executor._retention_policy(signal)
        bundle = (
            {'avg_price': 100.4}, 100.4,
            {'soft_sl': 99.0, 'soft_tp1': 102.0}, {},
            {'entry_policy': 'MARKET_OR_CONFIGURED'},
            {'realizable_edge_lcb': 2.5, 'entry_policy': 'MARKET_OR_CONFIGURED'},
        )
        with mock.patch.dict('os.environ', {
            'SMC_ENTRY_LIFECYCLE': 'OPPORTUNITY_RETENTION_V1',
            'SMC_ECONOMIC_ENGINE': 'DYNAMIC_PATH_V2',
        }), mock.patch.object(executor, '_dynamic_bundle', return_value=bundle):
            result = executor._market_conversion_assessment(
                state, signal, item, 0.001, 0.1,
            )
            self.assertTrue(result['eligible'])
            self.assertAlmostEqual(result['market_edge_lcb'], 2.5)
            bundle[-1]['realizable_edge_lcb'] = 1.99
            result = executor._market_conversion_assessment(
                state, signal, item, 0.001, 0.1,
            )
            self.assertFalse(result['eligible'])
            self.assertEqual(result['reason'], 'MARKET_EDGE_BELOW_BUFFER')

    def test_partial_fill_returns_immediately_for_protection(self):
        class API:
            def __init__(self):
                self.cancelled = False

            async def new_order(self, *args, **kwargs):
                return {
                    'orderId': 1, 'clientOrderId': kwargs['newClientOrderId'],
                    'status': 'NEW', 'executedQty': '0',
                }, 200

            async def query_order(self, *args):
                return {
                    'orderId': 1, 'clientOrderId': args[1],
                    'status': 'PARTIALLY_FILLED', 'executedQty': '0.001',
                }, 200

            async def cancel_order(self, *args):
                self.cancelled = True
                return {'orderId': 1}, 200

        state, signal, item = self.state_and_signal()
        api = API()
        result, status, _ = asyncio.run(executor.submit_passive_entry(
            api, state, signal, 'BUY', 0.002, {}, 0.1,
        ))
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'PARTIALLY_FILLED_CANCELED')
        self.assertTrue(api.cancelled)
        self.assertEqual(item['passive_intent_state'], 'PARTIAL_FILL')

    def test_reprice_cancels_before_new_order_and_keeps_one_open(self):
        class API:
            def __init__(self, state):
                self.state = state
                self.events = []
                self.order_id = 0
                self.queries = 0

            async def new_order(self, *args, **kwargs):
                self.order_id += 1
                self.events.append(('new', self.order_id))
                return {
                    'orderId': self.order_id,
                    'clientOrderId': kwargs['newClientOrderId'],
                    'status': 'NEW', 'executedQty': '0',
                }, 200

            async def query_order(self, symbol, client_id):
                self.queries += 1
                if self.queries == 1:
                    self.state.execution_best_bid = 99.6
                if self.order_id >= 2:
                    return {
                        'orderId': self.order_id, 'clientOrderId': client_id,
                        'status': 'FILLED', 'executedQty': '0.002',
                    }, 200
                return {
                    'orderId': self.order_id, 'clientOrderId': client_id,
                    'status': 'NEW', 'executedQty': '0',
                }, 200

            async def cancel_order(self, symbol, order_id):
                self.events.append(('cancel', order_id))
                return {'orderId': order_id}, 200

        state, signal, _ = self.state_and_signal()
        api = API(state)
        with mock.patch.object(executor, 'PASSIVE_REPRICE_MIN_SECONDS', 0.0):
            result, status, _ = asyncio.run(executor.submit_passive_entry(
                api, state, signal, 'BUY', 0.002, {}, 0.1,
            ))
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'FILLED')
        self.assertEqual(api.events[:3], [('new', 1), ('cancel', 1), ('new', 2)])

    def test_economics_turning_negative_cancels_without_409(self):
        class API:
            async def new_order(self, *args, **kwargs):
                return {
                    'orderId': 1, 'clientOrderId': kwargs['newClientOrderId'],
                    'status': 'NEW', 'executedQty': '0',
                }, 200

            async def query_order(self, symbol, client_id):
                return {
                    'orderId': 1, 'clientOrderId': client_id,
                    'status': 'CANCELED', 'executedQty': '0',
                }, 200

            async def cancel_order(self, *args):
                return {'orderId': 1}, 200

        state, signal, _ = self.state_and_signal()
        checks = iter((None, 'REALIZABLE_EDGE_NEGATIVE'))
        with mock.patch.object(
            executor, '_passive_economics_reason', side_effect=lambda *args: next(checks)
        ), mock.patch.object(executor, 'PASSIVE_REPRICE_MIN_SECONDS', 0.0):
            result, status, _ = asyncio.run(executor.submit_passive_entry(
                API(), state, signal, 'BUY', 0.002, {}, 0.1,
            ))
        self.assertEqual(status, 204)
        self.assertNotEqual(status, 409)
        self.assertEqual(result['reason'], 'REALIZABLE_EDGE_NEGATIVE')


class OpportunityIdentityV1Tests(unittest.TestCase):
    def test_neutral_rearms_share_one_opportunity_and_one_outcome(self):
        state = SimpleNamespace(
            setup_generation=0, structure_version=13,
            best_ask=100.1, best_bid=99.9,
            setup_outcomes=deque(maxlen=300), setup_followups=deque(maxlen=300),
            continuous_shadow_registry={}, continuous_shadow_events=deque(maxlen=10),
            journal_events=deque(maxlen=100),
        )
        candidate = {
            'zone_id': 'NEUTRAL-MOMENTUM:LONG:VAH',
            'mode': 'NEUTRAL-MOMENTUM', 'bias': 'LONG',
            'zone': 100.0, 'kind': 'zone', 'entry_style': 'PASSIVE_RETEST',
        }
        first = radar._new_setup(state, candidate, 1, 10.0)
        second = radar._new_setup(state, candidate, 2, 20.0)
        self.assertEqual(first['opportunity_id'], second['opportunity_id'])
        with mock.patch.object(radar.time, 'monotonic', return_value=30.0):
            radar._record_setup_outcome(state, first, 'WATCH', 'TTL', 100.0, 1000.0)
            radar._record_setup_outcome(state, second, 'WATCH', 'TTL', 100.0, 1001.0)
        self.assertEqual(len(state.setup_outcomes), 1)
        self.assertEqual(len(state.setup_followups), 1)
        self.assertEqual(len(state.setup_outcomes[0]['setup_ids']), 2)

    def test_terminal_intent_blocks_same_structure_only(self):
        state = SimpleNamespace(intent_terminal_opportunities={
            'opp': {'structure_version': 7, 'reason': 'EXPIRED_UNFILLED'}
        })
        self.assertTrue(radar._intent_terminal_blocked(state, 'opp', 7))
        self.assertFalse(radar._intent_terminal_blocked(state, 'opp', 8))
        self.assertNotIn('opp', state.intent_terminal_opportunities)


if __name__ == '__main__':
    unittest.main()
