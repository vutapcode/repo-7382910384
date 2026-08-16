import asyncio
import importlib.util
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


radar = load('breakout_economics_radar', '2_suy_luan_mapping/map_gia_tick.py')
policy = load(
    'breakout_economics_policy',
    '2_suy_luan_mapping/tong_ket_chi_huy/chinh_sach_breakout.py',
)
executor = load('breakout_economics_executor', '3_thuc_thi/dat_lenh.py')


class BreakoutEconomicsTests(unittest.TestCase):
    def state(self):
        return SimpleNamespace(
            symbol='BTCUSDT', structure_version=7,
            structure_broken_level=100.0, trend_m15='BULLISH',
            swing_high_m15=120.0, swing_low_m15=80.0,
            vah=110.0, val=90.0, klines_m1=[], klines_m15=[],
            exchange_filters={'tick_size': 0.1},
            breakout_opportunities={}, breakout_opportunity_sequence=0,
        )

    def candidate(self, event_id):
        return {
            'key': 'TRANSITION-BREAKOUT:LONG',
            'zone_id': 'TRANSITION-BREAKOUT:LONG',
            'mode': 'TRANSITION-BREAKOUT', 'bias': 'LONG',
            'zone': 100.0, 'kind': 'breakout',
            'breakout_event_id': event_id,
        }

    def test_consecutive_m1_events_share_one_opportunity(self):
        state = self.state()
        first = self.candidate('breakout:m1:1')
        second = self.candidate('breakout:m1:2')
        one = radar._coalesce_breakout_opportunity(
            state, first, 100.2, 2.0, 10.0, 20.0,
        )
        two = radar._coalesce_breakout_opportunity(
            state, second, 100.3, 2.0, 11.0, 21.0,
        )
        self.assertEqual(one['opportunity_id'], two['opportunity_id'])
        self.assertEqual(len(state.breakout_opportunities), 1)
        self.assertEqual(two['event_ids'], ['breakout:m1:1', 'breakout:m1:2'])

    def test_structure_revision_does_not_split_same_opportunity(self):
        state = self.state()
        first = self.candidate('m15:neutral-break:LONG:100.00000000')
        one = radar._coalesce_breakout_opportunity(
            state, first, 100.2, 2.0, 10.0, 20.0,
        )
        state.structure_version += 1
        second = self.candidate('m15:neutral-break:LONG:100.00000000')
        two = radar._coalesce_breakout_opportunity(
            state, second, 100.3, 2.0, 11.0, 21.0,
        )
        self.assertEqual(one['opportunity_id'], two['opportunity_id'])
        self.assertEqual(len(state.breakout_opportunities), 1)

    def test_shadow_only_survives_opportunity_coalescing(self):
        state = self.state()
        candidate = self.candidate('m15:neutral-break:LONG:100.00000000')
        candidate['advisory_only'] = True
        radar._coalesce_breakout_opportunity(
            state, candidate, 100.2, 2.0, 10.0, 20.0,
        )
        active = radar._active_opportunity_candidates(state, 21.0)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]['advisory_only'])

    def test_retest_must_hold_before_passive_entry(self):
        opportunity = {
            'direction': 'LONG', 'level': 100.0, 'state': 'WAIT_RETEST',
            'created_mono': 10.0, 'expires_mono': 400.0,
            'retest_since_mono': None, 'market_chase_allowed': False,
        }
        state, reason = radar._advance_breakout_opportunity(
            opportunity, 100.1, 2.0, 11.0,
        )
        self.assertEqual((state, reason), ('WAIT_RETEST', 'RETEST_TOUCH'))
        state, reason = radar._advance_breakout_opportunity(
            opportunity, 100.1, 2.0, 11.31,
        )
        self.assertEqual((state, reason), ('FULL_ARM', 'RETEST_HELD'))
        self.assertEqual(opportunity['entry_style'], 'PASSIVE_RETEST')
        self.assertEqual(opportunity['passive_entry_price'], 100.0)

    def test_chase_requires_target_above_full_cost_floor(self):
        state = self.state()
        too_close = policy.evaluate(state, 119.9, 'LONG', 0.1)
        sufficient = policy.evaluate(state, 100.0, 'LONG', 0.1)
        self.assertFalse(too_close['market_chase_allowed'])
        self.assertTrue(sufficient['market_chase_allowed'])
        self.assertGreaterEqual(
            sufficient['target_distance_bps'],
            sufficient['minimum_raw_target_bps'],
        )

    def test_passive_entry_uses_post_only_and_cancels_unfilled(self):
        class API:
            def __init__(self):
                self.order = None
                self.cancelled = False

            async def new_order(self, *args, **kwargs):
                self.order = (args, kwargs)
                return {'orderId': 9, 'status': 'NEW', 'executedQty': '0'}, 200

            async def query_order(self, *args):
                return {'orderId': 9, 'status': 'NEW', 'executedQty': '0'}, 200

            async def cancel_order(self, symbol, order_id):
                self.cancelled = True
                return {'orderId': order_id}, 200

        api = API()
        setup = {
            'setup_id': 'setup-passive', 'generation': 1,
            'state': 'EXECUTING',
        }
        state = SimpleNamespace(
            best_bid=99.9, best_ask=100.1,
            execution_best_bid=99.9, execution_best_ask=100.1,
            execution_price_time=time.time(), system_ready=True,
            trading_enabled=True, active_setups={'x': setup},
        )
        signal = {
            'passive_entry_price': 100.0,
            'client_order_id': 'smc-passive-test',
            'setup_id': 'setup-passive', 'setup_generation': 1,
            'continuous_score': {'confidence': 0.0, 'activation': 0.0},
        }
        with mock.patch.object(executor, 'PASSIVE_INTENT_BASE_SECONDS', 0.01):
            result, status, _ = asyncio.run(executor.submit_passive_entry(
                api, state, signal, 'BUY', 0.01, {}, 0.1,
            ))
        self.assertEqual(status, 204)
        self.assertTrue(api.cancelled)
        self.assertEqual(api.order[0][2], 'LIMIT')
        self.assertEqual(api.order[1]['timeInForce'], 'GTX')
        self.assertEqual(result['reason'], 'EXPIRED_UNFILLED')


if __name__ == '__main__':
    unittest.main()
