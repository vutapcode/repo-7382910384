from collections import deque
from types import SimpleNamespace
import unittest

from loi_he_thong import runtime_hardening_v3 as hardening


def _vote(side="ABSTAIN", conf=0.0, reason="", **metrics):
    return {
        "vote": side,
        "confidence": conf,
        "reason": reason,
        "metrics": metrics,
    }


class RuntimeBiasFlowHorizonTests(unittest.TestCase):
    def _base(self):
        bias = SimpleNamespace(vote=_vote)
        return SimpleNamespace(
            bias_council=bias,
            entry_council=SimpleNamespace(MIN_VOL_BTC_BY_VENUE={
                "spot": 0.015, "futures": 0.15, "coinbase": 0.002,
            }),
        )

    def _state(self, seconds=60):
        start = 101 - seconds
        return SimpleNamespace(
            flow_1s_buffer=deque(
                {"ts": float(sec), "buy": 0.10, "sell": 0.01}
                for sec in range(start, 101)
            ),
            futures_flow_1s_buffer=deque(
                {"second": sec, "ts": float(sec), "buy": 0.20, "sell": 0.02}
                for sec in range(start, 101)
            ),
            coinbase_cvd_1m=0.9,
            coinbase_volume_1m=1.0,
            coinbase_flow_1m_coverage_sec=float(max(0, seconds - 1)),
            thoi_gian_coinbase_cuoi=100.0,
            vol_pct90=1.0,
        )

    def test_full_background_window_can_vote_bias_flow(self):
        base = self._base()
        hardening._install_bias(base)
        result = base.bias_council.s3(self._state(60), 100.0)
        self.assertEqual(result["vote"], "LONG")
        self.assertEqual(result["metrics"]["horizon_sec"], 60.0)
        self.assertGreaterEqual(
            min(result["metrics"]["coverage_sec_by_venue"].values()), 45.0
        )

    def test_three_second_impulse_cannot_create_bias_flow_vote(self):
        base = self._base()
        hardening._install_bias(base)
        result = base.bias_council.s3(self._state(3), 100.0)
        self.assertEqual(result["vote"], "ABSTAIN")
        self.assertEqual(result["reason"], "INSUFFICIENT_MATERIAL_FLOW_CONSENSUS")
        self.assertEqual(result["metrics"]["horizon_sec"], 60.0)


if __name__ == "__main__":
    unittest.main()
