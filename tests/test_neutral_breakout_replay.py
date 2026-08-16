import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'test_neutral_breakout_replay_module',
    ROOT / '4_nghien_cuu_ai/replay_neutral_breakout.py',
)
replay_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_mod)


class NeutralBreakoutReplayTests(unittest.TestCase):
    def test_retest_proxy_is_directional_and_fee_aware(self):
        start = 1_000_000
        rows = [
            [start + 60_000, 101, 102, 99.5, 100.5, 1, start + 119_999],
        ]
        for minute in range(2, 48):
            close = 100.0 + minute * 0.05
            rows.append([
                start + minute * 60_000, close - 0.05, close + 0.1,
                close - 0.1, close, 1,
                start + (minute + 1) * 60_000 - 1,
            ])
        result = replay_mod.evaluate_retest_outcome(
            rows, start, 'LONG', 100.0,
            horizons=(900, 1800), round_trip_cost_bps=8.0,
        )
        self.assertEqual(result['entry']['price'], 100.0)
        self.assertIn('900', result['horizons'])
        self.assertIn('1800', result['horizons'])
        self.assertAlmostEqual(
            result['horizons']['900']['net_close_bps'],
            result['horizons']['900']['close_bps'] - 8.0,
        )

    def test_no_level_touch_means_no_hypothetical_entry(self):
        rows = [[60_000, 102, 103, 101, 102, 1, 119_999]]
        result = replay_mod.evaluate_retest_outcome(
            rows, 0, 'LONG', 100.0, horizons=(900,),
        )
        self.assertIsNone(result['entry'])
        self.assertEqual(result['horizons'], {})


if __name__ == '__main__':
    unittest.main()
