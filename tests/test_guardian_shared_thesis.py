import copy
import hashlib
import importlib
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from loi_he_thong import authority_contracts, market_thesis


guardian = importlib.import_module("3_thuc_thi.ve_si_lenh.guardian_s_tier")


def _control_handoff(side):
    segments = [{
        "state": "CONVERTING", "side": side,
        "price": {"vote": side}, "flow": {"vote": side},
    } for _ in range(2)]
    sealed = {
        "version": "CASH_CONTROL_ACQUISITION_HANDOFF_V1",
        "side": side,
        "first_converting_segment_onset_ms": 9_000,
        "ownership_completed_ms": 9_500,
        "venue_epochs": {"spot": 2, "coinbase": 3},
        "directional_cash_roots": [
            "BINANCE_SPOT_CASH", "COINBASE_USD_CASH",
        ],
        "temporal_persistence_segments": 2,
        "segment_evidence": segments,
        "bias_version": "BIAS_TEST_V1",
    }
    digest = hashlib.sha256(json.dumps(
        sealed, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    return {
        **sealed,
        "causal_wave_id": "cash-acquisition:" + digest[:20],
        "handoff_hash": digest,
        "sealed_payload": sealed,
        "status": "SEALED", "sealed": True,
        "authority": False, "entry_authority": False,
    }


def _entry_result():
    return {
        "decision": "GO",
        "reason": "IGNITION_PROVED",
        "side": "LONG",
        "causal_episode_id": "episode-shared-1",
        "authority_basis": "BIAS_ALIGNED",
        "ignition": {
            "causal_episode_id": "episode-shared-1",
            "side": "LONG",
            "proof_type": "PERSISTENT_METAORDER",
            "proposer": "binance_spot",
            "cash_venues": ["binance_spot", "coinbase_spot"],
            "current_cash_conversion": {
                "confirmed": True,
                "accepted_cash_venues": [
                    "binance_spot", "coinbase_spot",
                ],
            },
            "oi_verification_state": {"status": "UNCHANGED_UNKNOWN"},
            "clock_quality": {
                "binance_spot": {"source_health": "FRESH", "epoch": 2},
                "coinbase_spot": {"source_health": "FRESH", "epoch": 3},
            },
        },
    }


def _observation(*, spot=-2.0, coinbase=-2.0, futures=-2.0,
                 spot_flow=-0.4, coinbase_flow=-0.4,
                 futures_flow=-0.4, oi="NEUTRAL", source="FRESH",
                 cash_wave_side=None, wave_age_ms=0,
                 control_handoff_side=None):
    moves = {"spot": spot, "coinbase": coinbase, "futures": futures}
    result = {
        "version": "GUARDIAN_CANONICAL_OBSERVATION_V1",
        "causal_episode_id": "episode-shared-1",
        "position_side": "LONG",
        "source_health": {
            "spot": source,
            "coinbase": source,
            "futures": source,
        },
        "price_horizons": {
            "1.0": {"moves": moves},
            "3.0": {"moves": moves},
        },
        "flow_signed_imbalances": {
            "spot": spot_flow,
            "coinbase": coinbase_flow,
            "futures": futures_flow,
        },
        "oi": {"status": oi, "fresh": True},
        "gap_or_epoch_invalid": False,
        "observed_at_ms": 10_000,
    }
    if cash_wave_side:
        result["cash_control_wave"] = {
            "version": "CROSS_CASH_CAUSAL_WAVE_V1",
            "causal_wave_id": "cash-wave-opposing-1",
            "side": cash_wave_side,
            "state": "CONTROL_PERSISTING",
            "observed_at_ms": 10_000 - wave_age_ms,
            "cash_roots": {
                name: {
                    "side": cash_wave_side,
                    "state": "FLOW_LED_CONVERSION",
                    "conversion_held": True,
                    "root_evidence_id": "root-" + name,
                    "epoch": index,
                }
                for index, name in enumerate(
                    ("binance_spot", "coinbase_spot"), start=1,
                )
            },
        }
    if control_handoff_side:
        result["control_ownership"] = {
            "current_bias_side": control_handoff_side,
            "acquisition_handoff": _control_handoff(control_handoff_side),
        }
    return result


class SharedThesisObservationTests(unittest.TestCase):
    def setUp(self):
        self.truth = market_thesis.build(_entry_result())

    def test_dual_cash_snapshot_is_divergence_without_wave_identity(self):
        result = market_thesis.observe(self.truth, _observation())
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(result["old_thesis_falsified"])
        self.assertTrue(
            result["evidence"]["snapshot_persistent_dual_adverse"]
        )

    def test_opposing_micro_wave_without_control_owner_is_divergence(self):
        result = market_thesis.observe(
            self.truth, _observation(cash_wave_side="SHORT"),
        )
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(result["old_thesis_falsified"])

    def test_owned_distinct_opposing_cash_wave_is_control_transfer(self):
        result = market_thesis.observe(
            self.truth, _observation(
                cash_wave_side="SHORT", control_handoff_side="SHORT",
            ),
        )
        self.assertEqual(result["status"], "CONTROL_TRANSFER")
        self.assertTrue(result["old_thesis_falsified"])
        self.assertIn("OPPOSITE_DUAL_CASH_CONTROL", result["observed_falsifiers"])

    def test_stale_opposing_wave_cannot_transfer_control(self):
        result = market_thesis.observe(
            self.truth,
            _observation(
                cash_wave_side="SHORT", wave_age_ms=5_001,
                control_handoff_side="SHORT",
            ),
        )
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(result["old_thesis_falsified"])

    def test_unsealed_or_mutated_control_owner_cannot_transfer(self):
        row = _observation(
            cash_wave_side="SHORT", control_handoff_side="SHORT",
        )
        row["control_ownership"]["acquisition_handoff"][
            "handoff_hash"
        ] = "forged"
        result = market_thesis.observe(self.truth, row)
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(
            result["evidence"]["control_ownership_handoff_valid"],
        )

    def test_single_cash_pullback_is_divergence_not_falsification(self):
        result = market_thesis.observe(self.truth, _observation(
            coinbase=0.2, coinbase_flow=0.2, futures=0.2,
            futures_flow=0.2,
        ))
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(result["old_thesis_falsified"])
        self.assertEqual(result["observed_falsifiers"], [])

    def test_support_requires_current_price_and_flow_conversion(self):
        result = market_thesis.observe(self.truth, _observation(
            spot=2.0, coinbase=2.0, futures=1.0,
            spot_flow=0.4, coinbase_flow=0.4, futures_flow=0.1,
        ))
        self.assertEqual(result["status"], "SUPPORT")
        self.assertFalse(result["old_thesis_falsified"])

    def test_missing_source_or_gap_is_unknown_not_falsified(self):
        stale = market_thesis.observe(
            self.truth, _observation(source="STALE"),
        )
        self.assertEqual(stale["status"], "UNKNOWN")
        self.assertFalse(stale["old_thesis_falsified"])
        row = _observation()
        row["gap_or_epoch_invalid"] = True
        gap = market_thesis.observe(self.truth, row)
        self.assertEqual(gap["status"], "UNKNOWN")
        self.assertFalse(gap["old_thesis_falsified"])

    def test_pnl_and_capital_fields_cannot_change_market_truth(self):
        observation = _observation()
        baseline = market_thesis.observe(self.truth, observation)
        polluted = copy.deepcopy(observation)
        polluted.update({
            "pnl_bps": -500.0, "best_r": 20.0, "runner_active": True,
            "capital_preference": "EXIT",
        })
        other = market_thesis.observe(self.truth, polluted)
        for name in (
            "status", "reason", "observed_falsifiers",
            "old_thesis_falsified", "observation_hash",
        ):
            self.assertEqual(baseline[name], other[name])
        self.assertFalse(other["pnl_fields_used_for_thesis"])
        self.assertFalse(other["capital_fields_used_for_thesis"])

    def test_same_truth_and_events_are_deterministic(self):
        first = market_thesis.observe(self.truth, _observation())
        second = market_thesis.observe(self.truth, _observation())
        self.assertEqual(first, second)

    def test_mutated_or_missing_entry_truth_fails_unknown(self):
        damaged = copy.deepcopy(self.truth)
        damaged["mechanism"] = "REWRITTEN"
        result = market_thesis.observe(damaged, _observation())
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "ENTRY_MARKET_THESIS_INVALID")

    def test_shadow_mapping_never_claims_authority_or_ensemble(self):
        for status, decision in (
            ("SUPPORT", "HOLD"),
            ("DIVERGENCE", "DETERIORATING"),
            ("CONTROL_TRANSFER", "EXIT"),
            ("FALSIFY", "EXIT"),
            ("UNKNOWN", "HOLD"),
        ):
            result = guardian._shared_thesis_shadow_action({"status": status})
            self.assertEqual(result["decision"], decision)
            self.assertFalse(result["authority"])
            self.assertFalse(result["weighted_ensemble"])
            self.assertTrue(result["safety_bypass_separate"])

    def test_canonical_mapping_is_the_only_guardian_exit_authority(self):
        terminal = guardian._canonical_thesis_action({
            "status": "CONTROL_TRANSFER", "old_thesis_falsified": True,
        })
        divergence = guardian._canonical_thesis_action({
            "status": "DIVERGENCE", "old_thesis_falsified": False,
        })
        self.assertEqual(terminal["decision"], "EXIT")
        self.assertTrue(terminal["authority"])
        self.assertTrue(terminal["canonical_market_truth_exit_authorized"])
        self.assertEqual(divergence["decision"], "DETERIORATING")
        self.assertFalse(divergence["canonical_market_truth_exit_authorized"])

    def test_guardian_runtime_exits_only_on_canonical_terminal_truth(self):
        state = SimpleNamespace(
            best_bid=99.99, best_ask=100.01, coinbase_price=100.0,
            thoi_gian_coinbase_ticker_cuoi=100.0,
            open_interest=0.0, danh_sach_khop_lenh_futures=[],
        )
        position = SimpleNamespace(
            position_cycle_id="position-canonical", side="LONG",
            opened_at=99.0, causal_episode_id="episode-shared-1",
            best_r=0.0, floor_r=None,
        )
        neutral = guardian._vote("NEUTRAL", 0.0, "TEST_NEUTRAL")
        terminal_event = _observation(
            cash_wave_side="SHORT", control_handoff_side="SHORT",
        )
        with patch.object(
            guardian, "_s1", return_value=neutral,
        ), patch.object(
            guardian, "_s2", return_value=neutral,
        ), patch.object(
            guardian, "_s3", return_value=neutral,
        ), patch.object(
            guardian, "_canonical_thesis_observation",
            return_value=(self.truth, terminal_event),
        ):
            result = guardian.assess(state, position, now=100.0)

        self.assertEqual(result["decision"], "EXIT")
        self.assertEqual(result["reason"], "CANONICAL_MARKET_CONTROL_TRANSFER")
        self.assertTrue(
            result["canonical_thesis_action"][
                "canonical_market_truth_exit_authorized"
            ]
        )

    def test_adverse_wave_ledger_requires_distinct_causal_evidence(self):
        position = SimpleNamespace(
            side="LONG", causal_episode_id="episode-shared-1",
        )
        challenged = guardian._advance_adverse_wave_ledger(
            position, 100.0,
            {"status": "DIVERGENCE", "reason": "ADVERSE_INCOMPLETE",
             "observation_hash": "h1"},
            {"guardian_phase": "FIRST_PULLBACK"},
        )
        self.assertEqual(challenged["state"], "CHALLENGED")

        unknown = guardian._advance_adverse_wave_ledger(
            position, 100.1,
            {"status": "UNKNOWN", "reason": "NO_MATERIAL_CURRENT_THESIS_EVIDENCE",
             "observation_hash": "h2"},
            {"guardian_phase": "HEALTHY"},
        )
        self.assertEqual(unknown["state"], "CHALLENGED")

        candidate = guardian._advance_adverse_wave_ledger(
            position, 100.2,
            {"status": "CONTROL_TRANSFER", "reason": "OPPOSITE_CONTROL",
             "observation_hash": "h3"},
            {"guardian_phase": "BREAK_PENDING"},
        )
        repeated = guardian._advance_adverse_wave_ledger(
            position, 100.3,
            {"status": "CONTROL_TRANSFER", "reason": "OPPOSITE_CONTROL",
             "observation_hash": "h3"},
            {"guardian_phase": "BREAK_PENDING"},
        )
        confirmed = guardian._advance_adverse_wave_ledger(
            position, 100.4,
            {"status": "CONTROL_TRANSFER", "reason": "OPPOSITE_CONTROL",
             "observation_hash": "h4"},
            {"guardian_phase": "BREAK_PENDING"},
        )
        self.assertEqual(candidate["state"], "TRANSFER_CANDIDATE")
        self.assertEqual(repeated["state"], "TRANSFER_CANDIDATE")
        self.assertEqual(confirmed["state"], "TRANSFER_CONFIRMED")
        self.assertFalse(confirmed["authority"])
        self.assertFalse(confirmed["time_alone_transitions"])

    def test_adverse_wave_ledger_source_break_is_explicit(self):
        position = SimpleNamespace(
            side="LONG", causal_episode_id="episode-shared-1",
        )
        row = guardian._advance_adverse_wave_ledger(
            position, 100.0,
            {"status": "UNKNOWN", "reason": "THESIS_OBSERVATION_DISCONTINUITY",
             "observation_hash": "gap"},
            {"guardian_phase": "FIRST_PULLBACK"},
        )
        self.assertEqual(row["state"], "INVALIDATED_SOURCE_BREAK")
        self.assertFalse(row["unknown_falsifies"])

    def test_guardian_adapter_reads_exact_frozen_handoff(self):
        action = authority_contracts.seal(
            "ACTION", "ENTRY_ACTION_POLICY", "episode-shared-1",
            {"action": "ACT_TAKER_NOW"},
        )
        bundle = authority_contracts.bundle(
            self.truth,
            action,
            authority_contracts.seal(
                "EXECUTION", "EXECUTION_REVALIDATION", "episode-shared-1",
                {"execution_action": "EXECUTE"},
            ),
            authority_contracts.seal(
                "SAFETY", "MAINNET_SAFETY", "episode-shared-1",
                {"safety_state": "SAFE"},
            ),
        )
        handoff = authority_contracts.freeze_entry_handoff(bundle)
        position = SimpleNamespace(
            side="LONG", causal_episode_id="episode-shared-1",
            entry_causal_thesis={
                "causal_episode_id": "episode-shared-1",
                "entry_thesis_handoff": handoff,
            },
        )
        state = SimpleNamespace(
            thoi_gian_tick_cuoi=100.0, thoi_gian_dong_tien_cuoi=100.0,
            thoi_gian_coinbase_ticker_cuoi=100.0,
            coinbase_flow_3s_ts=100.0, thoi_gian_vi_mo_cuoi=100.0,
            guardian_s_spot_flow_ordering="MONOTONIC",
            guardian_s_futures_flow_ordering="MONOTONIC",
            spot_flow_epoch=2, coinbase_flow_epoch=3,
            danh_sach_khop_lenh_futures=[{
                "thoi_gian_ms": 100_000, "gia": 100.0,
            }],
        )
        s1 = {"metrics": {"horizons": {"3.0": {
            "threshold_bps": 1.5,
            "moves": {"spot": 2.0, "coinbase": 2.0, "futures": 1.0},
        }}}}
        s2 = {"metrics": {"signed_imbalances": {
            "spot": 0.4, "coinbase": 0.4, "futures": 0.1,
        }}}
        s3 = {"status": "NEUTRAL", "metrics": {"oi_pct": 0.0}}

        truth, event = guardian._canonical_thesis_observation(
            state, position, 100.0, s1, s2, s3,
        )
        observed = market_thesis.observe(truth, event)

        self.assertEqual(truth["contract_hash"], self.truth["contract_hash"])
        self.assertEqual(event["causal_episode_id"], "episode-shared-1")
        self.assertEqual(observed["status"], "SUPPORT")

        state.cross_cash_causal_wave_shadow = {
            "version": "CROSS_CASH_CAUSAL_WAVE_V1",
            "observed_at_ms": 100_000,
            "active_wave": {
                "causal_wave_id": "cash-wave-short",
                "side": "SHORT", "state": "CONTROL_PERSISTING",
                "cash_roots": {
                    name: {
                        "side": "SHORT", "state": "FLOW_LED_CONVERSION",
                        "conversion_held": True,
                        "root_evidence_id": "root-" + name,
                    }
                    for name in ("binance_spot", "coinbase_spot")
                },
            },
        }
        state.bias_state = "SHORT"
        state.bias_acquisition_handoff = _control_handoff("SHORT")
        _, transfer_event = guardian._canonical_thesis_observation(
            state, position, 100.0, s1, s2, s3,
        )
        transferred = market_thesis.observe(self.truth, transfer_event)
        self.assertEqual(transferred["status"], "CONTROL_TRANSFER")


if __name__ == "__main__":
    unittest.main()
