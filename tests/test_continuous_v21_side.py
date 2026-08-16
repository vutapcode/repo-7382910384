import importlib.util
import os
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


scorer = load(
    'continuous_v21_test_scorer',
    '2_suy_luan_mapping/tong_ket_chi_huy/cham_diem_continuous_v21.py',
)
calibration = load(
    'continuous_v21_test_calibration',
    '2_suy_luan_mapping/tong_ket_chi_huy/side_calibration.py',
)
radar = load('continuous_v21_test_radar', '2_suy_luan_mapping/map_gia_tick.py')
identity = load('continuous_v21_test_identity', 'loi_he_thong/order_identity.py')
risk = load(
    'continuous_v21_test_risk',
    '3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py',
)


FIELDS = (
    'continuous_m15', 'continuous_sweep_m1', 'continuous_breakout_m1',
    'continuous_footprint', 'continuous_persistent_flow',
    'continuous_zone_reaction', 'continuous_flow_divergence',
    'continuous_absorption_reaction', 'continuous_value_area_sweep',
)


def event(direction=0.0, strength=0.0, source='none'):
    return {
        'active': bool(strength), 'direction': direction,
        'strength': strength, 'quality': 1.0 if strength else 0.0,
        'ts': 1000.0, 'ttl': 30.0, 'source_event_id': source,
        'dependency_families': [source],
    }


def snapshot(side='LONG', price=100.0, poc=101.0):
    direction = 1.0 if side == 'LONG' else -1.0
    rows = {}
    for horizon, progress in ((15, 0.25), (60, 0.50), (180, 0.80)):
        rows[str(horizon)] = {
            'price_progress_atr': direction * progress,
            'range_expansion_atr': progress, 'price_efficiency': 0.8,
            'price_coverage_seconds': horizon,
            'flow_imbalance': direction * 0.7, 'flow_total': 100.0,
            'flow_coverage_seconds': horizon,
            'acceptance_long': 0.8 if side == 'LONG' else 0.0,
            'acceptance_short': 0.8 if side == 'SHORT' else 0.0,
        }
    values = {
        'snapshot_time': 1000.0, 'best_bid': price - 0.01,
        'best_ask': price + 0.01, 'atr_1m': 1.0,
        'poc': poc, 'vah': 102.0, 'val': 98.0,
        'vol_pct90': 2.0, 'current_cvd_buy_3s': 2.0,
        'current_cvd_sell_3s': 2.0, 'momentum_horizons': rows,
        'trend_m15': 'NEUTRAL', 'volume_profile_updated_at': 990.0,
        'volume_profile_coverage': 1.0, 'poc_movement_atr': 0.0,
    }
    for field in FIELDS:
        values[field] = event()
    values['continuous_zone_reaction'] = event(direction, 0.9, 'price')
    return SimpleNamespace(**values)


def setup(side='LONG'):
    return {
        'setup_id': 'setup-1', 'generation': 1,
        'opportunity_id': 'opp-1', 'semantic_key': 'opp-1',
        'mode': 'NEUTRAL-FADE', 'bias': side, 'zone': 100.0, 'kind': 'zone',
    }


def artifact(cells=()):
    data = calibration.prior_artifact()
    data.update({
        'calibration_version': 'TEST_V1', 'global_opportunity_count': 250,
        'side_opportunity_counts': {'LONG': 125, 'SHORT': 125},
        'evaluation_side_counts': {'LONG': 125, 'SHORT': 125},
        'evaluation_time_blocks': 3, 'cells': list(cells), 'quality_flags': [],
    })
    data['artifact_hash'] = calibration.canonical_hash(data)
    return data


class ContinuousV21Tests(unittest.TestCase):
    def test_poc_opposition_is_twice_support_before_clamp(self):
        common = artifact()
        with mock.patch.dict(os.environ, {'SMC_SIDE_CALIBRATION_MODE': 'SHADOW'}):
            support = scorer.score_side_calibrated(
                snapshot('LONG', 100.0, 101.0), setup('LONG'), artifact=common
            )
            oppose = scorer.score_side_calibrated(
                snapshot('LONG', 101.0, 100.0), setup('LONG'), artifact=common
            )
        self.assertAlmostEqual(
            abs(oppose['poc_effect']), 2.0 * abs(support['poc_effect']), places=6
        )
        self.assertGreater(support['poc_effect'], 0.0)
        self.assertLess(oppose['poc_effect'], 0.0)

    def test_poc_effect_is_continuous_around_zero(self):
        common = artifact()
        left = scorer.score_side_calibrated(
            snapshot('LONG', 100.0, 100.0001), setup('LONG'), artifact=common
        )
        right = scorer.score_side_calibrated(
            snapshot('LONG', 100.0, 99.9999), setup('LONG'), artifact=common
        )
        self.assertLess(abs(left['poc_effect'] - right['poc_effect']), 0.002)

    def test_mirrored_common_score_can_have_different_posterior_score(self):
        cells = [
            {'side': 'LONG', 'archetype': 'REVERSAL', 'regime': 'RANGE_NEUTRAL',
             'effective_sample_size': 50, 'evidence_multiplier': 1.15,
             'poc_weight': 4.0, 'parent_shrinkage': 0.2},
            {'side': 'SHORT', 'archetype': 'REVERSAL', 'regime': 'RANGE_NEUTRAL',
             'effective_sample_size': 50, 'evidence_multiplier': 0.85,
             'poc_weight': 4.0, 'parent_shrinkage': 0.2},
        ]
        data = artifact(cells)
        long = scorer.score_side_calibrated(
            snapshot('LONG', 100.0, 100.0), setup('LONG'), artifact=data
        )
        short = scorer.score_side_calibrated(
            snapshot('SHORT', 100.0, 100.0), setup('SHORT'), artifact=data
        )
        self.assertAlmostEqual(long['raw_common_score'], short['raw_common_score'], places=4)
        self.assertNotEqual(long['side_adjusted_score'], short['side_adjusted_score'])
        self.assertFalse(long['live_authority'])

    def test_low_sample_cell_shrinks_and_cannot_go_bounded_live(self):
        data = artifact([{
            'side': 'LONG', 'archetype': 'REVERSAL', 'regime': 'RANGE_NEUTRAL',
            'effective_sample_size': 39, 'evidence_multiplier': 1.15,
            'net_edge_lcb': 5.0, 'bounded_live_eligible': True,
            'parent_shrinkage': 0.8,
        }])
        profile = calibration.profile_for(data, 'LONG', 'REVERSAL', 'RANGE_NEUTRAL')
        self.assertFalse(calibration.bounded_live_allowed(data, profile))

    def test_approved_cell_is_bounded_and_never_exceeds_caps(self):
        data = artifact([{
            'side': 'LONG', 'archetype': 'REVERSAL', 'regime': 'RANGE_NEUTRAL',
            'effective_sample_size': 50, 'evidence_multiplier': 1.15,
            'confidence_multiplier': 1.15, 'activation_multiplier': 1.15,
            'floor_multiplier': 0.90, 'size_multiplier': 1.15,
            'net_edge_lcb': 2.0, 'bounded_live_eligible': True,
            'parent_shrinkage': 0.2, 'quality_flags': [],
        }])
        snap, item = snapshot('LONG', 100.0, 100.0), setup('LONG')
        base = scorer.v2.score_continuous(snap, item, {}, live=True)
        with mock.patch.dict(os.environ, {'SMC_SIDE_CALIBRATION_MODE': 'BOUNDED_LIVE'}):
            side = scorer.score_side_calibrated(snap, item, {}, artifact=data)
        live = scorer.apply_bounded_live(base, side)
        self.assertTrue(side['bounded_live_applied'])
        self.assertTrue(live['live_authority'])
        self.assertLessEqual(live['target_notional_pct'], 9.0)
        self.assertGreaterEqual(live['activation_floor'], 0.90 * base['activation_floor'] - 1e-6)

    def test_breakout_acceptance_reduces_old_poc_relevance_smoothly(self):
        data = artifact()
        item = setup('LONG')
        item['mode'] = 'TREND-BREAKOUT'
        low, high = snapshot('LONG', 100.0, 101.0), snapshot('LONG', 100.0, 101.0)
        for row in low.momentum_horizons.values():
            row['acceptance_long'] = 0.0
        for row in high.momentum_horizons.values():
            row['acceptance_long'] = 1.0
        low_result = scorer.score_side_calibrated(low, item, {}, artifact=data)
        high_result = scorer.score_side_calibrated(high, item, {}, artifact=data)
        self.assertGreater(low_result['poc_relevance'], high_result['poc_relevance'])
        self.assertGreater(high_result['poc_relevance'], 0.0)

    def test_duplicate_rescore_keeps_one_sample(self):
        state = SimpleNamespace(
            run_id='run-a', side_calibration_shadow_registry={},
            continuous_shadow_events=deque(maxlen=20),
            continuous_shadow_drop_count=0,
        )
        result = scorer.score_side_calibrated(snapshot(), setup(), artifact=artifact())
        radar._record_side_calibration_shadow(state, setup(), result, 1000.0)
        radar._record_side_calibration_shadow(state, setup(), result, 1000.5)
        radar._record_side_calibration_shadow(state, setup(), result, 1001.1)
        record = state.side_calibration_shadow_registry['opp-1']
        self.assertEqual(record['sample_weight'], 1)
        self.assertEqual(len(record['history']), 2)

    def test_order_identity_changes_across_boot_and_role(self):
        a, b = SimpleNamespace(run_id='a'), SimpleNamespace(run_id='b')
        first = identity.client_order_id(a, 'ENTRY', 'opp', 'setup', 1)
        self.assertNotEqual(first, identity.client_order_id(b, 'ENTRY', 'opp', 'setup', 1))
        self.assertNotEqual(first, identity.client_order_id(a, 'CLOSE', 'opp', 'setup', 1))
        self.assertLessEqual(len(first), 36)

    def test_adaptive_guardian_is_continuous_and_shadow_by_default(self):
        with mock.patch.dict(os.environ, {'SMC_ADAPTIVE_GUARDIAN_MODE': 'SHADOW'}):
            left = risk.adaptive_guardian_policy(100, 99.6, 102, 1, 0.02, 0.01)
            right = risk.adaptive_guardian_policy(100, 99.599, 102, 1, 0.02, 0.01)
        self.assertEqual(left['mode'], 'SHADOW')
        self.assertLess(abs(left['stop_distance'] - right['stop_distance']), 0.01)
        self.assertGreaterEqual(left['time_budget_seconds'], 900.0)


if __name__ == '__main__':
    unittest.main()
