import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / '4_nghien_cuu_ai' / 'ml_meta' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'test_{name}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scout = load('scout')
artifact = load('artifact')
policy = load('policy')
merger = load('merge_dataset')
labeler = load('label_actions')


class ScoutTests(unittest.TestCase):
    def snapshot(self):
        return SimpleNamespace(
            symbol='BTCUSDT', snapshot_time=1000.0, structure_version=7,
            best_bid=64000.0, best_ask=64000.1, atr_1m=100.0,
            poc=63950.0, vah=64100.0, val=63900.0,
            swing_high_m15=64200.0, swing_low_m15=63800.0,
            structure_broken_level=0.0, closed_m1_extrema=[],
            trend_m15='NEUTRAL', obi_top3=0.1, obi_top10=0.2,
            current_cvd_buy_3s=2.0, current_cvd_sell_3s=1.0,
            current_vol_3s=3.0, continuous_m15={'trend_strength': 0.3},
            bids_top_10=[[64000.0, 0.5]], asks_top_10=[[64000.1, 0.7]],
        )

    def score(self):
        side = {'score': 55, 'confidence': 0.6, 'activation': 0.5,
                'trade_power': 16.5, 'microflow_timing': 0.1,
                'impulse_conflict': 0.0, 'retest_fit': 0.8}
        return {'version': 'CONTINUOUS_V2', 'sides': {'LONG': side, 'SHORT': side},
                'momentum_state': 0.2, 'momentum_breakdown': {'price': 0.2},
                'data_confidence': 0.8}

    def test_symmetric_ladder_dedupes_and_keeps_gtx(self):
        snap = self.snapshot()
        long = scout.action_ladder(snap, 'LONG', scout.select_anchor(snap, 'LONG'), 0.1)
        short = scout.action_ladder(snap, 'SHORT', scout.select_anchor(snap, 'SHORT'), 0.1)
        self.assertEqual(len({row['price'] for row in long if row['kind'] == 'MAKER'}),
                         len([row for row in long if row['kind'] == 'MAKER']))
        self.assertTrue(all(row['price'] < snap.best_ask for row in long if row['kind'] == 'MAKER'))
        self.assertTrue(all(row['price'] > snap.best_bid for row in short if row['kind'] == 'MAKER'))
        self.assertEqual(long[0]['queue_ahead_qty'], 0.5)

    def test_rows_are_both_sides_and_never_live_authority(self):
        rows = scout.build_rows(self.snapshot(), self.score(), 'NEUTRAL-MOMENTUM', 0.1,
                                run_id='run', code_version='code')
        self.assertEqual({row['side'] for row in rows}, {'LONG', 'SHORT'})
        self.assertTrue(all(not row['live_authority'] for row in rows))
        self.assertNotIn('POC', {row['anchor_source'] for row in rows})

    def test_artifact_fails_closed(self):
        with patch.dict(os.environ, {'SMC_ML_META_MODE': 'BOUNDED_ACTION'}):
            self.assertEqual(artifact.authority(artifact.requested_mode(), None)['mode'], 'SHADOW')

    def test_self_declared_promotion_without_metrics_fails_closed(self):
        fake = {'promotion_pass': True, 'approved_mode': 'BOUNDED_ACTION'}
        decision = artifact.authority('BOUNDED_ACTION', fake)
        self.assertEqual(decision['mode'], 'SHADOW')
        self.assertIn('WALK_FORWARD_BLOCKS', decision['gate_reasons'])

    def test_minimum_quantity_never_upsizes(self):
        result = policy.executable_size(1.0, equity=4888.0, btc_price=64000.0)
        self.assertEqual(result['action'], 'WATCH')
        self.assertAlmostEqual(result['required_pct'], 1.3093, places=3)

    def test_market_needs_lcb_dominance_over_maker_ucb(self):
        actions = [
            {'action_id': 'maker', 'kind': 'MAKER', 'utility_lcb': 3, 'utility_ucb': 5},
            {'action_id': 'market', 'kind': 'MARKET', 'utility_lcb': 5.2, 'utility_ucb': 6},
        ]
        self.assertEqual(policy.choose_action(actions, 0.5)['action_id'], 'maker')
        actions[1]['utility_lcb'] = 5.6
        self.assertEqual(policy.choose_action(actions, 0.5)['action_id'], 'market')

    def test_legacy_duplicate_is_one_unresolved_sample(self):
        event = {'ts': 10.0, 'event': 'RADAR_WATCH', 'payload': {'setup_id': 's1'}}
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / 'test.sqlite'
            import sqlite3
            db = sqlite3.connect(db_path)
            db.execute('''CREATE TABLE events (signature TEXT PRIMARY KEY, run_id TEXT,
                opportunity_id TEXT, event_type TEXT, decision_time REAL, payload_hash TEXT,
                code_version TEXT, scorer_version TEXT, train_eligible INTEGER,
                source TEXT, payload TEXT, duplicate_count INTEGER)''')
            self.assertTrue(merger._insert_event(db, event, 'a'))
            self.assertFalse(merger._insert_event(db, event, 'backup'))
            row = db.execute('SELECT COUNT(*), MAX(duplicate_count), MAX(train_eligible) FROM events').fetchone()
            self.assertEqual(row, (1, 1, 0))
            db.close()

    def test_touch_without_queue_consumption_is_not_fill(self):
        action = {'price': 100.0, 'queue_ahead_qty': 2.0}
        trades = [{'_event_time_ms': 1, 'p': '100.0', 'q': '1.0', 'm': True}]
        self.assertEqual(labeler._maker_fill(action, 'LONG', trades)[0], False)
        trades.append({'_event_time_ms': 2, 'p': '100.0', 'q': '1.1', 'm': True})
        filled, _, basis = labeler._maker_fill(action, 'LONG', trades)
        self.assertTrue(filled)
        self.assertEqual(basis, 'QUEUE_CONSUMED')

    def test_trade_through_is_conservative_fill(self):
        action = {'price': 100.0, 'queue_ahead_qty': 100.0}
        trade = [{'_event_time_ms': 1, 'p': '99.9', 'q': '0.1', 'm': True}]
        self.assertEqual(labeler._maker_fill(action, 'LONG', trade)[2], 'TRADE_THROUGH')

    def test_toxicity_uses_15_seconds_after_fill_and_keeps_neither(self):
        flat = [{'_event_time_ms': 1000, 'mid_close': 100.0},
                {'_event_time_ms': 15000, 'mid_close': 100.01}]
        label = labeler._path_label('LONG', 100.0, flat, 1000)
        self.assertEqual(label['toxicity_outcome_15s'], 'NEITHER')
        self.assertFalse(label['toxic_15s'])
        adverse = flat + [{'_event_time_ms': 16000, 'mid_close': 99.95}]
        label = labeler._path_label('LONG', 100.0, adverse, 1000)
        self.assertEqual(label['toxicity_outcome_15s'], 'ADVERSE')


if __name__ == '__main__':
    unittest.main()
