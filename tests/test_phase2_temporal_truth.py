import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import orjson

from loi_he_thong import ignition_core
from loi_he_thong.market_event_contract import build_envelope
from recorder.collector import BinanceRecorder
from recorder.config import RecorderConfig
from recorder.health import HealthState
from recorder.replay import iter_merged_records


class _Store:
    def __init__(self):
        self.rows = []

    def publish(self, record):
        self.rows.append(record)
        return True


class CanonicalTemporalContractTests(unittest.TestCase):
    def test_recorder_emits_v7_availability_epoch_and_health(self):
        store = _Store()
        config = RecorderConfig()
        recorder = BinanceRecorder(config, store, HealthState(config))
        recorder._advance_epoch("test_stream")
        recorder.emit(
            "test_stream", {"clock_valid": True}, event_time_ms=900,
            receive_time_ms=1_000, available_time_ms=1_025,
            receive_time_monotonic_ns=10,
            available_time_monotonic_ns=20, source="test",
        )
        row = store.rows[-1]
        self.assertEqual(row["schema_version"], 7)
        self.assertEqual(row["exchange_event_time_ms"], 900)
        self.assertEqual(row["available_time_ms"], 1_025)
        self.assertEqual(row["available_time_monotonic_ns"], 20)
        self.assertEqual(row["epoch"], 1)
        self.assertEqual(row["source_health"], "FRESH")
        self.assertTrue(row["event_id"].startswith("me:"))

    def test_exchange_stream_without_calibration_never_claims_zero_uncertainty(self):
        store = _Store()
        config = RecorderConfig()
        recorder = BinanceRecorder(config, store, HealthState(config))
        recorder.emit(
            "open_interest", {"openInterest": "100", "time": 900},
            event_time_ms=900, receive_time_ms=1_000,
            source="binance_usdm",
        )
        row = store.rows[-1]
        self.assertGreaterEqual(row["clock_uncertainty_ms"], 250.0)
        self.assertEqual(
            row["payload"]["causal_order_time_basis"],
            "CORRECTED_EVENT_TIME_WITH_UNCERTAINTY",
        )

    def test_replay_orders_same_event_time_by_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {
                    **build_envelope(
                        source="a", stream="a", exchange_event_time_ms=500,
                        receive_time_ms=1_000, available_time_ms=1_100,
                        receive_time_monotonic_ns=1,
                        available_time_monotonic_ns=2,
                    ),
                    "schema_version": 7, "code_version": "x",
                    "config_version": "x", "source": "a",
                    "symbol": "BTCUSDT", "stream": "a", "payload": {},
                },
                {
                    **build_envelope(
                        source="b", stream="b", exchange_event_time_ms=500,
                        receive_time_ms=1_050, available_time_ms=1_075,
                        receive_time_monotonic_ns=3,
                        available_time_monotonic_ns=4,
                    ),
                    "schema_version": 7, "code_version": "x",
                    "config_version": "x", "source": "b",
                    "symbol": "BTCUSDT", "stream": "b", "payload": {},
                },
            ]
            for row in rows:
                path = root / "raw" / "wal" / row["stream"] / "2026-01-01" / "00.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(orjson.dumps(row) + b"\n")
            replayed = list(iter_merged_records(root, streams={"a", "b"}))
            self.assertEqual([row["stream"] for row in replayed], ["b", "a"])

    def test_uncertainty_larger_than_gap_cannot_create_leader(self):
        rows = [
            {
                "venue": "binance_spot", "corrected_event_time_ms": 1_000,
                "temporal_uncertainty_ms": 80, "clock_valid": True,
                "source_health": "FRESH", "epoch": 1,
            },
            {
                "venue": "coinbase_spot", "corrected_event_time_ms": 1_150,
                "temporal_uncertainty_ms": 80, "clock_valid": True,
                "source_health": "FRESH", "epoch": 1,
            },
        ]
        measured = ignition_core._leader_measurement_from_rows(rows)
        self.assertEqual(measured["status"], "SIMULTANEOUS_OR_UNRESOLVED")
        self.assertEqual(measured["leader"], "SIMULTANEOUS")
        self.assertEqual(measured["uncertainty_bound_ms"], 160.0)

    def test_dead_source_is_unknown_not_opposite_thesis(self):
        rows = [
            {
                "venue": "binance_spot", "corrected_event_time_ms": 1_000,
                "temporal_uncertainty_ms": 5, "clock_valid": True,
                "source_health": "FRESH", "epoch": 1,
            },
            {
                "venue": "coinbase_spot", "corrected_event_time_ms": 1_500,
                "temporal_uncertainty_ms": 5, "clock_valid": True,
                "source_health": "DEAD", "epoch": 1,
            },
        ]
        measured = ignition_core._leader_measurement_from_rows(rows)
        self.assertEqual(measured["status"], "CLOCK_OR_SOURCE_UNSAFE")
        self.assertEqual(measured["leader"], "SIMULTANEOUS")

    def test_sequence_gap_starts_new_epoch_without_bridge(self):
        store = _Store()
        config = RecorderConfig()
        recorder = BinanceRecorder(config, store, HealthState(config))
        recorder._advance_epoch("binance_spot_trade_100ms")
        with mock.patch.object(recorder, "now_ms", return_value=1_100):
            previous = recorder._emit_cash_batch(
                "binance_spot_trade_100ms", "binance_spot",
                {"bucket_end_ms": 1_099, "first_trade_id": 10,
                 "last_trade_id": 10, "last_event_time_ms": 1_000},
                None,
            )
            recorder._emit_cash_batch(
                "binance_spot_trade_100ms", "binance_spot",
                {"bucket_end_ms": 1_099, "first_trade_id": 12,
                 "last_trade_id": 12, "last_event_time_ms": 1_001},
                previous,
            )
        self.assertEqual(store.rows[0]["epoch"], 1)
        self.assertEqual(store.rows[1]["epoch"], 2)
        self.assertIsNone(store.rows[1]["previous_sequence"])
        self.assertEqual(store.rows[1]["source_health"], "DEGRADED")

    def test_same_exchange_oi_snapshot_is_unknown(self):
        before = {
            "value": 100.0, "updated_at": 1.0,
            "exchange_time_ms": 900, "epoch": 4,
        }
        after = {
            "value": 101.0, "updated_at": 1.2,
            "exchange_time_ms": 900, "epoch": 4,
        }
        result = ignition_core._oi_verification(
            {"intent": "POSITION_BUILD", "aligned_with_entry": True},
            before, after, episode_started_ms=800, decision_time=1.3,
        )
        self.assertEqual(result["status"], "UNCHANGED_UNKNOWN")
        self.assertFalse(result["fresh"])
        self.assertFalse(result["exchange_time_ordered"])

    def test_oi_epoch_change_cannot_form_delta(self):
        before = {
            "value": 100.0, "updated_at": 1.0,
            "exchange_time_ms": 900, "epoch": 4,
        }
        after = {
            "value": 101.0, "updated_at": 1.2,
            "exchange_time_ms": 1_100, "epoch": 5,
        }
        result = ignition_core._oi_verification(
            {"intent": "POSITION_BUILD", "aligned_with_entry": True},
            before, after, episode_started_ms=800, decision_time=1.3,
        )
        self.assertEqual(result["status"], "STALE_UNKNOWN")
        self.assertFalse(result["same_epoch"])

    def test_oi_received_in_episode_but_measured_before_it_is_unknown(self):
        before = {
            "value": 100.0, "updated_at": 10.1,
            "exchange_time_ms": 9_000, "epoch": 4,
        }
        after = {
            "value": 101.0, "updated_at": 10.4,
            "exchange_time_ms": 9_500, "epoch": 4,
        }
        result = ignition_core._oi_verification(
            {"intent": "POSITION_BUILD", "aligned_with_entry": True},
            before, after, episode_started_ms=10_000, decision_time=10.5,
        )
        self.assertEqual(result["status"], "STALE_UNKNOWN")
        self.assertTrue(result["refresh_observed"])
        self.assertFalse(result["inside_causal_window"])

    def test_oi_measured_and_received_in_episode_can_confirm(self):
        before = {
            "value": 100.0, "updated_at": 10.1,
            "exchange_time_ms": 10_050, "epoch": 4,
        }
        after = {
            "value": 101.0, "updated_at": 10.4,
            "exchange_time_ms": 10_350, "epoch": 4,
        }
        result = ignition_core._oi_verification(
            {"intent": "POSITION_BUILD", "aligned_with_entry": True},
            before, after, episode_started_ms=10_000, decision_time=10.5,
        )
        self.assertEqual(result["status"], "FRESH_POSITION_BUILD")
        self.assertTrue(result["inside_causal_window"])
        self.assertEqual(result["measurement_time_ms"], 10_350)


if __name__ == "__main__":
    unittest.main()
