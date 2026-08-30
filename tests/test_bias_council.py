import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


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
        {"ts": -80.0, "spot": 97.0, "coinbase": 97.0, "futures": 97.0, "oi": 970.0},
        {"ts": 40.0, "spot": 98.0, "coinbase": 98.0, "futures": 98.0, "oi": 980.0},
        {"ts": 85.0, "spot": 99.0, "coinbase": 99.0, "futures": 99.0, "oi": 990.0},
    ], maxlen=1536)
    s.danh_sach_khop_lenh_futures.append(
        {"gia": 100.2, "thoi_gian_ms": 100000.0, "khoi_luong": 1.0, "ban_chu_dong": False}
    )
    return s


class BiasCouncilTests(unittest.TestCase):
    def test_price_memory_does_not_bridge_venue_epoch(self):
        old = {
            "spot": 100.0, "coinbase": 100.0, "futures": 100.0,
            "venue_epochs": {"spot": 1, "coinbase": 1, "futures": 1},
        }
        current = {
            "spot": 101.0, "coinbase": 101.0, "futures": 101.0,
            "venue_epochs": {"spot": 2, "coinbase": 1, "futures": 1},
        }
        report = council.price_vote(current, old, 0.015)
        self.assertEqual(report["vote"], "LONG")
        self.assertEqual(report["metrics"]["epoch_mismatch"], ["spot"])
        cash = council.cash_price_vote(current, old, 0.015)
        self.assertEqual(cash["vote"], "ABSTAIN")
        self.assertEqual(cash["metrics"]["epoch_mismatch"], ["spot"])
        self.assertEqual(
            council.s2(current, old, 0.015, True)["reason"],
            "SPOT_PRICE_EPOCH_MISMATCH",
        )

    def test_bias_loop_gap_reset_is_observable(self):
        s = SimpleNamespace(
            bias_price_buckets={1: {"ts": 1.0}, 2: {"ts": 2.0}},
            _bias_bucket_last_sample_at=2.0,
            _bias_bucket_oldest_second=1,
        )
        council.bias_buckets(s, 6.0, None)
        self.assertEqual(s.bias_history_reset_reason, "BIAS_LOOP_GAP")
        self.assertEqual(s.bias_history_previous_coverage_seconds, 1)
        self.assertEqual(s.bias_history_reset_count, 1)

    def test_price_and_price_oi_are_not_two_independent_votes(self):
        s = state()
        r = council.evaluate(s, now=100.0, force_full=False)
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "LONG")
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "LONG")
        self.assertEqual(r["bias"], "ABSTAIN")
        self.assertEqual(r["reason"], "PRICE_OI_NOT_INDEPENDENT_WITHOUT_FLOW")
        self.assertGreaterEqual(r["quorum"], 2)

    def test_four_second_impulse_cannot_create_bias_without_slow_confirmation(self):
        s = state()
        s.bias_price_history = deque([
            {"ts": 96.0, "spot": 99.0, "coinbase": 99.0, "futures": 99.0, "oi": 990.0},
        ], maxlen=1536)
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "ABSTAIN")
        self.assertEqual(
            r["s_votes"]["S1_cross_price"]["metrics"]["fast_policy"],
            "FLIP_TELEMETRY_ONLY_NO_BIAS_ACQUISITION",
        )

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

    def test_direction_memory_labels_short_trigger_inside_long_context_as_pullback(self):
        s = state()
        s.bias_price_history = deque([
            {"ts": -80.0, "spot": 98.0, "coinbase": 98.0, "futures": 98.0, "oi": 970.0},
            {"ts": 40.0, "spot": 99.0, "coinbase": 99.0, "futures": 99.0, "oi": 980.0},
            {"ts": 85.0, "spot": 101.0, "coinbase": 101.0, "futures": 101.0, "oi": 990.0},
        ], maxlen=1536)
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["direction_memory"]["context_side"], "LONG")
        self.assertEqual(r["direction_memory"]["phase"], "PULLBACK_AGAINST_CONTEXT")

    def test_context_pullback_cannot_flip_existing_bias(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.8
        s._bias_last_supported_at = 100.0
        raw = {
            "ts": 100.1, "bias": "SHORT", "confidence": 0.8,
            "quorum": 3, "story": {"name": "NEW_SHORT_BUILD_CONFIRMED"},
            "direction_memory": {
                "context_side": "LONG", "phase": "PULLBACK_AGAINST_CONTEXT",
            },
        }
        side, _, reason = council._hyst(s, raw)
        self.assertEqual(side, "LONG")
        self.assertEqual(reason, "HOLD_CONTEXT_PULLBACK")

    def test_established_context_survives_short_raw_abstain_latch(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.8
        s._bias_last_supported_at = 90.0
        raw = {
            "ts": 100.0, "bias": "ABSTAIN", "confidence": 0.0,
            "quorum": 0, "story": {"name": "MIXED_OR_INCOMPLETE"},
            "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
        }
        side, confidence, reason = council._hyst(s, raw)
        self.assertEqual(side, "LONG")
        self.assertGreaterEqual(confidence, 0.35)
        self.assertEqual(reason, "HOLD_CONTEXT_THROUGH_ABSTAIN")

    def test_confirmed_flip_exports_non_authoritative_reversal_latch(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.80
        s._bias_flip_candidate = "SHORT"
        s._bias_flip_since = 97.0
        raw = {
            "ts": 100.0, "bias": "SHORT", "confidence": 0.80,
            "quorum": 3, "story": {"name": "NEW_SHORT_BUILD_CONFIRMED"},
            "direction_memory": {
                "context_side": "LONG", "phase": "REVERSAL_CANDIDATE",
            },
        }
        with patch.object(council, "evaluate", return_value=raw):
            report = council.update_state(s, now=100.0)
        latch = report["reversal_latch"]
        self.assertEqual(latch["status"], "CONFIRMED")
        self.assertEqual(latch["candidate_side"], "SHORT")
        self.assertEqual(latch["started_at"], 97.0)
        self.assertFalse(latch["authority"])



    def test_stale_spot_cannot_vote_direction(self):
        s = state()
        s.thoi_gian_tick_cuoi = 90.0
        r = council.evaluate(s, now=100.0)
        self.assertFalse(r["freshness"]["spot"])
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "ABSTAIN")

    def test_covering_is_story_not_new_long_build(self):
        s = state()
        # The causal OI seat now compares the 60-second base (980), not the old
        # 15-second trigger (990).
        s.open_interest = 970.0
        r = council.evaluate(s, now=100.0)
        metrics = r["s_votes"]["S2_price_x_oi"]["metrics"]
        self.assertEqual(metrics["regime"], "PRICE_UP_OI_CONTRACTION")
        self.assertEqual(
            metrics["mechanism_hypothesis"], "SHORT_COVERING_CANDIDATE"
        )
        self.assertFalse(metrics["mechanism_confirmed"])
        self.assertNotEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "LONG")

    def test_contract_remains_direction_only(self):
        r = council.evaluate(state(), now=100.0)
        self.assertEqual(r["contract"], "DIRECTION_ONLY_NO_ENTRY_TIMING")
        self.assertIn("story", r)
        for forbidden in ("entry", "zone", "setup", "action", "price_entry", "stop_loss", "take_profit"):
            self.assertNotIn(forbidden, r)

    def test_futures_bias_flow_uses_60s_aggregate_not_20s_raw_ring(self):
        s = state()
        s.futures_flow_1s_buffer = deque(
            {"second": sec, "ts": float(sec), "buy": 1.0, "sell": 0.0}
            for sec in range(41, 101)
        )
        # Raw retention can contain a short opposing tail; it must not replace
        # the declared 60s Bias aggregate.
        s.danh_sach_khop_lenh_futures = deque([
            {"thoi_gian_ms": 100000.0, "khoi_luong": 5.0,
             "ban_chu_dong": True, "gia": 100.0},
        ])
        imbalance, volume = council.flow_imb(s, 100.0, fut=True)
        self.assertAlmostEqual(imbalance, 1.0)
        self.assertAlmostEqual(volume, 60.0)

    def test_background_pressure_and_marginal_control_are_separate_questions(self):
        s = state()
        s.flow_1s_buffer = deque(
            {"ts": float(sec), "buy": 1.0, "sell": 0.0}
            for sec in range(96, 101)
        )
        s.futures_flow_1s_buffer = deque(
            {"ts": float(sec), "buy": 1.0, "sell": 0.0}
            for sec in range(96, 101)
        )
        s.coinbase_cvd_3s = -1.0
        s.coinbase_volume_3s = 1.0
        s.coinbase_flow_3s_ts = 100.0
        report = council.evaluate(s, now=100.0)
        context = report["flow_question_context"]
        self.assertIn(
            context["background_pressure_60s"]["side"],
            ("LONG", "SHORT", "ABSTAIN"),
        )
        self.assertFalse(
            context["marginal_control_1_5s"]["authority"]
        )
        self.assertTrue(
            context["causal_families"]["binance_complex_echo"]
        )
        self.assertEqual(
            context["causal_families"]["cash_family"]["dual_cash_side"],
            "UNKNOWN",
        )

    def test_spot_futures_echo_does_not_form_independent_s3_quorum(self):
        agreed, reason = council.flow_family_consensus([
            ("spot", "LONG", 0.8),
            ("futures", "LONG", 0.9),
        ])
        self.assertEqual(agreed, [])
        self.assertEqual(reason, "BINANCE_COMPLEX_ECHO_UNCORROBORATED")

    def test_coinbase_futures_can_form_cross_family_s3_quorum(self):
        agreed, reason = council.flow_family_consensus([
            ("coinbase", "SHORT", 0.8),
            ("futures", "SHORT", 0.9),
        ])
        self.assertEqual([row[0] for row in agreed], ["coinbase", "futures"])
        self.assertEqual(reason, "CASH_DERIVATIVE_FLOW")


if __name__ == "__main__":
    unittest.main()
