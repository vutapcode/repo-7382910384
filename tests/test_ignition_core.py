from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loi_he_thong import ignition_core, ignition_signals, entry_edge_tier


def state(now=3.0):
    return SimpleNamespace(
        bias_state="LONG", bias_confidence=0.80, bias_updated_at=now - 1.2,
        bias_version="TEST", bias_council={"s_votes": {}},
        best_bid=99.99, best_ask=100.01, thoi_gian_tick_cuoi=now,
        thoi_gian_dong_tien_cuoi=now,
        coinbase_price=100.0, thoi_gian_coinbase_ticker_cuoi=now,
        coinbase_flow_3s_ts=now,
        execution_best_bid=99.99, execution_best_ask=100.01,
        execution_price_time=now, thoi_gian_dong_tien_futures_cuoi=now,
        open_interest_updated_at=now, wstrade_live_armed=False,
        mainnet_commission_verified=True, mainnet_maker_fee_bps=2.0,
        mainnet_taker_fee_bps=5.0, mainnet_shadow_trades=0,
        mainnet_shadow_stress_25bps_pnl=0.0, atr_1m=0.1,
        atr_1m_updated_at=now,
    )


def bucket(s, venue, start_ms, *, qty=0.10, side="LONG", base=100.0):
    buy = side == "LONG"
    p1 = base
    p2 = base + (0.003 if buy else -0.003)
    ignition_signals.observe_trade(
        s, venue, receive_time_ms=start_ms + 1,
        event_time_ms=start_ms - 9, price=p1, qty=qty / 2,
        aggressive_buy=buy,
    )
    ignition_signals.observe_trade(
        s, venue, receive_time_ms=start_ms + 51,
        event_time_ms=start_ms + 41, price=p2, qty=qty / 2,
        aggressive_buy=buy,
    )


def warm(s, venue, start_ms=0):
    for index in range(ignition_signals.WARMUP_BUCKETS):
        begin = start_ms + index * 100
        ignition_signals.observe_trade(
            s, venue, receive_time_ms=begin + 1,
            event_time_ms=begin - 9, price=100.0, qty=0.01,
            aggressive_buy=True,
        )
        ignition_signals.observe_trade(
            s, venue, receive_time_ms=begin + 51,
            event_time_ms=begin + 41, price=100.001, qty=0.01,
            aggressive_buy=False,
        )
    ignition_signals.snapshot(s, start_ms + ignition_signals.WARMUP_BUCKETS * 100 + 1)


def evidence_row(receive_ms, side, price, *, strong=True, material=True):
    sign = 1.0 if side == "LONG" else -1.0
    conversion = 0.20 * sign if material else 0.01 * sign
    return {
        "venue": "binance_spot", "receive_time_ms": receive_ms,
        "bucket_start_ms": receive_ms - 100, "corrected_event_time_ms": receive_ms - 10,
        "clock_uncertainty_ms": 5.0, "clock_valid": True, "epoch": 1,
        "side": side, "strong": strong, "total_qty": 0.10 if material else 0.001,
        "imbalance": 0.60 * sign, "price_conversion_bps": conversion,
        "first_price": price - conversion / 10_000.0 * price,
        "price": price, "high": price, "low": price,
        "surprise_ratio": 2.0, "flow_acceleration": 1.0,
    }


class IgnitionCoreTests(unittest.TestCase):
    def test_oi_episode_binding_preserves_before_and_detects_refresh(self):
        s = state(now=3.2)
        s.open_interest = 1000.0
        s.prev_open_interest = 999.0
        s.open_interest_updated_at = 3.0
        s.open_interest_change_pct = 0.10
        s.open_interest_change_window_seconds = 5.0
        before = ignition_core._oi_state_snapshot(s)
        s.prev_open_interest = s.open_interest
        s.open_interest = 1002.0
        s.open_interest_updated_at = 3.3
        s.open_interest_change_pct = 0.20
        after = ignition_core._oi_state_snapshot(s)
        report = ignition_core._oi_verification(
            {"fresh": True, "intent": "POSITION_BUILD"}, before, after,
            episode_started_ms=3100, decision_time=3.4,
        )
        self.assertEqual(report["episode_before"]["value"], 1000.0)
        self.assertEqual(report["episode_after"]["value"], 1002.0)
        self.assertTrue(report["refresh_observed"])
        self.assertFalse(report["same_snapshot"])
        self.assertEqual(report["status"], "FRESH_POSITION_BUILD")

    def test_oi_young_snapshot_without_episode_refresh_is_unknown(self):
        sample = {"value": 1000.0, "updated_at": 3.0}
        report = ignition_core._oi_verification(
            {"fresh": True, "intent": "POSITION_BUILD"}, sample, sample,
            episode_started_ms=3100, decision_time=3.2,
        )
        self.assertEqual(report["status"], "UNCHANGED_UNKNOWN")
        self.assertFalse(report["fresh"])

    def test_oi_refresh_after_decision_cannot_confirm_with_lookahead(self):
        report = ignition_core._oi_verification(
            {"fresh": True, "intent": "POSITION_BUILD"},
            {"value": 1000.0, "updated_at": 3.0},
            {"value": 1002.0, "updated_at": 3.5},
            episode_started_ms=3100, decision_time=3.4,
        )
        self.assertEqual(report["status"], "UNAVAILABLE")
        self.assertFalse(report["no_lookahead"])

    def test_oi_old_refresh_is_stale_unknown(self):
        report = ignition_core._oi_verification(
            {"fresh": True, "intent": "POSITION_BUILD"},
            {"value": 1000.0, "updated_at": 1.0},
            {"value": 1002.0, "updated_at": 2.0},
            episode_started_ms=1500, decision_time=23.0,
        )
        self.assertEqual(report["status"], "STALE_UNKNOWN")
        self.assertFalse(report["fresh"])

    def test_oi_fresh_opposite_build_is_causal_conflict(self):
        report = ignition_core._oi_verification(
            {
                "fresh": True, "intent": "POSITION_BUILD",
                "aligned_with_entry": False,
            },
            {"value": 1000.0, "updated_at": 3.0},
            {"value": 1002.0, "updated_at": 3.3},
            episode_started_ms=3100, decision_time=3.4,
        )
        self.assertEqual(report["status"], "FRESH_CONFLICT")

    def test_flow_efficiency_requires_composite_cash_exhaustion(self):
        histories = {"binance_spot": [], "coinbase_spot": []}
        # Three contiguous 500 ms windows. Executed sell quote persists while
        # cash progress decays materially in both transitions.
        for venue in histories:
            price = 100.0
            for index in range(15):
                window = index // 5
                progress_bps = (0.50, 0.15, 0.01)[window]
                next_price = price * (1.0 - progress_bps / 10_000.0)
                histories[venue].append({
                    "venue": venue,
                    "bucket_start_ms": index * 100,
                    "receive_time_ms": index * 100 + 99,
                    "epoch": 1,
                    "clock_valid": True,
                    "buy_quote": 0.0,
                    "sell_quote": 100_000.0,
                    "buy_qty": 0.0,
                    "sell_qty": 1.0,
                    "total_qty": 1.0,
                    "first_price": price,
                    "price": next_price,
                })
                price = next_price
        report = ignition_core._flow_efficiency_snapshot(
            histories, "SHORT", ("binance_spot", "coinbase_spot")
        )
        self.assertEqual(
            report["venues"]["binance_spot"]["state"], "EXHAUSTED"
        )
        self.assertEqual(
            report["venues"]["coinbase_spot"]["state"], "EXHAUSTED"
        )

    def test_persistent_sparse_flow_uses_explicit_one_second_fallback(self):
        rows = []
        price = 100.0
        for bucket_start in (1_000, 1_400, 2_000, 2_400, 3_000, 3_400):
            next_price = price * (1.0 + 0.50 / 10_000.0)
            rows.append({
                "venue": "binance_spot",
                "bucket_start_ms": bucket_start,
                "receive_time_ms": bucket_start + 100,
                "epoch": 1, "clock_valid": True,
                "buy_quote": 25_000.0, "sell_quote": 0.0,
                "buy_qty": 0.25, "sell_qty": 0.0,
                "first_price": price, "price": next_price,
            })
            price = next_price
        report = ignition_core._flow_efficiency_snapshot(
            {"binance_spot": rows}, "LONG", ("binance_spot",)
        )["venues"]["binance_spot"]
        self.assertEqual(report["state"], "CONTINUING_CONFIRMED")
        self.assertEqual(report["window_resolution_ms"], 1_000)
        self.assertEqual(
            report["measurement_source"],
            "PERSISTENT_1S_EXECUTED_FLOW_FALLBACK",
        )
        self.assertGreater(report["marginal_conversion_now_bps"], 0.0)

    @staticmethod
    def _efficiency_rows(quotes, progresses, side="LONG"):
        rows = []
        price = 100.0
        sign = 1.0 if side == "LONG" else -1.0
        for index, (quote, progress) in enumerate(zip(quotes, progresses)):
            next_price = price * (1.0 + sign * progress / 10_000.0)
            rows.append({
                "venue": "binance_spot",
                "bucket_start_ms": index * 500,
                "receive_time_ms": (index + 1) * 500,
                "epoch": 1,
                "clock_valid": True,
                "buy_quote": quote if side == "LONG" else 0.0,
                "sell_quote": quote if side == "SHORT" else 0.0,
                "buy_qty": 1.0 if side == "LONG" else 0.0,
                "sell_qty": 1.0 if side == "SHORT" else 0.0,
                "total_qty": 1.0,
                "first_price": price,
                "price": next_price,
            })
            price = next_price
        return rows

    def test_ratio_illusion_is_fading_when_flow_and_progress_collapse(self):
        rows = self._efficiency_rows(
            (5_975.0, 11_141.0, 1_390.0),
            (0.103, 1.136, 0.248),
        )
        venue = ignition_core._flow_efficiency_snapshot(
            {"binance_spot": rows}, "LONG", ("binance_spot",)
        )["venues"]["binance_spot"]
        self.assertEqual(venue["state"], "FADING")
        self.assertEqual(
            venue["classification_reason"],
            "ABSOLUTE_FLOW_AND_PROGRESS_COLLAPSED",
        )
        self.assertTrue(venue["diagnostics"]["flow_collapsed"])
        self.assertTrue(venue["diagnostics"]["progress_collapsed"])
        self.assertGreater(
            venue["windows"][-1]["efficiency_bps_per_million"],
            venue["windows"][-2]["efficiency_bps_per_million"],
        )

    def test_single_burst_after_absorption_needs_reacceleration_proof(self):
        rows = self._efficiency_rows(
            (26_339.0, 28_873.0, 313_795.0),
            (0.0, 0.0, 2.188),
            side="SHORT",
        )
        venue = ignition_core._flow_efficiency_snapshot(
            {"binance_spot": rows}, "SHORT", ("binance_spot",)
        )["venues"]["binance_spot"]
        self.assertEqual(venue["state"], "REACCELERATION_UNCONFIRMED")
        self.assertEqual(
            venue["classification_reason"],
            "ONE_BURST_AFTER_TWO_NON_CONVERTING_WINDOWS",
        )

    def test_two_converting_windows_confirm_continuation(self):
        rows = self._efficiency_rows(
            (10_000.0, 11_000.0, 12_000.0),
            (0.20, 0.30, 0.35),
        )
        venue = ignition_core._flow_efficiency_snapshot(
            {"binance_spot": rows}, "LONG", ("binance_spot",)
        )["venues"]["binance_spot"]
        self.assertEqual(venue["state"], "CONTINUING_CONFIRMED")
        self.assertTrue(venue["diagnostics"]["conversion_survived"])

    def test_bias_wait_reasons_are_diagnostic_only(self):
        abstain = state(now=3.0)
        abstain.bias_state = "ABSTAIN"
        self.assertEqual(
            ignition_core.evaluate(abstain, now=3.0)["reason"], "BIAS_ABSTAIN"
        )
        low = state(now=3.0)
        low.bias_confidence = 0.40
        self.assertEqual(
            ignition_core.evaluate(low, now=3.0)["reason"],
            "BIAS_CONFIDENCE_LOW",
        )
        stale = state(now=3.0)
        stale.bias_updated_at = 0.0
        self.assertEqual(
            ignition_core.evaluate(stale, now=3.0)["reason"], "BIAS_STALE"
        )

    def test_precursor_progress_does_not_bridge_cash_reconnect(self):
        s = state(now=4.0)
        s.bias_price_buckets = {
            1: {"ts": 1.0, "spot": 100.0, "coinbase": 100.0,
                "venue_epochs": {"spot": 1, "coinbase": 1}},
            4: {"ts": 4.0, "spot": 101.0, "coinbase": 101.0,
                "venue_epochs": {"spot": 2, "coinbase": 2}},
        }
        report = ignition_core._precursor_cash_progress(
            s, {"receive_time_ms": 4_000, "side": "LONG"}
        )
        self.assertFalse(report["valid"])
        self.assertFalse(report["authority"])
        self.assertEqual(
            report["continuity_status"],
            "UNMEASURED_REQUIRES_EXECUTED_FLOW_PATH",
        )

    def test_precursor_reports_each_horizon_without_changing_winner(self):
        s = state(now=16.0)
        s.bias_price_buckets = {
            1: {"ts": 1.0, "spot": 100.0, "coinbase": 100.0,
                "venue_epochs": {"spot": 1, "coinbase": 1}},
            10: {"ts": 10.0, "spot": 100.10, "coinbase": 100.10,
                 "venue_epochs": {"spot": 1, "coinbase": 1}},
            13: {"ts": 13.0, "spot": 100.20, "coinbase": 100.20,
                 "venue_epochs": {"spot": 1, "coinbase": 1}},
            16: {"ts": 16.0, "spot": 100.30, "coinbase": 100.30,
                 "venue_epochs": {"spot": 1, "coinbase": 1}},
        }
        report = ignition_core._precursor_cash_progress(
            s, {"receive_time_ms": 16_000, "side": "LONG"}
        )
        self.assertTrue(report["valid"])
        self.assertEqual(report["horizon_seconds"], 15)
        self.assertEqual(set(report["horizons"]), {"3", "6", "15"})
        self.assertGreater(
            report["horizons"]["15"]["progress_bps"],
            report["horizons"]["3"]["progress_bps"],
        )

    def test_low_frozen_bias_reject_preserves_research_snapshot(self):
        s = state(now=3.2)
        s._ignition_bias_snapshots = __import__("collections").deque([
            {
                "direction": "LONG", "confidence": 0.5238,
                "raw_direction": "LONG", "raw_confidence": 0.51,
                "captured_at": 2.0, "updated_at": 2.0,
                "direction_context": {}, "s_votes": {},
            }
        ], maxlen=40)
        signal = evidence_row(3_200, "LONG", 100.01)
        self.assertIsNone(ignition_core._start_episode(s, signal))
        payload = s._ignition_last_reject_payload
        self.assertEqual(payload["bias_snapshot"]["confidence"], 0.5238)
        self.assertFalse(payload["authority"])
        self.assertTrue(payload["research_candidate_transition"])

    def test_counter_bias_impulse_creates_pending_episode_without_trade(self):
        s = state(now=3.2)
        self._freeze_bias_before_wave(s, side="LONG", captured_at=2.0)
        signal = evidence_row(3_200, "SHORT", 99.99)
        histories = {
            "binance_spot": (signal,), "coinbase_spot": (), "futures": (),
        }
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[signal]):
            result = ignition_core.evaluate(s, now=3.2)
        pending = s._ignition_pending_reversal_episode
        self.assertEqual(result["decision"], "WAIT")
        self.assertIsNone(getattr(s, "_ignition_episode", None))
        self.assertEqual(pending["side"], "SHORT")
        self.assertFalse(pending["authority"])
        self.assertEqual(
            result["reason"], "PENDING_REVERSAL_BIAS_CONFIRMATION"
        )

    def test_bias_flip_promotes_same_pending_episode_and_hash(self):
        s = state(now=3.2)
        self._freeze_bias_before_wave(s, side="LONG", captured_at=2.0)
        first = evidence_row(3_200, "SHORT", 99.99)
        histories = {
            "binance_spot": (first,), "coinbase_spot": (), "futures": (),
        }
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[first]):
            ignition_core.evaluate(s, now=3.2)
        pending = s._ignition_pending_reversal_episode
        episode_id, episode_hash = pending["episode_id"], pending["episode_hash"]
        onset_ms = pending["started_receive_ms"]

        second = evidence_row(3_300, "SHORT", 99.987)
        histories["binance_spot"] = (first, second)
        s.bias_state = "SHORT"
        s.bias_confidence = 0.80
        s.bias_updated_at = 3.3
        s.bias_council = {"s_votes": {}, "hysteresis": "CONFIRMED_FLIP"}
        ignition_signals.engine(s).venues["binance_spot"].epoch = 1
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[second]):
            result = ignition_core.evaluate(s, now=3.3)
        self.assertEqual(result["causal_episode_id"], episode_id)
        self.assertEqual(
            result["ignition"]["pending_reversal_episode_hash"], episode_hash
        )
        self.assertTrue(result["ignition"]["pending_reversal_promoted"])
        self.assertEqual(
            result["ignition"]["pending_reversal_original_onset"][
                "start_receive_ms"
            ], onset_ms,
        )
        self.assertEqual(
            result["ignition"]["pre_impulse_bias_snapshot"]["direction"],
            "LONG",
        )
        self.assertEqual(
            result["ignition"]["bias_confirmation_snapshot"]["direction"],
            "SHORT",
        )

    def test_dual_cash_failed_continuation_promotes_without_bias_flip(self):
        s = state(now=3.6)
        self._freeze_bias_before_wave(s, side="SHORT", captured_at=2.0)
        old_binance = evidence_row(3_000, "SHORT", 99.90)
        # Coinbase did not originate the flush; it only supplies an independent
        # pre-transition anchor and later accepts the reclaim.
        old_coinbase = evidence_row(
            3_050, "SHORT", 99.91, strong=False, material=False,
        )
        old_coinbase["venue"] = "coinbase_spot"
        first = evidence_row(3_200, "LONG", 99.94)
        histories = {
            "binance_spot": (old_binance, first),
            "coinbase_spot": (old_coinbase,), "futures": (),
        }
        pending = ignition_core._start_pending_reversal(
            s, first, s._ignition_bias_snapshots[-1], histories,
        )
        episode_id = pending["episode_id"]
        episode_hash = pending["episode_hash"]

        binance_follow = evidence_row(3_300, "LONG", 99.96)
        coinbase_follow = evidence_row(3_400, "LONG", 99.95)
        coinbase_follow["venue"] = "coinbase_spot"
        futures_follow = evidence_row(3_500, "LONG", 99.94)
        futures_follow["venue"] = "futures"
        futures_follow["total_qty"] = 0.20
        histories = {
            "binance_spot": (old_binance, first, binance_follow),
            "coinbase_spot": (old_coinbase, coinbase_follow),
            "futures": (futures_follow,),
        }
        ignition_core._observe_pending_reversal(
            s, [binance_follow, coinbase_follow, futures_follow], histories,
        )
        promoted = ignition_core._resolve_pending_reversal(
            s, histories, 3_550, allow_promotion=True,
        )
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted["episode_id"], episode_id)
        self.assertEqual(promoted["episode_hash"], episode_hash)
        self.assertEqual(promoted["status"], "REVERSAL_CONFIRMED")
        self.assertTrue(promoted["transition_confirmed"])
        self.assertEqual(
            promoted["transition_authority"]["accepted_cash_venues"],
            ["binance_spot", "coinbase_spot"],
        )
        self.assertTrue(
            promoted["transition_authority"]["cash_synchronous_transition"]
        )
        self.assertEqual(promoted["bias_snapshot"]["direction"], "SHORT")
        self.assertEqual(promoted["side"], "LONG")

    def test_single_cash_reclaim_cannot_bypass_frozen_bias(self):
        s = state(now=3.6)
        self._freeze_bias_before_wave(s, side="SHORT", captured_at=2.0)
        old_binance = evidence_row(3_000, "SHORT", 99.90)
        first = evidence_row(3_200, "LONG", 99.94)
        histories = {
            "binance_spot": (old_binance, first),
            "coinbase_spot": (), "futures": (),
        }
        pending = ignition_core._start_pending_reversal(
            s, first, s._ignition_bias_snapshots[-1], histories,
        )
        transition = ignition_core._transition_snapshot(
            pending, histories, 3_300,
        )
        self.assertFalse(transition["confirmed"])
        self.assertNotEqual(transition["status"], "REVERSAL_CONFIRMED")
        self.assertIsNone(ignition_core._resolve_pending_reversal(
            s, histories, 3_300, allow_promotion=True,
        ))
        self.assertIsNotNone(s._ignition_pending_reversal_episode)

    def test_dual_cash_opposing_return_invalidates_transition(self):
        s = state(now=3.8)
        self._freeze_bias_before_wave(s, side="SHORT", captured_at=2.0)
        old_binance = evidence_row(3_000, "SHORT", 99.90)
        old_coinbase = evidence_row(3_050, "SHORT", 99.91)
        old_coinbase["venue"] = "coinbase_spot"
        first = evidence_row(3_200, "LONG", 99.94)
        new_coinbase = evidence_row(3_300, "LONG", 99.95)
        new_coinbase["venue"] = "coinbase_spot"
        back_binance = evidence_row(3_500, "SHORT", 99.89)
        back_coinbase = evidence_row(3_550, "SHORT", 99.90)
        back_coinbase["venue"] = "coinbase_spot"
        histories = {
            "binance_spot": (old_binance, first, back_binance),
            "coinbase_spot": (
                old_coinbase, new_coinbase, back_coinbase,
            ),
            "futures": (),
        }
        pending = ignition_core._start_pending_reversal(
            s, first, s._ignition_bias_snapshots[-1], histories,
        )
        transition = ignition_core._transition_snapshot(
            pending, histories, 3_600,
        )
        self.assertEqual(transition["status"], "TRANSITION_FAILED")
        self.assertTrue(transition["hard_contradiction"])
        self.assertFalse(transition["confirmed"])

    def test_synchronous_dual_cash_transition_resolves_leader_uncertainty(self):
        s = state(now=10.0)
        first = evidence_row(9_600, "LONG", 100.01)
        second = evidence_row(9_700, "LONG", 100.02)
        second["venue"] = "coinbase_spot"
        futures = evidence_row(9_800, "LONG", 100.02)
        futures["venue"] = "futures"
        futures["total_qty"] = 0.20
        episode = {
            "causal_episode_id": "ign:binance_spot:LONG:9500",
            "side": "LONG", "proposer": "binance_spot",
            "started_receive_ms": 9_600, "last_evidence_ms": 9_800,
            "bias_snapshot": {
                "direction": "SHORT", "confidence": 0.80,
                "direction_context": {"phase": "ESTABLISHED_TREND"},
            },
            "signals": [first, second, futures],
            "epochs": {
                "binance_spot": 1, "coinbase_spot": 1, "futures": 1,
            },
            "precursor_measurement": {"valid": False},
            "oi_before_snapshot": {},
            "transition_confirmed": True,
            "transition_authority": {
                "status": "REVERSAL_CONFIRMED", "side": "LONG",
                "cash_synchronous_transition": True,
                "hard_contradiction": False,
            },
        }
        histories = {
            "binance_spot": (first,),
            "coinbase_spot": (second,), "futures": (futures,),
        }
        freshness = {
            "coinbase_mode": "FRESH", "binance_spot_ready": True,
            "futures_ready": True,
        }
        phase = {
            "valid": True, "source": "TEST", "phase_scale_bps": 10.0,
            "cash_displacement_bps": 2.0,
            "episode_cash_displacement_bps": 2.0,
            "precursor_cash_displacement_bps": 0.0,
            "consumed_fraction": 0.20,
        }
        with patch.object(
            ignition_core, "_proof",
            return_value=("METAORDER_CONTINUATION", first, "binance_spot"),
        ), patch.object(
            ignition_core, "_leader", return_value=("SIMULTANEOUS", -5.0),
        ), patch.object(
            ignition_core, "_phase_measurement", return_value=phase,
        ), patch.object(
            ignition_core, "_oi_verification",
            return_value={"status": "UNCHANGED_UNKNOWN", "intent": "NEUTRAL"},
        ):
            result = ignition_core._result_from_episode(
                s, episode, histories, freshness, 10.0,
            )
        self.assertEqual(result["decision"], "GO")
        self.assertTrue(result["ignition"]["transition_confirmed"])
        self.assertEqual(result["ignition"]["leader"], "SIMULTANEOUS")

    def test_late_bias_flip_cannot_reuse_expired_pending_episode(self):
        s = state(now=3.2)
        self._freeze_bias_before_wave(s, side="LONG", captured_at=2.0)
        signal = evidence_row(3_200, "SHORT", 99.99)
        histories = {
            "binance_spot": (signal,), "coinbase_spot": (), "futures": (),
        }
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[signal]):
            ignition_core.evaluate(s, now=3.2)
        expired_id = s._ignition_pending_reversal_episode["episode_id"]
        s.bias_state = "SHORT"
        s.bias_confidence = 0.80
        s.bias_updated_at = 9.0
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=9.0)
        self.assertIsNone(s._ignition_pending_reversal_episode)
        self.assertNotEqual(result.get("causal_episode_id"), expired_id)
        self.assertEqual(
            result["reason"], "PENDING_REVERSAL_TTL_EXPIRED"
        )

    def test_borderline_onset_is_teed_before_low_bias_early_return(self):
        s = state(now=3.2)
        s.bias_confidence = 0.52
        s._ignition_bias_snapshots = __import__("collections").deque([
            {
                "direction": "LONG", "confidence": 0.5238,
                "raw_direction": "LONG", "raw_confidence": 0.51,
                "captured_at": 2.0, "updated_at": 2.0,
                "direction_context": {}, "s_votes": {},
            }
        ], maxlen=40)
        signal = evidence_row(3_200, "LONG", 100.01)
        with patch.object(ignition_signals, "snapshot", return_value={
            "binance_spot": (), "coinbase_spot": (), "futures": (),
        }), patch.object(ignition_core, "_new_signals", return_value=[signal]):
            result = ignition_core.evaluate(s, now=3.2)
        self.assertEqual(result["reason"], "BIAS_CONFIDENCE_LOW")
        self.assertEqual(
            result["ignition"]["research_reject_reason"],
            "BORDERLINE_PRE_BIAS_RESEARCH",
        )
        self.assertEqual(result["ignition"]["research_side"], "LONG")
        self.assertFalse(result["ignition"]["authority"])

    def test_borderline_onset_is_teed_before_abstain_early_return(self):
        s = state(now=3.2)
        s.bias_state = "ABSTAIN"
        s.bias_confidence = 0.0
        s._ignition_bias_snapshots = __import__("collections").deque([
            {
                "direction": "SHORT", "confidence": 0.51,
                "raw_direction": "SHORT", "raw_confidence": 0.50,
                "captured_at": 2.0, "updated_at": 2.0,
                "direction_context": {}, "s_votes": {},
            }
        ], maxlen=40)
        signal = evidence_row(3_200, "SHORT", 99.99)
        with patch.object(ignition_signals, "snapshot", return_value={
            "binance_spot": (), "coinbase_spot": (), "futures": (),
        }), patch.object(ignition_core, "_new_signals", return_value=[signal]):
            result = ignition_core.evaluate(s, now=3.2)
        self.assertEqual(result["reason"], "BIAS_ABSTAIN")
        self.assertEqual(result["ignition"]["research_side"], "SHORT")
        self.assertFalse(result["ignition"]["authority"])

    def test_persistent_metaorder_measurement_has_no_direct_authority(self):
        histories = {}
        for venue in ("binance_spot", "futures"):
            rows = []
            for index in range(3):
                row = evidence_row((index + 1) * 1_000, "LONG", 100.0 + index * 0.01)
                row["venue"] = venue
                row["buy_qty"], row["sell_qty"] = (
                    (0.16, 0.04) if venue == "futures" else (0.08, 0.02)
                )
                rows.append(row)
            histories[venue] = rows
        histories["coinbase_spot"] = []
        report = ignition_core._persistent_metaorder_shadow(histories, "LONG", 3_100)
        self.assertEqual(report["status"], "PERSISTENT_METAORDER_CANDIDATE")
        self.assertFalse(report["authority"])
        self.assertEqual(
            report["policy"], "MEASUREMENT_ONLY_AUTHORITY_CHECKS_DOWNSTREAM"
        )

    def test_persistent_metaorder_is_observed_without_ignition_episode(self):
        histories = {}
        for venue in ("binance_spot", "futures"):
            rows = []
            for index in range(3):
                row = evidence_row((index + 1) * 1_000, "LONG", 100.0 + index * 0.01,
                                   strong=False)
                buy, sell = (
                    (0.16, 0.04) if venue == "futures" else (0.08, 0.02)
                )
                row.update({"venue": venue, "buy_qty": buy, "sell_qty": sell})
                rows.append(row)
            histories[venue] = tuple(rows)
        histories["coinbase_spot"] = ()
        s = state(now=3.1)
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "WAIT")
        self.assertIsNone(getattr(s, "_ignition_episode", None))
        self.assertEqual(s.persistent_metaorder_shadow["candidate_side"], "LONG")
        self.assertEqual(
            s.persistent_metaorder_shadow["candidate_id"], "pmeta:LONG:1000",
        )
        self.assertEqual(
            s.persistent_metaorder_shadow["candidate_started_at_ms"], 1_000,
        )
        self.assertFalse(s.persistent_metaorder_shadow["authority"])

    @staticmethod
    def _persistent_histories(side="LONG", cash="binance_spot", start_ms=1_000,
                              opposing_cash=False):
        histories = {name: [] for name in (
            "binance_spot", "coinbase_spot", "futures"
        )}
        sign = 1.0 if side == "LONG" else -1.0
        for venue in (cash, "futures"):
            for index in range(3):
                row = evidence_row(
                    start_ms + index * 1_000, side,
                    100.0 + sign * index * 0.01, strong=False,
                )
                total = 0.20 if venue == "futures" else 0.10
                aligned = total * 0.80
                residual = total - aligned
                row.update({
                    "venue": venue,
                    "buy_qty": aligned if side == "LONG" else residual,
                    "sell_qty": residual if side == "LONG" else aligned,
                    "buy_quote": (
                        aligned if side == "LONG" else residual
                    ) * row["price"],
                    "sell_quote": (
                        residual if side == "LONG" else aligned
                    ) * row["price"],
                })
                histories[venue].append(row)
        if opposing_cash:
            other = "coinbase_spot" if cash == "binance_spot" else "binance_spot"
            oppose = "SHORT" if side == "LONG" else "LONG"
            for index in range(3):
                row = evidence_row(
                    start_ms + index * 1_000, oppose,
                    100.0 - sign * index * 0.01, strong=False,
                )
                row.update({
                    "venue": other,
                    "buy_qty": 0.02 if side == "LONG" else 0.08,
                    "sell_qty": 0.08 if side == "LONG" else 0.02,
                })
                histories[other].append(row)
        return {name: tuple(rows) for name, rows in histories.items()}

    @staticmethod
    def _freeze_bias_before_wave(s, side="LONG", captured_at=0.0):
        s.bias_state = side
        s.bias_confidence = 0.80
        s._ignition_bias_snapshots = __import__("collections").deque([{
            "direction": side,
            "confidence": 0.80,
            "raw_direction": side,
            "raw_confidence": 0.80,
            "captured_at": captured_at,
            "updated_at": captured_at,
            "direction_context": {
                "phase": "ESTABLISHED_TREND",
                "context_side": side,
                "candidate_side": "ABSTAIN",
                "flow_price_trap": False,
            },
            "s_votes": {},
        }], maxlen=40)

    def test_persistent_cash_wave_becomes_shadow_entry_candidate(self):
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        histories = self._persistent_histories()
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "GO")
        self.assertEqual(result["entry_mode"], "PERSISTENT_METAORDER")
        self.assertEqual(result["ignition"]["proof_type"], "PERSISTENT_METAORDER")
        self.assertEqual(result["ignition"]["proposer"], "binance_spot")
        self.assertTrue(result["ignition"]["futures_follow_ok"])
        proposer_flow = result["ignition"]["flow_by_venue"]["binance_spot"]
        self.assertGreater(proposer_flow["recent_1s_volume_btc"], 0.0)
        self.assertGreater(proposer_flow["recent_1s_signed_imbalance"], 0.0)
        self.assertGreater(proposer_flow["recent_1s_price_progress_bps"], 0.0)

    def test_provisional_context_rejects_single_cash_neutral_oi(self):
        """Regression: the 18:46-style correlated echo is not whale flow."""
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        s._ignition_bias_snapshots[-1]["direction_context"].update({
            "phase": "CONTEXT_WITHOUT_CONFIRMATION",
            "oi_regime": "NEUTRAL",
        })
        histories = self._persistent_histories()
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(
            result["reason"],
            "PROVISIONAL_CONTEXT_REQUIRES_DUAL_CASH_OI_BUILD",
        )

    def test_provisional_context_waits_then_accepts_causal_oi_refresh(self):
        """A young pre-wave OI sample cannot masquerade as causal build."""
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        s._ignition_bias_snapshots[-1]["direction_context"].update({
            "phase": "CONTEXT_WITHOUT_CONFIRMATION",
            "oi_regime": "NEUTRAL",
        })
        s.open_interest = 1000.0
        s.prev_open_interest = 999.0
        s.open_interest_change_pct = 0.03
        s.open_interest_change_window_seconds = 3.0
        s.open_interest_updated_at = 0.9
        histories = dict(self._persistent_histories(cash="binance_spot"))
        coinbase = self._persistent_histories(cash="coinbase_spot")
        histories["coinbase_spot"] = coinbase["coinbase_spot"]
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
            self.assertEqual(result["decision"], "WAIT")
            s.prev_open_interest = 1000.0
            s.open_interest = 1002.0
            s.open_interest_updated_at = 3.15
            confirmed = ignition_core.evaluate(s, now=3.2)
        self.assertEqual(confirmed["decision"], "GO")
        self.assertEqual(
            confirmed["ignition"]["oi_verification_state"]["status"],
            "FRESH_POSITION_BUILD",
        )

    def test_unwind_rejects_single_cash_liquidation_aftershock(self):
        """Falling OI plus one cash venue is closure, not whale commitment."""
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        s.prev_open_interest = 1000.0
        s.open_interest_change_pct = -0.03
        s.open_interest_change_window_seconds = 3.0
        s.open_interest_updated_at = 3.0
        s.open_interest = 970.0
        s._persistent_oi_episode_binding = {
            "candidate_id": "pmeta:LONG:1000",
            "before": {"value": 1000.0, "updated_at": 0.9},
        }
        histories = self._persistent_histories(cash="binance_spot")
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(
            result["reason"], "UNWIND_REQUIRES_DUAL_CASH_PERSISTENCE",
        )

    def test_unwind_keeps_dual_cash_persistent_wave(self):
        """Anti-overfit: independently converted cash unwind remains valid."""
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        s.prev_open_interest = 1000.0
        s.open_interest_change_pct = -0.03
        s.open_interest_change_window_seconds = 3.0
        s.open_interest_updated_at = 3.0
        s.open_interest = 970.0
        s._persistent_oi_episode_binding = {
            "candidate_id": "pmeta:LONG:1000",
            "before": {"value": 1000.0, "updated_at": 0.9},
        }
        histories = dict(self._persistent_histories(cash="binance_spot"))
        coinbase = self._persistent_histories(cash="coinbase_spot")
        histories["coinbase_spot"] = coinbase["coinbase_spot"]
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "GO")
        self.assertEqual(
            result["ignition"]["unwind_cash_independence"], "DUAL_CASH",
        )

    def test_persistent_wave_cannot_reset_same_wave_precursor_consumed(self):
        s = state(now=16.1)
        self._freeze_bias_before_wave(s, captured_at=12.0)
        s.atr_1m = 0.10
        s.atr_1m_updated_at = 15.0
        s.bias_price_buckets = {
            10: {
                "ts": 10.0, "spot": 100.0, "coinbase": 100.0,
                "venue_epochs": {"spot": 1, "coinbase": 1},
            },
            13: {
                "ts": 13.0, "spot": 100.08, "coinbase": 100.08,
                "venue_epochs": {"spot": 1, "coinbase": 1},
            },
        }
        histories = self._persistent_histories(start_ms=13_000)
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=16.1)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_IGNITION")
        self.assertEqual(
            s.persistent_metaorder_shadow["candidate_side"], "LONG",
        )

    def test_persistent_wave_id_bridges_lull_without_duplicate_episode(self):
        first_histories = self._persistent_histories()
        first = ignition_core._persistent_metaorder_snapshot(
            first_histories, 3_100,
        )
        lull = ignition_core._persistent_metaorder_snapshot(
            {name: () for name in first_histories}, 4_500, first,
        )
        resumed = ignition_core._persistent_metaorder_snapshot(
            self._persistent_histories(start_ms=3_000), 5_500, lull,
        )
        self.assertTrue(lull["wave_bridge_active"])
        self.assertEqual(first["candidate_id"], resumed["candidate_id"])
        self.assertEqual(resumed["wave_started_at_ms"], 1_000)

    def test_continuous_wave_expires_without_becoming_a_second_episode(self):
        first = ignition_core._persistent_metaorder_snapshot(
            self._persistent_histories(), 3_100,
        )
        carried = dict(first, wave_last_confirmed_at_ms=16_000)
        continuous = ignition_core._persistent_metaorder_snapshot(
            self._persistent_histories(start_ms=16_000), 18_100, carried,
        )
        self.assertEqual(first["candidate_id"], continuous["candidate_id"])
        self.assertTrue(continuous["wave_expired"])

    def test_captured_persistent_wave_cannot_open_twice(self):
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        histories = self._persistent_histories()
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
            ignition_core.capture_episode(
                s, result["causal_episode_id"], side="LONG",
                last_evidence_ms=result["ignition"]["last_evidence_ms"],
            )
            later = ignition_core.evaluate(s, now=3.2)
        self.assertEqual(result["decision"], "GO")
        self.assertEqual(later["decision"], "WAIT")

    def test_persistent_lane_rejects_opposing_cash_and_hindsight_flip(self):
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        opposed = self._persistent_histories(opposing_cash=True)
        with patch.object(ignition_signals, "snapshot", return_value=opposed), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            blocked = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(blocked["decision"], "WAIT")

    def test_persistent_lane_rejects_warmup_context_from_smoke_loss(self):
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        s._ignition_bias_snapshots[-1]["direction_context"].update({
            "phase": "WARMUP_OR_NEUTRAL", "context_side": "ABSTAIN",
        })
        histories = self._persistent_histories(cash="coinbase_spot")
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            blocked = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(blocked["decision"], "WAIT")

        # The 15:03-style case is not allowed to rewrite a frozen SHORT Bias
        # merely because a later LONG move became visible in hindsight.
        s = state(now=3.1)
        self._freeze_bias_before_wave(s, side="SHORT")
        long_wave = self._persistent_histories(side="LONG")
        with patch.object(ignition_signals, "snapshot", return_value=long_wave), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            blocked = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(blocked["decision"], "WAIT")

    def test_persistent_shadow_bootstrap_live_still_requires_empirical_cohort(self):
        s = state(now=3.1)
        self._freeze_bias_before_wave(s)
        histories = self._persistent_histories()
        with patch.object(ignition_signals, "snapshot", return_value=histories), \
             patch.object(ignition_core, "_new_signals", return_value=[]):
            result = ignition_core.evaluate(s, now=3.1)
        allowed, report = entry_edge_tier.authorize(result, s)
        self.assertTrue(allowed)
        self.assertTrue(report["bootstrap_shadow_allowed"])
        self.assertEqual(report["shadow_ledger_type"], "RESEARCH_PROBE")
        self.assertFalse(report["live_like_shadow_allowed"])
        s.wstrade_live_armed = True
        allowed, report = entry_edge_tier.authorize(result, s)
        self.assertFalse(allowed)
        self.assertFalse(report["live_empirical_ok"])

    def test_persistent_metaorder_does_not_bridge_empty_seconds(self):
        histories = {}
        for venue in ("binance_spot", "futures"):
            rows = []
            for index, receive_ms in enumerate((1_000, 3_000, 5_000)):
                row = evidence_row(receive_ms, "LONG", 100.0 + index * 0.01)
                row["venue"] = venue
                row["buy_qty"], row["sell_qty"] = 0.08, 0.02
                rows.append(row)
            histories[venue] = rows
        histories["coinbase_spot"] = []
        report = ignition_core._persistent_metaorder_shadow(histories, "LONG", 5_100)
        self.assertEqual(report["status"], "OBSERVING")
        self.assertFalse(report["venues"]["binance_spot"]["contiguous_seconds"])

    def test_frozen_bias_keeps_direction_context(self):
        s = state()
        s.bias_council = {
            "hysteresis": "STABLE",
            "story": {"name": "NEW_LONG_BUILD_CONFIRMED"},
            "direction_memory": {
                "phase": "ESTABLISHED_TREND", "context_side": "LONG",
                "candidate_side": "ABSTAIN",
            },
            "s_votes": {
                "S1_cross_price": {"vote": "LONG"},
                "S2_price_x_oi": {"metrics": {"regime": "NEW_LONG_BUILD"}},
                "S3_multi_flow": {"vote": "LONG"},
            },
        }
        ignition_core._remember_bias(s, 2.0)
        frozen = s._ignition_bias_snapshots[-1]["direction_context"]
        self.assertEqual(frozen["phase"], "ESTABLISHED_TREND")
        self.assertEqual(frozen["oi_regime"], "NEW_LONG_BUILD")
        self.assertEqual(frozen["flow_vote"], "LONG")

    def test_reversal_candidate_cannot_reuse_old_frozen_bias(self):
        s = state()
        s.bias_council = {
            "direction_memory": {
                "phase": "REVERSAL_CANDIDATE", "context_side": "LONG",
                "candidate_side": "SHORT",
            },
            "s_votes": {},
        }
        ignition_core._remember_bias(s, 2.0)
        signal = evidence_row(3_100, "LONG", 100.01)
        self.assertIsNone(ignition_core._start_episode(s, signal))
        self.assertEqual(s._ignition_last_reject, "BIAS_REVERSAL_CANDIDATE_PENDING")

    def test_failed_reversion_needs_material_timed_acceptance(self):
        pre = evidence_row(900, "LONG", 100.0)
        initial = evidence_row(1_000, "LONG", 100.002)
        shock = evidence_row(1_100, "SHORT", 99.995)
        shock["low"] = 99.995
        reclaim = evidence_row(1_200, "LONG", 100.001)
        acceptance_1 = evidence_row(1_300, "LONG", 100.003)
        acceptance_2 = evidence_row(1_600, "LONG", 100.005)
        proof = ignition_core._failed_reversion(
            [pre, initial, shock, reclaim, acceptance_1, acceptance_2], "LONG", 1_000
        )
        self.assertIsNotNone(proof)
        detail = proof["_failed_reversion_evidence"]
        self.assertEqual(detail["version"], "FAILED_REVERSION_V4")
        self.assertGreaterEqual(detail["acceptance_duration_ms"], 400)
        self.assertGreaterEqual(detail["acceptance_material_buckets"], 1)

        too_brief = ignition_core._failed_reversion(
            [pre, initial, shock, reclaim, acceptance_1], "LONG", 1_000
        )
        self.assertIsNone(too_brief)

        weak_acceptance = evidence_row(1_600, "LONG", 100.003, material=False)
        rejected = ignition_core._failed_reversion(
            [pre, initial, shock, reclaim, acceptance_1, weak_acceptance], "LONG", 1_000
        )
        self.assertIsNone(rejected)

    def test_phase_is_observed_cash_displacement_over_atr(self):
        s = state()
        measured = ignition_core._phase_measurement(
            s, "LONG", ["binance_spot"], {"binance_spot": 2.0},
            {"price_conversion_bps": 0.5, "receive_time_ms": 3_000},
        )
        self.assertTrue(measured["valid"])
        self.assertEqual(
            measured["source"],
            "HYBRID_PRECURSOR_AND_EPISODE_CASH_DISPLACEMENT_OVER_ATR_1M",
        )
        self.assertAlmostEqual(measured["phase_scale_bps"], 10.0, places=3)
        self.assertAlmostEqual(measured["consumed_fraction"], 0.2, places=3)

    def test_phase_rejects_stale_atr(self):
        s = state(now=200.0)
        s.atr_1m_updated_at = 1.0
        measured = ignition_core._phase_measurement(
            s, "LONG", ["binance_spot"], {"binance_spot": 2.0},
            {"price_conversion_bps": 0.5, "receive_time_ms": 200_000},
        )
        self.assertFalse(measured["valid"])
        self.assertEqual(measured["source"], "ATR_1M_STALE")

    def test_precursor_progress_prevents_late_episode_phase_reset(self):
        s = state(now=20.0)
        s.bias_price_buckets = {
            5: {"ts": 5.0, "spot": 100.0, "coinbase": 100.0},
            17: {"ts": 17.0, "spot": 100.0, "coinbase": 100.0},
            20: {"ts": 20.0, "spot": 100.08, "coinbase": 100.08},
        }
        precursor = ignition_core._precursor_cash_progress(
            s, evidence_row(20_000, "LONG", 100.08),
        )
        self.assertTrue(precursor["valid"])
        self.assertAlmostEqual(precursor["progress_bps"], 8.0, places=3)
        episode = {"precursor_measurement": precursor}
        measured = ignition_core._phase_measurement(
            s, "LONG", ["binance_spot"], {"binance_spot": 2.0},
            {"price_conversion_bps": 0.5}, episode,
        )
        self.assertAlmostEqual(measured["episode_cash_displacement_bps"], 2.0)
        self.assertAlmostEqual(measured["precursor_cash_displacement_bps"], 8.0)
        self.assertGreater(measured["consumed_fraction"], ignition_core.MAX_CONSUMED_FRACTION)

    def test_oi_intent_direction_conflict_is_explicit(self):
        s = state(now=3.0)
        # A later mutable council update must not rewrite the pre-impulse OI
        # classification used by this candidate.
        s.bias_council = {"s_votes": {"S2_price_x_oi": {
            "metrics": {"regime": "NEW_SHORT_BUILD"}
        }}}
        frozen = {"direction_context": {
            "oi_regime": "SHORT_COVERING", "oi_updated_at": 3.0,
        }}
        intent = ignition_core._oi_intent(s, "SHORT", 3.0, frozen)
        self.assertTrue(intent["fresh"])
        self.assertFalse(intent["aligned_with_entry"])
        self.assertEqual(intent["causal_class"], "OI_DIRECTION_CONFLICT")
        self.assertEqual(intent["raw_regime"], "SHORT_COVERING")

    def test_frozen_oi_regime_cannot_borrow_new_live_timestamp(self):
        s = state(now=10.0)
        frozen = {"direction_context": {
            "oi_regime": "NEW_LONG_BUILD", "oi_updated_at": 1.0,
        }}
        intent = ignition_core._oi_intent(s, "LONG", 10.0, frozen)
        self.assertFalse(intent["fresh"])
        self.assertEqual(intent["intent_source"], "FROZEN_BIAS_OI_REGIME")
        self.assertEqual(intent["causal_class"], "OI_STALE_CONTEXT")

    def test_fresh_oi_delta_classifies_build_or_unwind_without_direction_vote(self):
        s = state(now=10.0)
        s.prev_open_interest = 100_000.0
        s.open_interest_change_window_seconds = 5.0
        s.open_interest_change_pct = -0.03
        frozen = {"direction_context": {"oi_regime": "NEW_SHORT_BUILD"}}
        unwind = ignition_core._oi_intent(s, "SHORT", 10.0, frozen)
        self.assertEqual(unwind["intent"], "UNWIND")
        self.assertEqual(unwind["intent_source"], "REFRESHED_OI_DELTA")
        self.assertTrue(unwind["aligned_with_entry"])

        s.open_interest_change_pct = 0.03
        build = ignition_core._oi_intent(s, "SHORT", 10.0, frozen)
        self.assertEqual(build["intent"], "POSITION_BUILD")
        self.assertEqual(build["intent_source"], "REFRESHED_OI_DELTA")

    def test_fresh_immaterial_oi_delta_overrides_stale_frozen_regime_as_neutral(self):
        s = state(now=10.0)
        s.prev_open_interest = 100_000.0
        s.open_interest_change_window_seconds = 5.0
        s.open_interest_change_pct = 0.008
        frozen = {"direction_context": {
            "oi_regime": "NEW_LONG_BUILD", "oi_updated_at": 1.0,
        }}
        intent = ignition_core._oi_intent(s, "LONG", 10.0, frozen)
        self.assertTrue(intent["fresh"])
        self.assertEqual(intent["intent"], "NEUTRAL")
        self.assertEqual(intent["intent_source"], "REFRESHED_OI_DELTA_NEUTRAL")
        self.assertEqual(intent["causal_class"], "OI_NEUTRAL")

    def test_futures_proposer_waits_for_fresh_oi_and_rejects_unwind(self):
        s = state(now=10.0)
        futures = evidence_row(9_600, "SHORT", 100.0)
        futures["venue"] = "futures"
        cash_1 = evidence_row(9_700, "SHORT", 99.997)
        cash_2 = evidence_row(9_800, "SHORT", 99.994)
        episode = {
            "causal_episode_id": "ign:futures:SHORT:9500",
            "side": "SHORT", "proposer": "futures",
            "started_receive_ms": 9_600, "last_evidence_ms": 9_800,
            "bias_snapshot": {
                "direction": "SHORT", "confidence": 0.8,
                "direction_context": {"oi_regime": "NEW_SHORT_BUILD"},
            },
            "signals": [futures, cash_1, cash_2],
            "epochs": {"futures": 1, "binance_spot": 1},
            "precursor_measurement": {"valid": False},
        }
        histories = {
            "binance_spot": (cash_1, cash_2),
            "coinbase_spot": (), "futures": (futures,),
        }
        freshness = {
            "coinbase_mode": "FRESH", "binance_spot_ready": True,
            "futures_ready": True,
        }
        s.open_interest_updated_at = 1.0
        stale = ignition_core._result_from_episode(
            s, dict(episode), histories, freshness, 10.0,
        )
        self.assertEqual(stale["reason"], "WAIT_CAUSAL_LEADER_UNCERTAIN")
        self.assertEqual(
            stale["ignition"]["oi_verification_state"]["status"],
            "UNAVAILABLE",
        )

        episode["oi_before_snapshot"] = {
            "value": 100_000.0, "updated_at": 9.0,
        }
        s.open_interest_updated_at = 10.0
        s.open_interest = 99_970.0
        s.prev_open_interest = 100_000.0
        s.open_interest_change_window_seconds = 5.0
        s.open_interest_change_pct = -0.03
        unwind = ignition_core._result_from_episode(
            s, dict(episode), histories, freshness, 10.0,
        )
        self.assertEqual(unwind["reason"], "WAIT_FUTURES_PROPOSER_OI_UNWIND")
        self.assertEqual(unwind["phase"], "INVALID")

    def test_stale_oi_is_labeled_context_not_position_build(self):
        s = state(now=10.0)
        s.open_interest_updated_at = 1.0
        frozen = {"direction_context": {"oi_regime": "NEW_LONG_BUILD"}}
        intent = ignition_core._oi_intent(s, "LONG", 10.0, frozen)
        self.assertFalse(intent["fresh"])
        self.assertEqual(intent["intent"], "POSITION_BUILD")
        self.assertEqual(intent["causal_class"], "OI_STALE_CONTEXT")

    def test_cash_metaorder_proves_early_and_only_once(self):
        s = state()
        warm(s, "binance_spot")
        warm(s, "futures")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "binance_spot", 3_000)
        first = ignition_core.evaluate(s, now=3.101)
        self.assertEqual(first["phase"], "PROBE")
        self.assertEqual(first["decision"], "WAIT")
        bucket(s, "binance_spot", 3_100, base=100.003)
        waiting = ignition_core.evaluate(s, now=3.201)
        self.assertEqual(waiting["decision"], "WAIT")
        self.assertEqual(waiting["reason"], "WAIT_CASH_IGNITION_FUTURES_RESPONSE")
        bucket(s, "futures", 3_300, qty=0.20, base=100.003)
        proved = ignition_core.evaluate(s, now=3.401)
        self.assertEqual(proved["decision"], "GO")
        self.assertEqual(proved["ignition"]["proof_type"], "METAORDER_CONTINUATION")
        self.assertEqual(
            proved["ignition"]["metaorder_proof_evidence"][
                "proof_bucket_gap_ms"
            ],
            100,
        )
        self.assertTrue(
            proved["ignition"]["metaorder_proof_evidence"][
                "proof_buckets_adjacent"
            ]
        )
        self.assertEqual(proved["ignition"]["residual_edge_proxy_bps"], 0.0)
        self.assertEqual(
            proved["ignition"]["residual_edge_source"],
            "EMPIRICAL_GUARDIAN_OUTCOME_REQUIRED",
        )
        self.assertIn("handoff_gap_bps", proved["ignition"])
        self.assertTrue(proved["ignition"]["futures_follow_ok"])
        self.assertLessEqual(proved["ignition"]["consumed_fraction"], 0.35)
        proposer_flow = proved["ignition"]["flow_by_venue"]["binance_spot"]
        self.assertGreater(proposer_flow["price_conversion_bps"], 0.0)
        self.assertGreater(proposer_flow["receive_time_ms"], 0)
        repeated = ignition_core.evaluate(s, now=3.402)
        self.assertEqual(repeated["decision"], "GO")
        self.assertEqual(repeated["causal_episode_id"], proved["causal_episode_id"])
        self.assertTrue(ignition_core.capture_episode(
            s, proved["causal_episode_id"], side="LONG",
            last_evidence_ms=proved["ignition"]["last_evidence_ms"],
        ))
        captured = ignition_core.evaluate(s, now=3.403)
        self.assertNotEqual(captured["decision"], "GO")

    def test_metaorder_proof_keeps_brief_nonmaterial_pause(self):
        first = evidence_row(3_100, "LONG", 100.001)
        pause = evidence_row(
            3_200, "SHORT", 100.001, strong=False, material=False,
        )
        second = evidence_row(3_300, "LONG", 100.003)
        episode = {
            "side": "LONG", "started_receive_ms": 3_000,
            "signals": [first, second],
        }
        proof_type, proof_signal, proof_venue = ignition_core._proof(
            episode,
            {"binance_spot": (first, pause, second)},
        )

        self.assertEqual(proof_type, "METAORDER_CONTINUATION")
        self.assertEqual(proof_signal["bucket_start_ms"], second["bucket_start_ms"])
        self.assertEqual(proof_venue, "binance_spot")
        evidence = proof_signal["_metaorder_evidence"]
        self.assertEqual(evidence["proof_buckets"], [3_000, 3_200])
        self.assertEqual(evidence["proof_bucket_gap_ms"], 200)
        self.assertFalse(evidence["proof_buckets_adjacent"])
        self.assertEqual(evidence["intervening_nonmaterial_buckets"], 1)
        self.assertTrue(evidence["metadata_authority"])

    def test_metaorder_proof_does_not_stitch_disconnected_cash_impulses(self):
        first = evidence_row(3_100, "LONG", 100.001)
        second = evidence_row(3_600, "LONG", 100.006)
        episode = {
            "side": "LONG", "started_receive_ms": 3_000,
            "signals": [first, second],
        }
        proof_type, _, _ = ignition_core._proof(
            episode,
            {"binance_spot": (first, second)},
        )

        self.assertIsNone(proof_type)

    def test_futures_alert_never_self_opens(self):
        s = state()
        warm(s, "futures")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "futures", 3_000, qty=1.0)
        result = ignition_core.evaluate(s, now=3.101)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["reason"], "WAIT_FUTURES_ALERT_CASH_RESPONSE")

    def test_futures_lead_can_prove_only_after_independent_cash_response(self):
        s = state()
        warm(s, "futures")
        warm(s, "binance_spot")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "futures", 3_000, qty=1.0)
        ignition_core.evaluate(s, now=3.101)
        bucket(s, "binance_spot", 3_300)
        waiting = ignition_core.evaluate(s, now=3.401)
        self.assertEqual(waiting["decision"], "WAIT")
        bucket(s, "binance_spot", 3_400, base=100.003)
        proved = ignition_core.evaluate(s, now=3.501)
        self.assertEqual(proved["decision"], "GO")
        self.assertEqual(proved["ignition"]["leader"], "futures")
        self.assertTrue(proved["ignition"]["futures_cash_response_ok"])

    def test_two_bucket_futures_reversal_invalidates_latched_follow(self):
        s = state(now=3.6)
        cash1 = evidence_row(3_100, "LONG", 100.003)
        cash2 = evidence_row(3_200, "LONG", 100.006)
        follow = evidence_row(3_300, "LONG", 100.004, strong=False)
        reverse1 = evidence_row(3_400, "SHORT", 100.001, strong=False)
        reverse2 = evidence_row(3_500, "SHORT", 99.998, strong=False)
        for row in (follow, reverse1, reverse2):
            row["venue"] = "futures"
            row["total_qty"] = 0.20
        episode = {
            "causal_episode_id": "ign:binance_spot:LONG:3100",
            "side": "LONG", "proposer": "binance_spot",
            "started_receive_ms": 3_100, "last_evidence_ms": 3_500,
            "bias_snapshot": {"direction": "LONG", "confidence": 0.8},
            "signals": [cash1, cash2, follow, reverse1, reverse2],
            "epochs": {"binance_spot": 1, "futures": 1},
        }
        report = ignition_core._result_from_episode(
            s, episode,
            {"binance_spot": (cash1, cash2), "coinbase_spot": (),
             "futures": (follow, reverse1, reverse2)},
            {"coinbase_mode": "FRESH", "binance_spot_ready": True,
             "futures_ready": True},
            3.6,
        )
        self.assertEqual(report["decision"], "WAIT")
        self.assertFalse(report["ignition"]["futures_follow_ok"])
        self.assertTrue(report["ignition"]["futures_follow_invalidated"])

    def test_non_adjacent_futures_reversals_do_not_invalidate_follow(self):
        s = state(now=3.7)
        cash1 = evidence_row(3_100, "LONG", 100.003)
        cash2 = evidence_row(3_200, "LONG", 100.006)
        follow = evidence_row(3_300, "LONG", 100.004, strong=False)
        reverse1 = evidence_row(3_400, "SHORT", 100.001, strong=False)
        aligned = evidence_row(3_500, "LONG", 100.003, strong=False)
        reverse2 = evidence_row(3_600, "SHORT", 99.998, strong=False)
        for row in (follow, reverse1, aligned, reverse2):
            row["venue"] = "futures"
            row["total_qty"] = 0.20
        episode = {
            "causal_episode_id": "ign:binance_spot:LONG:3100",
            "side": "LONG", "proposer": "binance_spot",
            "started_receive_ms": 3_100, "last_evidence_ms": 3_600,
            "bias_snapshot": {"direction": "LONG", "confidence": 0.8},
            "signals": [cash1, cash2, follow, reverse1, aligned, reverse2],
            "epochs": {"binance_spot": 1, "futures": 1},
        }
        report = ignition_core._result_from_episode(
            s, episode,
            {"binance_spot": (cash1, cash2), "coinbase_spot": (),
             "futures": (follow, reverse1, aligned, reverse2)},
            {"coinbase_mode": "FRESH", "binance_spot_ready": True,
             "futures_ready": True},
            3.7,
        )
        self.assertEqual(report["decision"], "GO")
        self.assertTrue(report["ignition"]["futures_follow_ok"])
        self.assertFalse(report["ignition"]["futures_follow_invalidated"])

    def test_bbo_without_executed_flow_cannot_start_episode(self):
        s = state()
        ignition_signals.observe_bbo(
            s, "binance_spot", bid=99.0, ask=101.0,
            bid_qty=100.0, ask_qty=0.1, receive_time_ms=3_000,
        )
        result = ignition_core.evaluate(s, now=3.1)
        self.assertEqual(result["decision"], "WAIT")
        self.assertIsNone(result.get("causal_episode_id"))

    def test_epoch_reset_does_not_bridge_an_episode(self):
        s = state()
        warm(s, "binance_spot")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "binance_spot", 3_000)
        first = ignition_core.evaluate(s, now=3.101)
        self.assertIsNotNone(first.get("causal_episode_id"))
        ignition_signals.reset_venue(s, "binance_spot", 2)
        reset = ignition_core.evaluate(s, now=3.102)
        self.assertEqual(reset["decision"], "WAIT")
        self.assertEqual(reset["reason"], "EXECUTED_FLOW_EPOCH_RESET")

    def test_late_exchange_event_cannot_rewrite_prior_receive_time_decision(self):
        s = state()
        # Keep the frozen Bias older than the first impulse while all feed ages
        # remain valid at the later receive time.
        s.bias_updated_at = 1.8
        warm(s, "binance_spot")
        warm(s, "coinbase_spot")
        warm(s, "futures")
        ignition_core.evaluate(s, now=2.0)
        bucket(s, "binance_spot", 3_000)
        ignition_core.evaluate(s, now=3.101)
        bucket(s, "binance_spot", 3_100, base=100.003)
        waiting = ignition_core.evaluate(s, now=3.201)
        self.assertEqual(waiting["reason"], "WAIT_CASH_IGNITION_FUTURES_RESPONSE")
        bucket(s, "futures", 3_300, qty=0.20, base=100.003)
        proved = ignition_core.evaluate(s, now=3.401)
        self.assertEqual(proved["decision"], "GO")
        episode_id = proved["causal_episode_id"]
        ignition_core.capture_episode(
            s, episode_id, side="LONG",
            last_evidence_ms=proved["ignition"]["last_evidence_ms"],
        )

        for name in (
            "thoi_gian_tick_cuoi", "thoi_gian_dong_tien_cuoi",
            "thoi_gian_coinbase_ticker_cuoi", "coinbase_flow_3s_ts",
            "execution_price_time", "thoi_gian_dong_tien_futures_cuoi",
        ):
            setattr(s, name, 4.0)

        # Coinbase event-time predates the entry, but it arrives afterwards.
        # It may affect only a future decision, never the already emitted one.
        ignition_signals.observe_trade(
            s, "coinbase_spot", receive_time_ms=4_001,
            event_time_ms=3_050, price=100.0, qty=0.10,
            aggressive_buy=False,
        )
        ignition_signals.observe_trade(
            s, "coinbase_spot", receive_time_ms=4_051,
            event_time_ms=3_060, price=99.997, qty=0.10,
            aggressive_buy=False,
        )
        later = ignition_core.evaluate(s, now=4.101)
        self.assertEqual(proved["causal_episode_id"], episode_id)
        self.assertNotEqual(later["decision"], "GO")

    def test_shadow_bootstrap_collects_but_live_fails_closed(self):
        s = state()
        result = {
            "decision": "GO", "side": "LONG", "entry_mode": "IGNITION",
            "phase": "RELEASE", "s_votes": {},
            "ignition": {
                "state": "PROVE", "proof_type": "METAORDER_CONTINUATION",
                "cash_venues": ["binance_spot"], "proposer": "binance_spot",
                "futures_follow_ok": True,
                "consumed_fraction": 0.20, "residual_edge_proxy_bps": 1.0,
                "venue_moves_bps": {"binance_spot": 0.5, "futures": 0.2},
            },
        }
        allowed, report = entry_edge_tier.authorize(result, s)
        self.assertTrue(allowed)
        self.assertTrue(report["bootstrap_shadow_allowed"])
        self.assertFalse(report["cost_ok"])
        s.wstrade_live_armed = True
        allowed, report = entry_edge_tier.authorize(result, s)
        self.assertFalse(allowed)
        self.assertFalse(report["live_empirical_ok"])

    def test_live_uses_net_empirical_outcome_not_handoff_gap_proxy(self):
        s = state()
        s.wstrade_live_armed = True
        s.wstrade_promotion = {
            "shadow_trades": 30, "stress_25bps_pnl_usdt": 1.0,
        }
        ignition = {
            "state": "PROVE", "proof_type": "METAORDER_CONTINUATION",
            "cash_venues": ["binance_spot"], "proposer": "binance_spot",
            "futures_follow_ok": True, "consumed_fraction": 0.20,
            "residual_edge_proxy_bps": 0.0,
            "venue_moves_bps": {"binance_spot": 0.5, "futures": 0.4},
            "flow_by_venue": {"binance_spot": {"signed_imbalance": 0.6}},
        }
        result = {
            "decision": "GO", "side": "LONG", "entry_mode": "IGNITION",
            "phase": "RELEASE", "ignition": ignition,
            "s_votes": ignition_core._compat_votes(True, ignition),
        }
        empirical = {
            "samples": 30, "live_empirical_ok": True,
            "mean_net_bps": 4.0, "lower_confidence_bound_bps": 1.0,
            "status": "ACTIVE", "level": "EXACT",
        }
        costs = {
            "total_cost_bps": 10.0, "minimum_net_edge_bps": 2.0,
            "commission_verified": True, "commission_source": "TEST",
            "execution_style": "TAKER",
        }
        forward = {
            "status": "ACTIVE", "level": "EXACT", "samples": 30,
            "positive_net": True, "expected_guardian_net_bps": 4.0,
            "lower_confidence_bound_bps": 1.0,
        }
        with patch.object(entry_edge_tier.edge_calibration_v2, "factor", return_value=empirical), patch.object(
            entry_edge_tier.verified_cost_model, "estimate", return_value=costs
        ), patch.object(
            entry_edge_tier.entry_economics_v2, "estimate", return_value=forward
        ):
            allowed, report = entry_edge_tier.authorize(result, s)
        self.assertTrue(report["cost_ok"])
        self.assertTrue(report["live_empirical_ok"])
        self.assertTrue(allowed)

        pooled = dict(empirical, level="PROOF_EXEC")
        with patch.object(entry_edge_tier.edge_calibration_v2, "factor", return_value=pooled), patch.object(
            entry_edge_tier.verified_cost_model, "estimate", return_value=costs
        ), patch.object(
            entry_edge_tier.entry_economics_v2, "estimate", return_value=forward
        ):
            allowed, report = entry_edge_tier.authorize(result, s)
        self.assertFalse(report["live_empirical_ok"])
        self.assertFalse(allowed)

    def test_edge_contract_rejects_cash_go_without_futures_follower(self):
        result = {
            "decision": "GO", "side": "LONG", "entry_mode": "IGNITION",
            "ignition": {
                "state": "PROVE", "proof_type": "METAORDER_CONTINUATION",
                "cash_venues": ["binance_spot"], "proposer": "binance_spot",
                "futures_follow_ok": False, "consumed_fraction": 0.20,
            },
        }
        self.assertFalse(entry_edge_tier.normal_contract_ok(result))

    def test_compat_votes_expose_cash_aliases_to_absorption_gate(self):
        ignition = {
            "cash_venues": ["binance_spot", "coinbase_spot"],
            "supporting_venues": ["binance_spot", "coinbase_spot"],
            "venue_moves_bps": {
                "binance_spot": 0.05, "coinbase_spot": 0.04,
                "futures": 0.06,
            },
            "flow_by_venue": {
                "binance_spot": {"signed_imbalance": 0.60},
                "coinbase_spot": {"signed_imbalance": 0.55},
            },
        }
        result = {
            "decision": "GO", "price_threshold_bps": 0.15,
            "s_votes": ignition_core._compat_votes(True, ignition),
        }
        from loi_he_thong import entry_microstructure
        impact = entry_microstructure.price_impact(result)
        self.assertTrue(impact["absorbed"])
        self.assertIn("spot", result["s_votes"]["S1_cross_venue_price_acceptance"]["metrics"]["moves"])

    def test_perp_lead_veto_applies_after_cash_proposer(self):
        s = state()
        result = {
            "decision": "GO", "side": "LONG", "entry_mode": "IGNITION",
            "phase": "RELEASE", "price_threshold_bps": 0.15,
            "ignition": {
                "state": "PROVE", "proof_type": "METAORDER_CONTINUATION",
                "cash_venues": ["binance_spot"], "proposer": "binance_spot",
                "futures_follow_ok": True, "consumed_fraction": 0.20,
                "venue_moves_bps": {"binance_spot": 0.5, "futures": 4.0},
            },
            "s_votes": ignition_core._compat_votes(True, {
                "cash_venues": ["binance_spot"],
                "supporting_venues": ["binance_spot", "futures"],
                "venue_moves_bps": {"binance_spot": 0.5, "futures": 4.0},
            }),
        }
        report = entry_edge_tier.classify(result, s)
        self.assertIn("PERP_LED_VETO", report["hard_vetoes"])

    def test_degraded_coinbase_allows_only_binance_cash_authority(self):
        def run(proposer):
            s = state(now=3.4)
            cash1 = evidence_row(3_100, "LONG", 100.003)
            cash2 = evidence_row(3_200, "LONG", 100.006)
            for row in (cash1, cash2):
                row["venue"] = proposer
            follower = evidence_row(3_300, "LONG", 100.004, strong=False)
            follower.update({"venue": "futures", "total_qty": 0.20})
            episode = {
                "causal_episode_id": "ign:%s:LONG:3000" % proposer,
                "side": "LONG", "proposer": proposer,
                "started_receive_ms": 3_100, "last_evidence_ms": 3_300,
                "bias_snapshot": {"direction": "LONG", "confidence": 0.8},
                "signals": [cash1, cash2, follower],
                "epochs": {proposer: 1, "futures": 1},
            }
            histories = {
                "binance_spot": tuple((cash1, cash2)) if proposer == "binance_spot" else (),
                "coinbase_spot": tuple((cash1, cash2)) if proposer == "coinbase_spot" else (),
                "futures": (follower,),
            }
            freshness = {
                "coinbase_mode": "DEGRADED", "binance_spot_ready": True,
                "futures_ready": True,
            }
            return ignition_core._result_from_episode(s, episode, histories, freshness, 3.4)

        allowed = run("binance_spot")
        self.assertEqual(allowed["decision"], "GO")
        rejected = run("coinbase_spot")
        self.assertEqual(rejected["decision"], "WAIT")
        self.assertEqual(
            rejected["reason"], "WAIT_DEGRADED_COINBASE_REQUIRES_BINANCE_CASH",
        )

    def test_cash_discovery_is_side_independent_and_non_authoritative(self):
        def episode(side):
            first = evidence_row(3_100, side, 100.003)
            second = evidence_row(3_350, side, 100.006)
            first["venue"] = "coinbase_spot"
            second["venue"] = "binance_spot"
            return {"signals": [first, second]}

        long = ignition_core._cash_discovery(episode("LONG"))
        short = ignition_core._cash_discovery(episode("SHORT"))
        self.assertEqual(long["leader"], "coinbase_spot")
        self.assertEqual(long["leader"], short["leader"])
        self.assertEqual(long["status"], "COINBASE_SPOT_LED_CANDIDATE")
        self.assertFalse(long["authority"])
        self.assertEqual(long["confirmation"], "TIMING_ONLY_NO_1_3S_ACCEPTANCE")


if __name__ == "__main__":
    unittest.main()
