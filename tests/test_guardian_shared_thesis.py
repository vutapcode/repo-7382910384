import copy
import hashlib
import importlib
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from loi_he_thong import authority_contracts, market_thesis


guardian = importlib.import_module("3_thuc_thi.ve_si_lenh.guardian_s_tier")


def _control_handoff(side, offset=0):
    segments = [{
        "state": "CONVERTING", "side": side,
        "price": {"vote": side}, "flow": {"vote": side},
    } for _ in range(2)]
    sealed = {
        "version": "CASH_CONTROL_ACQUISITION_HANDOFF_V1",
        "side": side,
        "first_converting_segment_onset_ms": 9_000 + offset,
        "ownership_completed_ms": 9_500 + offset,
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


def _entry_result(proof_type="PERSISTENT_METAORDER"):
    handoff = _control_handoff("LONG")
    roots = {
        name: {
            "side": "LONG", "state": "FLOW_LED_CONVERSION",
            "conversion_held": True,
            "root_evidence_id": "entry-root-" + name,
            "epoch": index,
        }
        for index, name in enumerate(
            ("binance_spot", "coinbase_spot"), start=2,
        )
    }
    return {
        "decision": "GO",
        "reason": "IGNITION_PROVED",
        "side": "LONG",
        "causal_episode_id": handoff["causal_wave_id"],
        "market_wave_id": handoff["causal_wave_id"],
        "bias_acquisition_handoff": handoff,
        "authority_basis": "BIAS_ALIGNED",
        "ignition": {
            "causal_episode_id": handoff["causal_wave_id"],
            "origin_kind": "SEALED_ACQUISITION_CONTINUATION",
            "acquisition_causal_wave_id": handoff["causal_wave_id"],
            "acquisition_handoff_hash": handoff["handoff_hash"],
            "side": "LONG",
            "proof_type": proof_type,
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
        "cross_cash_causal_wave": {
            "version": "CROSS_CASH_CAUSAL_WAVE_V2_LINEAGE_TOMBSTONES",
            "observed_at_ms": 9_900,
            "active_wave": {
                "causal_wave_id": "cash-wave-entry-1",
                "side": "LONG", "state": "CONTROL_PERSISTING",
                "cash_roots": roots,
            },
        },
    }


def _observation(*, spot=-2.0, coinbase=-2.0, futures=-2.0,
                 spot_flow=-0.4, coinbase_flow=-0.4,
                 futures_flow=-0.4, oi="NEUTRAL", source="FRESH",
                 cash_wave_side=None, wave_age_ms=0,
                 control_handoff_side=None, position_cash_state=None,
                 position_candidate="SHORT", old_side_failure=False,
                 old_side_still_converts=None, cross_state="UNKNOWN",
                 oi_regime="NEUTRAL", liquidation_phase="UNKNOWN",
                 lineage_relation="SAME_SIDE_PROCESS",
                 terminal_reason="OPPOSITE_DUAL_CASH_CONTROL",
                 proof_type="PERSISTENT_METAORDER"):
    entry_result = _entry_result(proof_type)
    truth = market_thesis.build(entry_result)
    if old_side_still_converts is None:
        old_side_still_converts = not old_side_failure and (
            position_cash_state != "CONTROL_ERODING"
        )
    root_id = truth["position_thesis_seed"]["root_id"]
    moves = {"spot": spot, "coinbase": coinbase, "futures": futures}
    result = {
        "version": "GUARDIAN_CANONICAL_OBSERVATION_V1",
        "causal_episode_id": truth["causal_episode_id"],
        "position_cycle_id": "position-shared-1",
        "position_side": "LONG",
        "position_market_wave_id": root_id,
        "position_market_truth_hash": truth["contract_hash"],
        "position_thesis_seed": truth["position_thesis_seed"],
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
        "cross_cash_wave": {
            "version": "CROSS_CASH_CAUSAL_WAVE_V1",
            "state": cross_state,
            "observed_at_ms": 10_000,
            "authority": False,
        },
        "derivative_context": {
            "oi_regime": oi_regime,
            "liquidation_phase": liquidation_phase,
            "authority": False,
        },
        "control_ownership": {
            "current_bias_side": "LONG",
            "acquisition_handoff": entry_result[
                "bias_acquisition_handoff"
            ],
        },
    }
    terminal = bool(old_side_failure and not old_side_still_converts)
    current_wave_id = "cash-wave-entry-1"
    current_side = "LONG"
    if cash_wave_side:
        current_wave_id = "cash-wave-opposing-1"
        current_side = cash_wave_side
    elif lineage_relation == "NEW_SAME_SIDE_WAVE":
        current_wave_id = "cash-wave-new-long"
    elif lineage_relation == "TERMINATED":
        current_wave_id = None
        current_side = "ABSTAIN"
    result["position_cash_wave"] = {
            "version": "CASH_WAVE_OBSERVATION_V3_POSITION_CHALLENGE",
            "observation_scope": "POSITION_RELATIVE_CASH_WAVE",
            "previous_side": "LONG",
            "position_identity": {
                "position_cycle_id": "position-shared-1",
                "market_wave_id": root_id,
                "market_truth_hash": truth["contract_hash"],
                "entry_mechanism": truth["mechanism"],
                "position_root_id": root_id,
                "position_root_hash": truth["position_thesis_seed"][
                    "root_hash"
                ],
                "position_identity_kind": truth["position_thesis_seed"][
                    "identity_kind"
                ],
            },
            "causal_lineage": {
                "version": "CURRENT_CASH_PROCESS_LINEAGE_V1",
                "position_root_id": root_id,
                "position_root_hash": truth["position_thesis_seed"][
                    "root_hash"
                ],
                "position_identity_kind": truth["position_thesis_seed"][
                    "identity_kind"
                ],
                "position_side": "LONG",
                "entry_causal_wave_id": "cash-wave-entry-1",
                "entry_side": "LONG",
                "current_causal_wave_id": current_wave_id,
                "current_side": current_side,
                "current_state": "CONTROL_PERSISTING",
                "lineage_relation": (
                    "OPPOSING_PROCESS" if cash_wave_side else (
                        "SAME_SIDE_PROCESS"
                        if lineage_relation == "NEW_SAME_SIDE_WAVE"
                        else lineage_relation
                    )
                ),
                "incumbent_terminal": terminal,
                "terminal_evidence": ({
                    "causal_wave_id": "cash-wave-entry-1",
                    "side": "LONG",
                    "termination_reason": terminal_reason,
                    "terminated_at_ms": 9_990,
                    "termination_evidence": ({
                        "binance_spot": {
                            "side": "LONG",
                            "state": "FLOW_NONCONVERSION",
                            "reclaimed_past_root": True,
                            "root_evidence_id": "dead-root-spot",
                        },
                    } if terminal_reason
                        == "FLOW_NONCONVERSION_WITH_RECLAIM" else {}),
                    "authority": False,
                } if terminal else {}),
                "authority": False,
            },
            "raw_side": (
                position_candidate
                if position_cash_state == "CONTROL_TRANSFER" else "LONG"
            ),
            "candidate_side": position_candidate,
            "wave_state": position_cash_state,
            "phase": position_cash_state,
            "control_transfer_confirmed": (
                position_cash_state == "CONTROL_TRANSFER"
            ),
            "old_side_failure_evidence": old_side_failure,
            "old_side_still_converts": old_side_still_converts,
            "observed_at_ms": 10_000,
            "gap_or_epoch_invalid": False,
            "authority": False,
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

    def _terminal_state(self, truth=None, reason="FLOW_NONCONVERSION_WITH_RECLAIM"):
        truth = truth or self.truth
        root_id = truth["position_thesis_seed"]["root_id"]
        return SimpleNamespace(market_truth_wave_tombstones={
            root_id: {"root_id": root_id, "reason": reason, "terminal": True},
        })

    def test_snapshot_cannot_overturn_exact_alive_incumbent(self):
        result = market_thesis.observe(self.truth, _observation())
        self.assertEqual(result["status"], "SUPPORT")
        self.assertEqual(result["incumbent_thesis"]["state"], "ALIVE")
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
                position_cash_state="CONTROL_TRANSFER",
                old_side_failure=True,
            ),
            state=self._terminal_state(reason="OPPOSITE_DUAL_CASH_CONTROL"),
        )
        self.assertEqual(result["status"], "CONTROL_TRANSFER")
        self.assertTrue(result["old_thesis_falsified"])
        self.assertIn("OPPOSITE_DUAL_CASH_CONTROL", result["observed_falsifiers"])

    def test_incumbent_failure_exits_flat_without_challenger_permission(self):
        result = market_thesis.observe(
            self.truth,
            _observation(
                position_cash_state="CONTROL_ERODING",
                old_side_failure=True,
                lineage_relation="TERMINATED",
                terminal_reason="FLOW_NONCONVERSION_WITH_RECLAIM",
            ),
            state=self._terminal_state(),
        )
        self.assertEqual(result["incumbent_thesis"]["state"], "FAILED")
        self.assertEqual(result["challenger_process"]["state"], "NONE")
        self.assertEqual(result["status"], "FALSIFY")
        self.assertEqual(
            guardian._canonical_thesis_action(result)["decision"], "EXIT",
        )

    def test_nonconversion_without_reclaim_only_erodes_incumbent(self):
        result = market_thesis.observe(
            self.truth,
            _observation(position_cash_state="CONTROL_ERODING"),
        )
        self.assertEqual(result["incumbent_thesis"]["state"], "ERODING")
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(result["old_thesis_falsified"])

    def test_secondary_only_reclaim_cannot_kill_primary_metaorder(self):
        row = _observation(
            position_cash_state="CONTROL_ERODING",
            old_side_failure=True,
            lineage_relation="TERMINATED",
            terminal_reason="FLOW_NONCONVERSION_WITH_RECLAIM",
        )
        evidence = row["position_cash_wave"]["causal_lineage"][
            "terminal_evidence"
        ]["termination_evidence"]
        evidence["coinbase_spot"] = evidence.pop("binance_spot")
        result = market_thesis.observe(self.truth, row)
        self.assertEqual(result["incumbent_thesis"]["state"], "ERODING")
        self.assertEqual(result["status"], "DIVERGENCE")

    def test_failed_incumbent_with_emerging_attacker_exits_flat(self):
        row = _observation(
            position_cash_state="TRANSITION",
            old_side_failure=True,
            lineage_relation="OPPOSING_WAVE",
            terminal_reason="FLOW_NONCONVERSION_WITH_RECLAIM",
        )
        row["position_cash_wave"]["causal_lineage"].update({
            "current_causal_wave_id": "cash-wave-short-emerging",
            "current_side": "SHORT",
        })
        result = market_thesis.observe(
            self.truth, row, state=self._terminal_state(),
        )
        self.assertEqual(result["incumbent_thesis"]["state"], "FAILED")
        self.assertEqual(result["challenger_process"]["state"], "EMERGING")
        self.assertEqual(result["status"], "FALSIFY")

    def test_failed_reversion_requires_surviving_opposite_acceptance(self):
        truth = market_thesis.build(_entry_result("FAILED_REVERSION"))
        unresolved = market_thesis.observe(
            truth,
            _observation(
                position_cash_state="CONTROL_ERODING",
                old_side_failure=True,
                lineage_relation="TERMINATED",
                terminal_reason="FLOW_NONCONVERSION_WITH_RECLAIM",
                proof_type="FAILED_REVERSION",
            ),
        )
        self.assertEqual(unresolved["incumbent_thesis"]["state"], "ERODING")
        self.assertEqual(unresolved["status"], "DIVERGENCE")

        accepted = market_thesis.observe(
            truth,
            _observation(
                cash_wave_side="SHORT",
                position_cash_state="CONTROL_TRANSFER",
                old_side_failure=True,
                terminal_reason="OPPOSITE_DUAL_CASH_CONTROL",
                proof_type="FAILED_REVERSION",
            ),
            state=self._terminal_state(
                truth, "OPPOSITE_DUAL_CASH_CONTROL"
            ),
        )
        self.assertEqual(accepted["incumbent_thesis"]["state"], "FAILED")
        self.assertEqual(accepted["status"], "CONTROL_TRANSFER")

    def test_metaorder_continuation_freezes_metaorder_mechanism(self):
        truth = market_thesis.build(_entry_result("METAORDER_CONTINUATION"))
        self.assertEqual(truth["mechanism"], "CASH_METAORDER")

    def test_dead_wave_cannot_be_resurrected_by_new_same_side_wave(self):
        state = self._terminal_state()
        dead = market_thesis.observe(
            self.truth,
            _observation(
                position_cash_state="CONTROL_ERODING",
                old_side_failure=True,
                lineage_relation="TERMINATED",
                terminal_reason="FLOW_NONCONVERSION_WITH_RECLAIM",
            ),
            state=state,
        )
        self.assertEqual(dead["status"], "FALSIFY")

        successor = _observation(
            position_cash_state="CONTROLLED",
            lineage_relation="NEW_SAME_SIDE_WAVE",
        )
        replayed = market_thesis.observe(self.truth, successor, state=state)
        self.assertEqual(replayed["incumbent_thesis"]["state"], "FAILED")
        self.assertEqual(replayed["status"], "FALSIFY")
        self.assertNotEqual(replayed["challenge"], "RECOVERED")

    def test_new_same_side_wave_without_terminal_proof_is_unknown(self):
        row = _observation(
            position_cash_state="CONTROLLED",
            lineage_relation="NEW_SAME_SIDE_WAVE",
        )
        row["control_ownership"]["acquisition_handoff"] = (
            _control_handoff("LONG", offset=1_000)
        )
        result = market_thesis.observe(self.truth, row)
        self.assertEqual(result["incumbent_thesis"]["state"], "UNKNOWN")
        self.assertEqual(
            result["challenge"], "SAME_SIDE_PROCESS_NOT_ROOT_PROOF",
        )
        self.assertFalse(result["old_thesis_falsified"])

    def test_opposing_owner_without_position_failure_is_unproven_transition(self):
        result = market_thesis.observe(
            self.truth, _observation(
                cash_wave_side="SHORT", control_handoff_side="SHORT",
                position_cash_state="CONTROL_TRANSFER",
                old_side_failure=False,
            ),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["incumbent_thesis"]["state"], "UNKNOWN")
        self.assertFalse(result["old_thesis_falsified"])

    def test_old_side_still_converting_blocks_takeover(self):
        result = market_thesis.observe(
            self.truth, _observation(
                cash_wave_side="SHORT", control_handoff_side="SHORT",
                position_cash_state="CONTROL_TRANSFER",
                old_side_failure=True, old_side_still_converts=True,
            ),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["incumbent_thesis"]["state"], "UNKNOWN")
        self.assertFalse(result["old_thesis_falsified"])

    def test_mutable_current_bias_does_not_duplicate_sealed_takeover_proof(self):
        row = _observation(
            cash_wave_side="SHORT", control_handoff_side="SHORT",
            position_cash_state="CONTROL_TRANSFER",
            old_side_failure=True,
        )
        row["control_ownership"]["current_bias_side"] = "ABSTAIN"
        result = market_thesis.observe(
            self.truth, row,
            state=self._terminal_state(reason="OPPOSITE_DUAL_CASH_CONTROL"),
        )
        self.assertEqual(result["status"], "CONTROL_TRANSFER")
        self.assertEqual(result["challenge"], "TAKEOVER_PROVEN")

    def test_position_challenge_taxonomy_does_not_invent_exit(self):
        cases = (
            ("PULLBACK", {}, "PULLBACK", "SUPPORT"),
            (
                "PULLBACK", {"cross_state": "PRICE_LED_CHASE"},
                "PRICE_LED_CHASE", "SUPPORT",
            ),
            (
                "PULLBACK", {"oi_regime": "CONTRACTION"},
                "SQUEEZE_ONLY", "SUPPORT",
            ),
            ("CONTROL_ERODING", {}, "OLD_CONTROL_ERODING", "DIVERGENCE"),
            ("TRANSITION", {}, "UNPROVEN_TRANSITION", "DIVERGENCE"),
            ("CONTROLLED", {}, "INCUMBENT_ALIVE", "SUPPORT"),
            ("FAKEOUT_ABSORBED", {}, "FAKEOUT_ABSORBED", "SUPPORT"),
        )
        for wave_state, extra, challenge, status in cases:
            with self.subTest(wave_state=wave_state, challenge=challenge):
                result = market_thesis.observe(
                    self.truth,
                    _observation(
                        position_cash_state=wave_state, **extra,
                    ),
                )
                self.assertEqual(result["challenge"], challenge)
                self.assertEqual(result["status"], status)
                self.assertFalse(result["old_thesis_falsified"])

    def test_stale_opposing_wave_cannot_transfer_control(self):
        result = market_thesis.observe(
            self.truth,
            _observation(
                cash_wave_side="SHORT", wave_age_ms=5_001,
                control_handoff_side="SHORT",
            ),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["old_thesis_falsified"])

    def test_unsealed_or_mutated_control_owner_cannot_transfer(self):
        row = _observation(
            cash_wave_side="SHORT", control_handoff_side="SHORT",
        )
        row["control_ownership"]["acquisition_handoff"][
            "handoff_hash"
        ] = "forged"
        result = market_thesis.observe(self.truth, row)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(
            result["evidence"]["control_ownership_handoff_valid"],
        )

    def test_single_cash_snapshot_cannot_falsify_alive_lineage(self):
        result = market_thesis.observe(self.truth, _observation(
            coinbase=0.2, coinbase_flow=0.2, futures=0.2,
            futures_flow=0.2,
        ))
        self.assertEqual(result["status"], "SUPPORT")
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
            market_truth_wave_tombstones=(
                self._terminal_state(
                    reason="OPPOSITE_DUAL_CASH_CONTROL"
                ).market_truth_wave_tombstones
            ),
        )
        position = SimpleNamespace(
            position_cycle_id="position-canonical", side="LONG",
            opened_at=99.0, causal_episode_id="episode-shared-1",
            best_r=0.0, floor_r=None,
        )
        neutral = guardian._vote("NEUTRAL", 0.0, "TEST_NEUTRAL")
        terminal_event = _observation(
            cash_wave_side="SHORT", control_handoff_side="SHORT",
            position_cash_state="CONTROL_TRANSFER", old_side_failure=True,
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
        episode_id = self.truth["causal_episode_id"]
        action = authority_contracts.seal(
            "ACTION", "ENTRY_ACTION_POLICY", episode_id,
            {"action": "ACT_TAKER_NOW"},
        )
        bundle = authority_contracts.bundle(
            self.truth,
            action,
            authority_contracts.seal(
                "EXECUTION", "EXECUTION_REVALIDATION", episode_id,
                {"execution_action": "EXECUTE"},
            ),
            authority_contracts.seal(
                "SAFETY", "MAINNET_SAFETY", episode_id,
                {"safety_state": "SAFE"},
            ),
        )
        handoff = authority_contracts.freeze_entry_handoff(bundle)
        root_id = self.truth["position_thesis_seed"]["root_id"]
        position = SimpleNamespace(
            side="LONG", causal_episode_id=episode_id,
            position_cycle_id="position-shared-1",
            market_wave_id=root_id,
            position_thesis_seed=self.truth["position_thesis_seed"],
            entry_causal_thesis={
                "causal_episode_id": episode_id,
                "entry_thesis_handoff": handoff,
            },
        )
        state = SimpleNamespace(
            thoi_gian_tick_cuoi=100.0, thoi_gian_dong_tien_cuoi=100.0,
            thoi_gian_coinbase_ticker_cuoi=100.0,
            coinbase_flow_3s_ts=100.0, thoi_gian_vi_mo_cuoi=100.0,
            guardian_s_spot_flow_ordering="MONOTONIC",
            guardian_s_futures_flow_ordering="MONOTONIC",
            bias_state="LONG",
            bias_acquisition_handoff=_entry_result()[
                "bias_acquisition_handoff"
            ],
            spot_flow_epoch=2, coinbase_flow_epoch=3,
            danh_sach_khop_lenh_futures=[{
                "thoi_gian_ms": 100_000, "gia": 100.0,
            }],
            post_entry_position_cash_wave=copy.deepcopy(
                _observation(position_cash_state="CONTROLLED")[
                    "position_cash_wave"
                ]
            ),
        )
        state.post_entry_position_cash_wave["observed_at_ms"] = 100_000
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
        self.assertEqual(event["causal_episode_id"], episode_id)
        self.assertEqual(observed["status"], "SUPPORT")

        # A historical entry epoch mismatch remains visible, but cannot poison
        # the position forever after fresh same-epoch segments are rebuilt.
        state.spot_flow_epoch = 9
        state.post_entry_position_cash_wave = copy.deepcopy(
            _observation(position_cash_state="CONTROLLED")[
                "position_cash_wave"
            ]
        )
        state.post_entry_position_cash_wave["observed_at_ms"] = 100_000
        _, recovered_event = guardian._canonical_thesis_observation(
            state, position, 100.0, s1, s2, s3,
        )
        self.assertTrue(recovered_event["entry_epoch_changed"])
        self.assertFalse(recovered_event["gap_or_epoch_invalid"])
        state.post_entry_position_cash_wave["gap_or_epoch_invalid"] = True
        _, interrupted_event = guardian._canonical_thesis_observation(
            state, position, 100.0, s1, s2, s3,
        )
        self.assertTrue(interrupted_event["gap_or_epoch_invalid"])

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
        state.market_truth_wave_tombstones = (
            self._terminal_state(
                reason="OPPOSITE_DUAL_CASH_CONTROL"
            ).market_truth_wave_tombstones
        )
        state.post_entry_position_cash_wave = copy.deepcopy(
            _observation(
                cash_wave_side="SHORT",
                position_cash_state="CONTROL_TRANSFER",
                old_side_failure=True,
            )["position_cash_wave"]
        )
        state.post_entry_position_cash_wave["observed_at_ms"] = 100_000
        _, transfer_event = guardian._canonical_thesis_observation(
            state, position, 100.0, s1, s2, s3,
        )
        transferred = market_thesis.observe(
            self.truth, transfer_event, state=state,
        )
        self.assertEqual(transferred["status"], "CONTROL_TRANSFER")


if __name__ == "__main__":
    unittest.main()
