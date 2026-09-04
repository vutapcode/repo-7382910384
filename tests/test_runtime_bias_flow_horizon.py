from collections import deque
from types import SimpleNamespace
import unittest

from importlib import import_module
from loi_he_thong import runtime_hardening_v3 as hardening

council = import_module("2_suy_luan_mapping.bias_council")


class RuntimeBiasOwnershipTests(unittest.TestCase):
    def test_runtime_hardening_does_not_replace_bias_s3(self):
        def sentinel(*_args, **_kwargs):
            return "ORIGINAL"

        bias = SimpleNamespace(s3=sentinel)
        base = SimpleNamespace(bias_council=bias)
        before = base.bias_council.s3
        status = hardening._install_bias(base)
        self.assertIs(base.bias_council.s3, before)
        self.assertEqual(status, "CANONICAL_BIAS_OWNER_UNCHANGED")

    def _state(self, seconds=60, *, spot_side="LONG", coinbase_side="LONG", futures_side="LONG"):
        start = 101 - seconds

        def rows(side, buy=0.10, sell=0.01):
            if side == "SHORT":
                buy, sell = sell, buy
            return deque(
                {"ts": float(sec), "second": sec, "buy": buy, "sell": sell}
                for sec in range(start, 101)
            )

        cb_delta = 0.9 if coinbase_side == "LONG" else -0.9
        return SimpleNamespace(
            flow_1s_buffer=rows(spot_side),
            futures_flow_1s_buffer=rows(futures_side, buy=0.20, sell=0.02),
            coinbase_cvd_1m=cb_delta,
            coinbase_volume_1m=1.0,
            thoi_gian_coinbase_cuoi=100.0,
        )

    def test_dual_cash_can_support_bias_flow(self):
        result = council.s3(self._state(), 100.0)
        self.assertEqual(result["vote"], "LONG")
        self.assertEqual(result["metrics"]["evidence_family"], "DUAL_CASH_FLOW")

    def test_futures_cannot_replace_missing_binance_spot_cash(self):
        s = self._state()
        s.flow_1s_buffer.clear()
        result = council.s3(s, 100.0)
        self.assertEqual(result["vote"], "ABSTAIN")
        self.assertEqual(result["reason"], "CASH_DERIVATIVE_NOT_DIRECTION_QUORUM")

    def test_binance_spot_futures_echo_is_not_independent_flow_quorum(self):
        s = self._state()
        s.coinbase_volume_1m = 0.0
        s.coinbase_cvd_1m = 0.0
        result = council.s3(s, 100.0)
        self.assertEqual(result["vote"], "ABSTAIN")
        self.assertEqual(result["reason"], "BINANCE_COMPLEX_ECHO_UNCORROBORATED")


if __name__ == "__main__":
    unittest.main()
