import copy
import tempfile
import unittest
from pathlib import Path

from recorder.residual_edge import ResidualEdgeBook
from recorder.wavefront import WavefrontShadowEvaluator
from recorder.liquidity_response import LiquidityResponseAnalyzer
from recorder.replay import DeterministicReplay


class WavefrontHarness:
    def __init__(self):
        self.rows = []
        self.engine = WavefrontShadowEvaluator(
            self.emit, warmup_samples=2,
            runtime_health_path=None, cpu_status_path=None,
        )
        self.sequence = {
            "binance_spot_trade_100ms": 0,
            "coinbase_spot_trade_100ms": 0,
            "futures_trade_100ms": 0,
        }

    def emit(self, stream, payload, event_time_ms=None):
        self.rows.append((stream, payload, event_time_ms))

    def record(self, stream, receive_ms, payload, event_ms=None):
        row = {
            "stream": stream, "receive_time_ms": receive_ms,
            "event_time_ms": event_ms if event_ms is not None else receive_ms,
            "payload": payload,
        }
        if stream in self.sequence:
            previous = self.sequence[stream]
            current = previous + 1
            self.sequence[stream] = current
            row.update({
                "previous_sequence": previous if previous else None,
                "sequence_start": current, "sequence_end": current,
            })
        self.engine.observe(row)
        return row

    @staticmethod
    def batch(price=100.0, buy=0.01, sell=0.01):
        return {
            "trade_count": 2, "buy_qty": buy, "sell_qty": sell,
            "buy_quote": buy * price, "sell_quote": sell * price,
            "first_price": price, "last_price": price,
            "high": price, "low": price,
        }

    def warm(self, stream, start_ms, price=100.0):
        for index in range(5):
            receive = start_ms + index * 100
            self.record(
                stream, receive, self.batch(price), event_ms=receive - 50,
            )

    def prepare_aligned_build(self):
        self.record("book_ticker", 99_000, {"b": "99.99", "a": "100.01"})
        self.record("binance_spot_ticker", 99_010, {"bid": "99.99", "ask": "100.01"})
        self.record("bot_event", 99_020, {
            "event": "DECISION_EVALUATED",
            "decision_record": {
                "inputs": {
                    "bias": {"direction": "LONG", "confidence": 0.8},
                    "regime": {"regime": "EXPANSION"},
                },
                "output": {"decision": "WAIT", "side": "LONG"},
            },
        })
        self.record("open_interest", 99_030, {"openInterest": "100"})
        self.record("open_interest", 99_040, {"openInterest": "100.03"})
        self.warm("binance_spot_trade_100ms", 99_100)
        self.warm("coinbase_spot_trade_100ms", 99_650)
        self.warm("futures_trade_100ms", 100_200)
        self.record("book_ticker", 100_750, {"b": "99.99", "a": "100.01"})

    def qualify(self):
        self.record(
            "binance_spot_trade_100ms", 100_800,
            self.batch(100.10, buy=0.20, sell=0.0), event_ms=100_750,
        )
        self.record(
            "futures_trade_100ms", 101_300,
            self.batch(100.10, buy=0.30, sell=0.0), event_ms=101_250,
        )


class WavefrontCausalityTests(unittest.TestCase):
    def test_cash_proposer_and_futures_follower_open_execution_twins(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        qualified = [
            payload for stream, payload, _ in h.rows
            if stream == "wavefront_candidate" and payload.get("decision") == "QUALIFIED"
        ]
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["causal_class"], "ALIGNED_BUILD")
        self.assertGreaterEqual(qualified[0]["lead_lower_bound_ms"], 100.0)
        entries = [row for row in h.rows if row[0] == "wavefront_virtual_entry"]
        self.assertEqual([row[1]["execution_twin"] for row in entries], ["TAKER_TWIN"])
        self.assertFalse(entries[0][1]["authority"])
        self.assertEqual(
            h.engine.guardian.VERSION,
            "GUARDIAN_S_TIER_V8_BREAK_CONFLICT_RECOVERY",
        )

    def test_maker_requires_real_trade_through_and_expires_without_it(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        h.record(
            "futures_trade_100ms", 101_400,
            h.batch(100.02, buy=0.0, sell=0.10), event_ms=101_350,
        )
        maker_entries = [
            payload for stream, payload, _ in h.rows
            if stream == "wavefront_virtual_entry"
            and payload.get("execution_twin") == "MAKER_TWIN"
        ]
        self.assertFalse(maker_entries)
        h.record("book_ticker", 102_100, {"b": "100.00", "a": "100.02"})
        expired = [
            payload for stream, payload, _ in h.rows
            if stream == "wavefront_virtual_exit"
            and payload.get("execution_twin") == "MAKER_TWIN"
        ]
        self.assertEqual(expired[-1]["exit_reason"], "MAKER_TTL_EXPIRED")
        self.assertFalse(expired[-1]["filled"])

    def test_late_coinbase_record_cannot_rewrite_existing_decision(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        before = [
            (stream, payload.get("decision"), payload.get("causal_episode_id"))
            for stream, payload, _ in h.rows if stream == "wavefront_candidate"
        ]
        h.record(
            "coinbase_spot_trade_100ms", 101_400,
            h.batch(99.0, buy=0.0, sell=1.0), event_ms=99_500,
        )
        after_prefix = [
            (stream, payload.get("decision"), payload.get("causal_episode_id"))
            for stream, payload, _ in h.rows[:len(before)]
            if stream == "wavefront_candidate"
        ]
        self.assertEqual(before, after_prefix)

    def test_sequence_gap_invalidates_open_virtual_positions(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        previous = h.sequence["futures_trade_100ms"]
        h.engine.observe({
            "stream": "futures_trade_100ms",
            "receive_time_ms": 101_400, "event_time_ms": 101_350,
            "previous_sequence": previous + 10,
            "sequence_start": previous + 11, "sequence_end": previous + 11,
            "payload": h.batch(100.0),
        })
        exits = [row[1] for row in h.rows if row[0] == "wavefront_virtual_exit"]
        self.assertTrue(exits)
        self.assertTrue(all(not row["valid"] for row in exits))
        self.assertFalse(h.engine.twins)

    def test_hard_risk_remains_final_for_virtual_twin(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        h.record("book_ticker", 101_400, {"b": "99.30", "a": "99.32"})
        exits = [
            row[1] for row in h.rows if row[0] == "wavefront_virtual_exit"
            and row[1].get("execution_twin") == "TAKER_TWIN"
        ]
        self.assertEqual(exits[-1]["exit_reason"], "HARD_SL")
        self.assertTrue(exits[-1]["valid"])
        self.assertLess(exits[-1]["net_pnl_bps"], 0.0)
        reports = [row[1] for row in h.rows if row[0] == "residual_edge_report"]
        self.assertEqual(reports[-1]["samples"], 1)
        self.assertFalse(reports[-1]["promotion_eligible"])

    def test_cpu_defensive_invalidates_research_not_raw_recording(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        h.engine.governor_mode = "DEFENSIVE"
        h.record("mark_price", 101_400, {"p": "100"})
        self.assertFalse(h.engine.twins)
        exits = [row[1] for row in h.rows if row[0] == "wavefront_virtual_exit"]
        self.assertTrue(all(not row["valid"] for row in exits))
        self.assertTrue(all(row["exit_reason"] == "CPU_DEFENSIVE" for row in exits))

    def test_futures_led_impulse_is_rejected(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.record(
            "futures_trade_100ms", 100_800,
            h.batch(100.10, buy=0.30, sell=0.0), event_ms=100_750,
        )
        h.record(
            "binance_spot_trade_100ms", 101_100,
            h.batch(100.10, buy=0.20, sell=0.0), event_ms=101_050,
        )
        rejects = [
            row[1] for row in h.rows if row[0] == "wavefront_candidate"
            and row[1].get("reason") == "FUTURES_LED"
        ]
        self.assertTrue(rejects)
        self.assertFalse(h.engine.twins)

    def test_core_launcher_does_not_import_wavefront(self):
        source = (
            Path(__file__).resolve().parents[1] / "mainnet_tier_s_lean_launcher.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("wavefront", source.lower())
        core_source = (
            Path(__file__).resolve().parents[1] / "mainnet_tier_s_shadow_launcher.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"guardian_s_tier.py"', core_source)


class ResidualEdgeTests(unittest.TestCase):
    def test_heuristic_labels_do_not_enter_empirical_report(self):
        book = ResidualEdgeBook()
        book.observe_candidate(1_000)
        report = book.observe_exit({
            "valid": True, "filled": True,
            "causal_class": "ALIGNED_BUILD", "side": "LONG",
            "proposer": "binance_spot", "bias_relation": "ALIGNED",
            "regime": "EXPANSION", "execution_twin": "TAKER_TWIN",
            "net_pnl_bps": 12.0, "stress_25bps_net_bps": 1.0,
            "exit_reason": "PROFIT_FLOOR", "economic_wave": True,
            "capture_ratio": 0.5, "core_shared_entry": False,
        })
        self.assertFalse(report["authority"])
        self.assertEqual(report["samples"], 1)
        self.assertNotIn("RUNNER_EDGE", str(report))
        self.assertIn("MANUAL_APPROVAL_REQUIRED", report["promotion_blockers"])

    def test_unverified_commission_is_explicit(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        entry = next(row[1] for row in h.rows if row[0] == "wavefront_virtual_entry")
        self.assertFalse(entry["cost_plan"]["commission_verified"])
        self.assertFalse(entry["cost_plan"]["promotion_cost_verified"])


class OfflineLiquidityTests(unittest.TestCase):
    def test_static_wall_change_without_execution_emits_nothing(self):
        rows = []
        analyzer = LiquidityResponseAnalyzer(
            lambda stream, payload, event_time_ms=None: rows.append((stream, payload))
        )
        analyzer.observe({
            "stream": "depth_checkpoint", "receive_time_ms": 1_000,
            "payload": {
                "lastUpdateId": 1,
                "bids": [["99", "2"]], "asks": [["101", "3"]],
            },
        })
        analyzer.observe({
            "stream": "depth_diff", "receive_time_ms": 1_100,
            "payload": {
                "partial": True, "U": 2, "u": 2, "pu": 1,
                "b": [["99", "2"]], "a": [["101", "0"]],
            },
        })
        analyzer.observe({"stream": "mark_price", "receive_time_ms": 5_000, "payload": {}})
        self.assertEqual(rows, [])

    def test_executed_flow_then_depletion_emits_offline_only_response(self):
        rows = []
        analyzer = LiquidityResponseAnalyzer(
            lambda stream, payload, event_time_ms=None: rows.append((stream, payload))
        )
        analyzer.observe({
            "stream": "depth_checkpoint", "receive_time_ms": 1_000,
            "payload": {
                "lastUpdateId": 1,
                "bids": [["99", "2"]],
                "asks": [["100", "1"], ["101", "2"]],
            },
        })
        analyzer.observe({
            "stream": "futures_trade_100ms", "receive_time_ms": 1_100,
            "payload": {
                "buy_qty": 1.0, "sell_qty": 0.0,
                "high": 100.0, "low": 100.0,
            },
        })
        analyzer.observe({
            "stream": "depth_diff", "receive_time_ms": 1_200,
            "payload": {
                "partial": True, "U": 2, "u": 2, "pu": 1,
                "b": [["99", "2"]], "a": [["100", "0.2"], ["101", "2"]],
            },
        })
        analyzer.observe({"stream": "mark_price", "receive_time_ms": 4_200, "payload": {}})
        response = rows[-1][1]
        self.assertFalse(response["authority"])
        self.assertFalse(response["eligible_for_live_gate"])
        self.assertFalse(response["cancel_is_execution"])
        self.assertGreater(response["executed_depletion_ratio"], 0.0)

    def test_refill_without_price_progress_is_absorption_research_only(self):
        rows = []
        analyzer = LiquidityResponseAnalyzer(
            lambda stream, payload, event_time_ms=None: rows.append((stream, payload))
        )
        analyzer.observe({
            "stream": "depth_checkpoint", "receive_time_ms": 1_000,
            "payload": {
                "lastUpdateId": 1, "bids": [["99", "2"]],
                "asks": [["100", "1"], ["101", "2"]],
            },
        })
        analyzer.observe({
            "stream": "futures_trade_100ms", "receive_time_ms": 1_100,
            "payload": {"buy_qty": 1.0, "sell_qty": 0.0,
                        "high": 100.0, "low": 100.0},
        })
        analyzer.observe({
            "stream": "depth_diff", "receive_time_ms": 1_200,
            "payload": {"partial": True, "U": 2, "u": 2, "pu": 1,
                        "b": [["99", "2"]],
                        "a": [["100", "0.2"], ["101", "2"]]},
        })
        analyzer.observe({
            "stream": "depth_diff", "receive_time_ms": 2_200,
            "payload": {"partial": True, "U": 3, "u": 3, "pu": 2,
                        "b": [["99", "2"]],
                        "a": [["100", "1"], ["101", "2"]]},
        })
        analyzer.observe({"stream": "mark_price", "receive_time_ms": 4_200,
                          "payload": {}})
        response = rows[-1][1]
        self.assertTrue(response["absorption_candidate"])
        self.assertFalse(response["authority"])
        self.assertFalse(response["eligible_for_live_gate"])


class WavefrontReplayTests(unittest.TestCase):
    def test_replay_is_deterministic_and_generated_rows_do_not_change_raw_digest(self):
        h = WavefrontHarness()
        h.prepare_aligned_build()
        h.qualify()
        records = []
        # Build a compact deterministic input independently of the harness emissions.
        for index in range(6):
            records.append({
                "stream": "mark_price", "receive_time_ms": 10_000 + index,
                "event_time_ms": 10_000 + index, "payload": {"p": "100"},
            })
        first = DeterministicReplay().run(copy.deepcopy(records))
        second = DeterministicReplay().run(copy.deepcopy(records))
        self.assertEqual(first["digest_sha256"], second["digest_sha256"])
        self.assertEqual(first["wavefront"], second["wavefront"])
        self.assertEqual(
            first["wavefront_generated_records"],
            second["wavefront_generated_records"],
        )

    def test_version_bound_state_survives_restart_and_orphan_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wavefront.json"
            first_rows = []
            first = WavefrontShadowEvaluator(
                lambda stream, payload, event_time_ms=None: first_rows.append((stream, payload)),
                warmup_samples=2, state_path=path, evidence_version="code:config",
            )
            first.residual.observe_candidate(1_000)
            first.twins["TAKER_TWIN"] = {
                "episode_id": "wf:test", "name": "TAKER_TWIN",
                "side": "LONG", "filled": True, "terminal": False,
                "candidate": {
                    "causal_class": "ALIGNED_BUILD", "bias_relation": "ALIGNED",
                    "proposer": "binance_spot",
                },
                "cost_plan": {"commission_verified": True},
            }
            first._persist()

            rows = []
            second = WavefrontShadowEvaluator(
                lambda stream, payload, event_time_ms=None: rows.append((stream, payload)),
                warmup_samples=2, state_path=path, evidence_version="code:config",
            )
            self.assertEqual(second.residual.candidates, 1)
            second.observe({
                "stream": "mark_price", "receive_time_ms": 2_000,
                "event_time_ms": 2_000, "payload": {"p": "100"},
            })
            orphan = next(payload for stream, payload in rows if stream == "wavefront_virtual_exit")
            self.assertEqual(orphan["exit_reason"], "RECORDER_RESTART_GAP")
            self.assertFalse(orphan["valid"])
            self.assertFalse(second.orphan_twins)

            third = WavefrontShadowEvaluator(
                lambda *args, **kwargs: None,
                state_path=path, evidence_version="different-version",
            )
            self.assertEqual(third.residual.candidates, 0)


if __name__ == "__main__":
    unittest.main()
