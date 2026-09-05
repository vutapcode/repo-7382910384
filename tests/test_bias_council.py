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


def _cash(buy, sell):
    return {
        "spot": {"buy": float(buy), "sell": float(sell)},
        "coinbase": {"buy": float(buy), "sell": float(sell)},
    }


def _history_row(ts, price, oi, buy, sell):
    return {
        "ts": float(ts), "spot": float(price), "coinbase": float(price),
        "futures": float(price), "oi": float(oi),
        "cash_totals": _cash(buy, sell),
        "venue_epochs": {"spot": 0, "coinbase": 0, "futures": 0},
    }


def state():
    s = SimpleNamespace(
        best_bid=100.0, best_ask=100.1, coinbase_price=100.05,
        thoi_gian_coinbase_ticker_cuoi=100.0, thoi_gian_tick_cuoi=100.0,
        open_interest=1000.0, thoi_gian_vi_mo_cuoi=100.0,
        atr_1m=0.05, funding_rate=0.0, flow_1s_buffer=deque(),
        futures_flow_1s_buffer=deque(), danh_sach_khop_lenh_futures=deque(),
        coinbase_cvd_1m=0.0, coinbase_volume_1m=0.0,
        coinbase_cvd_3s=0.0, coinbase_volume_3s=0.0,
        thoi_gian_coinbase_cuoi=100.0,
        spot_cvd_buy_total=30.0, spot_cvd_sell_total=3.0,
        coinbase_cvd_buy_total=30.0, coinbase_cvd_sell_total=3.0,
        spot_flow_epoch=0, coinbase_flow_epoch=0, futures_flow_epoch=0,
    )
    s.bias_price_history = deque([
        _history_row(-80.0, 97.0, 970.0, 0.0, 0.0),
        _history_row(40.0, 98.0, 980.0, 10.0, 1.0),
        _history_row(85.0, 99.0, 990.0, 20.0, 2.0),
    ], maxlen=1536)
    s.danh_sach_khop_lenh_futures.append(
        {"gia": 100.2, "thoi_gian_ms": 100000.0, "khoi_luong": 1.0, "ban_chu_dong": False}
    )
    return s


class BiasCouncilTests(unittest.TestCase):
    def test_neutral_acquisition_seals_persistent_cash_origin(self):
        s = state()
        s.bias_state, s.bias_confidence = "ABSTAIN", 0.0

        report = council.update_state(s, now=100.0)

        self.assertEqual(report["bias"], "LONG")
        handoff = report["acquisition_handoff"]
        self.assertEqual(handoff["status"], "SEALED")
        self.assertEqual(handoff["side"], "LONG")
        self.assertEqual(handoff["temporal_persistence_segments"], 2)
        self.assertEqual(
            handoff["directional_cash_roots"],
            ["BINANCE_SPOT_CASH", "COINBASE_USD_CASH"],
        )
        self.assertEqual(handoff["first_converting_segment_onset_ms"], 40_000)
        self.assertEqual(handoff["ownership_completed_ms"], 100_000)
        self.assertFalse(handoff["authority"])
        self.assertFalse(handoff["entry_authority"])
        self.assertEqual(
            handoff["handoff_hash"],
            council._canonical_hash(handoff["sealed_payload"]),
        )

    def test_emerging_cash_control_does_not_seal_acquisition(self):
        s = state()
        s.bias_state, s.bias_confidence = "ABSTAIN", 0.0
        s.bias_price_history = deque([
            _history_row(85.0, 99.0, 990.0, 20.0, 2.0),
        ], maxlen=1536)

        report = council.update_state(s, now=100.0)

        self.assertEqual(report["bias"], "ABSTAIN")
        self.assertFalse(hasattr(s, "bias_acquisition_handoff"))
        self.assertNotIn("acquisition_handoff", report)

    def test_acquisition_handoff_is_invalidated_on_cash_epoch_change(self):
        s = state()
        s.bias_state, s.bias_confidence = "ABSTAIN", 0.0
        council.update_state(s, now=100.0)
        sealed_hash = s.bias_acquisition_handoff["handoff_hash"]

        s.spot_flow_epoch = 1
        report = council.update_state(s, now=100.25)

        self.assertEqual(
            report["acquisition_handoff"]["status"],
            "INVALIDATED_EPOCH_CHANGE",
        )
        self.assertEqual(
            report["acquisition_handoff"]["handoff_hash"], sealed_hash,
        )

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
        )
        council.bias_buckets(s, 8.0, None)
        self.assertEqual(s.bias_history_reset_reason, "BIAS_LOOP_GAP")
        self.assertEqual(s.bias_history_reset_count, 1)

    def test_oi_is_context_not_second_direction_vote(self):
        s = state()
        r = council.evaluate(s, now=100.0, force_full=False)
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "LONG")
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "ABSTAIN")
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["reason"], "POSITIONING_CONTEXT_ONLY")
        self.assertEqual(r["bias"], "LONG")
        self.assertEqual(r["wave_state"], "CONTROLLED")
        self.assertTrue(r["cash_control"]["flow_price_conversion_required"])
        self.assertFalse(r["cash_control"]["oi_direction_authority"])

    def test_four_second_impulse_cannot_create_bias_without_slow_confirmation(self):
        s = state()
        s.bias_price_history = deque([
            _history_row(96.0, 99.0, 990.0, 20.0, 2.0),
        ], maxlen=1536)
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["s_votes"]["S1_cross_price"]["vote"], "ABSTAIN")
        self.assertEqual(
            r["s_votes"]["S1_cross_price"]["metrics"]["fast_policy"],
            "FLIP_TELEMETRY_ONLY_NO_BIAS_ACQUISITION",
        )
        self.assertEqual(r["bias"], "ABSTAIN")

    def test_falling_oi_never_votes_direction(self):
        s = state()
        s.open_interest = 980.0
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "ABSTAIN")
        self.assertFalse(r["derivative_context"]["authority"])

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
        self.assertEqual(r["contract"], "DIRECTION_ONLY_NO_ENTRY_TIMING")
        for forbidden in ("entry", "zone", "setup", "action", "price_entry"):
            self.assertNotIn(forbidden, r)

    def test_direction_memory_labels_short_price_without_short_flow_as_pullback(self):
        s = state()
        s.bias_state, s.bias_confidence, s.bias_wave_state = "LONG", 0.70, "CONTROLLED"
        s.bias_price_history = deque([
            _history_row(-80.0, 98.0, 970.0, 0.0, 0.0),
            _history_row(40.0, 99.0, 980.0, 10.0, 1.0),
            _history_row(85.0, 101.0, 990.0, 20.0, 2.0),
        ], maxlen=1536)
        # Balanced latest executed flow: opposite price alone cannot transfer.
        s.spot_cvd_buy_total = 21.0
        s.spot_cvd_sell_total = 3.0
        s.coinbase_cvd_buy_total = 21.0
        s.coinbase_cvd_sell_total = 3.0
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["direction_memory"]["context_side"], "LONG")
        self.assertEqual(r["direction_memory"]["phase"], "PULLBACK_AGAINST_CONTEXT")
        self.assertEqual(r["wave_state"], "PULLBACK")
        self.assertEqual(r["bias"], "LONG")

    def test_context_pullback_cannot_flip_existing_bias(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.8
        raw = {
            "ts": 100.1, "bias": "LONG", "confidence": 0.6,
            "knowledge_state": "SUPPORTED",
            "direction_memory": {
                "context_side": "LONG", "phase": "PULLBACK_AGAINST_CONTEXT",
            },
            "cash_control": {"control_transfer_confirmed": False},
        }
        side, _, reason = council._hyst(s, raw)
        self.assertEqual(side, "LONG")
        self.assertEqual(reason, "HOLD_CONTEXT_PULLBACK")

    def test_emerging_cash_control_cannot_acquire_bias_from_abstain(self):
        s = state()
        s.bias_state, s.bias_confidence = "ABSTAIN", 0.0
        raw = {
            "ts": 100.0, "bias": "LONG", "confidence": 0.52,
            "knowledge_state": "SUPPORTED",
            "direction_memory": {
                "context_side": "LONG", "phase": "CONTEXT_WITHOUT_CONFIRMATION",
            },
            "cash_control": {
                "wave_state": "EMERGING_CONTROL",
                "meaningful_for_action": False,
                "control_transfer_confirmed": False,
            },
        }
        side, confidence, reason = council._hyst(s, raw)
        self.assertEqual(side, "ABSTAIN")
        self.assertEqual(confidence, 0.0)
        self.assertEqual(reason, "OBSERVE_EMERGING_CASH_CONTROL")

    def test_controlled_cash_wave_can_acquire_bias_from_abstain(self):
        s = state()
        s.bias_state, s.bias_confidence = "ABSTAIN", 0.0
        raw = {
            "ts": 100.0, "bias": "LONG", "confidence": 0.70,
            "knowledge_state": "SUPPORTED",
            "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
            "cash_control": {
                "wave_state": "CONTROLLED",
                "meaningful_for_action": True,
                "control_transfer_confirmed": False,
            },
        }
        side, confidence, reason = council._hyst(s, raw)
        self.assertEqual(side, "LONG")
        self.assertEqual(confidence, 0.70)
        self.assertEqual(reason, "ACQUIRE_CASH_REGIME")

    def test_controlled_label_without_actionable_maturity_cannot_acquire(self):
        s = state()
        s.bias_state, s.bias_confidence = "ABSTAIN", 0.0
        raw = {
            "ts": 100.0, "bias": "LONG", "confidence": 0.70,
            "knowledge_state": "SUPPORTED",
            "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
            "cash_control": {
                "wave_state": "CONTROLLED",
                "meaningful_for_action": False,
                "control_transfer_confirmed": False,
            },
        }
        side, confidence, reason = council._hyst(s, raw)
        self.assertEqual(side, "ABSTAIN")
        self.assertEqual(confidence, 0.0)
        self.assertEqual(reason, "NO_ACTIONABLE_CASH_REGIME")

    def test_established_context_survives_raw_abstain_compatibility(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.8
        raw = {
            "ts": 100.0, "bias": "ABSTAIN", "confidence": 0.0,
            "knowledge_state": "UNKNOWN_MARKET",
            "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
            "cash_control": {"control_transfer_confirmed": False},
        }
        side, confidence, reason = council._hyst(s, raw)
        self.assertEqual(side, "LONG")
        self.assertGreaterEqual(confidence, 0.55)
        self.assertEqual(reason, "HOLD_CONTEXT_THROUGH_ABSTAIN")

    def test_exhausted_wave_does_not_use_long_lens_to_hold_bias(self):
        s = state()
        s.bias_state, s.bias_confidence, s.bias_wave_state = "LONG", 0.70, "CONTROLLED"
        # Executed buyers persist but price is flat to the latest cash anchor;
        # old 60/180s displacement remains LONG and must not keep Bias alive.
        s.best_bid, s.best_ask, s.coinbase_price = 98.99, 99.01, 99.0
        s.spot_cvd_buy_total = 30.0
        s.spot_cvd_sell_total = 2.0
        s.coinbase_cvd_buy_total = 30.0
        s.coinbase_cvd_sell_total = 2.0
        s.bias_price_history = deque([
            _history_row(-80.0, 97.0, 970.0, 0.0, 0.0),
            _history_row(40.0, 98.0, 980.0, 10.0, 1.0),
            _history_row(85.0, 99.0, 990.0, 20.0, 1.0),
        ], maxlen=1536)
        r = council.evaluate(s, now=100.0)
        self.assertEqual(r["wave_state"], "EXHAUSTION")
        self.assertEqual(r["bias"], "ABSTAIN")
        self.assertFalse(r["cash_control"]["historical_lens_direction_authority"])

    def test_confirmed_flip_requires_evidence_not_elapsed_timer(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.80
        raw = {
            "ts": 100.0, "bias": "SHORT", "confidence": 0.72,
            "knowledge_state": "SUPPORTED",
            "direction_memory": {
                "context_side": "LONG", "phase": "REVERSAL_CANDIDATE",
            },
            "cash_control": {"control_transfer_confirmed": True},
        }
        side, _, reason = council._hyst(s, raw)
        self.assertEqual(side, "SHORT")
        self.assertEqual(reason, "EVIDENCE_CONFIRMED_CONTROL_TRANSFER")

    def test_diverging_cash_evidence_releases_existing_bias(self):
        s = state()
        s.bias_state, s.bias_confidence = "LONG", 0.80
        raw = {
            "ts": 100.0, "bias": "LONG", "confidence": 0.70,
            "knowledge_state": "DIVERGING",
            "direction_memory": {"context_side": "LONG", "phase": "ESTABLISHED_TREND"},
            "cash_control": {"control_transfer_confirmed": False},
        }
        side, confidence, reason = council._hyst(s, raw)
        self.assertEqual(side, "ABSTAIN")
        self.assertEqual(confidence, 0.0)
        self.assertEqual(reason, "INDEPENDENT_CASH_EVIDENCE_DIVERGING")

    def test_update_state_exports_non_authoritative_reversal_latch(self):
        s = state()
        raw = {
            "ts": 100.0, "bias": "SHORT", "confidence": 0.72,
            "raw_bias": "SHORT", "raw_confidence": 0.72,
            "wave_state": "CONTROL_TRANSFER",
            "knowledge_state": "SUPPORTED",
            "cash_control": {"control_transfer_confirmed": True},
            "direction_memory": {"context_side": "LONG", "candidate_side": "SHORT", "phase": "REVERSAL_CANDIDATE"},
            "reversal_latch": {"status": "CONFIRMED", "candidate_side": "SHORT", "authority": False},
        }
        with patch.object(council, "evaluate", return_value=raw):
            report = council.update_state(s, now=100.0)
        self.assertFalse(report["reversal_latch"]["authority"])
        self.assertEqual(s.bias_wave_state, "CONTROL_TRANSFER")

    def test_stale_spot_cannot_vote_direction(self):
        s = state()
        s.thoi_gian_tick_cuoi = 90.0
        r = council.evaluate(s, now=100.0)
        self.assertFalse(r["freshness"]["spot"])
        self.assertEqual(r["bias"], "ABSTAIN")
        self.assertEqual(r["knowledge_state"], "UNKNOWN_SOURCE")

    def test_covering_is_positioning_context_not_new_long_build(self):
        s = state()
        s.open_interest = 970.0
        r = council.evaluate(s, now=100.0)
        metrics = r["s_votes"]["S2_price_x_oi"]["metrics"]
        self.assertEqual(metrics["regime"], "PRICE_UP_OI_CONTRACTION")
        self.assertEqual(metrics["mechanism_hypothesis"], "SHORT_COVERING_CANDIDATE")
        self.assertFalse(metrics["mechanism_confirmed"])
        self.assertEqual(r["s_votes"]["S2_price_x_oi"]["vote"], "ABSTAIN")

    def test_futures_flow_is_diagnostic_not_direction_authority(self):
        s = state()
        s.futures_flow_1s_buffer = deque(
            {"second": sec, "ts": float(sec), "buy": 1.0, "sell": 0.0}
            for sec in range(41, 101)
        )
        imbalance, volume = council.flow_imb(s, 100.0, fut=True)
        self.assertAlmostEqual(imbalance, 1.0)
        self.assertAlmostEqual(volume, 60.0)
        self.assertFalse(council.evaluate(s, now=100.0)["cash_control"]["futures_direction_authority"])

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
        report = council.evaluate(s, now=100.0)
        context = report["flow_question_context"]
        self.assertIn(context["background_pressure_60s"]["side"], ("LONG", "SHORT", "ABSTAIN"))
        self.assertFalse(context["marginal_control_1_5s"]["authority"])
        self.assertTrue(context["causal_families"]["binance_complex_echo"])

    def test_spot_futures_echo_does_not_form_independent_s3_quorum(self):
        agreed, reason = council.flow_family_consensus([
            ("spot", "LONG", 0.8),
            ("futures", "LONG", 0.9),
        ])
        self.assertEqual(agreed, [])
        self.assertEqual(reason, "BINANCE_COMPLEX_ECHO_UNCORROBORATED")

    def test_coinbase_futures_do_not_form_direction_quorum(self):
        agreed, reason = council.flow_family_consensus([
            ("coinbase", "SHORT", 0.8),
            ("futures", "SHORT", 0.9),
        ])
        self.assertEqual(agreed, [])
        self.assertEqual(reason, "CASH_DERIVATIVE_NOT_DIRECTION_QUORUM")

    def test_only_dual_independent_cash_forms_flow_support(self):
        agreed, reason = council.flow_family_consensus([
            ("spot", "LONG", 0.6),
            ("coinbase", "LONG", 0.7),
            ("futures", "LONG", 0.9),
        ])
        self.assertEqual([row[0] for row in agreed], ["spot", "coinbase"])
        self.assertEqual(reason, "DUAL_CASH_FLOW")

    def test_one_hour_is_observation_lens_not_fixed_rule(self):
        self.assertIn(3600.0, council.OBSERVATION_LENSES)
        self.assertEqual(
            council.FORECAST_SCOPE,
            "MEANINGFUL_DIRECTIONAL_REGIME_NOT_FIXED_TIME_TARGET",
        )
        self.assertFalse(council.evaluate(state(), now=100.0)["cash_control"]["historical_lens_direction_authority"])


if __name__ == "__main__":
    unittest.main()
