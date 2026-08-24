import asyncio
import importlib.util
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

if not (ROOT / '2_suy_luan_mapping' / 'map_dong_tien' / 'flash_flow.py').is_file():
    raise unittest.SkipTest(
        'legacy SMC workflow is retired from the Tier-S runtime'
    )


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ram = load('test_ram', 'loi_he_thong/bo_nho_ram.py')
execution_tick = load(
    'test_execution_tick',
    '1_tai_du_lieu/tai_gia_tick/tai_gia_tick.py',
)
delta = load('test_delta', '2_suy_luan_mapping/map_dong_tien/delta_cvd.py')
flash = load('test_flash', '2_suy_luan_mapping/map_dong_tien/flash_flow.py')
footprint = load('test_footprint', '2_suy_luan_mapping/map_dong_tien/footprint.py')
map_nen = load('test_map_nen', '2_suy_luan_mapping/map_nen_live.py')
map_so_lenh = load('test_map_so_lenh', '2_suy_luan_mapping/map_so_lenh.py')
map_vi_mo = load('test_map_vi_mo', '2_suy_luan_mapping/map_vi_mo.py')
reversal = load(
    'test_reversal',
    '2_suy_luan_mapping/tong_ket_chi_huy/reversal_context.py',
)
trend_context = load(
    'test_trend_context',
    '2_suy_luan_mapping/tong_ket_chi_huy/trend_context.py',
)
score = load('test_score', '2_suy_luan_mapping/tong_ket_chi_huy/cham_diem.py')
veto = load('test_veto', '2_suy_luan_mapping/tong_ket_chi_huy/kiem_duyet_veto.py')
commander = load('test_commander', '2_suy_luan_mapping/tong_ket_chi_huy/chi_huy_truong.py')
snapshot = load('test_snapshot', '2_suy_luan_mapping/tong_ket_chi_huy/decision_snapshot.py')
shark = load('test_shark', '2_suy_luan_mapping/tong_ket_chi_huy/shark_context.py')
radar = load('test_radar', '2_suy_luan_mapping/map_gia_tick.py')
structure = load('test_structure', '2_suy_luan_mapping/map-nen-offline/BOS_CHoCH.py')
volume_profile = load(
    'test_volume_profile',
    '2_suy_luan_mapping/map-nen-offline/POC_VAH_VAL.py',
)
mode_selector = load(
    'test_mode_selector',
    '2_suy_luan_mapping/tong_ket_chi_huy/chon_che_do.py',
)
risk = load('test_risk', '3_thuc_thi/quan_ly_vi_the/tinh_toan_rui_ro.py')
guardian = load('test_guardian', '3_thuc_thi/ve_si_lenh/bao_ve_khan_cap.py')
watchdog = load('test_watchdog', '3_thuc_thi/giam_sat_he_thong.py')
executor = load('test_executor', '3_thuc_thi/dat_lenh.py')
reconcile = load('test_reconcile', '3_thuc_thi/quan_ly_vi_the/dong_bo_trang_thai.py')
economic = load('test_economic', '2_suy_luan_mapping/tong_ket_chi_huy/kinh_te_lenh.py')
journal = load('test_journal', '3_thuc_thi/quan_ly_vi_the/nhat_ky_giao_dich.py')


class DataContractTests(unittest.TestCase):
    def test_kline_jitter_budget_is_consistent_across_live_gates(self):
        self.assertEqual(watchdog.FEEDS['Kline'][1], 8.0)
        self.assertEqual(
            snapshot.FEED_MAX_AGE['thoi_gian_nen_cuoi'],
            watchdog.FEEDS['Kline'][1],
        )

    def test_execution_ticker_accepts_ws_and_rest_schema(self):
        state = ram.SharedState()
        self.assertTrue(execution_tick._apply_execution_book_ticker(
            {'b': '100.0', 'a': '100.1'}, state,
        ))
        first_ts = state.execution_price_time
        self.assertTrue(execution_tick._apply_execution_book_ticker(
            {'bidPrice': '101.0', 'askPrice': '101.1'}, state,
        ))
        self.assertEqual(state.execution_best_bid, 101.0)
        self.assertGreaterEqual(state.execution_price_time, first_ts)
        self.assertFalse(execution_tick._apply_execution_book_ticker(
            {'bidPrice': '102.0', 'askPrice': '101.0'}, state,
        ))

    def test_trade_timestamp_drives_all_mappers(self):
        state = ram.SharedState()
        state.best_bid = 64000.0
        trade = {
            'gia': 64000.0,
            'khoi_luong': 1.0,
            'ban_chu_dong': False,
            'thoi_gian_ms': int(time.time() * 1000),
        }
        delta.cap_nhat_cvd(trade, state)
        flash.cap_nhat_nguong_ca_map(trade, state)
        footprint.cap_nhat_footprint(trade, state)
        self.assertEqual(state.dt_total_count, 1)
        self.assertIsNotNone(state.fp_current_candle)
        self.assertEqual(state.cvd_buy, 1.0)

    def test_cvd_window_prunes_without_silent_deque_loss(self):
        state = ram.SharedState()
        base = int(time.time() * 1000) - 1801 * 1000
        for second in range(1802):
            delta.cap_nhat_cvd({
                'gia': 1.0,
                'khoi_luong': 1.0,
                'ban_chu_dong': False,
                'thoi_gian_ms': base + second * 1000,
            }, state)
        self.assertLessEqual(state.cvd_buy_30m, 1801.0)
        self.assertEqual(len(state.cvd_30m_buffer), int(state.cvd_buy_30m))

    def test_closed_m1_and_scoring_do_not_raise(self):
        state = ram.SharedState()
        state.current_mode = {'bias': 'LONG'}
        state.swing_low_m15 = 99.0
        state.swing_high_m15 = 110.0
        state.atr_1m = 1.0
        candle = {'t': 1, 'o': 100.0, 'h': 101.0, 'l': 98.0, 'c': 100.5, 'v': 1.0, 'x': True}
        map_nen.cap_nhat_nen_m1(candle, state)
        result = score.cham_diem(state, {}, 'LONG')
        self.assertIn('core', result)

    def test_footprint_diagonal_is_symmetric_for_buy_and_sell(self):
        buy_rows = {
            99: [0.0, 1.0],
            100: [3.1, 1.0],
            101: [3.1, 1.0],
            102: [3.1, 0.0],
        }
        sell_rows = {
            100: [1.0, 3.1],
            101: [1.0, 3.1],
            102: [1.0, 3.1],
            103: [1.0, 0.0],
        }
        self.assertEqual(
            footprint._tinh_stacked_imbalances(buy_rows)[0]['direction'], 'buy'
        )
        self.assertEqual(
            footprint._tinh_stacked_imbalances(sell_rows)[0]['direction'], 'sell'
        )

    def test_footprint_realtime_only_activates_near_zone(self):
        state = ram.SharedState()
        now_ms = int(time.time() * 1000)
        rows = (
            (495.0, 1.0, True),
            (500.0, 3.1, False),
            (500.0, 1.0, True),
            (505.0, 3.1, False),
            (505.0, 1.0, True),
            (510.0, 3.1, False),
        )
        for price, qty, buyer_maker in rows:
            footprint.cap_nhat_footprint({
                'gia': price, 'khoi_luong': qty,
                'ban_chu_dong': buyer_maker, 'thoi_gian_ms': now_ms,
            }, state)
        self.assertIsNone(state.fp_last_imbalance.get('dir'))

        state.arm_state = 'PRE_ARM'
        state.fp_last_eval_mono = 0.0
        footprint.cap_nhat_footprint({
            'gia': 510.0, 'khoi_luong': 0.01,
            'ban_chu_dong': False, 'thoi_gian_ms': now_ms,
        }, state)
        self.assertEqual(state.fp_last_imbalance['dir'], 'buy')
        self.assertTrue(state.fp_last_imbalance['realtime'])

    def test_stale_footprint_does_not_score(self):
        state = ram.SharedState()
        state.cvd_buy_30m = 2.0
        state.cvd_sell_30m = 1.0
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': time.time() - 16.0, 'event_id': 'fp:stale',
        }
        result = score.cham_diem(state, {}, 'LONG')
        self.assertEqual(result['core'], 0)

    def test_trend_flow_family_requires_30m_and_dominant_3s_confirmation(self):
        state = ram.SharedState()
        state.cvd_buy_30m, state.cvd_sell_30m = 20.0, 10.0
        state.vol_pct90 = 10.0

        # CVD 30m chậm không được tự tạo CORE.
        self.assertEqual(score.cham_diem(state, {}, 'LONG')['core'], 0)

        # Spike 3s chỉ hơi nghiêng 51/49 cũng không đủ xác nhận.
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 10.2
        state.current_cvd_sell_3s = 9.8
        self.assertEqual(score.cham_diem(state, {}, 'LONG')['core'], 0)

        # Context 30m + volume P90 + dominance rõ ràng chỉ tạo một CORE.
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        result = score.cham_diem(state, {}, 'LONG')
        self.assertEqual(result['core'], 1)
        self.assertIn('xác nhận kép', result['detail'][0])

    def test_flow_1s_buffer_is_compressed_and_bounded(self):
        state = ram.SharedState()
        base_ms = 1_000_000
        for second in range(220):
            delta.cap_nhat_cvd({
                'gia': 100.0, 'khoi_luong': 1.0,
                'ban_chu_dong': second % 2 == 0,
                'thoi_gian_ms': base_ms + second * 1000,
            }, state)
        self.assertLessEqual(len(state.flow_1s_buffer), 191)
        self.assertEqual(
            len({item['ts'] for item in state.flow_1s_buffer}),
            len(state.flow_1s_buffer),
        )

    def test_persistent_flow_requires_price_progress_and_detects_trap(self):
        def make_state(end_price):
            state = ram.SharedState()
            state.vol_pct90 = 0.5
            for second in range(41, 101):
                state.flow_1s_buffer.append({
                    'ts': second, 'buy': 2.0, 'sell': 0.2,
                })
            state.trend_price_history.extend([
                {'ts': 85.0, 'price': 100.0},
                {'ts': 100.0, 'price': end_price},
            ])
            return state

        confirmed = make_state(100.6)
        trend_context._update_persistent_flow(
            confirmed, now=100.0, price=100.6, atr=10.0,
        )
        self.assertTrue(confirmed.persistent_flow['active'])
        self.assertEqual(confirmed.persistent_flow['direction'], 'LONG')
        self.assertFalse(confirmed.flow_price_trap['active'])

        trapped = make_state(99.4)
        trend_context._update_persistent_flow(
            trapped, now=100.0, price=99.4, atr=10.0,
        )
        self.assertFalse(trapped.persistent_flow['active'])
        self.assertTrue(trapped.flow_price_trap['active'])
        self.assertEqual(trapped.flow_price_trap['blocked_bias'], 'LONG')

    def test_pullback_zone_requires_rejection_and_detects_acceptance(self):
        reaction = ram.SharedState()
        reaction.current_mode = {
            'modes': ['TREND-PULLBACK'], 'bias': 'LONG',
            'pullback_zones': [None, 100.0],
        }
        trend_context._update_zone_reaction(
            reaction, now=100.0, price=100.0, atr=10.0,
        )
        self.assertFalse(reaction.zone_reaction['active'])
        trend_context._update_zone_reaction(
            reaction, now=101.0, price=101.9, atr=10.0,
        )
        self.assertTrue(reaction.zone_reaction['active'])
        self.assertEqual(reaction.zone_reaction['direction'], 'LONG')

        acceptance = ram.SharedState()
        acceptance.current_mode = {
            'modes': ['TREND-PULLBACK'], 'bias': 'SHORT',
            'pullback_zones': [100.0],
        }
        trend_context._update_zone_reaction(
            acceptance, now=100.0, price=100.0, atr=10.0,
        )
        trend_context._update_zone_reaction(
            acceptance, now=100.1, price=101.1, atr=10.0,
        )
        trend_context._update_zone_reaction(
            acceptance, now=102.2, price=101.2, atr=10.0,
        )
        self.assertTrue(acceptance.zone_acceptance_trap['active'])
        self.assertEqual(
            acceptance.zone_acceptance_trap['blocked_bias'], 'SHORT'
        )

    def test_zone_acceptance_persists_until_buffered_reclaim(self):
        state = ram.SharedState()
        state.current_mode = {
            'modes': ['TREND-PULLBACK'], 'bias': 'LONG',
            'pullback_zones': [100.0],
        }
        trend_context._update_zone_reaction(
            state, now=100.0, price=100.0, atr=10.0,
        )
        trend_context._update_zone_reaction(
            state, now=100.1, price=98.9, atr=10.0,
        )
        trend_context._update_zone_reaction(
            state, now=102.2, price=98.8, atr=10.0,
        )
        first_id = state.zone_acceptance_trap['event_id']

        # Xa vùng và quá TTL cũ vẫn phải nhớ: giá chưa reclaim.
        trend_context._update_zone_reaction(
            state, now=120.0, price=95.0, atr=10.0,
        )
        self.assertTrue(state.zone_acceptance_trap['active'])
        self.assertEqual(state.zone_acceptance_trap['event_id'], first_id)
        self.assertEqual(state.zone_acceptance_trap['ts'], 120.0)

        # LONG chỉ hết bẫy khi vượt zone thêm 0.05 ATR.
        trend_context._update_zone_reaction(
            state, now=121.0, price=100.6, atr=10.0,
        )
        self.assertFalse(state.zone_acceptance_trap['active'])

    def test_obi_needs_persistence_to_boost_shark(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.obi = 0.6
        state.obi_top3 = 0.7

        # Một snapshot đẹp không đủ thưởng size.
        single = score.cham_diem(state, {}, 'LONG')
        self.assertEqual(single['shark'], 0)

        for index in range(8):
            state.obi_history.append((now - 0.1 * index, 0.5))
        persistent = score.cham_diem(state, {}, 'LONG')
        self.assertEqual(persistent['shark'], 1)
        self.assertTrue(
            persistent['evidence_quality']['obi']['persistent']
        )

    def test_shallow_obi_and_correlated_bounce_only_nerf_money(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.cvd_buy_30m, state.cvd_sell_30m = 20.0, 10.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': now, 'event_id': 'fp:correlated',
        }
        state.obi_top3 = 0.8
        state.obi = 0.0
        result = score.cham_diem(
            state,
            {'modes': ['TREND-PULLBACK'], 'mode': 'TREND-PULLBACK'},
            'LONG',
        )

        # Vẫn 2 CORE, vẫn được execute; hai cờ yếu chỉ hạ size 40%.
        self.assertEqual(result['core'], 2)
        self.assertTrue(commander._score_allows(
            'TREND-PULLBACK', result, state,
        ))
        self.assertEqual(result['shark'], 0)
        self.assertEqual(
            set(result['advisory']['adverse']),
            {'OBI_SHALLOW_MISMATCH', 'CORRELATED_BURST_FLOW_FOOTPRINT'},
        )
        self.assertEqual(result['advisory']['size_nerf_pct'], 40)
        self.assertEqual(commander._position_size('TREND-PULLBACK', result), 1)

    def test_new_flow_and_zone_are_independent_without_double_count(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.persistent_flow = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'persistent:1:LONG', 'ttl': 5.0,
        }
        state.zone_reaction = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'zone:1:LONG', 'ttl': 15.0,
        }
        mode = {
            'modes': ['TREND-PULLBACK', 'TREND-BREAKOUT'],
            'mode': 'TREND-PULLBACK',
        }
        result = score.cham_diem(state, mode, 'LONG')
        self.assertEqual(result['core'], 2)

        # Legacy 30m+3s cùng họ flow không được cộng CORE lần thứ hai.
        state.cvd_buy_30m, state.cvd_sell_30m = 20.0, 10.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        self.assertEqual(score.cham_diem(state, mode, 'LONG')['core'], 2)

        breakout = dict(mode, mode='TREND-BREAKOUT')
        self.assertEqual(score.cham_diem(state, breakout, 'LONG')['core'], 1)

    def test_trap_advisory_nerfs_money_not_entry_core(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.persistent_flow = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'persistent:2:LONG', 'ttl': 5.0,
        }
        state.zone_reaction = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'zone:2:LONG', 'ttl': 15.0,
        }
        state.flow_price_trap = {
            'active': True, 'blocked_bias': 'LONG', 'ts': now,
            'event_id': 'trap:1:LONG', 'ttl': 8.0,
        }
        result = score.cham_diem(
            state,
            {'modes': ['TREND-PULLBACK'], 'mode': 'TREND-PULLBACK'},
            'LONG',
        )
        self.assertEqual(result['core'], 2)
        self.assertEqual(result['advisory']['size_nerf_pct'], 20)
        self.assertEqual(commander._position_size('TREND-PULLBACK', result), 2)

    def test_neutral_fade_scores_reversal_evidence_not_trend_cvd(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.cvd_buy_30m = 1.0
        state.cvd_sell_30m = 10.0
        state.absorption_reaction = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'classification': 'PASSIVE_HOLD_REACTION',
            'event_id': 'absreact:1:LONG',
        }
        state.value_area_sweep = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'vasweep:1:LONG',
        }

        neutral = score.cham_diem(
            state, {'modes': ['NEUTRAL-FADE']}, 'LONG'
        )
        trend = score.cham_diem(
            state, {'modes': ['TREND-PULLBACK']}, 'LONG'
        )
        self.assertEqual(neutral['core'], 2)
        self.assertEqual(trend['core'], 0)
        self.assertEqual(
            neutral['event_ids'],
            ['absreact:1:LONG', 'vasweep:1:LONG'],
        )

    def test_value_area_sweep_requires_excursion_then_reclaim(self):
        state = ram.SharedState()
        state.current_mode = {'modes': ['NEUTRAL-FADE']}
        state.atr_1m = 10.0
        state.val = 100.0
        state.vah = 120.0
        state.best_bid, state.best_ask = 99.3, 99.5
        reversal.update(state, now=100.0)
        self.assertFalse(state.value_area_sweep.get('active'))

        state.best_bid, state.best_ask = 100.2, 100.4
        reversal.update(state, now=100.1)
        event = state.value_area_sweep
        self.assertTrue(event['active'])
        self.assertEqual(event['direction'], 'LONG')

    def test_absorption_becomes_core_only_after_price_reaction(self):
        state = ram.SharedState()
        state.atr_1m = 10.0
        state.best_bid, state.best_ask = 99.9, 100.1
        state.absorption_event = {
            'active': True, 'side': 'buy', 'ts': 100.0,
            'event_id': 'abs:buy:1', 'reference_price': 100.0,
            'atr_at_event': 10.0,
        }
        reversal.update(state, now=100.0)
        self.assertFalse(state.absorption_reaction.get('active'))

        state.best_bid, state.best_ask = 100.5, 100.7
        reversal.update(state, now=101.1)
        event = state.absorption_reaction
        self.assertTrue(event['active'])
        self.assertEqual(event['direction'], 'LONG')
        self.assertEqual(event['classification'], 'PASSIVE_HOLD_REACTION')

    def test_flow_price_divergence_detects_sell_exhaustion(self):
        state = ram.SharedState()
        state.atr_1m = 10.0
        state.vol_pct90 = 0.25
        state.best_bid, state.best_ask = 99.9, 100.1
        for index in range(17):
            state.cvd_sell = float(index)
            reversal.update(state, now=100.0 + index * 0.5)
        event = state.flow_divergence
        self.assertTrue(event['active'])
        self.assertEqual(event['direction'], 'LONG')
        self.assertLessEqual(abs(event['price_progress_atr']), 0.12)

    def test_absorption_correlation_uses_aggtrade_event_time(self):
        state = ram.SharedState()
        state.start_time = time.time() - 20.0
        state.best_bid, state.best_ask = 99.9, 100.1
        state.prev_bids_dict = {99.9: 10.0}
        state.prev_asks_dict = {100.1: 10.0}
        state.prev_so_lenh_time_s = 100.0
        state.pending_pulls.append({
            'side': 'buy', 'drop': 10.0, 'cvd_start': 0.0,
            'time_s': 100.0, 'window_start_s': 99.9, 'obi_before': 0.0,
        })
        state.trade_flow_timeline.extend([
            {'ts': 100.1, 'buy': 0.0, 'sell': 3.0},
            {'ts': 100.5, 'buy': 0.0, 'sell': 4.1},
            {'ts': 100.7, 'buy': 0.0, 'sell': 99.0},
        ])
        map_so_lenh.cap_nhat_so_lenh({
            'bids': [[99.9, 10.0]], 'asks': [[100.1, 10.0]],
            'timestamp': 100.6,
        }, state)
        self.assertTrue(state.absorption_event['active'])
        self.assertEqual(state.absorption_event['side'], 'buy')
        self.assertAlmostEqual(state.absorption_event['matched_qty'], 7.1)
        self.assertEqual(state.absorption_event['correlation_window_ms'], 599)

    def test_wall_pull_waits_full_adaptive_window(self):
        state = ram.SharedState()
        state.start_time = time.time() - 20.0
        state.best_bid, state.best_ask = 99.9, 100.1
        state.prev_bids_dict = {99.9: 10.0}
        state.prev_asks_dict = {100.1: 10.0}
        state.prev_so_lenh_time_s = 100.0
        state.obi = -0.2
        state.pending_pulls.append({
            'side': 'buy', 'drop': 10.0, 'cvd_start': 0.0,
            'time_s': 100.0, 'window_start_s': 99.9, 'obi_before': 0.0,
        })
        map_so_lenh.cap_nhat_so_lenh({
            'bids': [[99.9, 1.0]], 'asks': [[100.1, 10.0]],
            'timestamp': 100.6,
        }, state)
        self.assertFalse(state.wall_pull_flag['active'])
        self.assertGreaterEqual(len(state.pending_pulls), 1)

        state.prev_so_lenh_time_s = 100.6
        map_so_lenh.cap_nhat_so_lenh({
            'bids': [[99.9, 1.0]], 'asks': [[100.1, 10.0]],
            'timestamp': 101.0,
        }, state)
        self.assertTrue(state.wall_pull_flag['active'])

    def test_depth20_distinguishes_near_wall_from_broad_book(self):
        state = ram.SharedState()
        bids = [[100.0 - index * 0.1, 10.0 if index < 3 else 0.1]
                for index in range(20)]
        asks = [[100.1 + index * 0.1, 1.0 if index < 3 else 5.0]
                for index in range(20)]
        map_so_lenh.cap_nhat_so_lenh({
            'bids': bids, 'asks': asks, 'timestamp': time.time(),
        }, state)

        self.assertGreater(state.obi_top3, 0.3)
        self.assertLess(state.obi, 0.0)
        state.snapshot_time = time.time()
        quality = score.cham_diem(state, {}, 'LONG')['evidence_quality']['obi']
        self.assertTrue(quality['shallow_mismatch'])

    def test_macro_mapper_does_not_reprocess_same_rest_sample(self):
        state = ram.SharedState()
        state.best_bid, state.best_ask = 99.9, 100.1
        state.atr_1m = 10.0
        state.open_interest = 1000.0
        state.thoi_gian_vi_mo_cuoi = 100.0
        map_vi_mo.cap_nhat_vi_mo(state)
        revision = state.decision_revision
        state.open_interest = 1100.0
        map_vi_mo.cap_nhat_vi_mo(state)
        self.assertEqual(state.decision_revision, revision)
        self.assertEqual(state.prev_open_interest, 1000.0)

    def test_positioning_detects_cvd_divergence_and_liquidation_recovery(self):
        state = ram.SharedState()
        divergence_window = [
            {'ts': 0.0, 'price': 100.0, 'oi': 1000.0,
             'cvd_buy': 0.0, 'cvd_sell': 0.0},
            {'ts': 300.0, 'price': 111.0, 'oi': 1000.0,
             'cvd_buy': 10.0, 'cvd_sell': 30.0},
        ]
        map_vi_mo._update_cvd_divergence(
            state, divergence_window, now=300.0, atr=10.0
        )
        self.assertTrue(state.positioning_cvd_divergence['active'])
        self.assertEqual(state.positioning_cvd_divergence['direction'], 'SHORT')

        liquidation_window = [
            {'ts': 0.0, 'price': 100.0, 'oi': 1000.0},
            {'ts': 100.0, 'price': 99.0, 'oi': 1000.0},
            {'ts': 200.0, 'price': 80.0, 'oi': 990.0},
            {'ts': 250.0, 'price': 84.0, 'oi': 991.0},
        ]
        map_vi_mo._update_liquidation_recovery(
            state, liquidation_window, now=250.0, atr=10.0
        )
        self.assertTrue(state.liquidation_recovery['active'])
        self.assertEqual(state.liquidation_recovery['direction'], 'LONG')

    def test_positioning_advisory_only_nerfs_size_not_core(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.cvd_buy_30m, state.cvd_sell_30m = 2.0, 1.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': now, 'event_id': 'fp:advisory',
        }
        state.positioning_cvd_divergence = {
            'active': True, 'direction': 'SHORT', 'ts': now,
            'event_id': 'cvddiv5m:1:SHORT',
        }
        result = score.cham_diem(
            state, {'modes': ['TREND-PULLBACK']}, 'LONG'
        )
        self.assertEqual(result['core'], 2)
        self.assertEqual(result['shark'], 0)
        self.assertEqual(result['advisory']['size_nerf_pct'], 20)
        self.assertEqual(commander._position_size('TREND-PULLBACK', result), 2)


class LogicTests(unittest.TestCase):
    def test_pre_arm_freezes_zone_and_level_confirms(self):
        started = 1000.0
        state_name, probe, reason = radar.advance_arm_probe(
            104.0, 104.5, 100.0, 10.0, 'LONG', now_mono=started,
        )
        self.assertEqual(state_name, 'PRE_ARM')
        self.assertEqual(reason, 'ENTERED_PRE')
        self.assertEqual(probe['zone'], 100.0)

        state_name, probe, _ = radar.advance_arm_probe(
            103.9, 104.0, 100.5, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.FULL_ARM_DWELL_SECONDS + 0.01,
        )
        self.assertEqual(state_name, 'FULL_ARM')
        self.assertEqual(probe['zone'], 100.0)

    def test_pre_arm_recovers_directional_gap_but_not_wrong_way_bounce(self):
        state_name, _, reason = radar.advance_arm_probe(
            94.0, 106.0, 100.0, 10.0, 'LONG', now_mono=1000.0,
        )
        self.assertEqual(state_name, 'FULL_ARM')
        self.assertEqual(reason, 'DIRECTIONAL_CROSSING')

        state_name, probe, _ = radar.advance_arm_probe(
            100.0, 99.0, 100.0, 10.0, 'LONG', now_mono=1000.0,
        )
        self.assertEqual(state_name, 'PRE_ARM')
        state_name, _, reason = radar.advance_arm_probe(
            101.0, 100.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=1001.0,
        )
        self.assertEqual(state_name, 'PRE_ARM')
        self.assertEqual(reason, 'WAITING_APPROACH')

    def test_pre_arm_restarts_only_when_live_zone_moves_materially(self):
        _, probe, _ = radar.advance_arm_probe(
            110.0, 111.0, 100.0, 10.0, 'LONG', now_mono=1000.0,
        )
        _, probe, reason = radar.advance_arm_probe(
            116.0, 117.0, 108.0, 10.0, 'LONG', probe=probe,
            now_mono=1000.1,
        )
        self.assertEqual(reason, 'ZONE_MOVED_RESTART')
        self.assertEqual(probe['zone'], 108.0)

    def test_pre_arm_ttl_transitions_to_bounded_sweep_wait(self):
        started = 1000.0
        state_name, probe, _ = radar.advance_arm_probe(
            104.0, 105.0, 100.0, 10.0, 'LONG', now_mono=started,
        )
        self.assertEqual(state_name, 'PRE_ARM')

        state_name, probe, reason = radar.advance_arm_probe(
            104.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + 0.1,
        )
        self.assertEqual(state_name, 'SWEEP_WAIT')
        self.assertEqual(reason, 'SWEEP_WAIT_STARTED')

        state_name, same_probe, reason = radar.advance_arm_probe(
            104.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + 10.0,
        )
        self.assertEqual(state_name, 'SWEEP_WAIT')
        self.assertIs(same_probe, probe)
        self.assertEqual(reason, 'WAITING_SWEEP_OR_RECLAIM')

        state_name, probe, reason = radar.advance_arm_probe(
            116.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + 11.0,
        )
        self.assertEqual(state_name, 'IDLE')
        self.assertIsNone(probe)
        self.assertEqual(reason, 'LEFT_SWEEP_WAIT')

    def test_sweep_wait_catches_1016_val_reclaim_without_one_tick_trigger(self):
        started = 1000.0
        _, probe, _ = radar.advance_arm_probe(
            64933.7, 64935.0, 64897.645, 24.5857, 'LONG',
            now_mono=started,
        )
        state_name, probe, reason = radar.advance_arm_probe(
            64929.5, 64930.0, 64897.645, 24.5857, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + 0.1,
        )
        self.assertEqual((state_name, reason), ('SWEEP_WAIT', 'SWEEP_WAIT_STARTED'))

        state_name, probe, reason = radar.advance_arm_probe(
            64875.0, 64916.7, 64897.645, 24.5857, 'LONG', probe=probe,
            now_mono=started + 60.0,
        )
        self.assertEqual((state_name, reason), ('SWEEP_WAIT', 'SWEEP_DETECTED'))
        self.assertLess(probe['max_penetration_atr'], 1.0)

        state_name, probe, reason = radar.advance_arm_probe(
            64900.2, 64896.0, 64897.645, 24.5857, 'LONG', probe=probe,
            now_mono=started + 61.0,
        )
        self.assertEqual((state_name, reason), ('SWEEP_WAIT', 'RECLAIM_HOLD'))
        # Một tick reclaim rồi rơi lại không được arm.
        state_name, probe, reason = radar.advance_arm_probe(
            64896.0, 64900.2, 64897.645, 24.5857, 'LONG', probe=probe,
            now_mono=started + 61.1,
        )
        self.assertEqual(state_name, 'SWEEP_WAIT')
        self.assertEqual(reason, 'WAITING_SWEEP_OR_RECLAIM')

        _, probe, reason = radar.advance_arm_probe(
            64900.3, 64896.0, 64897.645, 24.5857, 'LONG', probe=probe,
            now_mono=started + 62.0,
        )
        self.assertEqual(reason, 'RECLAIM_HOLD')
        state_name, probe, reason = radar.advance_arm_probe(
            64902.0, 64900.3, 64897.645, 24.5857, 'LONG', probe=probe,
            now_mono=started + 62.0 + radar.RECLAIM_HOLD_SECONDS + 0.01,
        )
        self.assertEqual((state_name, reason), ('FULL_ARM', 'SWEEP_RECLAIM_CONFIRMED'))

    def test_sweep_wait_rejects_deep_acceptance_and_expires(self):
        started = 1000.0
        _, probe, _ = radar.advance_arm_probe(
            104.0, 105.0, 100.0, 10.0, 'LONG', now_mono=started,
        )
        _, probe, _ = radar.advance_arm_probe(
            104.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + 0.1,
        )
        state_name, probe, reason = radar.advance_arm_probe(
            84.9, 100.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + 31.0,
        )
        self.assertEqual((state_name, probe, reason), ('IDLE', None, 'SWEEP_TOO_DEEP'))

        _, probe, _ = radar.advance_arm_probe(
            104.0, 105.0, 100.0, 10.0, 'LONG', now_mono=started,
        )
        _, probe, _ = radar.advance_arm_probe(
            104.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + 0.1,
        )
        state_name, probe, reason = radar.advance_arm_probe(
            104.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + radar.PRE_ARM_TTL_SECONDS + radar.SWEEP_WAIT_SECONDS + 0.2,
        )
        self.assertEqual(state_name, 'SWEEP_WAIT_COOLDOWN')
        self.assertIsNotNone(probe)
        self.assertEqual(reason, 'SWEEP_WAIT_EXPIRED')
        state_name, same_probe, reason = radar.advance_arm_probe(
            104.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + 140.0,
        )
        self.assertEqual(state_name, 'SWEEP_WAIT_COOLDOWN')
        self.assertIs(same_probe, probe)
        self.assertEqual(reason, 'SWEEP_WAIT_TOMBSTONE')
        state_name, probe, reason = radar.advance_arm_probe(
            116.0, 104.0, 100.0, 10.0, 'LONG', probe=probe,
            now_mono=started + 141.0,
        )
        self.assertEqual((state_name, probe, reason), (
            'IDLE', None, 'LEFT_SWEEP_WAIT_TOMBSTONE'
        ))

    def test_m15_minor_sweep_keeps_bias_but_decisive_close_invalidates(self):
        def candle(index, high, low, close, close_time=1):
            return [index, close, high, low, close, 1.0, close_time]

        base = [
            candle(0, 100, 95, 98),
            candle(1, 110, 100, 105),
            candle(2, 105, 90, 95),
            candle(3, 120, 100, 110),
            candle(4, 110, 95, 100),
            candle(5, 115, 100, 105),
        ]
        minor = base + [candle(6, 116, 93, 94)]
        result = structure.get_macro_structure(
            minor, pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(result['trend'], 'BULLISH')
        self.assertEqual(result['transition'], 'NONE')

        decisive = base + [candle(6, 116, 91, 92)]
        result = structure.get_macro_structure(
            decisive, pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(result['trend'], 'NEUTRAL')
        self.assertEqual(result['transition'], 'BULLISH_INVALIDATED')
        self.assertEqual(result['break_streak'], 1)

    def test_second_closed_m15_break_opens_symmetric_transition(self):
        def candle(index, high, low, close, close_time=1):
            return [index, close, high, low, close, 1.0, close_time]

        base = [
            candle(0, 100, 95, 98), candle(1, 110, 100, 105),
            candle(2, 105, 90, 95), candle(3, 120, 100, 110),
            candle(4, 110, 95, 100), candle(5, 115, 100, 105),
        ]
        broken = base + [
            candle(6, 116, 91, 92), candle(7, 117, 90.5, 91),
        ]
        bearish = structure.get_macro_structure(
            broken, pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(bearish['transition'], 'TRANSITION_BEARISH')
        self.assertEqual(bearish['break_streak'], 2)
        self.assertEqual(bearish['broken_level'], 95.0)

        def invert(row):
            index, open_, high, low, close, volume, close_time = row
            return [
                index, 200 - open_, 200 - low, 200 - high,
                200 - close, volume, close_time,
            ]

        bullish = structure.get_macro_structure(
            [invert(row) for row in broken],
            pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(bullish['transition'], 'TRANSITION_BULLISH')
        self.assertEqual(bullish['break_streak'], 2)
        self.assertEqual(bullish['broken_level'], 105.0)

    def test_neutral_range_requires_two_closed_m15_breaks(self):
        def candle(index, high, low, close, close_time=1):
            return [index, close, high, low, close, 1.0, close_time]

        # Higher high + lower low keeps the structural classification NEUTRAL.
        base = [
            candle(0, 100, 95, 98), candle(1, 110, 100, 105),
            candle(2, 105, 90, 95), candle(3, 120, 100, 110),
            candle(4, 110, 85, 100), candle(5, 115, 95, 105),
        ]
        candidate = structure.get_macro_structure(
            base + [candle(6, 124, 110, 123)],
            pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(candidate['trend'], 'NEUTRAL')
        self.assertEqual(
            candidate['transition'], 'NEUTRAL_BREAKOUT_BULLISH_CANDIDATE'
        )
        self.assertEqual(candidate['break_streak'], 1)
        self.assertEqual(candidate['broken_level'], 120.0)

        confirmed = structure.get_macro_structure(
            base + [
                candle(6, 124, 110, 123),
                candle(7, 125, 121, 124),
            ],
            pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(
            confirmed['transition'], 'NEUTRAL_TRANSITION_BULLISH'
        )
        self.assertEqual(confirmed['break_streak'], 2)
        self.assertEqual(confirmed['broken_level'], 120.0)

        def invert(row):
            index, open_, high, low, close, volume, close_time = row
            return [
                index, 200 - open_, 200 - low, 200 - high,
                200 - close, volume, close_time,
            ]

        bearish = structure.get_macro_structure(
            [invert(row) for row in base + [
                candle(6, 124, 110, 123),
                candle(7, 125, 121, 124),
            ]],
            pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(
            bearish['transition'], 'NEUTRAL_TRANSITION_BEARISH'
        )
        self.assertEqual(bearish['broken_level'], 80.0)

    def test_unclosed_second_break_does_not_advance_transition_streak(self):
        def candle(index, high, low, close, close_time=1):
            return [index, close, high, low, close, 1.0, close_time]

        rows = [
            candle(0, 100, 95, 98), candle(1, 110, 100, 105),
            candle(2, 105, 90, 95), candle(3, 120, 100, 110),
            candle(4, 110, 95, 100), candle(5, 115, 100, 105),
            candle(6, 116, 91, 92),
            candle(7, 117, 70, 70, close_time=1000),
        ]
        result = structure.get_macro_structure(
            rows, pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(result['transition'], 'BULLISH_INVALIDATED')
        self.assertEqual(result['break_streak'], 1)

    def test_unclosed_m15_candle_cannot_flip_structure(self):
        def candle(index, high, low, close, close_time=1):
            return [index, close, high, low, close, 1.0, close_time]

        rows = [
            candle(0, 100, 95, 98), candle(1, 110, 100, 105),
            candle(2, 105, 90, 95), candle(3, 120, 100, 110),
            candle(4, 110, 95, 100), candle(5, 115, 100, 105),
            candle(6, 116, 96, 100),
            candle(7, 116, 70, 70, close_time=1000),
        ]
        result = structure.get_macro_structure(
            rows, pivot_legs=1, break_buffer=2.0, now_ms=10,
        )
        self.assertEqual(result['trend'], 'BULLISH')

    def test_structure_transition_forces_standby(self):
        state = ram.SharedState()
        state.poc, state.vah, state.val, state.atr_1m = 100, 110, 90, 2
        state.trend_m15 = 'NEUTRAL'
        state.structure_transition = 'BULLISH_INVALIDATED'
        result = mode_selector.xac_dinh_che_do(state)
        self.assertEqual(result['modes'], ['STANDBY'])
        self.assertEqual(result['reason'], 'BULLISH_INVALIDATED')

    def test_m15_trend_exposes_both_m1_lanes_and_only_scores_context(self):
        state = ram.SharedState()
        state.poc, state.vah, state.val, state.atr_1m = 100, 110, 90, 2
        state.swing_high_m15, state.swing_low_m15 = 120, 80
        state.trend_m15 = 'BEARISH'
        result = mode_selector.xac_dinh_che_do(state)
        self.assertTrue(result['m15_scoring_only'])
        self.assertEqual(result['bias'], 'SHORT')  # legacy context, not a gate
        self.assertEqual(
            {lane['bias'] for lane in result['candidate_lanes']},
            {'LONG', 'SHORT'},
        )
        candidates = radar.build_candidates(result)
        self.assertEqual(
            {item['bias'] for item in candidates}, {'LONG', 'SHORT'}
        )
        self.assertEqual(
            len([item for item in candidates if item['kind'] == 'breakout']), 2
        )

    def test_adaptive_profile_adds_passive_value_migration_lane(self):
        state = ram.SharedState()
        state.poc, state.vah, state.val, state.atr_1m = 100, 110, 90, 2
        state.swing_high_m15, state.swing_low_m15 = 120, 80
        state.trend_m15 = 'BEARISH'
        with mock.patch.dict(
            'os.environ',
            {'SMC_STRATEGY_PROFILE': 'AUG13_ADAPTIVE_OVERFIT_V1'},
        ):
            result = mode_selector.xac_dinh_che_do(state)
        candidates = radar.build_candidates(result)
        migration = [
            item for item in candidates
            if item.get('value_migration_retest')
        ]
        self.assertEqual(len(migration), 1)
        self.assertEqual(migration[0]['bias'], 'SHORT')
        self.assertEqual(migration[0]['zone'], 90.0)
        self.assertEqual(migration[0]['value_boundary'], 'VAL')
        self.assertEqual(migration[0]['entry_style'], 'PASSIVE_RETEST')
        self.assertEqual(migration[0]['mode'], 'NEUTRAL-MOMENTUM')

        setup_item = radar._new_setup(state, migration[0], 1, 10.0)
        self.assertTrue(setup_item['value_migration_retest'])
        self.assertEqual(setup_item['location_role'], 'VALUE_MIGRATION_RETEST')

    def test_confirmed_trend_reversal_keeps_pullback_contract(self):
        state = ram.SharedState()
        state.poc, state.vah, state.val, state.atr_1m = 100, 110, 90, 2
        state.trend_m15 = 'NEUTRAL'
        state.structure_transition = 'TRANSITION_BEARISH'
        state.structure_broken_level = 105
        result = mode_selector.xac_dinh_che_do(state)
        self.assertEqual(result['modes'], ['TRANSITION-PULLBACK'])
        self.assertEqual(result['bias'], 'SHORT')
        self.assertEqual(result['pullback_zones'], [105.0, 100.0, 110.0])
        self.assertEqual(result['size_cap_pct'], 50)
        candidates = radar.build_candidates(result)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(
            {candidate['mode'] for candidate in candidates},
            {'TRANSITION-PULLBACK'},
        )

    def test_confirmed_neutral_break_keeps_production_pullback_parallel(self):
        state = ram.SharedState()
        state.poc, state.vah, state.val, state.atr_1m = 100, 110, 90, 2
        state.trend_m15 = 'NEUTRAL'
        state.structure_transition = 'NEUTRAL_TRANSITION_BULLISH'
        state.structure_broken_level = 105
        result = mode_selector.xac_dinh_che_do(state)
        self.assertEqual(
            result['modes'], ['TRANSITION-PULLBACK', 'TRANSITION-BREAKOUT']
        )
        self.assertEqual(result['bias'], 'LONG')
        self.assertEqual(result['breakout_level'], 105.0)
        self.assertTrue(result['advisory_only'])
        candidates = radar.build_candidates(result)
        self.assertEqual(len(candidates), 4)
        breakout = next(item for item in candidates if item['kind'] == 'breakout')
        production = [item for item in candidates if item['kind'] == 'zone']
        self.assertEqual(len(production), 3)
        self.assertTrue(all(not item.get('advisory_only') for item in production))
        self.assertEqual(breakout['mode'], 'TRANSITION-BREAKOUT')
        self.assertTrue(breakout['advisory_only'])
        self.assertTrue(breakout['breakout_event_id'].startswith(
            'm15:neutral-break:LONG:'
        ))

        self.assertTrue(commander._score_allows(
            'TRANSITION-PULLBACK', {'core': 1, 'event_ids': ['reaction:1']}, None,
        ))
        self.assertFalse(commander._score_allows(
            'TRANSITION-PULLBACK', {'core': 1, 'event_ids': []}, None,
        ))
        self.assertTrue(commander._score_allows(
            'TRANSITION-PULLBACK', {'core': 2}, None,
        ))
        self.assertEqual(commander._position_size(
            'TRANSITION-PULLBACK', {'core': 2, 'shark': 0},
        ), 3)
        weak = commander._position_size_details(
            'TRANSITION-PULLBACK', {'core': 1, 'shark': 9},
        )
        self.assertEqual(weak['size_pct'], 2)
        self.assertEqual(weak['tier'], 'WEAK_PROBE')
        self.assertEqual(weak['shark_bonus_pct'], 0)
        self.assertEqual(commander._position_size(
            'TRANSITION-PULLBACK', {'core': 4, 'shark': 9},
        ), 10)

    def test_position_size_scales_only_with_high_quality_and_stays_capped(self):
        self.assertEqual(commander._position_size(
            'TREND-PULLBACK', {'core': 2, 'shark': 0},
        ), 3)
        self.assertEqual(commander._position_size(
            'TREND-PULLBACK', {'core': 3, 'shark': 0},
        ), 5)
        self.assertEqual(commander._position_size(
            'TREND-PULLBACK', {'core': 4, 'shark': 0},
        ), 8)
        self.assertEqual(commander._position_size(
            'TREND-PULLBACK', {'core': 5, 'shark': 9},
        ), 15)
        policy = commander._position_size_details(
            'NEUTRAL-FADE', {'core': 5, 'shark': 9},
        )
        self.assertEqual(policy['size_pct'], 10)
        self.assertEqual(policy['tier'], 'MAX_CONVICTION')
        self.assertEqual(policy['policy_version'], 'data_first_v1')

    def test_volume_profile_resets_after_decisive_range_break(self):
        rows = []
        for index in range(150):
            price = 100.0 if index < 120 else 120.0
            rows.append([
                index, price, price + 0.5, price - 0.5, price, 1.0, index,
            ])
        selected = volume_profile.select_profile_klines(
            rows, 90.0, 110.0, atr=1.0, recent_limit=30,
        )
        self.assertEqual(len(selected), 30)
        self.assertTrue(all(float(row[4]) == 120.0 for row in selected))

        in_range = volume_profile.select_profile_klines(
            rows[:120], 90.0, 110.0, atr=1.0, recent_limit=30,
        )
        self.assertEqual(len(in_range), 120)

    def test_trend_modes_create_pullback_and_breakout_candidates(self):
        candidates = radar.build_candidates({
            'modes': ['TREND-PULLBACK', 'TREND-BREAKOUT'],
            'bias': 'LONG',
            'pullback_zones': [100.0, 95.0],
            'breakout_level': 110.0,
        })
        self.assertEqual(len(candidates), 3)
        self.assertEqual({c['mode'] for c in candidates}, {'TREND-PULLBACK', 'TREND-BREAKOUT'})

    def test_breakout_requires_cross_and_one_atr_body(self):
        liquidity = {'bsl': 100.0, 'ssl': 90.0}
        weak = {'o': 99.5, 'h': 100.5, 'l': 99.0, 'c': 100.2}
        strong = {'o': 99.0, 'h': 102.0, 'l': 98.8, 'c': 101.8}
        self.assertFalse(map_nen.check_breakout_m1(weak, liquidity, 'LONG', 1.0)[0])
        self.assertTrue(map_nen.check_breakout_m1(strong, liquidity, 'LONG', 1.0)[0])

    def test_cumulative_breakout_detects_three_step_displacement(self):
        liquidity = {'bsl': 100.0, 'ssl': 90.0}
        candles = [
            {'t': 1, 'o': 99.2, 'h': 100.0, 'l': 99.0, 'c': 99.8},
            {'t': 2, 'o': 99.8, 'h': 100.5, 'l': 99.7, 'c': 100.4},
            {'t': 3, 'o': 100.4, 'h': 100.9, 'l': 100.3, 'c': 100.8},
        ]
        flag, direction, meta = map_nen.detect_breakout_m1(
            candles, liquidity, 1.0,
        )
        self.assertTrue(flag)
        self.assertEqual(direction, 'LONG')
        self.assertEqual(meta['detection'], 'CUMULATIVE_DISPLACEMENT')
        self.assertGreaterEqual(meta['closes_beyond'], 2)

    def test_cumulative_breakout_rejects_weak_drift_and_single_close(self):
        liquidity = {'bsl': 100.0, 'ssl': 90.0}
        weak_drift = [
            {'t': 1, 'o': 99.7, 'h': 99.9, 'l': 99.6, 'c': 99.8},
            {'t': 2, 'o': 99.8, 'h': 100.1, 'l': 99.8, 'c': 100.05},
            {'t': 3, 'o': 100.05, 'h': 100.2, 'l': 100.0, 'c': 100.15},
        ]
        self.assertFalse(map_nen.detect_breakout_m1(
            weak_drift, liquidity, 1.0,
        )[0])
        single_close = [
            {'t': 1, 'o': 99.0, 'h': 99.8, 'l': 98.9, 'c': 99.7},
            {'t': 2, 'o': 99.7, 'h': 99.9, 'l': 99.5, 'c': 99.8},
            {'t': 3, 'o': 99.8, 'h': 100.7, 'l': 99.7, 'c': 100.6},
        ]
        self.assertFalse(map_nen.detect_breakout_m1(
            single_close, liquidity, 1.0,
        )[0])

    def test_runtime_breakout_detection_is_not_locked_to_old_bias(self):
        state = ram.SharedState()
        state.current_mode = {'modes': ['TREND-PULLBACK'], 'bias': 'SHORT'}
        state.swing_low_m15 = 90.0
        state.swing_high_m15 = 100.0
        state.atr_1m = 1.0
        map_nen.cap_nhat_nen_m1({
            't': 1, 'o': 99.0, 'h': 102.0, 'l': 98.8,
            'c': 101.8, 'v': 1.0, 'x': True,
        }, state)
        self.assertTrue(state.breakout_m1['flag'])
        self.assertEqual(state.breakout_m1['direction'], 'LONG')
        self.assertEqual(state.breakout_m1['level'], 100.0)

    def test_opposing_breakout_creates_transition_candidate_from_standby(self):
        candidates = radar.build_candidates(
            {'modes': ['STANDBY'], 'bias': 'NONE'},
            {
                'flag': True, 'direction': 'LONG', 'level': 100.0,
                'detection': 'CUMULATIVE_DISPLACEMENT',
            },
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['mode'], 'TRANSITION-BREAKOUT')
        self.assertEqual(candidates[0]['bias'], 'LONG')
        self.assertEqual(candidates[0]['kind'], 'breakout')

    def test_breakout_entry_requires_structure_plus_independent_confirmation(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.breakout_m1 = {
            'flag': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'breakout:1:LONG',
            'detection': 'CUMULATIVE_DISPLACEMENT', 'level': 100.0,
        }
        mode = {
            'modes': ['TRANSITION-BREAKOUT'],
            'mode': 'TRANSITION-BREAKOUT',
        }
        structure_only = score.cham_diem(state, mode, 'LONG')
        self.assertEqual(structure_only['core'], 1)
        self.assertFalse(commander._score_allows(
            'TRANSITION-BREAKOUT', structure_only, state,
        ))

        state.persistent_flow = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'persistent:1:LONG', 'ttl': 5.0,
        }
        confirmed = score.cham_diem(state, mode, 'LONG')
        self.assertEqual(confirmed['core'], 2)
        self.assertTrue(commander._score_allows(
            'TRANSITION-BREAKOUT', confirmed, state,
        ))

    def test_transition_breakout_uses_trend_scorer_over_neutral_regime(self):
        state = ram.SharedState()
        now = time.time()
        state.snapshot_time = now
        state.breakout_m1 = {
            'flag': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'breakout:neutral-regime:LONG',
            'detection': 'CUMULATIVE_DISPLACEMENT', 'level': 100.0,
        }
        state.persistent_flow = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'persistent:neutral-regime:LONG', 'ttl': 5.0,
        }

        result = score.cham_diem(
            state,
            {
                'modes': ['NEUTRAL-FADE'],
                'mode': 'TRANSITION-BREAKOUT',
            },
            'LONG',
        )

        self.assertEqual(result['core'], 2)
        self.assertTrue(result['evidence_quality']['trend']['breakout'])
        self.assertTrue(result['evidence_quality']['trend']['persistent_flow'])
        self.assertTrue(commander._score_allows(
            'TRANSITION-BREAKOUT', result, state,
        ))

    def test_transition_breakout_can_claim_while_macro_mode_is_standby(self):
        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.best_bid, state.best_ask = 100.0, 100.1
        state.hang_doi_tin_hieu = asyncio.Queue()
        state.breakout_m1 = {
            'flag': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'breakout:standby:LONG',
            'detection': 'CUMULATIVE_DISPLACEMENT', 'level': 100.0,
        }
        state.persistent_flow = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'persistent:standby:LONG', 'ttl': 5.0,
        }
        setup = {
            'setup_id': 'transition-breakout', 'generation': 1,
            'state': 'ARMED_WINDOW', 'mode': 'TRANSITION-BREAKOUT',
            'bias': 'LONG', 'score_count': 0, 'core_reject_count': 0,
            'max_core': 0, 'max_shark': 0, 'veto_count': 0,
        }
        signal = commander.phan_tich_va_ra_lenh(
            state, {'modes': ['STANDBY'], 'bias': 'NONE'},
            'TRANSITION-BREAKOUT', 'LONG', setup=setup,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal['mode'], 'TRANSITION-BREAKOUT')
        self.assertEqual(signal['score_core'], 2)
        self.assertLessEqual(signal['size_pct'], 50)

    def test_wall_pull_veto_is_directional_and_fresh(self):
        state = ram.SharedState()
        state.wall_pull_flag = {
            'active': True, 'side': 'buy', 'ts': time.time(),
            'confirmed_for_veto': True,
            'price_confirmed': True,
            'flow_corroborated': True,
            'confirmation_version': 'WALL_PRICE_FLOW_V2',
        }
        self.assertTrue(veto.kiem_tra_veto(state, 'LONG')[0])
        self.assertFalse(veto.kiem_tra_veto(state, 'SHORT')[0])

    def test_setup_id_does_not_depend_on_tick_or_zone_float(self):
        state = ram.SharedState()
        state.structure_version = 7
        candidate = {
            'zone_id': 'TREND-PULLBACK:LONG:0', 'mode': 'TREND-PULLBACK',
            'bias': 'LONG', 'zone': 100.0, 'kind': 'zone',
        }
        first = radar._new_setup(state, candidate, 1, time.monotonic())
        candidate['zone'] = 100.01
        second = radar._new_setup(state, candidate, 1, time.monotonic())
        self.assertEqual(first['setup_id'], second['setup_id'])
        self.assertNotEqual(first['generation'], second['generation'])
        self.assertAlmostEqual(
            second['expires_mono'] - second['created_mono'], 60.0
        )

    def test_neutral_momentum_opportunity_survives_minute_boundaries(self):
        state = ram.SharedState()
        state.structure_version = 7
        candidate = {
            'zone_id': 'NEUTRAL:LONG:VAH', 'mode': 'NEUTRAL-MOMENTUM',
            'bias': 'LONG', 'zone': 100.0, 'kind': 'zone',
        }
        item = radar._new_setup(state, candidate, 1, time.monotonic())
        self.assertAlmostEqual(
            item['expires_mono'] - item['created_mono'], 900.0
        )
        self.assertEqual(item['opportunity_ttl_seconds'], 900.0)

    def test_semantic_setup_key_ignores_arm_sequence_and_requires_zone_exit(self):
        state = ram.SharedState()
        state.structure_version = 7
        candidate = {
            'zone_id': 'TREND-PULLBACK:LONG:1', 'mode': 'TREND-PULLBACK',
            'bias': 'LONG', 'zone': 100.0, 'kind': 'zone',
        }
        first = radar._new_setup(state, candidate, 1, time.monotonic())
        second = radar._new_setup(state, candidate, 2, time.monotonic())
        self.assertNotEqual(first['setup_id'], second['setup_id'])
        self.assertEqual(first['semantic_key'], second['semantic_key'])
        state.rearm_blocks[first['semantic_key']] = {'zone': 100.0}
        self.assertTrue(radar._rearm_block_active(
            state, first['semantic_key'], 105.0, 100.0, 10.0,
        ))
        self.assertFalse(radar._rearm_block_active(
            state, first['semantic_key'], 111.0, 100.0, 10.0,
        ))

    def test_consumed_footprint_cannot_open_another_setup(self):
        state = ram.SharedState()
        now = time.time()
        event_id = 'fp:reused-buy'
        state.snapshot_time = now
        state.cvd_buy_30m, state.cvd_sell_30m = 2.0, 1.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': now, 'event_id': event_id,
        }
        first = score.cham_diem(
            state, {'modes': ['TREND-PULLBACK']}, 'LONG'
        )
        self.assertEqual(first['core'], 2)
        state.consumed_market_events[event_id] = 'semantic-setup'
        second = score.cham_diem(
            state, {'modes': ['TREND-PULLBACK']}, 'LONG'
        )
        self.assertEqual(second['core'], 1)
        self.assertNotIn(event_id, second['event_ids'])

    def test_rejected_setup_tracks_no_lookahead_mfe_mae(self):
        state = ram.SharedState()
        created_mono = time.monotonic()
        setup = {
            'setup_id': 'missed:1', 'generation': 1,
            'state': 'ARMED_WINDOW', 'mode': 'NEUTRAL-FADE', 'bias': 'LONG',
            'zone': 100.0, 'armed_price': 100.0,
            'created_at': 1000.0, 'created_mono': created_mono,
            'score_count': 10, 'core_reject_count': 10, 'veto_count': 0,
            'max_core': 1, 'max_shark': 2,
            'best_score': {'core': 1, 'shark': 2},
            'seen_score_details': ['CORE+1: test'],
        }
        setups = {'missed': setup}
        radar._invalidate(
            state, setups, 'missed', 'TTL', reference_price=100.0,
            now_wall=1000.0,
        )
        self.assertEqual(len(state.setup_outcomes), 1)
        outcome = state.setup_outcomes[0]
        self.assertEqual(outcome['terminal_state'], 'EXPIRED')
        self.assertEqual(outcome['max_core'], 1)

        radar._update_setup_followups(state, 102.0, 1030.0)
        self.assertAlmostEqual(outcome['followup']['mfe_bps'], 200.0)
        self.assertIn('30', outcome['followup']['checkpoints'])
        radar._update_setup_followups(state, 98.0, 1120.0)
        self.assertAlmostEqual(outcome['followup']['mae_bps'], -200.0)
        self.assertFalse(outcome['followup']['completed'])
        self.assertIn('120', outcome['followup']['checkpoints'])
        self.assertAlmostEqual(
            outcome['followup']['checkpoints']['120']['net_move_bps'],
            -200.0 - radar.SETUP_FOLLOWUP_COST_BPS,
        )
        radar._update_setup_followups(state, 105.0, 3700.0)
        self.assertIn('900', outcome['followup']['checkpoints'])
        self.assertIn('1800', outcome['followup']['checkpoints'])
        self.assertIn('2700', outcome['followup']['checkpoints'])
        self.assertTrue(outcome['followup']['completed'])

    def test_structural_breakout_still_needs_independent_flow(self):
        state = ram.SharedState()
        state.snapshot_time = time.time()
        state.structure_transition = 'NEUTRAL_TRANSITION_BULLISH'
        state.structure_broken_level = 100.0
        state.structure_break_streak = 2
        state.current_vol_3s = 0.0
        state.vol_pct90 = 10.0
        state.breakout_m1 = {'flag': False, 'ts': 0.0}
        without_flow = score.cham_diem(
            state, {'mode': 'TRANSITION-BREAKOUT'}, 'LONG'
        )
        self.assertEqual(without_flow['core'], 1)
        self.assertFalse(score.score_allows(
            'TRANSITION-BREAKOUT', without_flow
        ))

        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.cvd_buy_30m = 100.0
        state.cvd_sell_30m = 50.0
        with_flow = score.cham_diem(
            state, {'mode': 'TRANSITION-BREAKOUT'}, 'LONG'
        )
        self.assertEqual(with_flow['core'], 2)
        self.assertTrue(score.score_allows(
            'TRANSITION-BREAKOUT', with_flow
        ))
        self.assertEqual(
            with_flow['evidence_quality']['trend']['breakout_detection'],
            'M15_NEUTRAL_TWO_CLOSE_CONFIRMATION',
        )

    def test_persistent_flow_is_held_only_on_material_microflow_reversal(self):
        state = ram.SharedState()
        state.snapshot_time = time.time()
        state.trend_m15 = 'BEARISH'
        state.vol_pct90 = 5.351
        state.persistent_flow = {
            'active': True, 'direction': 'SHORT', 'ts': state.snapshot_time,
            'event_id': 'persistent:loss-replay:SHORT', 'ttl': 5.0,
        }
        state.current_cvd_buy_3s = 2.447
        state.current_cvd_sell_3s = 0.003
        blocked = score.cham_diem(
            state, {'mode': 'TREND-PULLBACK'}, 'SHORT'
        )
        self.assertEqual(blocked['core'], 0)
        quality = blocked['evidence_quality']['trend']
        self.assertTrue(quality['persistent_flow_raw'])
        self.assertTrue(quality['microflow_reversal_conflict'])

        # Dao chieu nho hon floor khong duoc lam filter qua gat.
        state.current_cvd_buy_3s = 0.40
        state.current_cvd_sell_3s = 0.01
        allowed = score.cham_diem(
            state, {'mode': 'TREND-PULLBACK'}, 'SHORT'
        )
        self.assertEqual(allowed['core'], 1)
        self.assertTrue(allowed['evidence_quality']['trend']['persistent_flow'])

    def test_gap_recovered_weak_flow_waits_for_zone_reaction(self):
        weak_flow = {
            'core': 1,
            'evidence_quality': {'trend': {'zone_reaction': False}},
        }
        self.assertTrue(commander._weak_gap_requires_reaction(
            {'activation_reason': 'GAP_RECOVERED'}, weak_flow
        ))
        self.assertFalse(commander._weak_gap_requires_reaction(
            {'activation_reason': 'DIRECTIONAL_CROSSING'}, weak_flow
        ))
        self.assertFalse(commander._weak_gap_requires_reaction(
            {'activation_reason': 'GAP_RECOVERED'}, {
                'core': 1,
                'evidence_quality': {'trend': {'zone_reaction': True}},
            }
        ))
        self.assertFalse(commander._weak_gap_requires_reaction(
            {'activation_reason': 'GAP_RECOVERED'}, {
                'core': 2,
                'evidence_quality': {'trend': {'zone_reaction': False}},
            }
        ))

    def test_m15_is_half_point_context_not_a_core_replacement(self):
        base = {'core': 1, 'event_ids': ['reaction:1']}
        self.assertTrue(score.score_allows(
            'TREND-PULLBACK', dict(base, m15_modifier=0.5)
        ))
        self.assertTrue(score.score_allows(
            'TREND-PULLBACK', dict(base, m15_modifier=0.0)
        ))
        self.assertFalse(score.score_allows(
            'TREND-PULLBACK', dict(base, m15_modifier=-0.5)
        ))
        self.assertTrue(score.score_allows(
            'TREND-PULLBACK', {
                'core': 2, 'm15_modifier': -0.5, 'event_ids': ['flow:1'],
            }
        ))

        state = ram.SharedState()
        state.snapshot_time = time.time()
        state.trend_m15 = 'BULLISH'
        state.zone_reaction = {
            'active': True, 'direction': 'LONG',
            'ts': state.snapshot_time, 'event_id': 'reaction:m15-test',
        }
        scored = score.cham_diem(
            state, {'mode': 'TREND-PULLBACK'}, 'LONG'
        )
        self.assertEqual(scored['core'], 1)
        self.assertEqual(scored['m15_modifier'], 0.5)
        self.assertEqual(scored['effective_core'], 1.5)
        self.assertEqual(scored['total'], 1.5)

        # Đổi riêng context M15 không được sinh thêm CORE.
        opposed = score.cham_diem(
            state, {'mode': 'TREND-PULLBACK'}, 'SHORT'
        )
        self.assertEqual(opposed['core'], 0)
        self.assertEqual(opposed['m15_modifier'], -0.5)
        self.assertEqual(opposed['effective_core'], -0.5)

    def test_poc_context_is_distance_weighted_directional_and_bounded(self):
        state = ram.SharedState()
        state.atr_1m = 10.0
        state.poc = 100.0

        # Trong deadband 0.25 ATR, POC không làm điểm rung theo vài tick.
        state.best_bid, state.best_ask = 101.9, 102.1
        self.assertEqual(score._poc_context_modifier(state, 'SHORT'), 0.0)

        # Nửa ramp: (1.625 - 0.25) / (3.0 - 0.25) = 0.5.
        state.best_bid, state.best_ask = 116.2, 116.3
        self.assertEqual(score._poc_context_modifier(state, 'SHORT'), 0.35)
        self.assertEqual(score._poc_context_modifier(state, 'LONG'), -0.35)

        # Từ 3 ATR trở đi bị chặn đối xứng tại +/-0.7.
        state.best_bid, state.best_ask = 129.9, 130.1
        self.assertEqual(score._poc_context_modifier(state, 'SHORT'), 0.7)
        self.assertEqual(score._poc_context_modifier(state, 'LONG'), -0.7)
        state.best_bid, state.best_ask = 69.9, 70.1
        self.assertEqual(score._poc_context_modifier(state, 'LONG'), 0.7)
        self.assertEqual(score._poc_context_modifier(state, 'SHORT'), -0.7)

    def test_poc_context_cannot_replace_core_and_can_hold_weak_trade(self):
        self.assertFalse(score.score_allows(
            'NEUTRAL-FADE', {
                'core': 0, 'poc_modifier': 0.7, 'event_ids': ['poc:context'],
            },
        ))
        self.assertTrue(score.score_allows(
            'NEUTRAL-FADE', {
                'core': 1, 'poc_modifier': 0.7, 'event_ids': ['reaction:1'],
            },
        ))
        self.assertFalse(score.score_allows(
            'NEUTRAL-FADE', {
                'core': 1, 'poc_modifier': -0.7, 'event_ids': ['reaction:1'],
            },
        ))

    def test_weak_probe_plus_is_shadow_only_and_requires_full_quality(self):
        quality_score = {
            'core': 1, 'shark': 1, 'm15_modifier': 0.5,
            'advisory': {'adverse': [], 'size_nerf_pct': 0},
            'evidence_quality': {
                'trend': {
                    'zone_reaction': True,
                    'zone_reaction_displacement_atr': 0.284,
                    'zone_reaction_max_adverse_atr': 0.063,
                    'flow_dominance': 0.943,
                    'flow_volume_ratio': 0.89,
                },
                'obi': {
                    'persistent': True, 'aligned_ratio': 1.0,
                    'mean_signed': 0.735,
                },
            },
        }
        policy = commander._position_size_details(
            'TREND-PULLBACK', quality_score
        )
        self.assertEqual(policy['tier'], 'WEAK_PROBE_PLUS')
        self.assertEqual(policy['size_pct'], 2.5)
        self.assertTrue(policy['weak_probe_plus_candidate'])
        self.assertEqual(policy['fallback_size_pct'], 2.0)

        weak_obi = dict(quality_score)
        weak_obi['evidence_quality'] = {
            **quality_score['evidence_quality'],
            'obi': {'persistent': True, 'aligned_ratio': 0.8, 'mean_signed': 0.4},
        }
        rejected = commander._position_size_details(
            'TREND-PULLBACK', weak_obi
        )
        self.assertFalse(rejected['weak_probe_plus_candidate'])
        self.assertEqual(rejected['size_pct'], 2)

    def test_weak_probe_plus_shadow_rechecks_economics_at_2_5_percent(self):
        state = ram.SharedState()
        state.balance_usdt = 1000.0
        state.best_bid, state.best_ask = 99.9, 100.0
        state.bids_top_10 = [(99.9, 100.0)]
        state.asks_top_10 = [(100.0, 100.0)]
        state.exchange_filters = {
            'step_size': 0.001, 'min_qty': 0.001,
            'min_notional': 5.0, 'tick_size': 0.1,
        }
        signal = {
            'size_pct': 2.5,
            'size_policy': {
                'tier': 'WEAK_PROBE_PLUS', 'weak_probe_plus_candidate': True,
                'weak_probe_plus_min_edge_bps': 12.0,
                'fallback_tier': 'WEAK_PROBE', 'fallback_size_pct': 2.0,
                'weak_probe_plus_qualification': {'candidate': True},
            },
            'setup_kind': 'zone',
        }
        strong_economic = economic.observe(
            state, 'SHORT', 0.25, 99.0, target_basis='TEST_TARGET'
        )
        result = executor.weak_probe_plus_economic_evaluation(
            signal, 0.25, strong_economic
        )
        self.assertTrue(result['qualified'])
        self.assertEqual(result['evaluated_size_pct'], 2.5)

        marginal_economic = economic.observe(
            state, 'SHORT', 0.25, 99.8, target_basis='TEST_TARGET'
        )
        marginal = executor.weak_probe_plus_economic_evaluation(
            signal, 0.25, marginal_economic
        )
        self.assertFalse(marginal['qualified'])

    def test_weak_probe_has_moderate_six_bps_edge_buffer(self):
        signal = {'size_policy': {'tier': 'WEAK_PROBE'}}
        blocked = executor.weak_probe_economic_evaluation(
            signal, {'expected_net_edge_bps': 4.93}
        )
        self.assertTrue(blocked['applies'])
        self.assertFalse(blocked['qualified'])

        allowed = executor.weak_probe_economic_evaluation(
            signal, {'expected_net_edge_bps': 6.01}
        )
        self.assertTrue(allowed['qualified'])
        strong_tier = executor.weak_probe_economic_evaluation(
            {'size_policy': {'tier': 'PROBE'}},
            {'expected_net_edge_bps': 4.0},
        )
        self.assertFalse(strong_tier['applies'])
        self.assertTrue(strong_tier['qualified'])

    def test_shadow_structural_setup_cannot_claim_execution(self):
        state = ram.SharedState()
        state.system_ready = True
        state.trading_enabled = True
        state.best_bid, state.best_ask = 100.0, 100.1
        state.snapshot_time = time.time()
        state.structure_transition = 'NEUTRAL_TRANSITION_BULLISH'
        state.structure_broken_level = 99.0
        state.structure_break_streak = 2
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.cvd_buy_30m = 100.0
        state.cvd_sell_30m = 50.0
        state.breakout_m1 = {'flag': False, 'ts': 0.0}
        state.hang_doi_tin_hieu = asyncio.Queue()
        setup = {
            'setup_id': 'neutral-shadow:1', 'generation': 1,
            'state': 'ARMED_WINDOW', 'mode': 'TRANSITION-BREAKOUT',
            'bias': 'LONG', 'advisory_only': True,
            'shadow_qualified_once': False,
        }
        result = commander.phan_tich_va_ra_lenh(
            state, {'modes': ['TRANSITION-BREAKOUT']},
            'TRANSITION-BREAKOUT', 'LONG', setup=setup,
        )
        self.assertIsNone(result)
        self.assertFalse(state.execution_in_flight)
        self.assertTrue(state.hang_doi_tin_hieu.empty())
        self.assertEqual(setup['state'], 'ARMED_WINDOW')
        self.assertTrue(setup['shadow_qualified_once'])
        self.assertEqual(
            state.journal_events[-1]['payload']['result'], 'SHADOW_ONLY'
        )

    def test_same_event_is_recomputed_not_accumulated(self):
        state = ram.SharedState()
        now = time.time()
        state.cvd_buy_30m = 2.0
        state.cvd_sell_30m = 1.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': now, 'used': False, 'event_id': 'fp:1:buy',
        }
        first = score.cham_diem(state, {}, 'LONG')
        second = score.cham_diem(state, {}, 'LONG')
        self.assertEqual(first['core'], 2)
        self.assertEqual(second['core'], 2)
        self.assertEqual(second['event_ids'], ['fp:1:buy'])

    def test_shark_context_requires_multiple_evidence_families(self):
        state = ram.SharedState()
        now = time.time()
        state.wall_pull_flag = {'active': True, 'side': 'buy', 'ts': now}
        result = shark.evaluate(state, 'LONG', now)
        self.assertEqual(result['status'], 'NEUTRAL')

        state.wall_pull_flag = {'active': False, 'side': None, 'ts': 0.0}
        state.current_vol_3s = 20.0
        state.vol_pct90 = 10.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        for i in range(8):
            state.obi_history.append((now - i * 0.1, 0.5))
        result = shark.evaluate(state, 'LONG', now)
        self.assertEqual(result['status'], 'SHARK_SUPPORTIVE')
        self.assertGreaterEqual(result['support_count'], 2)

    def test_tp2_extension_and_adverse_eject_need_confirmation(self):
        state = ram.SharedState()
        position = state.vi_the_hien_tai
        position.tp1_done = True
        supportive = {
            'status': 'SHARK_SUPPORTIVE', 'support_count': 2, 'adverse_count': 0,
        }
        self.assertTrue(guardian.should_extend_tp2(position, supportive))
        adverse = {
            'status': 'SHARK_ADVERSE', 'adverse_count': 2,
            'adverse': ['BOOK', 'FOOTPRINT'],
        }
        self.assertFalse(guardian.should_eject_for_shark('LONG', 101, 100, adverse, 2.0))
        self.assertTrue(guardian.should_eject_for_shark('LONG', 99, 100, adverse, 1.0))

    def test_obi_and_wall_pull_count_as_one_book_family(self):
        state = ram.SharedState()
        now = time.time()
        for i in range(8):
            state.obi_history.append((now - i * 0.1, -0.5))
        state.wall_pull_flag = {'active': True, 'side': 'buy', 'ts': now}
        result = shark.evaluate(state, 'LONG', now)
        self.assertEqual(result['adverse'], ['BOOK'])
        self.assertEqual(result['adverse_count'], 1)
        self.assertEqual(result['status'], 'NEUTRAL')

    def test_opposing_flash_flow_must_dominate_supporting_flow(self):
        state = ram.SharedState()
        now = time.time()
        state.p95_value = 2.0
        state.vol_pct90 = 6.94
        state.current_vol_3s = 25.556
        # Hồi quy incident: sell vượt threshold nhưng buy lớn hơn nhiều.
        state.current_cvd_buy_3s = 18.376
        state.current_cvd_sell_3s = 7.18
        result = shark.evaluate(state, 'LONG', now)
        self.assertNotIn('FLASH_FLOW', result['adverse'])
        self.assertIn('FLOW', result['support'])

        state.current_cvd_buy_3s = 7.0
        state.current_cvd_sell_3s = 18.0
        result = shark.evaluate(state, 'LONG', now)
        self.assertIn('FLASH_FLOW', result['adverse'])

    def test_entry_veto_flash_flow_also_requires_dominance(self):
        state = ram.SharedState()
        state.p95_value = 2.0
        state.vol_pct90 = 6.94
        state.current_cvd_buy_3s = 18.376
        state.current_cvd_sell_3s = 7.18
        self.assertFalse(veto.kiem_tra_veto(state, 'LONG')[0])

        state.current_cvd_buy_3s = 7.0
        state.current_cvd_sell_3s = 18.0
        blocked, reason = veto.kiem_tra_veto(state, 'LONG')
        self.assertTrue(blocked)
        self.assertIn('Flash Flow', reason)

    def test_executor_preflight_uses_atomic_claim_lease_without_rescoring_core(self):
        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.execution_in_flight = True
        state.execution_setup_id = 'leased-setup'
        state.execution_generation = 7
        state.best_bid, state.best_ask = 99.99, 100.0
        state.atr_1m = 10.0
        state.exchange_filters = {'tick_size': 0.1}
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        setup = {
            'setup_id': 'leased-setup', 'generation': 7,
            'state': 'EXECUTING', 'zone': 100.0,
            'zone_id': 'TREND-PULLBACK:LONG:0', 'kind': 'zone',
        }
        state.active_setups = {'leased': setup}
        signal = {
            'setup_id': 'leased-setup', 'setup_generation': 7,
            'bias': 'LONG', 'mode': 'TREND-PULLBACK',
            'created_mono': time.monotonic(), 'decision_price': 100.0,
            'setup_zone': 100.0,
            'setup_zone_id': 'TREND-PULLBACK:LONG:0',
            'setup_kind': 'zone',
        }

        # Live state intentionally has zero CORE evidence. The atomic Commander
        # snapshot owns that decision during the short lease.
        ok, reason = executor.preflight_signal(signal, state)
        self.assertTrue(ok, reason)

        signal['created_mono'] = (
            time.monotonic() - executor.PREFLIGHT_DECISION_LEASE_SECONDS - 0.01
        )
        ok, reason = executor.preflight_signal(signal, state)
        self.assertFalse(ok)
        self.assertIn('lease hết hạn', reason)

    def test_executor_preflight_rejects_metadata_or_adverse_entry_drift(self):
        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.execution_in_flight = True
        state.execution_setup_id = 'drift-setup'
        state.execution_generation = 2
        state.best_bid, state.best_ask = 100.99, 101.0
        state.atr_1m = 2.0
        state.exchange_filters = {'tick_size': 0.1}
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        setup = {
            'setup_id': 'drift-setup', 'generation': 2,
            'state': 'EXECUTING', 'zone': 100.0,
            'zone_id': 'zone-live', 'kind': 'zone',
        }
        state.active_setups = {'drift': setup}
        signal = {
            'setup_id': 'drift-setup', 'setup_generation': 2,
            'bias': 'LONG', 'created_mono': time.monotonic(),
            'decision_price': 100.0, 'setup_zone': 100.0,
            'setup_zone_id': 'zone-live', 'setup_kind': 'zone',
        }
        ok, reason = executor.preflight_signal(signal, state)
        self.assertFalse(ok)
        self.assertIn('Giá entry trôi bất lợi', reason)

        state.best_bid, state.best_ask = 99.99, 100.0
        signal['setup_zone_id'] = 'zone-stale'
        ok, reason = executor.preflight_signal(signal, state)
        self.assertFalse(ok)
        self.assertIn('setup_zone_id', reason)

    def test_armed_window_uses_wider_retention_zone_and_ttl(self):
        state = ram.SharedState()
        state.atr_1m = 10.0
        state.structure_version = 1
        candidate = {
            'mode': 'TREND-PULLBACK', 'bias': 'LONG', 'kind': 'zone', 'zone': 100.0,
        }
        setup = {
            'state': 'ARMED_WINDOW', 'mode': 'TREND-PULLBACK', 'bias': 'LONG',
            'zone': 100.0, 'structure_version': 1,
            'expires_mono': time.monotonic() + 10.0,
        }
        valid, _ = radar._setup_is_current(
            setup, candidate, state, 108.0, time.time(), time.monotonic()
        )
        self.assertTrue(valid)
        valid, reason = radar._setup_is_current(
            setup, candidate, state, 111.0, time.time(), time.monotonic()
        )
        self.assertFalse(valid)
        self.assertEqual(reason, 'left RETENTION zone')

    def test_zone_setup_is_invalidated_by_fresh_opposing_breakout(self):
        state = ram.SharedState()
        state.atr_1m = 10.0
        state.structure_version = 1
        state.breakout_m1 = {
            'flag': True, 'direction': 'LONG', 'ts': time.time(),
        }
        candidate = {
            'mode': 'NEUTRAL-FADE', 'bias': 'SHORT',
            'kind': 'zone', 'zone': 100.0,
        }
        setup = {
            'state': 'ARMED_WINDOW', 'mode': 'NEUTRAL-FADE', 'bias': 'SHORT',
            'zone': 100.0, 'structure_version': 1,
            'expires_mono': time.monotonic() + 10.0,
        }
        valid, reason = radar._setup_is_current(
            setup, candidate, state, 100.0, time.time(), time.monotonic()
        )
        self.assertFalse(valid)
        self.assertEqual(reason, 'opposing breakout')

    def test_setup_funnel_tracks_score_rejections(self):
        state = ram.SharedState()
        state.system_ready = True
        state.trading_enabled = True
        setup = {
            'setup_id': 'funnel', 'generation': 1, 'state': 'ARMED_WINDOW',
            'score_count': 0, 'core_reject_count': 0,
            'max_core': 0, 'max_shark': 0, 'veto_count': 0,
        }
        commander.phan_tich_va_ra_lenh(
            state, {'modes': ['NEUTRAL-FADE']},
            'NEUTRAL-FADE', 'LONG', setup=setup,
        )
        self.assertEqual(setup['score_count'], 1)
        self.assertEqual(setup['core_reject_count'], 1)
        self.assertEqual(setup['max_core'], 0)


class RiskAndExecutionTests(unittest.IsolatedAsyncioTestCase):
    def test_quantity_uses_exchange_filters_and_floor(self):
        filters = {'step_size': 0.0001, 'min_qty': 0.0001, 'min_notional': 50.0}
        qty = risk.calculate_qty(1000.0, 50, 64000.0, filters)
        self.assertEqual(qty, 0.0078)

    def test_quantity_never_upsizes_a_small_probe_to_exchange_minimum(self):
        filters = {'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 100.0}
        # Requested notional is only 30 USDT. The old behavior silently raised
        # this to at least one 0.001 BTC lot (~64 USDT).
        self.assertEqual(risk.calculate_qty(1000.0, 3, 64000.0, filters), 0.0)

    def test_early_protection_is_cumulative_and_reserves_half_for_sl(self):
        self.assertEqual(
            risk.calculate_early_protection_qty(1.0, 1.0, 0.0, 1.0, 0.1),
            0.5,
        )
        self.assertEqual(
            risk.calculate_early_protection_qty(0.7, 1.0, 0.3, 0.7, 0.1),
            0.2,
        )
        # TP1 đã để lại đúng nửa entry gốc: guardian không được cắt thêm.
        self.assertEqual(
            risk.calculate_early_protection_qty(0.5, 1.0, 0.0, 0.5, 0.1),
            0.0,
        )

    def test_value_area_poc_split_sl_is_directional_and_extends_sl2(self):
        long_policy = risk.build_value_area_split_sl_policy(
            'LONG', 90.0, 100.0, 110.0, 90.0, 0.7, 10.0, 0.1,
            {'soft_sl': 70.0, 'hard_sl': 60.0},
        )
        self.assertTrue(long_policy['enabled'])
        self.assertEqual(long_policy['standard_sl1'], 70.0)
        self.assertEqual(long_policy['standard_hard_sl'], 60.0)
        self.assertEqual(long_policy['sl2'], 19.0)
        self.assertEqual(long_policy['sl1_close_fraction'], 0.90)

        short_policy = risk.build_value_area_split_sl_policy(
            'SHORT', 110.0, 100.0, 110.0, 90.0, 0.7, 10.0, 0.1,
            {'soft_sl': 130.0, 'hard_sl': 140.0},
        )
        self.assertTrue(short_policy['enabled'])
        self.assertEqual(short_policy['sl2'], 181.0)

        opposed = risk.build_value_area_split_sl_policy(
            'LONG', 90.0, 80.0, 110.0, 90.0, -0.7, 10.0, 0.1,
            {'soft_sl': 70.0, 'hard_sl': 60.0},
        )
        inside_value = risk.build_value_area_split_sl_policy(
            'LONG', 100.0, 105.0, 110.0, 90.0, 0.7, 10.0, 0.1,
            {'soft_sl': 80.0, 'hard_sl': 70.0},
        )
        self.assertFalse(opposed['enabled'])
        self.assertFalse(inside_value['enabled'])

    def test_split_sl1_quantity_never_consumes_ten_percent_tail(self):
        self.assertEqual(
            risk.calculate_split_sl1_close_qty(0.010, 0.010, 0.001), 0.009
        )
        # Early protection đã cắt 50%; SL1 chỉ cắt thêm 40%, vẫn còn 10% entry gốc.
        self.assertEqual(
            risk.calculate_split_sl1_close_qty(0.005, 0.010, 0.001), 0.004
        )
        self.assertEqual(
            risk.calculate_split_sl1_close_qty(0.001, 0.010, 0.001), 0.0
        )

    async def test_split_sl1_partial_close_marks_done_and_keeps_tail(self):
        class FakeAPI:
            async def new_order(self, *args, **kwargs):
                qty = float(args[3])
                return {
                    'orderId': 91, 'clientOrderId': kwargs['newClientOrderId'],
                    'status': 'FILLED', 'executedQty': str(qty),
                    'avgPrice': '90.0',
                }, 200

        state = ram.SharedState()
        position = state.vi_the_hien_tai
        position.active = True
        position.side = 'LONG'
        position.qty = 0.010
        position.initial_qty = 0.010
        position.split_sl_enabled = True
        state.co_lenh_mo = True
        state.best_bid, state.best_ask = 90.0, 90.1
        state.execution_best_bid, state.execution_best_ask = 89.9, 90.0
        state.exchange_filters = {
            'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0,
        }
        qty = risk.calculate_split_sl1_close_qty(
            position.qty, position.initial_qty, 0.001,
        )
        self.assertTrue(await guardian.close_partial(
            FakeAPI(), 'BTCUSDT', 'LONG', qty, state, 'SL1_90'
        ))
        self.assertAlmostEqual(position.qty, 0.001, places=12)
        self.assertTrue(position.split_sl1_done)
        self.assertTrue(position.active)

    async def test_split_sl_reserves_whole_position_for_sl1_not_early_protection(self):
        state = ram.SharedState()
        position = state.vi_the_hien_tai
        position.active = True
        position.side = 'LONG'
        position.qty = 0.010
        position.initial_qty = 0.010
        position.split_sl_enabled = True
        state.exchange_filters = {
            'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 50.0,
        }
        self.assertFalse(await guardian.close_early_protection(
            None, 'BTCUSDT', 'LONG', state, 'SHARK_ADVERSE_CONFIRMED'
        ))
        self.assertEqual(position.qty, 0.010)
        self.assertIn('SHARK_ADVERSE_CONFIRMED', position.protection_reasons_done)

    async def test_early_guardian_closes_half_once_then_sl_can_close_rest(self):
        class FakeAPI:
            def __init__(self):
                self.quantities = []

            async def new_order(self, *args, **kwargs):
                qty = float(args[3])
                self.quantities.append(qty)
                return {
                    'orderId': len(self.quantities),
                    'clientOrderId': kwargs['newClientOrderId'],
                    'status': 'FILLED', 'executedQty': str(qty),
                    'avgPrice': '100.0',
                }, 200

        state = ram.SharedState()
        state.vi_the_hien_tai.active = True
        state.vi_the_hien_tai.side = 'LONG'
        state.vi_the_hien_tai.qty = 1.0
        state.vi_the_hien_tai.initial_qty = 1.0
        state.co_lenh_mo = True
        state.best_bid, state.best_ask = 100.0, 100.1
        state.execution_best_bid, state.execution_best_ask = 99.9, 100.0
        state.exchange_filters = {
            'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 5.0,
        }
        api = FakeAPI()
        self.assertTrue(await guardian.close_early_protection(
            api, 'BTCUSDT', 'LONG', state, 'SHARK_ADVERSE_CONFIRMED'
        ))
        self.assertEqual(api.quantities, [0.5])
        self.assertEqual(state.vi_the_hien_tai.qty, 0.5)
        self.assertEqual(state.vi_the_hien_tai.protection_closed_qty, 0.5)
        self.assertFalse(await guardian.close_early_protection(
            api, 'BTCUSDT', 'LONG', state, 'TIME_STOP'
        ))
        self.assertEqual(api.quantities, [0.5])

        # SL vẫn có toàn quyền đóng phần 50% được giữ lại.
        self.assertTrue(await guardian.close_position(
            api, 'BTCUSDT', 'LONG', 0.5, state, 'SOFT_SL'
        ))
        self.assertEqual(api.quantities, [0.5, 0.5])
        self.assertFalse(state.vi_the_hien_tai.active)

    def test_shadow_early_protection_matches_live_cap(self):
        state = ram.SharedState()
        state.exchange_filters = {
            'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 5.0,
        }
        state.bids_top_10 = [[100.0, 10.0]]
        state.asks_top_10 = [[100.1, 10.0]]
        state.trade_cycles['pc-protect'] = {'shadow': {'orders': []}}
        position = {
            'position_cycle_id': 'pc-protect', 'side': 'LONG',
            'entry_price': 100.0, 'qty': 1.0, 'initial_qty': 1.0,
            'gross_pnl_quote': 0.0, 'fee_quote': 0.0,
            'protection_closed_qty': 0.0, 'protection_reasons_done': [],
        }
        self.assertTrue(journal._shadow_early_protection(
            state, position, 'SHARK_ADVERSE_CONFIRMED', 100.0
        ))
        self.assertEqual(position['qty'], 0.5)
        self.assertEqual(position['protection_closed_qty'], 0.5)
        self.assertFalse(journal._shadow_early_protection(
            state, position, 'TIME_STOP', 100.0
        ))
        self.assertEqual(position['qty'], 0.5)
        self.assertEqual(
            state.trade_cycles['pc-protect']['shadow']['orders'][0]['role'],
            'PROTECT',
        )

    def test_initial_soft_sl_is_at_least_41_price_units_from_entry(self):
        state = ram.SharedState()
        state.atr_1m = 10.0
        state.poc = 100.0
        state.vah = 102.0
        state.val = 98.0
        state.swing_high_m15 = 120.0
        state.swing_low_m15 = 80.0

        long_levels = risk.calculate_levels(
            state, 100.0, 'LONG', 0.1, mode='NEUTRAL-FADE'
        )
        short_levels = risk.calculate_levels(
            state, 100.0, 'SHORT', 0.1, mode='NEUTRAL-FADE'
        )

        self.assertGreaterEqual(100.0 - long_levels['soft_sl'], 41.0)
        self.assertGreaterEqual(short_levels['soft_sl'] - 100.0, 41.0)
        self.assertLess(long_levels['hard_sl'], long_levels['soft_sl'])
        self.assertGreater(short_levels['hard_sl'], short_levels['soft_sl'])

    def test_level_geometry_blocks_incident_and_translates_between_venues(self):
        levels = {
            'hard_sl': 64422.7, 'soft_sl': 64570.0,
            'soft_tp1': 64795.2, 'soft_tp2': 64971.0,
        }
        ok, reason = risk.validate_level_geometry(
            levels, 64556.6, 'LONG', 0.1, 44.6357,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'LONG_SL_NOT_BELOW_ENTRY')

        valid = {
            'hard_sl': 64422.7, 'soft_sl': 64520.0,
            'soft_tp1': 64795.2, 'soft_tp2': 64971.0,
        }
        translated = risk.translate_levels(valid, 64556.6, 64529.8, 0.1)
        self.assertEqual(translated['soft_sl'], 64493.2)
        self.assertTrue(risk.validate_level_geometry(
            translated, 64529.8, 'LONG', 0.1, 44.6357,
        )[0])

    def test_close_payload_matches_account_mode(self):
        state = ram.SharedState()
        state.account_hedge_mode = True
        self.assertEqual(guardian._close_kwargs(state, 'LONG'), {'positionSide': 'LONG'})
        state.account_hedge_mode = False
        self.assertEqual(guardian._close_kwargs(state, 'LONG'), {'reduceOnly': 'true'})

    def test_guardian_strategy_and_execution_prices_are_separate(self):
        state = ram.SharedState()
        state.best_bid, state.best_ask = 64893.2, 64893.3
        state.execution_best_bid, state.execution_best_ask = 64830.9, 64831.0
        self.assertEqual(guardian._strategy_prices(state), (64893.2, 64893.3))
        self.assertEqual(guardian._execution_prices(state), (64830.9, 64831.0))
        context = guardian._close_price_context(state, 'LONG')
        self.assertEqual(context['strategy_trigger_price'], 64893.2)
        self.assertEqual(context['execution_reference_price'], 64830.9)

    def test_close_journal_records_both_venues(self):
        state = ram.SharedState()
        signal = {
            'setup_id': 'dual-venue', 'setup_generation': 1,
            'mode': 'NEUTRAL-FADE', 'bias': 'LONG',
        }
        cycle_id = journal.create_cycle(
            state, signal, 1.0, 64860.9, {'mode': 'LOG_ONLY'}
        )
        result = {
            'orderId': 99, 'clientOrderId': 'close', 'status': 'FILLED',
            'executedQty': '1.0', 'avgPrice': '64828.788',
        }
        journal.record_actual_order(
            state, cycle_id, 'CLOSE', result, 1.0, 64830.9,
            reason='SHARK_ADVERSE_CONFIRMED',
            strategy_reference_price=64893.2,
            execution_reference_price=64830.9,
        )
        journal.mark_actual_closed(
            state, cycle_id, 'SHARK_ADVERSE_CONFIRMED', 64893.2,
            result=result, execution_reference_price=64830.9,
        )
        cycle = state.trade_cycles[cycle_id]
        order = cycle['actual']['orders'][0]
        self.assertEqual(cycle['exit_decision_price_mainnet'], 64893.2)
        self.assertEqual(cycle['exit_execution_reference_price_testnet'], 64830.9)
        self.assertEqual(order['strategy_reference_price_mainnet'], 64893.2)
        self.assertEqual(order['execution_reference_price_testnet'], 64830.9)

    def test_readiness_requires_all_feeds_and_reconcile(self):
        state = ram.SharedState()
        now = time.time()
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        state.atr_1m = 10.0
        state.poc = 64000.0
        state.execution_best_bid, state.execution_best_ask = 63999.9, 64000.0
        state.exchange_filters = {'step_size': 0.0001}
        state.balance_usdt = 1000.0
        state.reconcile_ready = True
        self.assertFalse(watchdog.readiness(state)[0])
        state.account_ready = True
        self.assertTrue(watchdog.readiness(state)[0])


class EconomicAndShadowTests(unittest.TestCase):
    def test_depth_walk_uses_executable_levels_and_refuses_missing_depth(self):
        fill = economic.estimate_market_fill([[100.0, 1.0], [101.0, 2.0]], 2.0)
        self.assertTrue(fill['available'])
        self.assertEqual(fill['avg_price'], 100.5)
        self.assertGreater(fill['slippage_bps'], 0.0)
        missing = economic.estimate_market_fill([[100.0, 0.5]], 1.0)
        self.assertFalse(missing['available'])
        self.assertEqual(missing['reason'], 'TOP10_DEPTH_INSUFFICIENT')

    def test_economic_gate_truthfully_reports_enforced_policy(self):
        state = ram.SharedState()
        state.bids_top_10 = [[99.9, 10.0]]
        state.asks_top_10 = [[100.0, 10.0]]
        result = economic.observe(state, 'LONG', 1.0, 100.05, capture_ratio=0.0)
        self.assertFalse(result['economic_pass'])
        self.assertTrue(result['blocks_entry'])
        self.assertEqual(result['mode'], 'ENFORCED_NET_EDGE')
        self.assertFalse(result['structural_fee_floor_pass'])

    def test_structural_fee_floor_uses_projected_capture_not_raw_tp1(self):
        state = ram.SharedState()
        state.bids_top_10 = [[100.0, 10.0]]
        state.asks_top_10 = [[100.0, 10.0]]
        # Raw TP1 = 13 bps vượt required 12 bps, nhưng capture 60% chỉ 7.8 bps.
        result = economic.observe(state, 'LONG', 1.0, 100.13, capture_ratio=0.60)
        self.assertGreater(result['tp1_distance_bps'], result['required_capture_bps'])
        self.assertLess(result['projected_capture_bps'], result['required_capture_bps'])
        # Execution cost riêng đã pass; chỉ policy capture tạm thời còn chặn.
        self.assertTrue(result['execution_floor_pass'])
        self.assertEqual(result['execution_floor_mode'], 'OBSERVE_ONLY')
        self.assertFalse(result['structural_fee_floor_pass'])
        self.assertEqual(
            result['structural_fee_floor_reason'],
            'PROJECTED_CAPTURE_BELOW_ALL_IN_COST_PLUS_EDGE',
        )

    def test_recent_short_incident_is_blocked_by_projected_fee_floor(self):
        state = ram.SharedState()
        state.bids_top_10 = [[64848.2, 1.0]]
        state.asks_top_10 = [[64848.3, 1.0]]
        result = economic.observe(
            state, 'SHORT', 0.0455, 64768.9, capture_ratio=0.60
        )
        self.assertAlmostEqual(result['tp1_distance_bps'], 12.228558, places=5)
        self.assertAlmostEqual(result['projected_capture_bps'], 7.337135, places=5)
        self.assertFalse(result['structural_fee_floor_pass'])

    def test_cycle_groups_order_and_distinct_fills(self):
        state = ram.SharedState()
        signal = {
            'setup_id': 'setup-journal', 'setup_generation': 1,
            'mode': 'NEUTRAL-FADE', 'bias': 'SHORT',
            'signal_price': 100.0, 'decision_price': 99.9,
        }
        cycle_id = journal.create_cycle(
            state, signal, 2.0, 99.9, {'mode': 'LOG_ONLY', 'economic_pass': False}
        )
        journal.record_actual_order(
            state, cycle_id, 'ENTRY',
            {'orderId': 77, 'clientOrderId': 'smc_entry_x', 'status': 'FILLED'},
            2.0, 99.9,
        )
        trades = [
            {'id': 1, 'orderId': 77, 'price': '100', 'qty': '1', 'quoteQty': '100',
             'commission': '0.04', 'commissionAsset': 'USDT', 'realizedPnl': '0', 'time': 1},
            {'id': 2, 'orderId': 77, 'price': '101', 'qty': '1', 'quoteQty': '101',
             'commission': '0.0404', 'commissionAsset': 'USDT', 'realizedPnl': '0', 'time': 2},
        ]
        journal._apply_trade_fills(state, trades)
        journal._apply_trade_fills(state, trades)
        cycle = state.trade_cycles[cycle_id]
        self.assertEqual(len(cycle['actual']['orders']), 1)
        self.assertEqual(len(cycle['actual']['orders'][0]['fills']), 2)
        self.assertEqual(cycle['actual']['entry_fill_ids'], [1, 2])
        self.assertAlmostEqual(cycle['actual']['entry_fill_price'], 100.5)
        self.assertFalse(cycle['economic_result_valid'])

    def test_cycle_pruning_skips_open_shadow_without_leaking_terminal_cycles(self):
        state = ram.SharedState()
        state.trade_cycles = {
            'active-oldest': {
                'created_at': 0.0, 'status': 'ABORTED',
                'shadow': {'status': 'OPEN'},
            }
        }
        for index in range(journal.MAX_CYCLES_IN_RAM + 5):
            state.trade_cycles[f'closed-{index}'] = {
                'created_at': float(index + 1), 'status': 'ABORTED',
                'shadow': {'status': 'DEDUPED_EXISTING_OPPORTUNITY'},
            }
        journal._prune_cycles(state)
        self.assertEqual(len(state.trade_cycles), journal.MAX_CYCLES_IN_RAM)
        self.assertIn('active-oldest', state.trade_cycles)
        self.assertNotIn('closed-0', state.trade_cycles)

    def test_shadow_ledger_closes_on_tp2_and_reports_net_mfe_mae(self):
        state = ram.SharedState()
        state.exchange_filters = {'tick_size': 0.1, 'step_size': 0.1, 'min_notional': 0.0}
        state.atr_1m = 1.0
        state.poc = 101.0
        state.vah = 102.0
        state.val = 98.0
        state.swing_high_m15 = 102.0
        state.swing_low_m15 = 98.0
        state.best_bid = 99.9
        state.best_ask = 100.0
        state.bids_top_10 = [[99.9, 10.0]]
        state.asks_top_10 = [[100.0, 10.0]]
        signal = {
            'setup_id': 'setup-shadow', 'setup_generation': 1,
            'mode': 'TREND-PULLBACK', 'bias': 'LONG',
            'signal_price': 99.95, 'decision_price': 100.0,
        }
        entry = economic.estimate_market_fill(state.asks_top_10, 1.0)
        econ = economic.observe(state, 'LONG', 1.0, 101.0)
        cycle_id = journal.create_cycle(state, signal, 1.0, 100.0, econ)
        self.assertTrue(journal.activate_shadow(state, cycle_id, signal, 1.0, entry))

        # TP1 chốt 50%, sau đó TP2 đóng phần còn lại.
        state.best_bid = 101.1
        state.best_ask = 101.2
        state.bids_top_10 = [[101.1, 10.0]]
        state.asks_top_10 = [[101.2, 10.0]]
        journal.shadow_step(state)
        self.assertTrue(state.shadow_position['tp1_done'])
        state.best_bid = 102.1
        state.best_ask = 102.2
        state.bids_top_10 = [[102.1, 10.0]]
        state.asks_top_10 = [[102.2, 10.0]]
        journal.shadow_step(state)
        shadow = state.trade_cycles[cycle_id]['shadow']
        self.assertEqual(shadow['status'], 'CLOSED')
        self.assertEqual(shadow['exit_reason'], 'TP2')
        self.assertGreater(shadow['gross_pnl_quote'], 0.0)
        self.assertGreater(shadow['MFE_bps'], 0.0)
        self.assertLess(shadow['net_pnl_quote'], shadow['gross_pnl_quote'])
        self.assertIsNone(state.shadow_position)

    def test_fee_blocked_shadow_is_full_lifecycle_and_deduped_by_semantic_setup(self):
        state = ram.SharedState()
        state.exchange_filters = {
            'tick_size': 0.1, 'step_size': 0.1, 'min_notional': 0.0,
        }
        state.atr_1m = 1.0
        state.poc, state.vah, state.val = 101.0, 102.0, 98.0
        state.swing_high_m15, state.swing_low_m15 = 102.0, 98.0
        state.best_bid, state.best_ask = 99.9, 100.0
        state.bids_top_10, state.asks_top_10 = [[99.9, 10.0]], [[100.0, 10.0]]
        entry = economic.estimate_market_fill(state.asks_top_10, 1.0)
        entry['captured_at'] = time.time()
        economic_observation = economic.observe(state, 'LONG', 1.0, 101.0)
        signal_a1 = {
            'setup_id': 'semantic-zone:a1', 'setup_generation': 1,
            'mode': 'TREND-PULLBACK', 'bias': 'LONG',
        }
        signal_a2 = dict(signal_a1, setup_id='semantic-zone:a2')
        cycle_a1 = journal.create_cycle(
            state, signal_a1, 1.0, 100.0, economic_observation
        )
        cycle_a2 = journal.create_cycle(
            state, signal_a2, 1.0, 100.0, economic_observation
        )
        journal.abort_cycle(state, cycle_a1, 'STRUCTURAL_FEE_FLOOR_BLOCKED')
        journal.abort_cycle(state, cycle_a2, 'STRUCTURAL_FEE_FLOOR_BLOCKED')
        self.assertTrue(journal.activate_fee_blocked_shadow(
            state, cycle_a1, signal_a1, 1.0, entry,
            semantic_key='semantic-zone',
        ))
        self.assertFalse(journal.activate_fee_blocked_shadow(
            state, cycle_a2, signal_a2, 1.0, entry,
            semantic_key='semantic-zone',
        ))
        self.assertEqual(len(state.fee_blocked_shadow_positions), 1)
        self.assertEqual(
            state.trade_cycles[cycle_a2]['shadow']['deduped_to_cycle_id'], cycle_a1
        )
        self.assertEqual(
            state.fee_blocked_shadow_clusters['semantic-zone']['attempt_count'], 2
        )

        # Primary sample vẫn chạy đúng TP1 -> TP2 và tính net sau phí.
        state.best_bid, state.best_ask = 101.1, 101.2
        state.bids_top_10, state.asks_top_10 = [[101.1, 10.0]], [[101.2, 10.0]]
        journal.shadow_step(state)
        self.assertTrue(state.fee_blocked_shadow_positions[0]['tp1_done'])
        state.best_bid, state.best_ask = 102.1, 102.2
        state.bids_top_10, state.asks_top_10 = [[102.1, 10.0]], [[102.2, 10.0]]
        journal.shadow_step(state)
        shadow = state.trade_cycles[cycle_a1]['shadow']
        self.assertEqual(shadow['status'], 'CLOSED')
        self.assertEqual(shadow['exit_reason'], 'TP2')
        self.assertEqual(shadow['shadow_kind'], 'FEE_BLOCKED')
        self.assertTrue(shadow['valid_for_strategy_evaluation'])
        self.assertGreater(shadow['net_pnl_bps'], 0.0)
        self.assertEqual(state.fee_blocked_shadow_positions, [])
        self.assertEqual(
            state.fee_blocked_shadow_clusters['semantic-zone']['status'], 'CLOSED'
        )

    def test_guardian_counterfactual_advances_without_lookahead(self):
        state = ram.SharedState()
        state.exchange_filters = {'tick_size': 0.1, 'step_size': 0.1, 'min_notional': 0.0}
        state.atr_1m = 1.0
        state.poc = 101.0
        state.vah = 102.0
        state.val = 98.0
        state.swing_high_m15 = 102.0
        state.swing_low_m15 = 98.0
        state.best_bid, state.best_ask = 99.99, 100.0
        state.bids_top_10, state.asks_top_10 = [[99.9, 10.0]], [[100.0, 10.0]]
        signal = {
            'setup_id': 'setup-cf', 'setup_generation': 1,
            'mode': 'TREND-PULLBACK', 'bias': 'LONG',
        }
        entry = economic.estimate_market_fill(state.asks_top_10, 1.0)
        cycle_id = journal.create_cycle(state, signal, 1.0, 100.0, {'mode': 'LOG_ONLY'})
        journal.activate_shadow(state, cycle_id, signal, 1.0, entry)

        soft_sl = state.shadow_position['soft_sl']
        state.best_bid, state.best_ask = soft_sl - 0.1, soft_sl
        state.bids_top_10, state.asks_top_10 = [
            [soft_sl - 0.1, 10.0]
        ], [[soft_sl, 10.0]]
        journal.shadow_step(state)
        self.assertEqual(len(state.guardian_counterfactuals), 1)
        tracker = state.guardian_counterfactuals[0]
        self.assertIsNone(tracker['normal_exit'])
        self.assertTrue(tracker['no_lookahead'])

        state.best_bid, state.best_ask = 102.1, 102.2
        state.bids_top_10, state.asks_top_10 = [[102.1, 10.0]], [[102.2, 10.0]]
        journal.shadow_step(state, now=tracker['started_at'] + 31.0)
        self.assertIn('30', tracker['checkpoints_bps'])
        self.assertEqual(tracker['normal_exit']['event'], 'TP2')
        self.assertIn('guardian_value_bps', tracker)

    def test_unlinked_exchange_close_is_forensic_not_heuristically_borrowed(self):
        state = ram.SharedState()
        signal = {'setup_id': 'stale', 'setup_generation': 1, 'mode': 'NEUTRAL-FADE', 'bias': 'LONG'}
        cycle_id = journal.create_cycle(state, signal, 1.0, 100.0, {'mode': 'LOG_ONLY'})
        journal.record_actual_order(
            state, cycle_id, 'ENTRY',
            {'orderId': 10, 'clientOrderId': 'entry', 'status': 'FILLED', 'executedQty': 1.0},
            1.0, 100.0,
        )
        entry_trade = {
            'id': 1, 'orderId': 10, 'side': 'BUY', 'positionSide': 'LONG',
            'price': '100', 'qty': '1', 'quoteQty': '100', 'commission': '0.04',
            'commissionAsset': 'USDT', 'realizedPnl': '0', 'time': 1000,
        }
        journal._apply_trade_fills(state, [entry_trade])
        state.trade_cycles[cycle_id]['status'] = 'OPEN'
        close_trade = {
            'id': 2, 'orderId': 11, 'side': 'SELL', 'positionSide': 'LONG',
            'price': '101', 'qty': '1', 'quoteQty': '101', 'commission': '0.0404',
            'commissionAsset': 'USDT', 'realizedPnl': '1', 'time': 2000,
        }
        journal._apply_trade_fills(state, [close_trade])
        cycle = state.trade_cycles[cycle_id]
        self.assertEqual(cycle['status'], 'OPEN')
        self.assertEqual(len(cycle['actual']['orders']), 1)
        self.assertEqual(cycle['actual']['exit_fill_ids'], [])
        self.assertIn('2', state.unresolved_forensic_fill_ids)
        unresolved = [
            item for item in state.journal_events
            if item.get('event') == 'UNRESOLVED_FORENSIC'
        ]
        self.assertEqual(len(unresolved), 1)

    def test_profit_protect_candidate_runs_only_in_shadow(self):
        state = ram.SharedState()
        state.atr_1m = 1.0
        cycle_id = 'pc-profit'
        state.trade_cycles[cycle_id] = {
            'position_cycle_id': cycle_id,
            'economic_observation': {'required_capture_bps': 12.0},
        }
        position = {
            'position_cycle_id': cycle_id, 'side': 'LONG', 'entry_price': 100.0,
            'original_hard_sl': 97.0, 'original_tp1': 101.0, 'original_tp2': 102.0,
            'expires_at': time.time() + 1000,
        }
        journal._start_counterfactual(
            state, position, 'SHARK_ADVERSE_CONFIRMED', 100.2
        )
        tracker = state.guardian_counterfactuals[0]
        self.assertEqual(tracker['profit_protect_candidate']['status'], 'ACTIVE')
        state.best_bid, state.best_ask = 102.1, 102.2
        journal._update_counterfactuals(state, time.time())
        candidate = tracker['profit_protect_candidate']
        self.assertEqual(candidate['status'], 'CLOSED')
        self.assertEqual(candidate['exit']['event'], 'TP2')
        self.assertGreater(candidate['exit']['net_bps'], 0.0)


class FailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_post_exception_releases_execution_claim(self):
        state = ram.SharedState()
        state.execution_in_flight = True
        state.execution_setup_id = 'pre-post-error'
        state.execution_generation = 7
        setup = {
            'setup_id': 'pre-post-error', 'generation': 7,
            'state': 'EXECUTING',
        }
        state.active_setups = {'error': setup}
        signal = {'setup_id': 'pre-post-error', 'setup_generation': 7}

        with mock.patch.object(
            executor, '_xu_ly_tin_hieu',
            new=mock.AsyncMock(side_effect=ValueError('malformed depth')),
        ):
            with self.assertRaisesRegex(ValueError, 'malformed depth'):
                await executor.xu_ly_tin_hieu(signal, state, object())

        self.assertFalse(state.execution_in_flight)
        self.assertEqual(setup['state'], 'INVALIDATED')

    async def test_invalid_soft_sl_geometry_blocks_before_exchange_post(self):
        class FakeAPI:
            def __init__(self):
                self.new_calls = 0
                self.testnet = True

            async def new_order(self, *args, **kwargs):
                self.new_calls += 1
                return {'status': 'FILLED'}, 200

        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.execution_in_flight = True
        state.execution_setup_id = 'geometry'
        state.execution_generation = 1
        state.balance_usdt = 1000.0
        state.exchange_filters = {
            'step_size': 0.001, 'min_qty': 0.001,
            'tick_size': 0.1, 'min_notional': 5.0,
        }
        state.best_bid, state.best_ask = 99.99, 100.0
        state.execution_best_bid, state.execution_best_ask = 90.0, 90.1
        state.bids_top_10, state.asks_top_10 = [[99.99, 10.0]], [[100.0, 10.0]]
        state.atr_1m = 1.0
        state.poc, state.vah, state.val = 103.0, 105.0, 101.0
        state.swing_high_m15, state.swing_low_m15 = 110.0, 95.0
        state.cvd_buy_30m, state.cvd_sell_30m = 2.0, 1.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': now, 'event_id': 'fp:geometry',
        }
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        setup = {
            'setup_id': 'geometry', 'semantic_key': 'semantic-geometry',
            'generation': 1, 'state': 'EXECUTING', 'zone': 101.0,
        }
        state.active_setups = {'geometry': setup}
        signal = {
            'setup_id': 'geometry', 'setup_generation': 1,
            'client_order_id': 'smc_entry_geometry', 'bias': 'LONG',
            'mode': 'TREND-PULLBACK', 'size_pct': 50,
            'created_at': now, 'created_mono': time.monotonic(),
            'decision_price': 100.0,
        }
        api = FakeAPI()
        invalid_levels = {
            'hard_sl': 100.1,
            'soft_sl': 99.0,
            'soft_tp1': 101.5,
            'soft_tp2': 110.0,
        }
        with mock.patch.object(
            executor.risk, 'calculate_levels', return_value=invalid_levels
        ):
            self.assertFalse(await executor.xu_ly_tin_hieu(signal, state, api))
        self.assertEqual(api.new_calls, 0)
        cycle = next(iter(state.trade_cycles.values()))
        self.assertEqual(
            cycle['abort_reason'],
            'INVALID_LEVEL_GEOMETRY:LONG_SL_NOT_BELOW_ENTRY',
        )

    async def test_structural_fee_floor_blocks_before_exchange_post(self):
        class FakeAPI:
            def __init__(self):
                self.new_calls = 0

            async def new_order(self, *args, **kwargs):
                self.new_calls += 1
                return {'status': 'FILLED'}, 200

        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.execution_in_flight = True
        state.execution_setup_id = 'fee-floor'
        state.execution_generation = 1
        state.balance_usdt = 100.0
        state.exchange_filters = {
            'step_size': 0.001, 'min_qty': 0.001,
            'tick_size': 0.001, 'min_notional': 5.0,
        }
        state.best_bid, state.best_ask = 99.99, 100.0
        state.bids_top_10, state.asks_top_10 = [[99.99, 10.0]], [[100.0, 10.0]]
        state.atr_1m = 0.1
        state.poc, state.vah, state.val = 100.05, 100.1, 99.9
        state.swing_high_m15, state.swing_low_m15 = 101.0, 99.0
        state.cvd_buy_30m, state.cvd_sell_30m = 2.0, 1.0
        state.fp_last_imbalance = {'dir': 'buy', 'ts': now, 'event_id': 'fp:fee'}
        state.absorption_reaction = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'classification': 'PASSIVE_HOLD_REACTION',
            'event_id': 'absreact:fee:LONG',
        }
        state.value_area_sweep = {
            'active': True, 'direction': 'LONG', 'ts': now,
            'event_id': 'vasweep:fee:LONG',
        }
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        setup = {'setup_id': 'fee-floor', 'generation': 1, 'state': 'EXECUTING'}
        state.active_setups = {'fee': setup}
        signal = {
            'setup_id': 'fee-floor', 'setup_generation': 1,
            'client_order_id': 'smc_entry_fee', 'bias': 'LONG',
            'mode': 'NEUTRAL-FADE', 'size_pct': 50,
            'created_at': now, 'created_mono': time.monotonic(),
            'decision_price': 100.0,
        }
        api = FakeAPI()
        self.assertFalse(await executor.xu_ly_tin_hieu(signal, state, api))
        self.assertEqual(api.new_calls, 0)
        self.assertEqual(setup['state'], 'INVALIDATED')
        cycle = next(iter(state.trade_cycles.values()))
        self.assertEqual(cycle['abort_reason'], 'STRUCTURAL_FEE_FLOOR_BLOCKED')
        self.assertEqual(len(state.fee_blocked_shadow_positions), 1)
        self.assertEqual(cycle['shadow']['status'], 'OPEN')
        self.assertEqual(cycle['shadow']['shadow_kind'], 'FEE_BLOCKED')
        self.assertEqual(
            cycle['shadow']['sample_independence'],
            'PRIMARY_SEMANTIC_OPPORTUNITY',
        )

    async def test_full_entry_flow_confirms_position_and_hard_sl(self):
        class FakeAPI:
            def __init__(self):
                self.entry_calls = 0
                self.sl_calls = 0
                self.testnet = True
                self.sl_trigger = None

            async def new_order(self, *args, **kwargs):
                self.entry_calls += 1
                return {'status': 'FILLED', 'avgPrice': '90.0'}, 200

            async def get_positions(self, symbol):
                return [{
                    'positionSide': 'LONG', 'positionAmt': '0.5', 'entryPrice': '90.0',
                }], 200

            async def new_algo_order(self, **params):
                self.sl_calls += 1
                self.sl_trigger = params['triggerPrice']
                return {'algoId': 77, 'clientAlgoId': params['clientAlgoId']}, 200

        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.execution_in_flight = True
        state.execution_setup_id = 'setup-live'
        state.execution_generation = 3
        state.execution_client_order_id = 'smc_entry_live'
        state.balance_usdt = 100.0
        state.exchange_filters = {
            'step_size': 0.0001, 'min_qty': 0.0001,
            'tick_size': 0.1, 'min_notional': 5.0,
        }
        state.best_bid = 99.99
        state.best_ask = 100.0
        state.execution_best_bid = 89.9
        state.execution_best_ask = 90.0
        state.bids_top_10 = [[99.99, 10.0]]
        state.asks_top_10 = [[100.0, 10.0]]
        state.atr_1m = 2.0
        state.poc = 100.0
        state.vah = 110.0
        state.val = 90.0
        state.swing_high_m15 = 115.0
        state.swing_low_m15 = 85.0
        state.cvd_buy_30m = 2.0
        state.cvd_sell_30m = 1.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        state.fp_last_imbalance = {'dir': 'buy', 'ts': now, 'event_id': 'fp:live'}
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        setup = {
            'setup_id': 'setup-live', 'generation': 3, 'state': 'EXECUTING',
        }
        state.active_setups = {'live': setup}
        signal = {
            'setup_id': 'setup-live', 'setup_generation': 3,
            'client_order_id': 'smc_entry_live', 'bias': 'LONG',
            'mode': 'TREND-PULLBACK', 'size_pct': 50,
            'created_mono': time.monotonic(), 'event_ids': ['fp:live'],
            'decision_price': 100.0,
        }
        api = FakeAPI()
        self.assertTrue(await executor.xu_ly_tin_hieu(signal, state, api))
        self.assertTrue(state.vi_the_hien_tai.active)
        position = state.vi_the_hien_tai
        self.assertEqual(position.entry_price, 100.0)
        self.assertEqual(position.strategy_entry_price, 100.0)
        self.assertEqual(position.execution_entry_price, 90.0)
        self.assertEqual(position.soft_sl, 59.0)
        self.assertEqual(position.hard_sl, position.strategy_hard_sl - 10.0)
        self.assertAlmostEqual(float(api.sl_trigger), position.hard_sl)
        self.assertEqual(state.vi_the_hien_tai.hard_sl_algo_id, 77)
        self.assertFalse(state.execution_in_flight)
        self.assertEqual(setup['state'], 'EXECUTED')
        self.assertEqual(api.entry_calls, 1)
        self.assertEqual(api.sl_calls, 1)

    async def test_armed_window_rescores_and_claims_only_once(self):
        state = ram.SharedState()
        state.system_ready = True
        state.trading_enabled = True
        state.hang_doi_tin_hieu = asyncio.Queue(maxsize=5)
        state.cvd_buy_30m = 2.0
        state.cvd_sell_30m = 1.0
        state.vol_pct90 = 10.0
        state.current_vol_3s = 20.0
        state.current_cvd_buy_3s = 15.0
        state.current_cvd_sell_3s = 5.0
        setup = {
            'setup_id': 'BTCUSDT:zone:sv1:a1', 'generation': 1,
            'state': 'ARMED_WINDOW', 'mode': 'TREND-PULLBACK', 'bias': 'LONG',
            'zone': 100.0, 'zone_id': 'TREND-PULLBACK:LONG:0', 'kind': 'zone',
        }
        state.active_setups = {'zone': setup}
        mode = {'modes': ['TREND-PULLBACK'], 'bias': 'LONG'}
        self.assertIsNone(
            commander.phan_tich_va_ra_lenh(
                state, mode, 'TREND-PULLBACK', 'LONG', setup=setup
            )
        )
        state.fp_last_imbalance = {
            'dir': 'buy', 'ts': time.time(), 'used': False, 'event_id': 'fp:2:buy',
        }
        signal = commander.phan_tich_va_ra_lenh(
            state, mode, 'TREND-PULLBACK', 'LONG', setup=setup
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal['setup_zone'], 100.0)
        self.assertEqual(signal['setup_zone_id'], 'TREND-PULLBACK:LONG:0')
        self.assertEqual(signal['setup_kind'], 'zone')
        self.assertEqual(setup['state'], 'EXECUTING')
        self.assertEqual(state.hang_doi_tin_hieu.qsize(), 1)
        self.assertIsNone(
            commander.phan_tich_va_ra_lenh(
                state, mode, 'TREND-PULLBACK', 'LONG', setup=setup
            )
        )
        self.assertEqual(state.hang_doi_tin_hieu.qsize(), 1)
        decisions = [
            event['payload']['result'] for event in state.journal_events
            if event.get('event') == 'DECISION_EVALUATED'
        ]
        self.assertIn('CORE_REJECT', decisions)
        self.assertIn('CLAIMED', decisions)

    async def test_preflight_veto_can_cancel_after_core_reached(self):
        state = ram.SharedState()
        now = time.time()
        state.system_ready = True
        state.trading_enabled = True
        state.execution_in_flight = True
        state.execution_setup_id = 'setup-1'
        state.execution_generation = 4
        state.atr_1m = 10.0
        state.best_bid = 100.0
        state.best_ask = 100.01
        state.cvd_buy_30m = 2.0
        state.fp_last_imbalance = {'dir': 'buy', 'ts': now, 'event_id': 'fp:x'}
        for _, (field, _) in watchdog.FEEDS.items():
            setattr(state, field, now)
        # A fresh hard veto must still win even when the claim lease metadata is
        # intentionally absent/expired. Use independent aggressive flow rather
        # than the separately calibrated wall-pull advisory path.
        state.p95_value = 1.0
        state.vol_pct90 = 1.0
        state.current_cvd_buy_3s = 0.0
        state.current_cvd_sell_3s = 10.0
        setup = {'setup_id': 'setup-1', 'generation': 4, 'state': 'EXECUTING'}
        state.active_setups = {'x': setup}
        signal = {
            'setup_id': 'setup-1', 'setup_generation': 4, 'bias': 'LONG',
            'mode': 'TREND-PULLBACK',
        }
        ok, reason = executor.preflight_signal(signal, state)
        self.assertFalse(ok)
        self.assertIn('VETO', reason)

    async def test_entry_timeout_recovers_by_same_client_id(self):
        class FakeAPI:
            def __init__(self):
                self.new_calls = 0
                self.query_ids = []

            async def new_order(self, *args, **kwargs):
                self.new_calls += 1
                return {'code': 'NETWORK'}, 599

            async def query_order(self, symbol, client_id):
                self.query_ids.append(client_id)
                return {'status': 'FILLED', 'avgPrice': '100'}, 200

        state = ram.SharedState()
        api = FakeAPI()
        signal = {'client_order_id': 'smc_entry_stable', 'bias': 'LONG'}
        result, status, recovered = await executor.submit_entry_idempotent(
            api, state, signal, 'BUY', 0.1, {'positionSide': 'LONG'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(recovered)
        self.assertEqual(api.new_calls, 1)
        self.assertEqual(api.query_ids, ['smc_entry_stable'])

    async def test_close_timeout_does_not_post_a_second_id(self):
        class FakeAPI:
            def __init__(self):
                self.new_calls = 0
                self.position_checks = 0

            async def new_order(self, *args, **kwargs):
                self.new_calls += 1
                return {'code': 'NETWORK'}, 599

            async def query_order(self, symbol, client_id):
                return {'code': 'NETWORK'}, 599

            async def get_positions(self, symbol):
                self.position_checks += 1
                qty = '1.0' if self.position_checks <= 3 else '0.0'
                return [{'positionSide': 'LONG', 'positionAmt': qty}], 200

        state = ram.SharedState()
        state.vi_the_hien_tai.active = True
        state.vi_the_hien_tai.side = 'LONG'
        state.vi_the_hien_tai.qty = 1.0
        state.exchange_filters = {'step_size': 0.001}
        api = FakeAPI()
        _, first_status = await guardian._submit_close_order(
            api, 'BTCUSDT', 'LONG', 1.0, state, 'close'
        )
        self.assertEqual(first_status, 202)
        second_status = None
        for _ in range(5):
            state.pending_close['next_check_at'] = 0.0
            _, second_status = await guardian._submit_close_order(
                api, 'BTCUSDT', 'LONG', 1.0, state, 'close'
            )
            if second_status == 200:
                break
        self.assertEqual(second_status, 200)
        self.assertEqual(api.new_calls, 1)
        self.assertIsNone(state.pending_close)

    async def test_filled_close_result_wins_over_stale_position_endpoint(self):
        class FakeAPI:
            def __init__(self):
                self.new_calls = 0
                self.position_calls = 0

            async def new_order(self, *args, **kwargs):
                self.new_calls += 1
                return {
                    'orderId': 99, 'clientOrderId': kwargs['newClientOrderId'],
                    'status': 'FILLED', 'executedQty': '1.0', 'avgPrice': '101.0',
                }, 200

            async def get_positions(self, symbol):
                self.position_calls += 1
                return [{'positionSide': 'LONG', 'positionAmt': '1.0'}], 200

        state = ram.SharedState()
        state.vi_the_hien_tai.active = True
        state.vi_the_hien_tai.side = 'LONG'
        state.vi_the_hien_tai.qty = 1.0
        state.co_lenh_mo = True
        state.exchange_filters = {'step_size': 0.001}
        api = FakeAPI()
        self.assertTrue(await guardian.close_position(
            api, 'BTCUSDT', 'LONG', 1.0, state, reason='TEST_CLOSE'
        ))
        self.assertEqual(api.new_calls, 1)
        self.assertEqual(api.position_calls, 0)
        self.assertFalse(state.vi_the_hien_tai.active)
        self.assertIsNone(state.pending_close)

    async def test_reconcile_releases_confirmed_nonexistent_unknown_entry(self):
        class FakeAPI:
            async def get_positions(self, symbol):
                return [], 200

            async def query_order(self, symbol, client_id):
                return {'code': -2013}, 400

            async def get_open_algo_orders(self, symbol):
                return [], 200

        state = ram.SharedState()
        state.execution_in_flight = True
        state.execution_unknown = True
        state.execution_unknown_since = time.time() - 20.0
        state.execution_client_order_id = 'smc_entry_missing'
        state.execution_setup_id = 'setup-x'
        state.execution_generation = 1
        state.active_setups = {
            'x': {'setup_id': 'setup-x', 'generation': 1, 'state': 'EXECUTING'}
        }
        self.assertTrue(await reconcile.reconcile_once(state, FakeAPI()))
        self.assertFalse(state.execution_in_flight)
        self.assertFalse(state.execution_unknown)

    async def test_hard_sl_network_unknown_is_recovered_without_duplicate(self):
        class FakeAPI:
            def __init__(self):
                self.calls = 0
                self.order = None

            async def new_algo_order(self, **params):
                self.calls += 1
                self.order = {
                    'algoId': 123,
                    'clientAlgoId': params['clientAlgoId'],
                    'orderType': 'STOP_MARKET',
                    'positionSide': params['positionSide'],
                }
                return {'code': 'NETWORK'}, 599

            async def get_open_algo_orders(self, symbol):
                return [self.order], 200

        state = ram.SharedState()
        state.vi_the_hien_tai.active = True
        state.vi_the_hien_tai.side = 'LONG'
        state.vi_the_hien_tai.hard_sl = 63000.0
        api = FakeAPI()
        ok, result = await executor.place_hard_sl(state, api)
        self.assertTrue(ok)
        self.assertEqual(result['algoId'], 123)
        self.assertEqual(api.calls, 1)

    async def test_hard_sl_is_tick_canonical_and_client_error_is_not_retried(self):
        class FakeAPI:
            def __init__(self):
                self.calls = 0
                self.trigger = None

            async def new_algo_order(self, **params):
                self.calls += 1
                self.trigger = params['triggerPrice']
                return {'code': -1111, 'msg': 'Precision is over the maximum'}, 400

            async def get_open_algo_orders(self, symbol):
                return [], 200

        state = ram.SharedState()
        state.exchange_filters = {'tick_size': 0.1}
        state.vi_the_hien_tai.active = True
        state.vi_the_hien_tai.side = 'LONG'
        state.vi_the_hien_tai.hard_sl = 62850.700000000004
        api = FakeAPI()
        ok, result = await executor.place_hard_sl(state, api)
        self.assertFalse(ok)
        self.assertEqual(result['code'], -1111)
        self.assertEqual(api.trigger, '62850.7')
        self.assertEqual(api.calls, 1)

    async def test_reconcile_cancels_only_smc_orphan_algos(self):
        class FakeAPI:
            def __init__(self):
                self.cancelled = []

            async def get_positions(self, symbol):
                return [], 200

            async def get_open_algo_orders(self, symbol):
                return [
                    {'algoId': 1, 'clientAlgoId': 'smc_sl_1'},
                    {'algoId': 2, 'clientAlgoId': 'manual_stop'},
                ], 200

            async def cancel_algo_order(self, algo_id):
                self.cancelled.append(algo_id)
                return {}, 200

        state = ram.SharedState()
        api = FakeAPI()
        self.assertTrue(await reconcile.reconcile_once(state, api))
        self.assertEqual(api.cancelled, [1])


if __name__ == '__main__':
    unittest.main()
