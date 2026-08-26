import unittest
from types import SimpleNamespace

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


path = Path(__file__).parents[1] / '1_tai_du_lieu' / 'tai_whale_depth' / 'tai_whale_depth.py'
spec = spec_from_file_location('test_whale_depth_module', path)
depth = module_from_spec(spec)
spec.loader.exec_module(depth)


class WhaleDepthTests(unittest.TestCase):
    def state(self):
        return SimpleNamespace(
            futures_depth_bids_top_20=[], futures_depth_asks_top_20=[],
            futures_depth_last_u=0, futures_depth_gap_count=0,
        )

    def test_compact_book_metrics_and_sequence_gap(self):
        state = self.state()
        first = {'u': 10, 'pu': 0, 'b': [['100', '2'], ['99', '1']],
                 'a': [['101', '1'], ['102', '2']]}
        self.assertTrue(depth.apply_depth_message(state, first, now=1.0))
        self.assertEqual(len(state.futures_depth_bids_top_20), 2)
        self.assertGreater(state.futures_depth_metrics['imbalance_top20'], -1.0)
        second = {'u': 12, 'pu': 9, 'b': [['100', '3']], 'a': [['101', '1']]}
        self.assertFalse(depth.apply_depth_message(state, second, now=2.0))
        self.assertFalse(state.futures_depth_synced)
        self.assertEqual(state.futures_depth_gap_count, 1)

    def test_partial_snapshot_boundary_churn_is_not_consumption(self):
        state = self.state()
        first = {
            'u': 10, 'pu': 0,
            'b': [['100', '1'], ['99', '2']],
            'a': [['101', '1'], ['102', '2']],
        }
        second = {
            'u': 11, 'pu': 10,
            'b': [['101', '3'], ['100', '1']],
            'a': [['101.5', '3'], ['102', '2']],
        }
        self.assertTrue(depth.apply_depth_message(state, first, now=1.0))
        self.assertTrue(depth.apply_depth_message(state, second, now=1.1))

        metrics = state.futures_depth_metrics
        self.assertEqual(metrics['bid_removed'], 0.0)
        self.assertEqual(metrics['ask_removed'], 0.0)
        self.assertEqual(metrics['bid_removed_ambiguous_boundary'], 2.0)
        self.assertEqual(metrics['ask_removed_ambiguous_boundary'], 1.0)


if __name__ == '__main__':
    unittest.main()
