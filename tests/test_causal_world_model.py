import unittest

from recorder.causal_world_model import CausalWorldModel, MarketEventV1
from recorder.coinbase_l2 import CoinbaseL2Book, CoinbaseL2Error
from recorder.replay import DeterministicReplay


def record(stream, at_ms, payload=None, source="test"):
    return {
        "schema_version": 6,
        "code_version": "test",
        "config_version": "test",
        "source": source,
        "symbol": "BTCUSDT",
        "stream": stream,
        "event_time_ms": at_ms,
        "receive_time_ms": at_ms,
        "sequence_start": None,
        "sequence_end": None,
        "previous_sequence": None,
        "payload": dict(payload or {}),
    }


def trade(side, first, last, trade_id):
    buy = 2.0 if side == "LONG" else 0.2
    sell = 0.2 if side == "LONG" else 2.0
    return {
        "buy_qty": buy, "sell_qty": sell,
        "buy_quote": buy * last, "sell_quote": sell * last,
        "first_price": first, "last_price": last,
        "first_trade_id": trade_id, "last_trade_id": trade_id,
        "batch_available_time_ms": trade_id,
        "clock_valid": True,
    }


class MarketEventTests(unittest.TestCase):
    def test_availability_is_no_lookahead_boundary(self):
        row = record("binance_spot_trade_100ms", 1_000, {})
        row["receive_time_ms"] = 1_100
        row["payload"]["batch_available_time_ms"] = 1_101
        self.assertFalse(MarketEventV1.from_record(row).clock_valid)


class CoinbaseL2Tests(unittest.TestCase):
    def test_snapshot_update_and_checkpoint(self):
        book = CoinbaseL2Book()
        book.reset({
            "bids": [["99", "2"], ["98", "3"]],
            "asks": [["101", "2"], ["102", "3"]],
        })
        book.apply({"changes": [["buy", "99", "0"], ["buy", "100", "4"]]})
        checkpoint = book.checkpoint(2)
        self.assertEqual(checkpoint["bids"][0], ("100", "4"))
        self.assertEqual(checkpoint["epoch"], 1)

    def test_update_requires_snapshot(self):
        with self.assertRaises(CoinbaseL2Error):
            CoinbaseL2Book().apply({"changes": [["buy", "99", "1"]]})


class CausalWorldModelTests(unittest.TestCase):
    def test_derivatives_alone_never_become_action_authority(self):
        rows = []
        model = CausalWorldModel(
            lambda stream, payload, event_time_ms=None: rows.append(payload)
        )
        payload = trade("LONG", 100.0, 100.1, 1_000)
        model.observe(record("futures_trade_100ms", 1_000, payload))
        self.assertEqual(rows[-1]["supported_hypotheses"], ["DERIVATIVE_DISLOCATION"])
        self.assertFalse(rows[-1]["authority"])
        self.assertFalse(rows[-1]["eligible_for_entry"])
        self.assertEqual(rows[-1]["episode_side"], "ABSTAIN")

    def test_dual_cash_conversion_supports_cash_metaorder(self):
        rows = []
        model = CausalWorldModel(
            lambda stream, payload, event_time_ms=None: rows.append(payload)
        )
        model.observe(record(
            "binance_spot_trade_100ms", 1_000,
            trade("LONG", 100.0, 100.1, 1_000), "binance_spot",
        ))
        model.observe(record(
            "coinbase_spot_trade_100ms", 1_200,
            trade("LONG", 100.0, 100.2, 1_200), "coinbase_spot",
        ))
        self.assertIn("CASH_METAORDER", rows[-1]["supported_hypotheses"])
        self.assertEqual(rows[-1]["episode_side"], "LONG")
        self.assertEqual(rows[-1]["action_readiness"]["state"], "RESEARCH_ONLY")

    def test_absorbed_old_side_then_opposite_cash_is_control_transfer(self):
        rows = []
        model = CausalWorldModel(
            lambda stream, payload, event_time_ms=None: rows.append(payload)
        )
        model.observe(record("spot_liquidity_response", 1_000, {
            "side": "SHORT", "absorption_candidate": True,
        }))
        model.observe(record(
            "binance_spot_trade_100ms", 1_200,
            trade("LONG", 100.0, 100.1, 1_200), "binance_spot",
        ))
        model.observe(record(
            "coinbase_spot_trade_100ms", 1_300,
            trade("LONG", 100.0, 100.2, 1_300), "coinbase_spot",
        ))
        self.assertIn(
            "ABSORPTION_CONTROL_TRANSFER", rows[-1]["supported_hypotheses"]
        )

    def test_replay_world_output_is_deterministic(self):
        records = [
            record(
                "binance_spot_trade_100ms", 1_000,
                trade("SHORT", 100.0, 99.9, 1_000), "binance_spot",
            ),
            record(
                "coinbase_spot_trade_100ms", 1_200,
                trade("SHORT", 100.0, 99.8, 1_200), "coinbase_spot",
            ),
        ]
        first = DeterministicReplay(wavefront=False, canonical_mirror=False).run(records)
        second = DeterministicReplay(wavefront=False, canonical_mirror=False).run(records)
        self.assertEqual(
            first["causal_world_output_hash"], second["causal_world_output_hash"]
        )
        self.assertEqual(first["causal_world_generated_records"], 2)


if __name__ == "__main__":
    unittest.main()

