import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


risk = load(
    'fee_target_risk',
    '3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py',
)
economic = load(
    'fee_target_economic',
    '2_suy_luan_mapping/tong_ket_chi_huy/kinh_te_lenh.py',
)
journal = load(
    'fee_target_journal',
    '3_thuc_thi/quan_ly_vi_the/nhat_ky_giao_dich.py',
)


def market_state():
    return SimpleNamespace(
        atr_1m=2.0,
        poc=100.0,
        vah=110.0,
        val=90.0,
        swing_high_m15=120.0,
        swing_low_m15=80.0,
        sweep_m1={},
        exchange_filters={'tick_size': 0.1},
        bids_top_10=[[99.9, 10.0]],
        asks_top_10=[[100.0, 10.0]],
        best_bid=99.9,
        best_ask=100.0,
    )


class StructureAwareTargetTests(unittest.TestCase):
    def assert_stop_reserve(self, entry, side, levels):
        if side == 'LONG':
            self.assertGreaterEqual(entry - levels['soft_sl'], 41.0)
            self.assertLess(levels['hard_sl'], levels['soft_sl'])
        else:
            self.assertGreaterEqual(levels['soft_sl'] - entry, 41.0)
            self.assertGreater(levels['hard_sl'], levels['soft_sl'])

    def test_poc_pullback_targets_opposite_value_edge(self):
        state = market_state()
        long_levels = risk.calculate_levels(
            state, 100.0, 'LONG', 0.1, 'TREND-PULLBACK',
            setup_zone=100.0, setup_kind='zone',
        )
        short_levels = risk.calculate_levels(
            state, 100.0, 'SHORT', 0.1, 'TREND-PULLBACK',
            setup_zone=100.0, setup_kind='zone',
        )

        self.assertEqual(long_levels['soft_tp1'], 110.0)
        self.assertEqual(long_levels['soft_tp2'], 120.0)
        self.assertEqual(long_levels['target_basis'], 'PULLBACK_POC_TO_VAH')
        self.assertEqual(short_levels['soft_tp1'], 90.0)
        self.assertEqual(short_levels['soft_tp2'], 80.0)
        self.assertEqual(short_levels['target_basis'], 'PULLBACK_POC_TO_VAL')
        self.assert_stop_reserve(100.0, 'LONG', long_levels)
        self.assert_stop_reserve(100.0, 'SHORT', short_levels)

    def test_outer_pullback_targets_poc_then_other_value_edge(self):
        state = market_state()
        long_levels = risk.calculate_levels(
            state, 90.0, 'LONG', 0.1, 'TREND-PULLBACK',
            setup_zone=90.0, setup_kind='zone',
        )
        short_levels = risk.calculate_levels(
            state, 110.0, 'SHORT', 0.1, 'TREND-PULLBACK',
            setup_zone=110.0, setup_kind='zone',
        )

        self.assertEqual((long_levels['soft_tp1'], long_levels['soft_tp2']), (100.0, 110.0))
        self.assertEqual((short_levels['soft_tp1'], short_levels['soft_tp2']), (100.0, 90.0))
        self.assertEqual(long_levels['target_basis'], 'PULLBACK_OUTER_TO_POC')
        self.assertEqual(short_levels['target_basis'], 'PULLBACK_OUTER_TO_POC')
        self.assert_stop_reserve(90.0, 'LONG', long_levels)
        self.assert_stop_reserve(110.0, 'SHORT', short_levels)

    def test_neutral_fade_targets_poc_from_both_outer_edges(self):
        state = market_state()
        long_levels = risk.calculate_levels(
            state, 90.0, 'LONG', 0.1, 'NEUTRAL-FADE',
            setup_zone=90.0, setup_kind='zone',
        )
        short_levels = risk.calculate_levels(
            state, 110.0, 'SHORT', 0.1, 'NEUTRAL-FADE',
            setup_zone=110.0, setup_kind='zone',
        )

        self.assertEqual(long_levels['soft_tp1'], 100.0)
        self.assertEqual(short_levels['soft_tp1'], 100.0)
        self.assertEqual(long_levels['target_basis'], 'NEUTRAL_OUTER_TO_POC')
        self.assertEqual(short_levels['target_basis'], 'NEUTRAL_OUTER_TO_POC')

    def test_tick_rounding_never_erodes_the_41_price_stop_reserve(self):
        state = market_state()
        long_entry = 100.05
        short_entry = 99.95
        long_levels = risk.calculate_levels(
            state, long_entry, 'LONG', 0.1, 'TREND-PULLBACK',
            setup_zone=100.0, setup_kind='zone',
        )
        short_levels = risk.calculate_levels(
            state, short_entry, 'SHORT', 0.1, 'TREND-PULLBACK',
            setup_zone=100.0, setup_kind='zone',
        )
        self.assert_stop_reserve(long_entry, 'LONG', long_levels)
        self.assert_stop_reserve(short_entry, 'SHORT', short_levels)

    def test_shadow_uses_same_setup_zone_target_policy(self):
        state = market_state()
        signal = {
            'bias': 'LONG',
            'mode': 'TREND-PULLBACK',
            'setup_zone': 100.0,
            'setup_kind': 'zone',
        }
        _, levels, _, _ = journal._build_shadow_position(
            state,
            'pc-target-policy',
            signal,
            1.0,
            {
                'available': True,
                'avg_price': 100.0,
                'reference_price': 100.0,
                'slippage_bps': 0.0,
                'captured_at': 1.0,
            },
            'FEE_BLOCKED',
        )
        self.assertEqual(levels['soft_tp1'], 110.0)
        self.assertEqual(levels['target_basis'], 'PULLBACK_POC_TO_VAH')

    def test_breakout_uses_explicit_liquidity_target_not_atr(self):
        state = market_state()
        levels = risk.calculate_levels(
            state, 100.0, 'LONG', 0.1, 'TRANSITION-BREAKOUT',
            setup_zone=100.0, setup_kind='breakout',
            breakout_target=110.0, breakout_target2=120.0,
            breakout_target_basis='BREAKOUT_NEAREST_LIQUIDITY_EXTREMUM',
        )
        self.assertEqual(levels['soft_tp1'], 110.0)
        self.assertEqual(levels['soft_tp2'], 120.0)
        self.assertNotIn('ATR_FALLBACK', levels['target_basis'])

    def test_breakout_atr_geometry_fallback_is_not_economic_edge(self):
        state = market_state()
        levels = risk.calculate_levels(
            state, 100.0, 'LONG', 0.1, 'TRANSITION-BREAKOUT',
            setup_zone=100.0, setup_kind='breakout',
        )
        result = economic.observe(
            state, 'LONG', 1.0, levels['soft_tp1'], setup_kind='breakout',
            target_basis=levels['target_basis'], entry_style='PASSIVE_RETEST',
        )
        self.assertEqual(
            levels['target_basis'],
            'BREAKOUT_TECHNICAL_ATR_FALLBACK_NO_ECONOMIC_TARGET',
        )
        self.assertFalse(result['structural_fee_floor_pass'])
        self.assertEqual(result['reason'], 'NO_MEANINGFUL_LIQUIDITY_TARGET')


class EnforcedEconomicContractTests(unittest.TestCase):
    def test_policy_reports_that_net_edge_is_enforced(self):
        state = market_state()
        blocked = economic.observe(
            state, 'LONG', 1.0, 100.05, capture_ratio=0.60,
        )
        passed = economic.observe(
            state, 'LONG', 1.0, 100.30, capture_ratio=0.60,
        )

        self.assertEqual(blocked['mode'], 'ENFORCED_NET_EDGE')
        self.assertTrue(blocked['blocks_entry'])
        self.assertFalse(blocked['structural_fee_floor_pass'])
        self.assertEqual(
            blocked['reason'],
            'PROJECTED_CAPTURE_BELOW_ALL_IN_COST_PLUS_EDGE',
        )
        self.assertTrue(passed['structural_fee_floor_pass'])
        self.assertEqual(passed['reason'], 'PASS')


if __name__ == '__main__':
    unittest.main()
