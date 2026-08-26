from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loi_he_thong import edge_calibration_v2
from loi_he_thong import entry_council_shadow as entry
from loi_he_thong import entry_edge_tier
from loi_he_thong import microstructure_regime

import importlib.util
from pathlib import Path


def _load_guardian():
    path = (
        Path(__file__).resolve().parents[1]
        / "3_thuc_thi"
        / "ve_si_lenh"
        / "guardian_s_tier.py"
    )
    spec = importlib.util.spec_from_file_location("guardian_s_anti_manip_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guardian = _load_guardian()


def _entry_state(now=100.0, side="LONG"):
    sign = 1.0 if side == "LONG" else -1.0
    base = 100.0
    spot = base * (1.0 + sign * 1.0 / 10000.0)
    coinbase = base * (1.0 + sign * 0.8 / 10000.0)
    futures = base * (1.0 + sign * 0.9 / 10000.0)
    state = SimpleNamespace(
        bias_state=side,
        bias_confidence=0.80,
        bias_updated_at=now,
        best_bid=spot - 0.001,
        best_ask=spot + 0.001,
        thoi_gian_tick_cuoi=now,
        coinbase_price=coinbase,
        thoi_gian_coinbase_ticker_cuoi=now,
        atr_1m=0.07,
        thoi_gian_dong_tien_cuoi=now,
        open_interest_updated_at=now,
        current_cvd_buy_3s=0.08 if side == "LONG" else 0.02,
        current_cvd_sell_3s=0.02 if side == "LONG" else 0.08,
        coinbase_flow_3s_ts=now,
        coinbase_volume_3s=0.02,
        coinbase_cvd_3s=sign * 0.012,
        bias_council={
            "s_votes": {
                "S2_price_x_oi": {
                    "metrics": {
                        "regime": "NEW_LONG_BUILD" if side == "LONG" else "NEW_SHORT_BUILD",
                        "oi_pct": 0.03,
                        "oi_5m_pct": 0.08,
                    }
                }
            }
        },
    )
    state.danh_sach_khop_lenh_futures = deque(
        [
            {
                "gia": futures,
                "thoi_gian_ms": now * 1000.0,
                "khoi_luong": 0.8,
                "ban_chu_dong": side == "SHORT",
            },
            {
                "gia": futures,
                "thoi_gian_ms": now * 1000.0,
                "khoi_luong": 0.2,
                "ban_chu_dong": side == "LONG",
            },
        ]
    )
    state.entry_shadow_price_history = deque(
        [{"ts": now - 1.6, "spot": base, "coinbase": base, "futures": base}],
        maxlen=256,
    )
    previous = {
        "ts": now - 3.1,
        "venues": {
            "spot": {"signed_imbalance": 0.6, "volume_btc": 0.1},
            "futures": {"signed_imbalance": 0.6, "volume_btc": 1.0},
        },
        "supporters": ["spot", "futures"],
        "opponents": [],
        "strong_supporters": ["spot", "futures"],
        "strong_opponents": [],
    }
    state.entry_flow_persistence_buckets = deque([previous], maxlen=4)
    state._entry_flow_persistence_side = side
    state._entry_acceptance_signature = (side, "CASH_DERIVATIVE_PERSISTENT")
    state._entry_acceptance_since = now - 2.0
    return state


class EntryAntiManipulationTests(unittest.TestCase):
    def test_two_non_overlapping_cash_anchored_buckets_can_enter(self):
        result = entry.evaluate(_entry_state(), now=100.0)
        self.assertEqual(result["decision"], "GO")
        self.assertTrue(result["causal"]["persistence"]["ok"])
        self.assertTrue(result["causal"]["evidence_groups"]["price_independent"])
        self.assertTrue(result["causal"]["evidence_groups"]["flow_independent"])

    def test_single_impulse_cannot_self_confirm_entry(self):
        state = _entry_state()
        state.entry_flow_persistence_buckets.clear()
        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "WAIT")
        self.assertFalse(result["causal"]["persistence"]["ok"])

    def test_futures_running_more_than_three_bps_ahead_is_chase(self):
        state = _entry_state()
        for trade in state.danh_sach_khop_lenh_futures:
            trade["gia"] = 100.06
        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["phase"], "WAIT_CHASE")
        self.assertEqual(result["causal"]["handoff"]["reason"], "PERP_AHEAD_OF_CASH")

    def test_chase_clearing_cannot_reuse_pre_chase_acceptance_timer(self):
        state = _entry_state()
        for trade in state.danh_sach_khop_lenh_futures:
            trade["gia"] = 100.06
        chased = entry.evaluate(state, now=100.0)
        self.assertEqual(chased["phase"], "WAIT_CHASE")

        for trade in state.danh_sach_khop_lenh_futures:
            trade["gia"] = 100.009
            trade["thoi_gian_ms"] = 100.2 * 1000.0
        cleared = entry.evaluate(state, now=100.2)
        self.assertEqual(cleared["decision"], "WAIT")
        self.assertEqual(cleared["reason"], "WAIT_ACCEPTANCE_PERSISTENCE")
        self.assertFalse(cleared["causal"]["post_chase_retest"]["ok"])
        self.assertLess(cleared["causal"]["acceptance"]["elapsed_seconds"], 0.25)

    def test_cash_arbitrage_without_derivative_price_is_not_independent(self):
        state = _entry_state()
        for trade in state.danh_sach_khop_lenh_futures:
            trade["gia"] = 100.0
        result = entry.evaluate(state, now=100.0)
        self.assertNotEqual(result["decision"], "GO")
        self.assertFalse(result["causal"]["evidence_groups"]["price_independent"])

    def test_closing_oi_context_cannot_authorize_any_entry_lane(self):
        state = _entry_state()
        state.bias_council["s_votes"]["S2_price_x_oi"]["metrics"]["regime"] = "SHORT_COVERING"
        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["entry_mode"], "NONE")
        self.assertEqual(result["reason"], "WAIT_OI_CLOSING_CONTEXT")
        self.assertTrue(result["causal"]["oi_intent"]["closing"])

    def test_neutral_oi_context_does_not_block_normal_entry(self):
        state = _entry_state()
        state.bias_council["s_votes"]["S2_price_x_oi"]["metrics"]["regime"] = "OI_NEUTRAL"
        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "GO")
        self.assertIn(result["entry_mode"], {"NORMAL", "FAST"})

    def test_stale_positive_oi_loses_uplift_but_does_not_block_core(self):
        state = _entry_state()
        state.open_interest_updated_at = 90.0
        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "GO")
        self.assertFalse(result["causal"]["oi_intent"]["edge_fresh"])
        self.assertFalse(result["causal"]["oi_intent"]["new_position_build"])
        self.assertEqual(
            result["causal"]["oi_intent"]["regime"],
            "UNKNOWN_STALE_FOR_ENTRY",
        )

    def test_stair_step_impulse_uses_episode_distance_not_only_rolling_window(self):
        state = _entry_state()
        state._entry_chase_episode_side = "LONG"
        state._entry_chase_episode_anchor = {
            "ts": 94.0, "spot": 100.0, "coinbase": 100.0, "futures": 100.0,
        }
        state._entry_chase_episode_started_at = 94.0
        state._entry_chase_episode_last_evidence_at = 99.0
        state.best_bid, state.best_ask = 100.049, 100.051
        state.coinbase_price = 100.049
        for trade in state.danh_sach_khop_lenh_futures:
            trade["gia"] = 100.049
        state.entry_shadow_price_history = deque(
            [{"ts": 98.4, "spot": 100.04, "coinbase": 100.04, "futures": 100.04}],
            maxlen=256,
        )

        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(result["phase"], "WAIT_CHASE")
        self.assertEqual(
            result["causal"]["handoff"]["reason"],
            "TOTAL_EPISODE_IMPULSE_TOO_EXTENDED",
        )
        self.assertGreater(
            max(result["causal"]["handoff"]["episode_moves"].values()),
            result["causal"]["handoff"]["chase_budget_bps"],
        )
        self.assertEqual(
            result["reason"],
            "WAIT_CHASE_TOTAL_EPISODE_IMPULSE_TOO_EXTENDED",
        )

    def test_single_venue_extension_cannot_deny_entry(self):
        state = _entry_state()
        current = {"spot": 100.05, "coinbase": 100.01, "futures": 100.01}
        reference = {"spot": 100.0, "coinbase": 100.0, "futures": 100.0}
        result = entry._handoff(
            state, current, reference, "LONG", 0.5,
            episode_ref=reference, now=100.0,
        )
        self.assertNotEqual(result["status"], "WAIT_CHASE")
        self.assertFalse(result["extension_quorums"]["episode"]["ok"])
        self.assertEqual(
            result["extension_quorums"]["episode"]["extended_venues"],
            ["spot"],
        )

    def test_two_venue_extension_with_cash_anchor_blocks_chase(self):
        state = _entry_state()
        current = {"spot": 100.05, "coinbase": 100.01, "futures": 100.05}
        reference = {"spot": 100.0, "coinbase": 100.0, "futures": 100.0}
        result = entry._handoff(
            state, current, reference, "LONG", 0.5,
            episode_ref=reference, now=100.0,
        )
        self.assertEqual(result["status"], "WAIT_CHASE")
        self.assertEqual(
            result["extension_quorum"]["extended_venues"],
            ["futures", "spot"],
        )

    def test_recorded_short_squeeze_shape_rejects_short(self):
        state = _entry_state(side="SHORT")
        # Counterfactual shape of the bad trade: cash and Futures rise with
        # aggressive buy flow while OI falls.  A fleeting SHORT bias cannot
        # turn that single covering impulse into a SHORT entry.
        state.best_bid, state.best_ask = 100.019, 100.021
        state.coinbase_price = 100.018
        state.current_cvd_buy_3s, state.current_cvd_sell_3s = 0.09, 0.01
        state.coinbase_cvd_3s = 0.016
        state.bias_council["s_votes"]["S2_price_x_oi"]["metrics"]["regime"] = "SHORT_COVERING"
        for trade in state.danh_sach_khop_lenh_futures:
            trade["gia"] = 100.02
            trade["ban_chu_dong"] = False
        result = entry.evaluate(state, now=100.0)
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["reason"], "PRICE_AND_FLOW_OPPOSE")


class EmpiricalEdgeTests(unittest.TestCase):
    def test_stale_oi_vote_cannot_upgrade_normal_edge_to_runner(self):
        result = {
            "decision": "GO", "entry_mode": "FAST", "side": "LONG",
            "phase": "RELEASE",
            "causal": {"oi_intent": {
                "edge_fresh": False, "new_position_build": False,
                "closing": False,
            }},
            "s_votes": {
                "S1_cross_venue_price_acceptance": {
                    "status": "PASS", "metrics": {
                        "strong_supporters": ["spot", "coinbase", "futures"],
                    },
                },
                "S2_multi_venue_executed_flow": {
                    "status": "PASS", "metrics": {
                        "supporters": ["spot", "futures"],
                        "strong_supporters": ["spot"],
                        "strong_opponents": [],
                        "volume_floor_btc": 0.01,
                        "volume_floor_btc_by_venue": {
                            "spot": 0.01, "futures": 0.1,
                        },
                        "venues": {
                            "spot": {"volume_btc": 1.0},
                            "futures": {"volume_btc": 2.0},
                        },
                    },
                },
            },
        }
        state = SimpleNamespace(
            bias_state="LONG",
            bias_council={"s_votes": {
                "S1_cross_price": {"vote": "LONG"},
                "S2_price_x_oi": {"vote": "LONG"},
                "S3_multi_flow": {"vote": "LONG"},
            }},
            best_bid=99.999, best_ask=100.001,
            execution_best_bid=99.999, execution_best_ask=100.001,
            mainnet_commission_verified=True,
            mainnet_maker_fee_bps=2.0, mainnet_taker_fee_bps=5.0,
        )
        with patch.object(entry_edge_tier.micro, "price_impact", return_value={
            "absorbed": False,
        }), patch.object(entry_edge_tier.micro, "spot_perp_basis", return_value={
            "perp_expansion": False,
        }), patch.object(entry_edge_tier.regime_engine, "classify", return_value={
            "regime": "NORMAL", "price_factor": 1.0,
            "cost_factor": 1.0, "expectancy_factor": 1.0,
        }):
            report = entry_edge_tier.classify(result, state)
        self.assertEqual(report["edge_class"], "HARD_VETO")
        self.assertIn("IGNITION_CONTRACT_FAIL", report["hard_vetoes"])
        self.assertFalse(report["cost_ok"])

    def test_expansion_alone_does_not_improve_expectancy_or_discount_cost(self):
        state = SimpleNamespace(
            best_bid=99.999, best_ask=100.001, open_interest=1000.0,
            current_vol_3s=10.0, vol_pct90=1.0, atr_1m=0.05,
            bias_state="LONG", mainnet_commission_verified=True,
            mainnet_maker_fee_bps=2.0, mainnet_taker_fee_bps=5.0,
            execution_best_bid=99.999, execution_best_ask=100.001,
        )
        state._micro_regime_hist = deque(
            [(92.0, 99.95, 1000.0, 1.0)], maxlen=64
        )
        with patch.object(microstructure_regime.time, "time", return_value=100.0):
            regime = microstructure_regime.classify(state, "LONG")
        self.assertEqual(regime["regime"], "EXPANSION")
        self.assertEqual(regime["price_factor"], 1.0)
        self.assertEqual(regime["cost_factor"], 1.0)
        self.assertEqual(regime["expectancy_factor"], 1.0)

        with patch.object(entry_edge_tier.regime_engine, "classify", return_value={
            **regime, "cost_factor": 0.80,
        }):
            report = entry_edge_tier.classify({"decision": "WAIT"}, state)
        self.assertEqual(
            report["cost_budget_bps_model"],
            report["cost_components"]["total_cost_bps"],
        )
        self.assertIsNone(report["expected_excursion_bps_base"])

    def test_live_requires_thirty_samples_and_non_negative_lower_bound(self):
        state = SimpleNamespace()
        for value in [4.0] * 30:
            edge_calibration_v2.record(
                state, "NORMAL", "TREND", value, "LONG", "HIGH_EDGE",
                execution_cost_bps=4.0,
            )
        report = edge_calibration_v2.factor(
            state, "NORMAL", "TREND", "LONG", "HIGH_EDGE",
            current_cost_bps=4.0, minimum_net_edge_bps=2.0,
        )
        self.assertTrue(report["live_empirical_ok"])
        self.assertGreaterEqual(report["lower_confidence_bound_bps"], 0.0)

    def test_live_reprices_exact_cohort_to_current_cost(self):
        state = SimpleNamespace()
        for _ in range(30):
            edge_calibration_v2.record(
                state, "IGNITION", "NORMAL", 4.0, "LONG",
                "RESIDUAL_POSITIVE", "FAILED_REVERSION", "BINANCE_SPOT",
                "MAKER", execution_cost_bps=4.0,
            )
        cheap = edge_calibration_v2.factor(
            state, "IGNITION", "NORMAL", "LONG", "RESIDUAL_POSITIVE",
            "FAILED_REVERSION", "BINANCE_SPOT", "MAKER",
            current_cost_bps=4.0, minimum_net_edge_bps=2.0,
        )
        expensive = edge_calibration_v2.factor(
            state, "IGNITION", "NORMAL", "LONG", "RESIDUAL_POSITIVE",
            "FAILED_REVERSION", "BINANCE_SPOT", "MAKER",
            current_cost_bps=10.0, minimum_net_edge_bps=2.0,
        )
        self.assertTrue(cheap["live_empirical_ok"])
        self.assertFalse(expensive["live_empirical_ok"])
        self.assertEqual(
            expensive["current_cost_adjustment"]["mean_net_bps"], -2.0
        )

    def test_shadow_bucket_with_too_few_samples_cannot_go_live(self):
        state = SimpleNamespace()
        for value in [4.0] * 29:
            edge_calibration_v2.record(state, "NORMAL", "TREND", value, "LONG", "HIGH_EDGE")
        report = edge_calibration_v2.factor(state, "NORMAL", "TREND", "LONG", "HIGH_EDGE")
        self.assertFalse(report["live_empirical_ok"])
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")

    def test_positive_mean_with_negative_lower_bound_cannot_go_live(self):
        state = SimpleNamespace()
        values = [-20.0] * 8 + [10.0] * 22
        for value in values:
            edge_calibration_v2.record(state, "NORMAL", "TREND", value, "LONG", "HIGH_EDGE")
        report = edge_calibration_v2.factor(state, "NORMAL", "TREND", "LONG", "HIGH_EDGE")
        self.assertGreater(report["mean_net_bps"], 0.0)
        self.assertLess(report["lower_confidence_bound_bps"], 0.0)
        self.assertFalse(report["live_empirical_ok"])

    def test_empirical_cohorts_do_not_mix_proof_or_execution(self):
        state = SimpleNamespace()
        for _ in range(64):
            edge_calibration_v2.record(
                state, "IGNITION", "TREND", 5.0, "LONG", "BOOTSTRAP_UNVERIFIED",
                "FAILED_REVERSION", "BINANCE_SPOT", "MAKER",
            )
        report = edge_calibration_v2.factor(
            state, "IGNITION", "TREND", "LONG", "BOOTSTRAP_UNVERIFIED",
            "METAORDER_CONTINUATION", "BINANCE_SPOT", "TAKER",
        )
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")
        self.assertFalse(report["live_empirical_ok"])

    def test_legacy_calibration_rows_cannot_authorize_known_cohort(self):
        state = SimpleNamespace(_edge_cal_v2_rows=[
            ("LONG", "IGNITION", "TREND", "BOOTSTRAP_UNVERIFIED", 5.0)
            for _ in range(96)
        ])
        report = edge_calibration_v2.factor(
            state, "IGNITION", "TREND", "LONG", "BOOTSTRAP_UNVERIFIED",
            "FAILED_REVERSION", "BINANCE_SPOT", "MAKER",
        )
        self.assertEqual(report["status"], "INSUFFICIENT_DATA")


class GuardianDeteriorationTests(unittest.TestCase):
    def _state(self, now, price, sell=True):
        return SimpleNamespace(
            best_bid=price - 0.001,
            best_ask=price + 0.001,
            coinbase_price=price,
            thoi_gian_coinbase_ticker_cuoi=now,
            atr_1m=0.01,
            open_interest=1000.0,
            flow_1s_buffer=deque(
                [{"ts": now, "buy": 0.1 if sell else 0.9, "sell": 0.9 if sell else 0.1}]
            ),
            danh_sach_khop_lenh_futures=deque(
                [{"gia": price, "thoi_gian_ms": now * 1000.0, "khoi_luong": 1.0, "ban_chu_dong": sell}]
            ),
            coinbase_flow_3s_ts=now,
            coinbase_volume_3s=1.0,
            coinbase_cvd_3s=-0.8 if sell else 0.8,
        )

    def test_price_plus_flow_must_persist_before_exit(self):
        pos = SimpleNamespace(position_cycle_id="p1", side="LONG", opened_at=99.0)
        state = self._state(100.0, 100.0, sell=False)
        first = guardian.assess(state, pos, now=100.0)
        self.assertEqual(first["decision"], "HOLD")

        for key, value in vars(self._state(101.1, 99.97, sell=True)).items():
            setattr(state, key, value)
        deteriorating = guardian.assess(state, pos, now=101.1)
        self.assertEqual(deteriorating["decision"], "DETERIORATING")

        for key, value in vars(self._state(102.35, 99.94, sell=True)).items():
            setattr(state, key, value)
        exited = guardian.assess(state, pos, now=102.35)
        self.assertEqual(exited["decision"], "EXIT")
        self.assertGreaterEqual(exited["deterioration_elapsed_seconds"], 1.0)

    def test_dynamic_threshold_never_below_one_point_five_bps(self):
        state = SimpleNamespace(atr_1m=0.001)
        self.assertGreaterEqual(guardian._threshold(state, 100.0), 1.5)

    def test_guardian_futures_flow_is_bounded_and_fails_neutral_on_disorder(self):
        state = SimpleNamespace(danh_sach_khop_lenh_futures=deque([
            {"thoi_gian_ms": 96_000, "khoi_luong": 1000, "ban_chu_dong": False},
            {"thoi_gian_ms": 99_000, "khoi_luong": 2, "ban_chu_dong": False},
            {"thoi_gian_ms": 99_100, "khoi_luong": 1, "ban_chu_dong": True},
        ]))
        imbalance, total = guardian._fut_flow(state, 100.0)
        self.assertAlmostEqual(total, 3.0)
        self.assertAlmostEqual(imbalance, 1.0 / 3.0)
        state.danh_sach_khop_lenh_futures = deque([
            {"thoi_gian_ms": 99_100, "khoi_luong": 1, "ban_chu_dong": False},
            {"thoi_gian_ms": 99_000, "khoi_luong": 1, "ban_chu_dong": True},
        ])
        self.assertEqual(guardian._fut_flow(state, 100.0), (0.0, 0.0))
        self.assertEqual(state.guardian_s_futures_flow_ordering, "DISORDERED_NEUTRAL")

    def test_fresh_coinbase_disagreement_blocks_binance_only_exit(self):
        pos = SimpleNamespace(position_cycle_id="p2", side="LONG", opened_at=99.0)
        state = self._state(100.0, 100.0, sell=False)
        guardian.assess(state, pos, now=100.0)

        for now, price in ((101.1, 99.97), (102.35, 99.94)):
            update = self._state(now, price, sell=True)
            for key, value in vars(update).items():
                setattr(state, key, value)
            state.coinbase_price = 100.0
            state.thoi_gian_coinbase_ticker_cuoi = now
            state.coinbase_cvd_3s = 0.8
            result = guardian.assess(state, pos, now=now)

        self.assertEqual(result["decision"], "DETERIORATING")
        self.assertEqual(result["reason"], "BINANCE_ONLY_ADVERSE_AWAITING_EXTERNAL_OR_OI")
        self.assertTrue(result["exchange_independence"]["blocks_binance_only_exit"])

    def test_stale_coinbase_does_not_disable_position_safety_exit(self):
        pos = SimpleNamespace(position_cycle_id="p3", side="LONG", opened_at=99.0)
        state = self._state(100.0, 100.0, sell=False)
        guardian.assess(state, pos, now=100.0)

        for now, price in ((101.1, 99.97), (102.35, 99.94)):
            update = self._state(now, price, sell=True)
            for key, value in vars(update).items():
                setattr(state, key, value)
            state.thoi_gian_coinbase_ticker_cuoi = 95.0
            result = guardian.assess(state, pos, now=now)

        self.assertEqual(result["decision"], "EXIT")
        self.assertFalse(result["exchange_independence"]["coinbase_strict_fresh"])
        self.assertFalse(result["exchange_independence"]["blocks_binance_only_exit"])

    @staticmethod
    def _causal_votes(price=-2.0, flow=-0.30, price_venues=None):
        price_venues = price_venues or ("spot", "coinbase", "futures")
        moves = {name: price for name in price_venues}
        s1 = guardian._vote(
            "ADVERSE", 0.80, "TEST_PRICE",
            horizons={"1.0": {
                "moves": moves,
                "adverse": list(price_venues),
                "supportive": [],
            }},
        )
        s2 = guardian._vote(
            "ADVERSE", 0.80, "TEST_FLOW",
            signed_imbalances={name: flow for name in price_venues},
            venues=list(price_venues),
        )
        s3 = guardian._vote("NEUTRAL", 0.10, "TEST_OI")
        return s1, s2, s3

    def _assess_with_votes(self, state, pos, now, votes):
        s1, s2, s3 = votes
        with patch.object(guardian, "_s1", return_value=s1), patch.object(
            guardian, "_s2", return_value=s2
        ), patch.object(guardian, "_s3", return_value=s3):
            return guardian.assess(state, pos, now=now)

    def test_runner_shield_waits_longer_but_still_exits(self):
        state = self._state(100.0, 100.0, sell=True)
        votes = self._causal_votes()
        normal = SimpleNamespace(
            position_cycle_id="normal", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
        )
        runner = SimpleNamespace(
            position_cycle_id="runner", side="LONG", opened_at=90.0,
            best_r=2.0, floor_r=0.75,
        )

        self._assess_with_votes(state, normal, 100.0, votes)
        normal_exit = self._assess_with_votes(state, normal, 100.80, votes)
        self.assertEqual(normal_exit["decision"], "EXIT")

        self._assess_with_votes(state, runner, 101.0, votes)
        shielded = self._assess_with_votes(state, runner, 102.20, votes)
        self.assertEqual(shielded["decision"], "DETERIORATING")
        self.assertTrue(shielded["runner_shield_active"])
        runner_exit = self._assess_with_votes(state, runner, 102.81, votes)
        self.assertEqual(runner_exit["decision"], "EXIT")
        self.assertEqual(runner_exit["exit_profile"], "RUNNER_SHIELD")

    def test_extreme_price_and_flow_bypass_runner_shield_quickly(self):
        state = self._state(100.0, 100.0, sell=True)
        pos = SimpleNamespace(
            position_cycle_id="fast-kill", side="LONG", opened_at=90.0,
            best_r=3.0, floor_r=1.0,
        )
        votes = self._causal_votes(price=-3.0, flow=-0.80)

        first = self._assess_with_votes(state, pos, 100.0, votes)
        self.assertEqual(first["decision"], "DETERIORATING")
        exited = self._assess_with_votes(state, pos, 100.26, votes)
        self.assertEqual(exited["decision"], "EXIT")
        self.assertTrue(exited["kill_fast"])
        self.assertFalse(exited["runner_shield_active"])

    def test_frozen_established_trend_delays_noise_but_still_exits(self):
        state = self._state(100.0, 100.0, sell=True)
        state.bias_council = {
            "bias": "LONG", "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
        }
        pos = SimpleNamespace(
            position_cycle_id="trend", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
            entry_causal_thesis={
                "primary_cash_anchor": "spot", "cash_anchors": ["spot"],
                "bias_thesis": {
                    "context_side": "LONG", "phase": "ESTABLISHED_TREND",
                },
            },
        )
        votes = self._causal_votes()
        self._assess_with_votes(state, pos, 100.0, votes)
        shielded = self._assess_with_votes(state, pos, 101.2, votes)
        self.assertEqual(shielded["decision"], "DETERIORATING")
        self.assertTrue(shielded["trend_shield_active"])
        still_holding = self._assess_with_votes(state, pos, 101.81, votes)
        self.assertEqual(still_holding["decision"], "DETERIORATING")
        # The slow whale-flow lane is deliberately not governed by a sub-2s
        # HFT echo. A continuous three-second causal reversal still exits.
        exited = self._assess_with_votes(state, pos, 103.01, votes)
        self.assertEqual(exited["decision"], "EXIT")
        self.assertEqual(exited["exit_profile"], "TREND_SHIELD")

    def test_transient_abstain_keeps_frozen_trend_shield(self):
        state = self._state(100.0, 100.0, sell=True)
        state.bias_council = {
            "bias": "ABSTAIN", "direction_memory": {
                "context_side": "ABSTAIN", "phase": "WARMUP_OR_NEUTRAL",
            },
        }
        pos = SimpleNamespace(
            position_cycle_id="trend-abstain", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
            entry_causal_thesis={
                "primary_cash_anchor": "spot", "cash_anchors": ["spot"],
                "bias_thesis": {
                    "context_side": "LONG", "phase": "ESTABLISHED_TREND",
                },
            },
        )
        votes = self._causal_votes(price=-3.0, flow=-0.80)
        first = self._assess_with_votes(state, pos, 100.0, votes)
        self.assertEqual(first["decision"], "DETERIORATING")
        early = self._assess_with_votes(state, pos, 100.26, votes)
        self.assertEqual(early["decision"], "DETERIORATING")
        self.assertTrue(early["trend_shield_active"])
        self.assertFalse(early["kill_fast"])

    def test_correlated_small_sweep_does_not_bypass_active_trend(self):
        state = self._state(100.0, 100.0, sell=True)
        state.bias_council = {
            "bias": "LONG", "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
        }
        pos = SimpleNamespace(
            position_cycle_id="trend-sweep", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
            entry_causal_thesis={
                "primary_cash_anchor": "spot", "cash_anchors": ["spot"],
                "bias_thesis": {
                    "context_side": "LONG", "phase": "ESTABLISHED_TREND",
                },
            },
        )
        votes = self._causal_votes(price=-3.0, flow=-0.80)
        self._assess_with_votes(state, pos, 100.0, votes)
        early = self._assess_with_votes(state, pos, 100.26, votes)
        self.assertEqual(early["decision"], "DETERIORATING")
        self.assertTrue(early["trend_shield_active"])
        self.assertFalse(early["kill_fast"])

    def test_material_cash_break_still_bypasses_trend_shield(self):
        state = self._state(100.0, 100.0, sell=True)
        state.bias_council = {
            "bias": "LONG", "direction_memory": {
                "context_side": "LONG", "phase": "ESTABLISHED_TREND",
            },
        }
        pos = SimpleNamespace(
            position_cycle_id="trend-break", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
            entry_causal_thesis={
                "primary_cash_anchor": "spot", "cash_anchors": ["spot"],
                "bias_thesis": {
                    "context_side": "LONG", "phase": "ESTABLISHED_TREND",
                },
            },
        )
        votes = self._causal_votes(price=-5.5, flow=-0.80)
        self._assess_with_votes(state, pos, 100.0, votes)
        exited = self._assess_with_votes(state, pos, 100.26, votes)
        self.assertEqual(exited["decision"], "EXIT")
        self.assertTrue(exited["kill_fast"])
        self.assertFalse(exited["trend_shield_active"])

    def test_reversal_candidate_disables_trend_shield(self):
        state = self._state(100.0, 100.0, sell=True)
        state.bias_council = {
            "bias": "ABSTAIN", "direction_memory": {
                "context_side": "LONG", "phase": "REVERSAL_CANDIDATE",
            },
        }
        pos = SimpleNamespace(
            position_cycle_id="reversal", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
            entry_causal_thesis={
                "primary_cash_anchor": "spot", "cash_anchors": ["spot"],
                "bias_thesis": {
                    "context_side": "LONG", "phase": "ESTABLISHED_TREND",
                },
            },
        )
        votes = self._causal_votes()
        self._assess_with_votes(state, pos, 100.0, votes)
        exited = self._assess_with_votes(state, pos, 100.8, votes)
        self.assertEqual(exited["decision"], "EXIT")
        self.assertFalse(exited["trend_shield_active"])

    def test_other_venue_noise_does_not_break_primary_cash_thesis(self):
        state = self._state(100.0, 100.0, sell=True)
        pos = SimpleNamespace(
            position_cycle_id="cash-thesis", side="LONG", opened_at=90.0,
            best_r=0.0, floor_r=None,
            entry_causal_thesis={
                "primary_cash_anchor": "spot",
                "cash_anchors": ["spot", "coinbase"],
            },
        )
        votes = self._causal_votes(
            price=-2.0, flow=-0.30,
            price_venues=("coinbase", "futures"),
        )

        result = self._assess_with_votes(state, pos, 100.0, votes)
        self.assertEqual(result["decision"], "DETERIORATING")
        self.assertEqual(result["reason"], "ENTRY_THESIS_NOT_BROKEN")
        self.assertFalse(result["entry_thesis"]["broken"])


if __name__ == "__main__":
    unittest.main()
