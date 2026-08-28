from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loi_he_thong import entry_edge_tier, entry_thesis_gate


def result(*, intent="UNWIND", consumed=0.32, recent_progress=0.02,
           cash=("binance_spot", "coinbase_spot"), flow_states=None):
    flow = {
        venue: {
            "signed_imbalance": 0.70,
            "recent_1s_signed_imbalance": 0.70,
            "recent_1s_price_progress_bps": recent_progress,
            "recent_1s_volume_btc": 0.20,
        }
        for venue in cash
    }
    ignition = {
        "state": "PROVE", "proof_type": "PERSISTENT_METAORDER",
        "proposer": cash[0] if cash else "futures",
        "cash_venues": list(cash), "supporting_venues": list(cash) + ["futures"],
        "futures_follow_ok": True, "futures_cash_response_ok": bool(cash),
        "current_cash_conversion": {
            "confirmed": bool(cash),
            "accepted_cash_venues": list(cash),
            "dual_cash_synchronous_control": set(cash) == {
                "binance_spot", "coinbase_spot",
            },
        },
        "consumed_fraction": consumed,
        "phase_measurement": {
            "source": "HYBRID_PRECURSOR_AND_EPISODE_CASH_DISPLACEMENT_OVER_ATR_1M",
            "phase_scale_bps": 10.0,
            "cash_displacement_bps": consumed * 10.0,
            "episode_cash_displacement_bps": 1.0,
            "precursor_cash_displacement_bps": consumed * 10.0,
        },
        "venue_moves_bps": {venue: 0.50 for venue in cash} | {"futures": 0.40},
        "flow_by_venue": flow,
        "flow_efficiency": {
            "version": "FLOW_EFFICIENCY_V5_VENUE_FRESHNESS",
            "venues": {
                venue: {
                    "state": (flow_states or {}).get(
                        venue,
                        "EXHAUSTED" if recent_progress < 0.10
                        else "CONTINUING_CONFIRMED",
                    )
                }
                for venue in cash
            },
        },
        "oi_intent": {
            "intent": intent, "fresh": True,
            "causal_class": "CASH_LED_UNWIND" if intent == "UNWIND" else "ALIGNED_BUILD",
        },
        "oi_verification_state": {
            "status": (
                "FRESH_UNWIND" if intent == "UNWIND"
                else "FRESH_POSITION_BUILD"
            ),
            "intent": intent,
        },
        "bias_snapshot": {
            "direction": "SHORT", "confidence": 0.80,
            "direction_context": {
                "phase": "ESTABLISHED_TREND", "context_side": "SHORT",
                "candidate_side": "ABSTAIN",
            },
        },
    }
    return {
        "decision": "GO", "side": "SHORT",
        "entry_mode": "PERSISTENT_METAORDER", "phase": "RELEASE",
        "execution_policy": "TAKER",
        "price_threshold_bps": 0.15, "ignition": ignition,
        "ts": 100.0, "s_votes": {},
    }


PASS_IMPACT = {"absorbed": False, "efficient": True, "status": "PASS"}
PASS_BASIS = {"perp_expansion": False, "status": "CASH_CONFIRMED"}
NO_LIQUIDATION = {"phase": "QUIET", "burst": False, "decelerating": False}


class EntryThesisGateTests(unittest.TestCase):
    def test_entry_contract_requires_present_tense_cash_conversion(self):
        candidate = result(consumed=0.20, recent_progress=0.20)
        candidate["ignition"].pop("current_cash_conversion")
        self.assertFalse(entry_edge_tier.normal_contract_ok(candidate))

    def test_mature_unwind_with_strong_flow_but_no_price_conversion_waits(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), result(), PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION
        )
        self.assertEqual(audit["decision"], "WAIT")
        self.assertIn("UNWIND_TAIL_VETO", audit["blocking_reasons"])
        self.assertEqual(
            audit["questions"]["q3_flow_efficiency"]["status"],
            "EXHAUSTED",
        )

    def test_efficient_early_dual_cash_unwind_is_preserved(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(),
            result(consumed=0.20, recent_progress=0.20),
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertEqual(audit["decision"], "PASS")
        self.assertNotIn("UNWIND_TAIL_VETO", audit["blocking_reasons"])
        self.assertEqual(
            audit["questions"]["q3_flow_efficiency"]["status"],
            "CONTINUING_CONFIRMED",
        )

    def test_primary_absorption_is_not_a_veto_when_other_cash_continues(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(),
            result(
                intent="POSITION_BUILD", consumed=0.20,
                flow_states={
                    "binance_spot": "ABSORBED",
                    "coinbase_spot": "CONTINUING_CONFIRMED",
                },
            ),
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertEqual(audit["decision"], "PASS")
        self.assertNotIn("FLOW_EFFICIENCY_V3_VETO", audit["blocking_reasons"])

    def test_primary_absorption_without_independent_continuation_vetoes(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(entry_economics_v3_replay_approved=True),
            result(
                intent="POSITION_BUILD", consumed=0.20,
                flow_states={
                    "binance_spot": "ABSORBED",
                    "coinbase_spot": "DECAYING",
                },
            ),
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertEqual(audit["decision"], "WAIT")
        self.assertIn("FLOW_EFFICIENCY_V3_VETO", audit["blocking_reasons"])

    def test_old_episode_progress_cannot_claim_current_conversion(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20, recent_progress=0.20,
            flow_states={
                "binance_spot": "UNKNOWN", "coinbase_spot": "UNKNOWN",
            },
        )
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), candidate,
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        flow = audit["questions"]["q3_flow_efficiency"]
        self.assertEqual(flow["status"], "UNKNOWN")
        self.assertFalse(flow["converts"])
        self.assertGreater(flow["episode_cash_progress_bps"], 0.0)

    def test_persistent_decaying_soft_waits_instead_of_immediate_taker(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "DECAYING", "coinbase_spot": "UNKNOWN",
            },
        )
        allowed, report = entry_edge_tier.authorize(candidate, SimpleNamespace(
            entry_economics_v3_replay_approved=False,
            wstrade_live_armed=False,
        ))
        self.assertFalse(allowed)
        self.assertEqual(report["execution_style"], "TAKER")
        self.assertIn(
            "WAIT_PERSISTENT_FLOW_EFFICIENCY",
            report["soft_wait_reasons"],
        )
        self.assertNotIn(
            "WAIT_PERSISTENT_FLOW_EFFICIENCY", report["hard_vetoes"]
        )

    def test_persistent_continuing_remains_eligible(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "CONTINUING_CONFIRMED",
                "coinbase_spot": "UNKNOWN",
            },
        )
        allowed, report = entry_edge_tier.authorize(candidate, SimpleNamespace(
            entry_economics_v3_replay_approved=False,
            wstrade_live_armed=False,
        ))
        self.assertTrue(allowed)
        self.assertEqual(report["soft_wait_reasons"], [])

    def test_ignition_metaorder_unknown_waits_instead_of_immediate_taker(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "UNKNOWN", "coinbase_spot": "UNKNOWN",
            },
        )
        candidate["entry_mode"] = "IGNITION"
        candidate["ignition"]["proof_type"] = "METAORDER_CONTINUATION"
        allowed, report = entry_edge_tier.authorize(
            candidate,
            SimpleNamespace(
                entry_economics_v3_replay_approved=False,
                wstrade_live_armed=False,
            ),
        )
        self.assertFalse(allowed)
        self.assertEqual(report["execution_style"], "TAKER")
        self.assertIn(
            "WAIT_IGNITION_FLOW_EFFICIENCY",
            report["soft_wait_reasons"],
        )
        self.assertNotIn(
            "WAIT_IGNITION_FLOW_EFFICIENCY", report["hard_vetoes"]
        )

    def test_failed_reversion_maker_keeps_its_separate_proof_contract(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "UNKNOWN", "coinbase_spot": "UNKNOWN",
            },
        )
        candidate["entry_mode"] = "IGNITION"
        candidate["phase"] = "ACCEPTANCE"
        candidate["execution_policy"] = "MAKER"
        candidate["ignition"]["proof_type"] = "FAILED_REVERSION"
        allowed, report = entry_edge_tier.authorize(
            candidate,
            SimpleNamespace(
                entry_economics_v3_replay_approved=False,
                wstrade_live_armed=False,
            ),
        )
        self.assertTrue(allowed)
        self.assertEqual(report["execution_style"], "MAKER")
        self.assertEqual(report["soft_wait_reasons"], [])

    def test_confirmed_fast_transition_bypasses_only_bias_alignment(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "CONTINUING_CONFIRMED",
                "coinbase_spot": "CONTINUING_CONFIRMED",
            },
        )
        candidate["side"] = "LONG"
        candidate["ignition"]["bias_snapshot"]["direction"] = "SHORT"
        candidate["ignition"]["transition_confirmed"] = True
        candidate["ignition"]["transition_authority"] = {
            "status": "REVERSAL_CONFIRMED", "side": "LONG",
            "old_side_failure_confirmed": True,
            "new_side_cash_control_confirmed": True,
            "cash_synchronous_transition": True,
            "hard_contradiction": False,
        }
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), candidate,
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertEqual(audit["decision"], "PASS")
        q1 = audit["questions"]["q1_bias"]
        self.assertTrue(q1["conflict"])
        self.assertTrue(q1["transition_confirmed"])
        self.assertEqual(
            q1["authority"], "FAST_TRANSITION_BIAS_ALIGNMENT_BYPASS"
        )

    def test_incomplete_transition_cannot_bypass_bias_alignment(self):
        candidate = result(intent="POSITION_BUILD", consumed=0.20)
        candidate["side"] = "LONG"
        candidate["ignition"]["bias_snapshot"]["direction"] = "SHORT"
        candidate["ignition"]["transition_confirmed"] = True
        candidate["ignition"]["transition_authority"] = {
            "status": "NEW_SIDE_CONVERTS", "side": "LONG",
            "cash_synchronous_transition": False,
            "hard_contradiction": False,
        }
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), candidate,
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertEqual(audit["decision"], "WAIT")
        self.assertIn("BIAS_THESIS_FAIL", audit["blocking_reasons"])

    def test_persistent_fading_soft_waits_without_hard_veto(self):
        candidate = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "FADING", "coinbase_spot": "UNKNOWN",
            },
        )
        allowed, report = entry_edge_tier.authorize(candidate, SimpleNamespace(
            entry_economics_v3_replay_approved=False,
            wstrade_live_armed=False,
        ))
        self.assertFalse(allowed)
        self.assertIn(
            "WAIT_PERSISTENT_FLOW_FADING", report["soft_wait_reasons"]
        )
        self.assertNotIn(
            "WAIT_PERSISTENT_FLOW_FADING", report["hard_vetoes"]
        )

    def test_reacceleration_waits_unless_independent_cash_confirms(self):
        ambiguous = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "REACCELERATION_UNCONFIRMED",
                "coinbase_spot": "UNKNOWN",
            },
        )
        allowed, report = entry_edge_tier.authorize(
            ambiguous,
            SimpleNamespace(
                entry_economics_v3_replay_approved=False,
                wstrade_live_armed=False,
            ),
        )
        self.assertFalse(allowed)
        self.assertIn(
            "WAIT_PERSISTENT_REACCELERATION_CONFIRMATION",
            report["soft_wait_reasons"],
        )

        corroborated = result(
            intent="POSITION_BUILD", consumed=0.20,
            flow_states={
                "binance_spot": "REACCELERATION_UNCONFIRMED",
                "coinbase_spot": "CONTINUING_CONFIRMED",
            },
        )
        allowed, report = entry_edge_tier.authorize(
            corroborated,
            SimpleNamespace(
                entry_economics_v3_replay_approved=False,
                wstrade_live_armed=False,
            ),
        )
        self.assertTrue(allowed)
        self.assertEqual(report["soft_wait_reasons"], [])
        self.assertEqual(
            report["entry_thesis_audit"]["questions"][
                "q3_flow_efficiency"
            ]["status"],
            "CONTINUING_CONFIRMED",
        )
        flow = report["entry_thesis_audit"]["questions"][
            "q3_flow_efficiency"
        ]
        self.assertEqual(
            flow["confirmation_source"], "INDEPENDENT_CASH_WITNESS"
        )
        self.assertEqual(flow["independent_witness_venues"], ["coinbase_spot"])

    def test_v3_absorption_is_telemetry_until_canonical_replay_is_approved(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(entry_economics_v3_replay_approved=False),
            result(
                intent="POSITION_BUILD", consumed=0.20,
                flow_states={
                    "binance_spot": "ABSORBED",
                    "coinbase_spot": "DECAYING",
                },
            ),
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertEqual(audit["decision"], "PASS")
        self.assertNotIn("FLOW_EFFICIENCY_V3_VETO", audit["blocking_reasons"])

    def test_position_build_is_not_relabelled_forced_unwind(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), result(intent="POSITION_BUILD"),
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        self.assertFalse(audit["forced_unwind_tail"])
        self.assertNotIn("UNWIND_TAIL_VETO", audit["blocking_reasons"])

    def test_shared_wave_reconstruction_cannot_reset_consumed_lower(self):
        candidate = result(consumed=0.20, recent_progress=0.20)
        phase = candidate["ignition"]["phase_measurement"]
        phase["cash_displacement_bps"] = 4.0
        phase["precursor_cash_displacement_bps"] = 4.0
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), candidate,
            PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION,
        )
        maturity = audit["questions"]["q5_maturity"]
        self.assertEqual(maturity["status"], "MATURE")
        self.assertEqual(maturity["shared_wave_consumed"], 0.4)
        self.assertTrue(maturity["consumed_reset_mismatch"])

    def test_depth_research_is_never_claimed_as_entry_authority(self):
        audit = entry_thesis_gate.evaluate(
            SimpleNamespace(), result(), PASS_IMPACT, PASS_BASIS, NO_LIQUIDATION
        )
        liquidity = audit["questions"]["q4_liquidity"]
        self.assertFalse(liquidity["depth_authority"])
        self.assertEqual(liquidity["liquidity_response"], "RECORDER_RESEARCH_ONLY")

    def test_edge_authority_receives_composite_unwind_veto(self):
        state = SimpleNamespace(
            bias_state="SHORT", wstrade_live_armed=False,
            mainnet_commission_verified=True, mainnet_maker_fee_bps=2.0,
            mainnet_taker_fee_bps=5.0, wstrade_promotion={},
        )
        costs = {
            "total_cost_bps": 8.0, "minimum_net_edge_bps": 2.0,
            "commission_verified": True, "commission_source": "TEST",
            "execution_style": "TAKER",
        }
        calibration = {
            "samples": 0, "status": "INSUFFICIENT_DATA", "level": "NONE",
            "live_empirical_ok": False,
        }
        with patch.object(entry_edge_tier.micro, "price_impact", return_value=PASS_IMPACT), \
             patch.object(entry_edge_tier.micro, "spot_perp_basis", return_value=PASS_BASIS), \
             patch.object(entry_edge_tier.regime_engine, "classify", return_value={"regime": "TREND"}), \
             patch.object(entry_edge_tier.verified_cost_model, "estimate", return_value=costs), \
             patch.object(entry_edge_tier.verified_cost_model, "freeze_execution_cost_contract", return_value={}), \
             patch.object(entry_edge_tier.edge_calibration_v2, "factor", return_value=calibration), \
             patch.object(entry_edge_tier.liquidation_context, "assess_entry", return_value=NO_LIQUIDATION):
            allowed, report = entry_edge_tier.authorize(result(), state)
        self.assertFalse(allowed)
        self.assertIn("UNWIND_TAIL_VETO", report["hard_vetoes"])
        self.assertEqual(report["entry_thesis_audit"]["decision"], "WAIT")
        self.assertEqual(
            report["execution_urgency"]["status"],
            "EXECUTION_URGENCY_UNVERIFIED",
        )
        self.assertFalse(report["execution_urgency"]["authority"])


if __name__ == "__main__":
    unittest.main()
