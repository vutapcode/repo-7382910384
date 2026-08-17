import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import unittest


def _load():
    path = Path(__file__).resolve().parents[1] / "2_suy_luan_mapping" / "bias_council.py"
    spec = importlib.util.spec_from_file_location("bias_council_tested", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


council = _load()


def state():
    s = SimpleNamespace(
        best_bid=100.0, best_ask=100.1, coinbase_price=100.05,
        thoi_gian_coinbase_ticker_cuoi=100.0, thoi_gian_tick_cuoi=100.0, open_interest=1000.0, thoi_gian_vi_mo_cuoi=100.0,
        atr_1m=0.05, funding_rate=0.0, flow_1s_buffer=deque(),
        danh_sach_khop_lenh_futures=deque(), coinbase_cvd_1m=0.0,
        thoi_gian_coinbase_cuoi=100.0,
    )
    s.bias_price_history = deque([
        {"ts": 85.0, "spot": 99.0, "coinbase": 99.0, "futures": 99.0, "oi": 990.0}
    ], maxlen=192)
    s.danh_sach_khop_lenh_futures.append(
        {"gia": 100.2, "thoi_gian_ms": 100000.0, "khoi_luong": 1.0, "ban_chu_dong": False}
    )
    return s


class BiasCouncilTests(unittest.TestCase):
    def test_two_s_votes_create_long_consensus(self):
        s = state()
        r = council.evaluate(s, now=100.0, force_full=False)
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "LONG")
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "LONG")
        self.assertEqual(r["bias"], "LONG")
        self.assertGreaterEqual(r["quorum"], 2)

    def test_falling_oi_abstains_in_price_x_oi(self):
        s = state()
        s.open_interest = 980.0
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "ABSTAIN")

    def test_old_structure_fields_do_not_vote(self):
        s = state()
        s.trend_m15 = "BEARISH"
        s.structure_transition = "CHOCH_DOWN"
        s.poc = 100.0
        s.obi = -0.99
        r = council.evaluate(s, now=100.0)
        self.assertEqual(set(r["s_votes"]), {"S1_cross_price", "S2_price_x_oi", "S3_multi_flow"})
        self.assertEqual(set(r["a_votes"]), {"A1_funding_basis", "A2_spot_lead"})

    def test_output_is_direction_confidence_only_contract(self):
        r = council.evaluate(state(), now=100.0)
        self.assertIn(r["bias"], ("LONG", "SHORT", "ABSTAIN"))
        self.assertGreaterEqual(r["confidence"], 0.0)
        self.assertLessEqual(r["confidence"], 1.0)
        for forbidden in ("entry", "zone", "setup", "action", "price_entry"):
            self.assertNotIn(forbidden, r)



    def test_stale_spot_cannot_vote_direction(self):
        s = state()
        s.thoi_gian_tick_cuoi = 90.0
        r = council.evaluate(s, now=100.0)
        self.assertFalse(r["freshness"]["spot"])
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "ABSTAIN")

    def test_covering_is_story_not_new_long_build(self):
        s = state()
        s.open_interest = 980.0
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["metrics"]["regime"], "SHORT_COVERING")
        self.assertNotEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "LONG")

    def test_contract_remains_direction_only(self):
        r = council.evaluate(state(), now=100.0)
        self.assertEqual(r["contract"], "DIRECTION_ONLY_NO_ENTRY_TIMING")
        self.assertIn("story", r)
        for forbidden in ("entry", "zone", "setup", "action", "price_entry", "stop_loss", "take_profit"):
            self.assertNotIn(forbidden, r)


if __name__ == "__main__":
    unittest.main()
