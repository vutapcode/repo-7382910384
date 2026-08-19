import unittest
from collections import deque
from types import SimpleNamespace

from loi_he_thong import ws_idle_recovery_hook as hook


class WsIdleRecoveryHookTest(unittest.TestCase):
    def _state(self):
        return SimpleNamespace(
            danh_sach_khop_lenh=deque([{"x": 1}]),
            flow_1s_buffer=deque([{"x": 2}]),
            trade_flow_timeline=deque([{"x": 3}]),
            _micro_regime_hist=deque([{"x": 4}]),
            thoi_gian_dong_tien_cuoi=123.0,
            last_trade_event_time_s=124.0,
            last_3s_window_ts=125.0,
            current_vol_3s=7.0,
            current_cvd_sell_3s=3.0,
            current_cvd_buy_3s=4.0,
            # Long-horizon context must survive a short transport outage.
            cvd_history=deque([11.0, 12.0]),
            cvd_30m_history=deque([21.0, 22.0]),
            vol_3s_history=deque([31.0, 32.0]),
            vol_3s_pct90=99.0,
            cvd_current=55.0,
        )

    def test_spot_gap_clears_only_short_lived_causal_evidence(self):
        state = self._state()
        hook._reset_spot_causal_epoch(state)

        self.assertEqual(len(state.danh_sach_khop_lenh), 0)
        self.assertEqual(len(state.flow_1s_buffer), 0)
        self.assertEqual(len(state.trade_flow_timeline), 0)
        self.assertEqual(len(state._micro_regime_hist), 0)
        self.assertEqual(state.thoi_gian_dong_tien_cuoi, 0.0)
        self.assertEqual(state.last_trade_event_time_s, 0.0)
        self.assertEqual(state.last_3s_window_ts, 0.0)
        self.assertEqual(state.current_vol_3s, 0.0)
        self.assertEqual(state.current_cvd_sell_3s, 0.0)
        self.assertEqual(state.current_cvd_buy_3s, 0.0)

        self.assertEqual(list(state.cvd_history), [11.0, 12.0])
        self.assertEqual(list(state.cvd_30m_history), [21.0, 22.0])
        self.assertEqual(list(state.vol_3s_history), [31.0, 32.0])
        self.assertEqual(state.vol_3s_pct90, 99.0)
        self.assertEqual(state.cvd_current, 55.0)


if __name__ == "__main__":
    unittest.main()
