import unittest

from recorder.cross_derivative_context import CrossDerivativeContext


def record(stream, source, available_ms, oi=None, **payload):
    if oi is not None:
        payload["open_interest"] = oi
    return {
        "stream": stream,
        "source": source,
        "available_time_ms": available_ms,
        "event_time_ms": available_ms,
        "epoch": 1,
        "payload": payload,
    }


class CrossDerivativeContextTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.tracker = CrossDerivativeContext(
            lambda stream, payload, event_time_ms=None: self.rows.append(
                (stream, payload, event_time_ms)
            )
        )

    def test_both_contracting_with_liquidation_is_only_hypothesis(self):
        self.tracker.observe(record("open_interest", "binance_usdm", 1_000, 100))
        self.tracker.observe(record("bybit_derivative_state", "bybit_linear", 1_100, 200))
        self.tracker.observe(record(
            "bybit_liquidation", "bybit_linear", 1_900,
            executed_size="2.0", liquidated_position_side="LONG",
        ))
        self.tracker.observe(record("open_interest", "binance_usdm", 2_000, 99))
        self.tracker.observe(record("bybit_derivative_state", "bybit_linear", 2_100, 198))
        payload = self.rows[-1][1]
        self.assertEqual(payload["relation"], "BOTH_CONTRACTING")
        self.assertEqual(
            payload["mechanism_hypothesis"],
            "CROSS_DERIVATIVE_FORCED_UNWIND_CANDIDATE",
        )
        self.assertFalse(payload["authority"])
        self.assertFalse(payload["direction_authority"])
        self.assertFalse(payload["veto_authority"])

    def test_epoch_change_does_not_bridge_oi_delta(self):
        self.tracker.observe(record("open_interest", "binance_usdm", 1_000, 100))
        changed = record("open_interest", "binance_usdm", 2_000, 99)
        changed["epoch"] = 2
        self.tracker.observe(changed)
        self.tracker.observe(record("bybit_derivative_state", "bybit_linear", 1_100, 200))
        self.tracker.observe(record("bybit_derivative_state", "bybit_linear", 2_100, 198))
        self.assertEqual(self.rows, [])

    def test_divergence_never_creates_side(self):
        self.tracker.observe(record("open_interest", "binance_usdm", 1_000, 100))
        self.tracker.observe(record("bybit_derivative_state", "bybit_linear", 1_100, 200))
        self.tracker.observe(record("open_interest", "binance_usdm", 2_000, 101))
        self.tracker.observe(record("bybit_derivative_state", "bybit_linear", 2_100, 199))
        payload = self.rows[-1][1]
        self.assertEqual(payload["relation"], "DIVERGENT")
        self.assertNotIn("side", payload)


if __name__ == "__main__":
    unittest.main()
