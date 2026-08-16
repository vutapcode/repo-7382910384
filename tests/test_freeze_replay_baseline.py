import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'freeze_replay_baseline_test_module',
    ROOT / '4_nghien_cuu_ai/freeze_replay_baseline.py',
)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


class FreezeReplayBaselineTests(unittest.TestCase):
    def test_vietnam_timestamp_is_real_utc_plus_seven(self):
        self.assertEqual(
            baseline.vietnam_iso(1786320632999),
            '2026-08-10T07:10:32.999000+07:00',
        )

    def test_market_summary_is_directional_and_fee_aware(self):
        rows = [
            {
                'event_time_ms': 1000, 'mid_open': 100.0,
                'mid_high': 101.0, 'mid_low': 99.0, 'mid_close': 100.0,
                'buy_qty': 2.0, 'sell_qty': 1.0,
            },
            {
                'event_time_ms': 2000, 'mid_open': 100.0,
                'mid_high': 102.0, 'mid_low': 99.5, 'mid_close': 101.0,
                'buy_qty': 3.0, 'sell_qty': 1.0,
            },
        ]
        result = baseline.market_summary(rows, 1000, 'LONG')
        self.assertAlmostEqual(result['directional_mfe_bps'], 200.0)
        self.assertAlmostEqual(
            result['directional_net_mfe_bps_after_8bps'], 192.0
        )
        self.assertEqual(result['unique_feature_seconds'], 2)

    def test_missed_edge_is_explicitly_research_only(self):
        case = {
            'expected_direction': 'LONG',
            'counterfactual_target': 102.0,
        }
        result = baseline.counterfactual_annotation(
            case, {'focus_price': 100.0}
        )
        self.assertFalse(result['evaluated_by_live_bot'])
        self.assertIn('RESEARCH_ONLY', result['label'])
        self.assertAlmostEqual(result['projected_net_edge_bps'], 112.0)


if __name__ == '__main__':
    unittest.main()
