import unittest

from recorder.replay import DeterministicReplay
from ops.wstrade_replay_validation import (
    REQUIRED_WHALE_STREAMS, WhaleStrategyReplayAudit, whale_stream_coverage,
)


class ReplayWhaleCoverageTests(unittest.TestCase):
    def test_all_cash_anchor_streams_are_required(self):
        counts, missing = whale_stream_coverage({
            'streams': {stream: 1 for stream in REQUIRED_WHALE_STREAMS}
        })
        self.assertFalse(missing)
        self.assertTrue(all(counts.values()))

    def test_missing_coinbase_flow_fails_coverage(self):
        present = {
            stream: 2 for stream in REQUIRED_WHALE_STREAMS
            if stream != 'coinbase_spot_trade_100ms'
        }
        _, missing = whale_stream_coverage({'streams': present})
        self.assertEqual(missing, ['coinbase_spot_trade_100ms'])

    def test_strategy_audit_rejects_catch_without_material_flow(self):
        snapshot = {
            'state': 'RELEASE', 'side': 'LONG', 'lane': 'CATCH',
            'evidence': ['DEPTH_CONSUMPTION'], 'vetoes': [],
            'flow_volume_floor_btc': 0.02,
            'flow': {
                venue: {'1.0': {'volume': 0.000001, 'imbalance': 1.0}}
                for venue in ('spot', 'coinbase', 'futures')
            },
        }
        violations = WhaleStrategyReplayAudit._validate_snapshot(snapshot)
        self.assertIn('CATCH_WITHOUT_MATERIAL_FLOW_QUORUM', violations)

    def test_strategy_audit_accepts_release_with_depth_and_volume(self):
        snapshot = {
            'state': 'RELEASE', 'side': 'SHORT', 'lane': 'CATCH',
            'evidence': ['DEPTH_CONSUMPTION'], 'vetoes': [],
            'flow_volume_floor_btc': 0.02,
            'flow': {
                venue: {'1.0': {'volume': 0.2, 'imbalance': -0.5}}
                for venue in ('spot', 'coinbase', 'futures')
            },
        }
        self.assertEqual(
            WhaleStrategyReplayAudit._validate_snapshot(snapshot), []
        )

    def test_late_warmup_feature_is_not_a_false_flow_mismatch(self):
        replay = DeterministicReplay(metrics_start_ms=2000)
        replay.apply({
            'stream': 'coinbase_spot_trade_100ms',
            'receive_time_ms': 2000, 'event_time_ms': 1900,
            'sequence_end': 1,
            'payload': {'buy_qty': 0.1, 'sell_qty': 0.0},
        })
        replay.apply({
            'stream': 'feature_1s',
            'receive_time_ms': 6000, 'event_time_ms': 1999,
            'sequence_end': 1,
            'payload': {
                'buy_qty': 1.0, 'sell_qty': 0.0,
                'cash_flow': {
                    'binance_spot': {'buy_qty': 0.0, 'sell_qty': 0.0},
                    'coinbase_spot': {'buy_qty': 1.0, 'sell_qty': 0.0},
                },
            },
        })
        result = replay.summary()
        self.assertEqual(result['feature_flow_rows'], 0)
        self.assertEqual(result['feature_flow_mismatches'], 0)


if __name__ == '__main__':
    unittest.main()
