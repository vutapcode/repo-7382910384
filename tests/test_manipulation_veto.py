import importlib.util
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if not (ROOT / '2_suy_luan_mapping' / 'map_so_lenh.py').is_file():
    raise unittest.SkipTest(
        'legacy SMC manipulation stack is retired from the Tier-S runtime'
    )


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ram = load('manipulation_ram', 'loi_he_thong/bo_nho_ram.py')
orderbook = load('manipulation_book', '2_suy_luan_mapping/map_so_lenh.py')
veto = load(
    'manipulation_veto',
    '2_suy_luan_mapping/tong_ket_chi_huy/kiem_duyet_veto.py',
)
score = load(
    'manipulation_score',
    '2_suy_luan_mapping/tong_ket_chi_huy/cham_diem.py',
)
shark = load(
    'manipulation_shark',
    '2_suy_luan_mapping/tong_ket_chi_huy/shark_context.py',
)


class ManipulationResistantVetoTests(unittest.TestCase):
    def test_raw_wall_pull_is_advisory_not_hard_veto(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.best_bid, state.best_ask = 100.0, 100.01
        state.wall_pull_flag = {
            'active': True,
            'side': 'buy',
            'ts': now,
            'event_id': 'wall:raw:buy',
            'classification': 'UNCONFIRMED_WALL_PULL',
            'confirmed_for_veto': False,
        }

        self.assertFalse(veto.kiem_tra_veto(state, 'LONG')[0])
        result = score.cham_diem(
            state,
            {'mode': 'TREND-PULLBACK', 'modes': ['TREND-PULLBACK']},
            'LONG',
        )
        self.assertIn('WALL_PULL_UNCONFIRMED', result['advisory']['adverse'])
        self.assertEqual(result['advisory']['size_nerf_pct'], 20)

    def test_wall_boolean_alone_cannot_forge_hard_veto(self):
        state = ram.SharedState()
        state.wall_pull_flag = {
            'active': True,
            'side': 'buy',
            'ts': time.time(),
            'confirmed_for_veto': True,
        }
        self.assertFalse(veto.kiem_tra_veto(state, 'LONG')[0])

        state.wall_pull_flag.update({
            'price_confirmed': True,
            'flow_corroborated': True,
            'confirmation_version': 'WALL_PRICE_FLOW_V2',
            'price_displacement_bps': 0.5,
            'adverse_flow_share': 0.75,
        })
        blocked, reason = veto.kiem_tra_veto(state, 'LONG')
        self.assertTrue(blocked)
        self.assertIn('giá+flow', reason)

    def test_wall_mapper_requires_price_followthrough_and_flow_corroboration(self):
        state = ram.SharedState()
        state.start_time = time.time() - 20.0
        state.atr_1m = 1.0
        state.p95_value = 3.0
        state.prev_bids_dict = {100.0: 10.0}
        state.prev_asks_dict = {100.2: 10.0}
        state.prev_so_lenh_time_s = 100.0
        state.pending_pulls.append({
            'side': 'buy',
            'drop': 10.0,
            'cvd_start': 0.0,
            'cvd_buy_start': 0.0,
            'cvd_sell_start': 0.0,
            'time_s': 100.0,
            'window_start_s': 99.9,
            'obi_before': 0.0,
            'reference_price': 100.0,
            'atr_at_pull': 1.0,
        })
        state.trade_flow_timeline.extend([
            {'ts': 100.2, 'buy': 0.1, 'sell': 0.0},
            {'ts': 100.8, 'buy': 0.0, 'sell': 1.5},
        ])

        orderbook.cap_nhat_so_lenh({
            'bids': [[99.8, 1.0]],
            'asks': [[100.0, 10.0]],
            'timestamp': 101.0,
        }, state)

        wall = state.wall_pull_flag
        self.assertTrue(wall['active'])
        self.assertTrue(wall['price_confirmed'])
        self.assertTrue(wall['flow_corroborated'])
        self.assertTrue(wall['confirmed_for_veto'])
        self.assertGreaterEqual(wall['adverse_flow_share'], 0.65)
        self.assertGreaterEqual(wall['price_displacement_bps'], 0.25)

    def test_flash_flow_requires_share_and_material_net_delta(self):
        state = ram.SharedState()
        state.p95_value = 3.0
        state.vol_pct90 = 8.0

        # Opposing qty vượt threshold và share >65%, nhưng net vẫn dưới floor.
        state.current_cvd_sell_3s = 9.1
        state.current_cvd_buy_3s = 4.8
        self.assertFalse(veto.kiem_tra_veto(state, 'LONG')[0])

        # Burst một phía rõ ràng giữ nguyên quyền hard-VETO.
        state.current_cvd_sell_3s = 10.0
        state.current_cvd_buy_3s = 4.0
        blocked, reason = veto.kiem_tra_veto(state, 'LONG')
        self.assertTrue(blocked)
        self.assertIn('Flash Flow', reason)

    def test_confirmed_flash_creates_bounded_side_specific_memory(self):
        state = ram.SharedState()
        state.p95_value = 3.0
        state.vol_pct90 = 8.0
        state.current_cvd_sell_3s = 30.0
        state.current_cvd_buy_3s = 1.0
        record = veto.remember_confirmed_flash(
            state, state, 'LONG', now=1000.0
        )
        self.assertIsNotNone(record)
        self.assertEqual(record['blocked_bias'], 'LONG')
        self.assertGreater(record['severity'], 0.75)
        self.assertEqual(state.adverse_flow_memory_by_bias['SHORT'], {})

    def test_wall_and_obi_same_source_only_nerf_once(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.obi = 0.0
        state.obi_top3 = 0.8
        state.obi_top10 = 0.7
        state.wall_pull_flag = {
            'active': True,
            'side': 'buy',
            'ts': now,
            'event_id': 'wall:raw:dedupe',
            'confirmed_for_veto': False,
        }
        result = score.cham_diem(
            state,
            {'mode': 'TREND-PULLBACK', 'modes': ['TREND-PULLBACK']},
            'LONG',
        )
        self.assertEqual(
            result['advisory']['adverse'], ['WALL_PULL_UNCONFIRMED']
        )
        self.assertEqual(result['advisory']['size_nerf_pct'], 20)

    def test_guardian_ignores_raw_absorption_wall_and_balanced_flash(self):
        state = ram.SharedState()
        now = time.time()
        state.p95_value = 3.0
        state.vol_pct90 = 8.0
        state.current_cvd_sell_3s = 9.1
        state.current_cvd_buy_3s = 4.8
        state.absorption_event = {
            'active': True, 'side': 'sell', 'ts': now,
            'event_id': 'raw-absorption',
        }
        state.wall_pull_flag = {
            'active': True, 'side': 'buy', 'ts': now,
            'confirmed_for_veto': False,
        }
        context = shark.evaluate(state, 'LONG', now)
        self.assertEqual(context['status'], 'NEUTRAL')
        self.assertNotIn('FLASH_FLOW', context['adverse'])
        self.assertNotIn('BOOK', context['adverse'])
        self.assertNotIn('ABSORPTION', context['adverse'])

    def test_guardian_accepts_only_confirmed_absorption_reaction(self):
        state = ram.SharedState()
        now = time.time()
        state.absorption_reaction = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'absreact:confirmed',
        }
        context = shark.evaluate(state, 'LONG', now)
        self.assertIn('ABSORPTION_REACTION', context['support'])


if __name__ == '__main__':
    unittest.main()
