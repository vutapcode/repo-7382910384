import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mainnet_tier_s_shadow_launcher as launcher
from loi_he_thong import ignition_core
from recorder.decision_outcomes import DecisionOutcomeTracker


class MissTaxonomyTests(unittest.TestCase):
    def test_post_go_rejection_has_full_attribution_contract(self):
        result = {
            "decision": "GO", "side": "LONG",
            "decision_cycle_id": "cycle-1",
            "causal_episode_id": "episode-1",
            "authority_basis": "TRANSITION_CONFIRMED",
            "authority_proof_hash": "proof-1",
            "execution_policy": "TAKER",
            "ignition": {"proof_type": "METAORDER_CONTINUATION"},
        }
        with patch.object(launcher, "_append_event") as append:
            self.assertTrue(launcher._record_post_go_rejection(
                result, "AUTHORITY_REVALIDATION", "TEST_REJECT",
                "current_cash_conversion",
            ))
        event, payload = append.call_args.args
        self.assertEqual(event, "ENTRY_POST_GO_REJECTED")
        for key in (
            "reject_stage", "blocking_reason", "authority_basis",
            "proof_hash", "failed_dependency",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["proof_hash"], "proof-1")

    def test_launcher_allows_only_canonical_dual_cash_transition_bypass(self):
        state = SimpleNamespace(bias_state="SHORT")
        base = {"decision": "GO", "side": "LONG", "ignition": {}}
        self.assertFalse(launcher._bias_or_transition_authorized(base, state))

        payload = {
            "causal_episode_id": "transition-1",
            "proof_type": "METAORDER_CONTINUATION",
            "proof_venue": "binance_spot",
            "transition_confirmed": True,
            "current_cash_conversion": {"venues": {
                "binance_spot": {"receive_time_ms": 1_000, "epoch": 1},
                "coinbase_spot": {"receive_time_ms": 1_100, "epoch": 1},
            }},
            "transition_authority": {
                "status": "REVERSAL_CONFIRMED",
                "side": "LONG",
                "background_side": "SHORT",
                "old_side_failure_confirmed": True,
                "old_failure_venues": ["binance_spot"],
                "new_side_cash_control_confirmed": True,
                "cash_synchronous_transition": True,
                "cash_acceptance_span_ms": 100,
                "accepted_cash_venues": [
                    "binance_spot", "coinbase_spot",
                ],
            },
        }
        basis, dependencies, proof_hash = (
            ignition_core._freeze_authority_proof(
                payload, "LONG", payload["proof_type"], "transition-1",
            )
        )
        transition = {
            **base,
            "causal_episode_id": "transition-1",
            "authority_basis": basis,
            "authority_dependencies": dependencies,
            "authority_proof_hash": proof_hash,
            "ignition": payload,
        }
        self.assertTrue(
            launcher._bias_or_transition_authorized(transition, state)
        )

        # Mutable Transition telemetry may decay after GO. The launcher must
        # preserve the frozen proof instead of reconstructing strategy.
        transition["ignition"]["transition_authority"] = {}
        self.assertTrue(
            launcher._bias_or_transition_authorized(transition, state)
        )

        # Transition authority does not depend on the later slow-Bias value.
        state.bias_state = "ABSTAIN"
        self.assertFalse(launcher._bias_or_transition_authorized(base, state))
        self.assertTrue(
            launcher._bias_or_transition_authorized(transition, state)
        )

        tampered = dict(transition)
        tampered["authority_dependencies"] = dict(dependencies)
        tampered["authority_dependencies"]["side"] = "SHORT"
        self.assertFalse(
            launcher._bias_or_transition_authorized(tampered, state)
        )

    def test_frozen_bias_mismatch_has_distinct_taxonomy(self):
        result = {
            "decision": "WAIT",
            "reason": "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS",
            "side": "LONG",
            "s_votes": {},
        }
        primary, failed = launcher._miss_taxonomy(result, {}, False)
        self.assertEqual(primary, "BIAS_ALIGNMENT_FAIL")
        self.assertIn("BIAS_ALIGNMENT_FAIL", failed)
        self.assertNotIn("BIAS_NOT_READY", failed)

    def test_compatibility_votes_are_diagnostics_not_live_blockers(self):
        result = {
            "decision": "WAIT",
            "reason": "WAIT_FUTURES_ALERT_CASH_RESPONSE",
            "side": "SHORT",
            "s_votes": {
                "S1_cross_venue_price_acceptance": {"status": "FAIL"},
                "S2_multi_venue_executed_flow": {"status": "FAIL"},
            },
        }
        details = launcher._miss_taxonomy_details(
            result, {"bootstrap_shadow_allowed": True}, False,
        )
        self.assertEqual(details["blocking_reason"], "WAIT_CASH_RESPONSE")
        self.assertEqual(details["blocking_reasons"], ["WAIT_CASH_RESPONSE"])
        self.assertEqual(
            details["diagnostic_reasons"],
            ["PRICE_QUORUM_FAIL", "FLOW_QUORUM_FAIL"],
        )

    def test_go_contract_failure_has_canonical_blocker(self):
        result = {
            "decision": "GO", "reason": "IGNITION_METAORDER_CONTINUATION",
            "side": "LONG", "s_votes": {},
        }
        details = launcher._miss_taxonomy_details(
            result, {"bootstrap_shadow_allowed": True}, False,
        )
        self.assertEqual(
            details["blocking_reason"], "ENTRY_AUTHORITY_CONTRACT_FAIL"
        )
        self.assertEqual(details["diagnostic_reasons"], [])

    def test_snapshot_reports_final_wait_when_raw_go_is_not_authorized(self):
        state = SimpleNamespace(
            open_interest=0.0, thoi_gian_vi_mo_cuoi=0.0,
            bias_council={}, bias_state="LONG", bias_confidence=0.8,
            execution_best_bid=100.0, execution_best_ask=100.1,
        )
        result = {
            "decision": "GO",
            "reason": "IGNITION_METAORDER_CONTINUATION",
            "side": "LONG",
            "s_votes": {},
        }
        snapshot = launcher._decision_snapshot(
            state, result, {"bootstrap_shadow_allowed": True}, False,
            "cycle-blocked-go", 100.0,
        )
        output = snapshot["output"]
        self.assertEqual(output["decision"], "WAIT")
        self.assertEqual(
            output["reason"], "ENTRY_AUTHORITY_CONTRACT_FAIL"
        )
        self.assertEqual(output["entry_authority_decision"], "GO")
        self.assertEqual(output["authorization_status"], "BLOCKED")

    def test_current_cash_refresh_has_specific_blocking_reason(self):
        result = {
            "decision": "WAIT", "reason": "WAIT_CURRENT_CASH_CONVERSION",
            "side": "SHORT", "s_votes": {},
        }
        details = launcher._miss_taxonomy_details(result, {}, False)
        self.assertEqual(
            details["blocking_reason"], "WAIT_CURRENT_CASH_CONVERSION"
        )
        self.assertNotIn("FLOW_QUORUM_FAIL", details["blocking_reasons"])

    def test_snapshot_oi_freshness_uses_poll_aware_contract(self):
        state = SimpleNamespace(
            open_interest=100.0, thoi_gian_vi_mo_cuoi=83.0,
            oi_poll_interval_effective_seconds=15.0,
            bias_council={}, bias_state="ABSTAIN", bias_confidence=0.0,
            execution_best_bid=100.0, execution_best_ask=100.1,
        )
        snapshot = launcher._decision_snapshot(
            state, {"side": "ABSTAIN", "s_votes": {}}, {}, False,
            "cycle-test", 100.0,
        )
        oi = snapshot["inputs"]["open_interest"]
        self.assertTrue(oi["fresh"])
        self.assertEqual(oi["max_age_seconds"], 18.0)
        self.assertIn("opportunity_research", snapshot["inputs"])

    def test_pending_reversal_keeps_episode_side_research_only(self):
        state = SimpleNamespace(
            open_interest=100.0, thoi_gian_vi_mo_cuoi=99.0,
            bias_council={}, bias_state="SHORT", bias_confidence=0.7,
            execution_best_bid=100.0, execution_best_ask=100.1,
        )
        episode_id = "ign:futures:LONG:100000"
        result = {
            "decision": "WAIT",
            "reason": "BIAS_NOT_READY",
            "side": "SHORT",
            "causal_episode_id": episode_id,
            "s_votes": {},
            "ignition": {
                "causal_episode_id": episode_id,
                "episode_id": episode_id,
                "side": "LONG",
                "status": "PENDING_BIAS_FLIP",
                "authority": False,
                "transition_confirmed": False,
                "transition_authority": {"confirmed": False, "side": "LONG"},
            },
        }
        snapshot = launcher._decision_snapshot(
            state, result, {}, False, "cycle-reversal", 100.0,
            opportunity={"causal_episode_id": episode_id},
        )
        self.assertEqual(snapshot["background_bias_side"], "SHORT")
        self.assertEqual(snapshot["decision_side"], "SHORT")
        self.assertEqual(snapshot["causal_episode_side"], "LONG")
        self.assertEqual(snapshot["counterfactual"]["side"], "LONG")
        self.assertEqual(
            snapshot["counterfactual"]["counterfactual_side"], "LONG"
        )
        self.assertFalse(snapshot["counterfactual"]["economic_miss_eligible"])
        self.assertEqual(
            snapshot["counterfactual"]["research_only_reason"],
            "UNCONFIRMED_PENDING_REVERSAL",
        )

    def test_accepted_bootstrap_shadow_trade_is_not_a_miss(self):
        result = {
            "decision": "GO", "reason": "IGNITION_METAORDER_CONTINUATION",
            "side": "LONG", "s_votes": {},
        }
        edge = {
            "cost_ok": False, "bootstrap_shadow_allowed": True,
            "live_empirical_ok": False, "price_impact": {},
            "spot_perp_basis": {},
        }
        self.assertEqual(launcher._miss_taxonomy(result, edge, True), (None, []))

    def test_unwind_tail_audit_is_visible_as_first_blocking_gate(self):
        result = {
            "decision": "GO", "reason": "IGNITION_PERSISTENT_METAORDER",
            "side": "SHORT", "s_votes": {},
        }
        edge = {
            "cost_ok": False, "bootstrap_shadow_allowed": False,
            "live_empirical_ok": False, "price_impact": {},
            "spot_perp_basis": {},
            "entry_thesis_audit": {
                "blocking_reasons": ["UNWIND_TAIL_VETO"],
            },
        }
        primary, failed = launcher._miss_taxonomy(result, edge, True)
        self.assertEqual(primary, "UNWIND_TAIL_VETO")
        self.assertIn("UNWIND_TAIL_VETO", failed)

    def test_persistent_decay_is_soft_wait_not_empirical_failure(self):
        result = {
            "decision": "GO", "reason": "PERSISTENT_METAORDER_PROVED",
            "side": "LONG", "s_votes": {},
        }
        edge = {
            "cost_ok": False, "bootstrap_shadow_allowed": False,
            "live_empirical_ok": False, "price_impact": {},
            "spot_perp_basis": {},
            "soft_wait_reasons": ["WAIT_PERSISTENT_FLOW_EFFICIENCY"],
            "entry_thesis_audit": {"blocking_reasons": []},
        }
        primary, failed = launcher._miss_taxonomy(result, edge, False)
        self.assertEqual(primary, "WAIT_PERSISTENT_FLOW_EFFICIENCY")
        self.assertNotIn("EMPIRICAL_ALPHA_NOT_READY", failed)

    def test_ignition_unknown_flow_is_recorded_as_soft_timing_wait(self):
        result = {
            "decision": "GO", "reason": "IGNITION_METAORDER_CONTINUATION",
            "side": "SHORT", "s_votes": {},
        }
        edge = {
            "cost_ok": False, "bootstrap_shadow_allowed": False,
            "live_empirical_ok": False, "price_impact": {},
            "spot_perp_basis": {},
            "soft_wait_reasons": ["WAIT_IGNITION_FLOW_EFFICIENCY"],
            "entry_thesis_audit": {"blocking_reasons": []},
        }
        primary, failed = launcher._miss_taxonomy(result, edge, False)
        self.assertEqual(primary, "WAIT_IGNITION_FLOW_EFFICIENCY")
        self.assertNotIn("EMPIRICAL_ALPHA_NOT_READY", failed)


    def test_proximity_cluster_does_not_dedupe_distinct_causal_episodes(self):
        tracker = DecisionOutcomeTracker(lambda *args, **kwargs: None)
        wave_a = tracker._economic_wave_id("ep-a", "LONG", 1_000, 100.0)
        wave_b = tracker._economic_wave_id("ep-b", "LONG", 4_000, 100.01)
        cluster_a = tracker._economic_cluster_id("LONG", 1_000, 100.0)
        cluster_b = tracker._economic_cluster_id("LONG", 4_000, 100.01)

        self.assertNotEqual(wave_a, wave_b)
        self.assertEqual(cluster_a, cluster_b)
        self.assertEqual(
            wave_a,
            tracker._economic_wave_id("ep-a", "LONG", 4_500, 100.02),
        )



if __name__ == "__main__":
    unittest.main()
