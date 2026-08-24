from collections import deque
from types import SimpleNamespace
import unittest

from loi_he_thong import futures_flow_hardening as hardening


def state():
    return SimpleNamespace(
        danh_sach_khop_lenh=deque(),
        danh_sach_khop_lenh_futures=deque(maxlen=12_000),
    )


class FuturesFlowHardeningTests(unittest.TestCase):
    def test_combined_stream_envelope_is_unwrapped_without_changing_event(self):
        event = {"e": "aggTrade", "p": "100", "q": "0.1", "m": False}
        self.assertIs(hardening._unwrap_combined_stream({
            "stream": "btcusdt@aggTrade", "data": event,
        }), event)
        self.assertEqual(hardening._unwrap_combined_stream(event), event)

    def test_spot_trade_preserves_canonical_cvd_queue_contract(self):
        s = state()
        result = hardening._apply_spot_event(s, {
            "E": 1_000, "p": "100", "q": "0.25", "m": False,
        }, 1_100.0)
        self.assertEqual(result, "TRADE")
        self.assertEqual(s.thoi_gian_dong_tien_cuoi, 1.1)
        self.assertEqual(s.danh_sach_khop_lenh[-1]["nguon"], "SPOT")

        hardening._apply_spot_event(s, {
            "E": 1_200, "p": "101", "q": "0.40", "m": True,
        }, 1_250.0)
        self.assertTrue(s.danh_sach_khop_lenh[-1]["ban_chu_dong"])
        self.assertEqual(s.danh_sach_khop_lenh[-1]["khoi_luong"], 0.40)

    def test_invalid_spot_trade_does_not_mark_flow_fresh(self):
        s = state()
        result = hardening._apply_spot_event(
            s, {"p": "100", "q": "0", "m": False}, 2_000.0
        )
        self.assertEqual(result, "INVALID")
        self.assertFalse(hasattr(s, "thoi_gian_dong_tien_cuoi"))
        self.assertEqual(len(s.danh_sach_khop_lenh), 0)

    def test_trade_updates_canonical_ring_and_freshness(self):
        s = state()
        result = hardening._apply_futures_event(s, {
            "e": "trade", "E": 1_000, "p": "100", "q": "0.25", "m": False,
        }, 1_100.0)
        self.assertEqual(result, "TRADE")
        self.assertEqual(len(s.danh_sach_khop_lenh_futures), 1)
        self.assertEqual(s.thoi_gian_dong_tien_futures_cuoi, 1.1)

        hardening._apply_futures_event(s, {
            "e": "trade", "E": 1_200, "p": "101", "q": "0.40", "m": True,
        }, 1_250.0)
        self.assertTrue(s.danh_sach_khop_lenh_futures[-1]["ban_chu_dong"])
        self.assertEqual(s.danh_sach_khop_lenh_futures[-1]["khoi_luong"], 0.40)
        self.assertEqual(len(s.futures_flow_1s_buffer), 1)
        self.assertAlmostEqual(s.futures_flow_1s_buffer[-1]["buy"], 0.25)
        self.assertAlmostEqual(s.futures_flow_1s_buffer[-1]["sell"], 0.40)

    def test_bias_flow_buckets_retain_exact_60_second_aggregate(self):
        s = state()
        for second in range(1, 62):
            hardening._apply_futures_event(s, {
                "e": "aggTrade", "E": second * 1000,
                "p": "100", "q": "0.10", "m": second % 2 == 0,
            }, second * 1000.0)
        self.assertLessEqual(len(s.danh_sach_khop_lenh_futures), 21)
        self.assertEqual(len(s.futures_flow_1s_buffer), 61)
        total = sum(
            row["buy"] + row["sell"] for row in s.futures_flow_1s_buffer
        )
        self.assertAlmostEqual(total, 6.1)

    def test_force_order_is_not_part_of_canonical_flow_contract(self):
        s = state()
        result = hardening._apply_futures_event(s, {
            "e": "forceOrder", "E": 2_000,
            "o": {"S": "SELL", "q": "2", "ap": "100"},
        }, 2_100.0)
        self.assertEqual(result, "IGNORED")
        self.assertEqual(len(s.danh_sach_khop_lenh_futures), 0)

    def test_unrelated_event_does_not_mark_flow_fresh(self):
        s = state()
        result = hardening._apply_futures_event(s, {"e": "markPriceUpdate"}, 3_000.0)
        self.assertEqual(result, "IGNORED")
        self.assertFalse(hasattr(s, "thoi_gian_dong_tien_futures_cuoi"))


if __name__ == "__main__":
    unittest.main()
