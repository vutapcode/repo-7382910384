import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import unittest


def _load():
    path = Path(__file__).resolve().parents[1] / "loi_he_thong" / "entry_council_shadow.py"
    spec = importlib.util.spec_from_file_location("entry_council_shadow_tested", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


council = _load()


def _state(now=1000.0, side="LONG", confidence=0.80):
    state = SimpleNamespace(
        _api_is_testnet=True,
        bias_state=side,
        bias_confidence=confidence,
        bias_updated_at=now,
        best_bid=100.095,
        best_ask=100.105,
        coinbase_price=100.10,
        thoi_gian_coinbase_ticker_cuoi=now,
        atr_1m=0.10,
        current_cvd_buy_3s=9.0,
        current_cvd_sell_3s=1.0,
        coinbase_cvd_3s=8.0,
        coinbase_volume_3s=10.0,
        coinbase_flow_3s_ts=now,
        danh_sach_khop_lenh_futures=deque([
            {
                "gia": 100.10,
                "khoi_luong": 5.0,
                "ban_chu_dong": False,
                "thoi_gian_ms": int(now * 1000),
            }
        ]),
    )
    state.entry_shadow_price_history = deque([
        {"ts": now - 2.0, "spot": 100.0, "coinbase": 100.0, "futures": 100.0}
    ], maxlen=128)
    return state


class EntryCouncilShadowTests(unittest.TestCase):
    def test_non_testnet_is_noop(self):
        state = _state()
        state._api_is_testnet = False
        result = council.update_state(state, now=1000.0)
        self.assertIsNone(result)
        self.assertFalse(hasattr(state, "entry_shadow_decision"))

    def test_abstain_bias_waits(self):
        state = _state(side="ABSTAIN")
        result = council.evaluate(state, now=1000.0)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["reason"], "BIAS_ABSTAIN")

    def test_two_or_more_s_tier_passes_go(self):
        result = council.evaluate(_state(), now=1000.0)
        self.assertEqual(result["decision"], "GO")
        self.assertGreaterEqual(result["s_quorum"], 2)
        self.assertEqual(
            result["s_votes"]["S1_cross_venue_price_acceptance"]["status"], "PASS"
        )
        self.assertEqual(
            result["s_votes"]["S2_multi_venue_executed_flow"]["status"], "PASS"
        )

    def test_legacy_structure_fields_cannot_change_vote(self):
        baseline = _state()
        result_a = council.evaluate(baseline, now=1000.0)

        mutated = _state()
        mutated.trend_m15 = "BEARISH"
        mutated.structure_transition = "CHOCH_DOWN"
        mutated.structure_broken_level = 999999.0
        mutated.poc = 1.0
        mutated.vah = 2.0
        mutated.val = 0.5
        mutated.obi = -0.99
        mutated.wall_pull_flag = {"active": True, "side": "BID"}
        result_b = council.evaluate(mutated, now=1000.0)

        self.assertEqual(result_a["decision"], result_b["decision"])
        self.assertEqual(result_a["s_votes"], result_b["s_votes"])
        self.assertEqual(result_a["a_votes"], result_b["a_votes"])

    def test_two_opposing_price_and_flow_sources_reject(self):
        state = _state(side="LONG")
        state.best_bid = 99.895
        state.best_ask = 99.905
        state.coinbase_price = 99.90
        state.current_cvd_buy_3s = 1.0
        state.current_cvd_sell_3s = 9.0
        state.coinbase_cvd_3s = -8.0
        state.coinbase_volume_3s = 10.0
        state.danh_sach_khop_lenh_futures = deque([
            {
                "gia": 99.90,
                "khoi_luong": 5.0,
                "ban_chu_dong": True,
                "thoi_gian_ms": 1000000,
            }
        ])
        result = council.evaluate(state, now=1000.0)
        self.assertEqual(result["decision"], "REJECT")


if __name__ == "__main__":
    unittest.main()
