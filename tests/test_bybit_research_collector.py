import importlib
import unittest

from recorder.config import RecorderConfig
from recorder.health import HealthState


bybit = importlib.import_module('1_tai_du_lieu.tai_bybit.collector')


class BybitResearchCollectorTests(unittest.TestCase):
    def test_ticker_preserves_derivative_facts_without_direction(self):
        row = bybit.derivative_state({
            "ts": 1234, "cs": 9,
            "data": {
                "symbol": "BTCUSDT", "openInterest": "100.5",
                "openInterestValue": "8000000", "lastPrice": "80000",
                "markPrice": "80001", "indexPrice": "79999",
                "fundingRate": "0.00001",
            },
        })
        self.assertEqual(row["open_interest"], "100.5")
        self.assertEqual(row["semantic_role"], "DERIVATIVE_STRESS_DATA_ONLY")
        self.assertFalse(row["authority"])
        self.assertFalse(row["direction_authority"])

    def test_liquidation_is_forced_closing_flow_not_direction(self):
        rows = bybit.liquidation_rows({
            "ts": 2000,
            "data": [{
                "T": 1990, "s": "BTCUSDT", "S": "Buy",
                "v": "2.5", "p": "79000",
            }],
        })
        self.assertEqual(rows[0]["liquidated_position_side"], "LONG")
        self.assertEqual(rows[0]["semantic_role"], "FORCED_CLOSING_FLOW_ONLY")
        self.assertFalse(rows[0]["direction_authority"])

    def test_optional_feed_failure_does_not_degrade_canonical_health(self):
        health = HealthState(RecorderConfig())
        health.connection("binance", True)
        health.optional_connection("bybit_research_ws", False)
        health.optional_error("bybit_research_ws", "test failure")
        snapshot = health.snapshot()
        self.assertEqual(snapshot["current_status"], "OK")
        self.assertFalse(snapshot["optional_connections"]["bybit_research_ws"])


if __name__ == '__main__':
    unittest.main()
