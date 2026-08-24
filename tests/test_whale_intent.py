import unittest
from types import SimpleNamespace

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


path = Path(__file__).parents[1] / '2_suy_luan_mapping' / 'whale_intent.py'
spec = spec_from_file_location('test_whale_intent_module', path)
whale = module_from_spec(spec)
spec.loader.exec_module(whale)


def state(now=100.0):
    return SimpleNamespace(
        cvd_buy=0.0, cvd_sell=0.0,
        coinbase_cvd_buy_total=0.0, coinbase_cvd_sell_total=0.0,
        futures_cvd_buy_total=0.0, futures_cvd_sell_total=0.0,
        long_liquidation_quote_total=0.0, short_liquidation_quote_total=0.0,
        best_bid=99.9, best_ask=100.1, coinbase_price=100.0,
        execution_best_bid=99.9, execution_best_ask=100.1,
        open_interest=1000.0,
        thoi_gian_tick_cuoi=now, thoi_gian_coinbase_ticker_cuoi=now,
        execution_price_time=now, thoi_gian_dong_tien_cuoi=now,
        thoi_gian_dong_tien_futures_cuoi=now, futures_depth_updated_at=now,
        thoi_gian_vi_mo_cuoi=now, futures_depth_synced=True,
        futures_depth_gap_count=0, futures_depth_epoch=1, coinbase_flow_epoch=1,
        futures_depth_metrics={
            'imbalance_top20': 0.25, 'ask_removed': 2.0,
            'bid_removed': 0.0, 'bid_replenished': 1.0,
            'ask_replenished': 0.0,
        },
    )


class WhaleIntentTests(unittest.TestCase):
    def test_cash_led_release_builds_catch_contract(self):
        s = state()
        engine = whale.WhaleIntentEngine()
        engine.evaluate(s, now=100.0)
        s.cvd_buy = 3.0
        s.coinbase_cvd_buy_total = 2.0
        s.futures_cvd_buy_total = 4.0
        s.best_bid, s.best_ask = 100.1, 100.3
        s.coinbase_price = 100.2
        s.execution_best_bid, s.execution_best_ask = 100.1, 100.3
        snap = engine.evaluate(s, now=100.2)
        self.assertEqual(snap['state'], 'RELEASE')
        self.assertEqual(snap['lane'], 'CATCH')
        result = engine.entry_result(snap)
        self.assertEqual(result['decision'], 'GO')
        self.assertEqual(result['s_votes']['S1_cross_venue_price_acceptance']['status'], 'PASS')

    def test_perp_only_move_is_trap(self):
        s = state()
        engine = whale.WhaleIntentEngine()
        engine.evaluate(s, now=100.0)
        s.cvd_buy = 1.0
        s.futures_cvd_buy_total = 4.0
        s.execution_best_bid, s.execution_best_ask = 100.2, 100.4
        snap = engine.evaluate(s, now=100.2)
        self.assertNotEqual(snap['lane'], 'CATCH')

    def test_pressure_is_reachable_before_depth_consumption(self):
        s = state()
        engine = whale.WhaleIntentEngine()
        engine.evaluate(s, now=100.0)
        s.cvd_buy = 3.0
        s.coinbase_cvd_buy_total = 2.0
        s.futures_cvd_buy_total = 4.0
        s.best_bid, s.best_ask = 100.1, 100.3
        s.coinbase_price = 100.2
        s.execution_best_bid, s.execution_best_ask = 100.1, 100.3
        s.futures_depth_metrics['ask_removed'] = 0.0

        snap = engine.evaluate(s, now=100.2)

        self.assertEqual(snap['state'], 'PRESSURE')
        self.assertEqual(snap['lane'], 'SHADOW_PROBE')
        self.assertIn('EXECUTED_FLOW', snap['evidence'])
        self.assertNotIn('DEPTH_CONSUMPTION', snap['evidence'])

    def test_dust_flow_cannot_authorize_catch(self):
        s = state()
        engine = whale.WhaleIntentEngine()
        engine.evaluate(s, now=100.0)
        s.cvd_buy = 0.00000004
        s.coinbase_cvd_buy_total = 0.00000004
        s.futures_cvd_buy_total = 0.00000004
        s.best_bid, s.best_ask = 100.1, 100.3
        s.coinbase_price = 100.2
        s.execution_best_bid, s.execution_best_ask = 100.1, 100.3

        snap = engine.evaluate(s, now=100.2)

        self.assertEqual(snap['flow_volume_floor_btc'], 0.02)
        self.assertNotEqual(snap['lane'], 'CATCH')
        self.assertIsNone(engine.entry_result(snap))

    def test_one_opportunity_can_only_be_claimed_once(self):
        s = state()
        s.whale_opportunity_count = 7
        snapshot = {"opportunity_id": 7}
        self.assertTrue(whale.WhaleIntentEngine.claim_opportunity(snapshot, s))
        self.assertEqual(s.whale_last_consumed_opportunity_id, 7)
        self.assertFalse(whale.WhaleIntentEngine.claim_opportunity(snapshot, s))
        self.assertTrue(whale.WhaleIntentEngine.claim_opportunity(
            {"opportunity_id": 8}, s
        ))

    def test_restored_position_consumes_same_side_episode_only(self):
        s = state()
        s.whale_last_consumed_opportunity_id = 7
        pos = SimpleNamespace(active=True, side="LONG", whale_opportunity_id=7)
        same_side = {
            "state": "RELEASE", "side": "LONG", "opportunity_id": 8,
        }

        self.assertTrue(
            whale.WhaleIntentEngine.adopt_position_opportunity(same_side, s, pos)
        )
        self.assertEqual(s.whale_last_consumed_opportunity_id, 8)
        self.assertEqual(pos.whale_opportunity_id, 8)
        self.assertFalse(whale.WhaleIntentEngine.claim_opportunity(same_side, s))

        reversal = {
            "state": "RELEASE", "side": "SHORT", "opportunity_id": 9,
        }
        self.assertFalse(
            whale.WhaleIntentEngine.adopt_position_opportunity(reversal, s, pos)
        )
        self.assertTrue(whale.WhaleIntentEngine.claim_opportunity(reversal, s))

    def test_stale_depth_fails_closed(self):
        s = state()
        s.futures_depth_updated_at = 90.0
        snap = whale.WhaleIntentEngine().evaluate(s, now=100.0)
        self.assertIn('FEED_INVALID', snap['vetoes'])
        self.assertEqual(snap['state'], 'INVALID')

    def test_wall_removal_without_matching_fills_is_spoof_veto(self):
        s = state()
        engine = whale.WhaleIntentEngine()
        engine.evaluate(s, now=100.0)
        s.cvd_buy = 3.0
        s.coinbase_cvd_buy_total = 3.0
        s.best_bid, s.best_ask = 100.1, 100.3
        s.coinbase_price = 100.2
        s.execution_best_bid, s.execution_best_ask = 100.1, 100.3
        snap = engine.evaluate(s, now=100.2)
        self.assertIn('WALL_WITHDRAWAL_WITHOUT_EXECUTED_FLOW', snap['vetoes'])
        self.assertNotEqual(snap['lane'], 'CATCH')

    def test_reversal_is_exhaustion_of_prior_whale_side(self):
        s = state()
        engine = whale.WhaleIntentEngine()
        engine.evaluate(s, now=100.0)
        s.cvd_buy = s.coinbase_cvd_buy_total = s.futures_cvd_buy_total = 4.0
        s.best_bid, s.best_ask = 100.1, 100.3
        s.coinbase_price = 100.2
        s.execution_best_bid, s.execution_best_ask = 100.1, 100.3
        self.assertEqual(engine.evaluate(s, now=100.2)['state'], 'RELEASE')

        s.cvd_sell = s.coinbase_cvd_sell_total = s.futures_cvd_sell_total = 12.0
        s.best_bid, s.best_ask = 99.7, 99.9
        s.coinbase_price = 99.8
        s.execution_best_bid, s.execution_best_ask = 99.7, 99.9
        snap = engine.evaluate(s, now=100.4)
        self.assertEqual(snap['state'], 'EXHAUSTION')
        self.assertEqual(snap['side'], 'LONG')
        self.assertEqual(snap['lane'], 'NONE')


if __name__ == '__main__':
    unittest.main()
