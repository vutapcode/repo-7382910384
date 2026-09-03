from collections import deque
from importlib import import_module
from types import SimpleNamespace
import unittest


delta = import_module("2_suy_luan_mapping.map_dong_tien.delta_cvd")


class SpotCumulativeFlowOwnerTests(unittest.TestCase):
    def _state(self):
        return SimpleNamespace(
            cvd_day=None,
            cvd_buy=0.0,
            cvd_sell=0.0,
            cvd_buy_30m=0.0,
            cvd_sell_30m=0.0,
            spot_cvd_buy_total=0.0,
            spot_cvd_sell_total=0.0,
            last_trade_event_time_s=0.0,
            trade_flow_timeline=deque(maxlen=32),
            cvd_30m_buffer=deque(),
            flow_1s_buffer=deque(),
            decision_revision=0,
            last_3s_window_ts=0.0,
            current_vol_3s=0.0,
            current_cvd_buy_3s=0.0,
            current_cvd_sell_3s=0.0,
            vol_3s_history=deque(maxlen=32),
            vol_pct90=0.0,
        )

    def test_mapper_updates_cumulative_spot_flow_exactly_once_per_trade(self):
        state = self._state()
        delta.cap_nhat_cvd({
            "khoi_luong": 0.25,
            "ban_chu_dong": False,
            "thoi_gian_ms": 1_788_443_400_000,
        }, state)
        self.assertAlmostEqual(state.spot_cvd_buy_total, 0.25)
        self.assertAlmostEqual(state.spot_cvd_sell_total, 0.0)

        delta.cap_nhat_cvd({
            "khoi_luong": 0.10,
            "ban_chu_dong": True,
            "thoi_gian_ms": 1_788_443_401_000,
        }, state)
        self.assertAlmostEqual(state.spot_cvd_buy_total, 0.25)
        self.assertAlmostEqual(state.spot_cvd_sell_total, 0.10)

    def test_transport_source_does_not_mutate_spot_cumulative_totals(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        source = (root / "1_tai_du_lieu" / "tai_dong_tien" / "tai_dong_tien.py").read_text(encoding="utf-8")
        self.assertNotIn("spot_cvd_buy_total +=", source)
        self.assertNotIn("spot_cvd_sell_total +=", source)
        self.assertIn("delta_cvd.cap_nhat_cvd", source)


if __name__ == "__main__":
    unittest.main()
