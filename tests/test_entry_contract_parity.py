import unittest
from unittest.mock import patch
from types import SimpleNamespace

import mainnet_tier_s_shadow_launcher as launcher
_base_entry_quorum_ok = launcher._entry_quorum_ok
import mainnet_tier_s_shadow_risk_launcher as active_launcher
from loi_he_thong import entry_edge_tier
from loi_he_thong import ignition_core


def frozen_result(mode="IGNITION", proof="METAORDER_CONTINUATION"):
    payload = {
        "causal_episode_id": "contract-1",
        "state": "PROVE",
        "side": "LONG",
        "proposer": "binance_spot",
        "proof_type": proof,
        "proof_venue": "binance_spot",
        "cash_venues": ["binance_spot"],
        "futures_follow_ok": True,
        "current_cash_conversion": {
            "confirmed": True,
            "accepted_cash_venues": ["binance_spot"],
            "venues": {
                "binance_spot": {
                    "receive_time_ms": 1_000,
                    "epoch": 1,
                    "imbalance": 0.5,
                    "price_conversion_bps": 0.3,
                },
            },
        },
        "consumed_fraction": 0.2,
        "bias_snapshot": {
            "direction": "LONG", "confidence": 0.7, "updated_at": 0.0,
        },
    }
    basis, dependencies, proof_hash = ignition_core._freeze_authority_proof(
        payload, "LONG", proof, "contract-1",
    )
    payload.update({
        "authority_basis": basis,
        "authority_dependencies": dependencies,
        "authority_proof_hash": proof_hash,
    })
    return {
        "decision": "GO",
        "side": "LONG",
        "entry_mode": mode,
        "execution_policy": "TAKER",
        "phase": "RELEASE",
        "causal_episode_id": "contract-1",
        "authority_basis": basis,
        "authority_dependencies": dependencies,
        "authority_proof_hash": proof_hash,
        "ignition": payload,
    }


class EntryContractParityTests(unittest.TestCase):
    def test_blocking_stage_uses_canonical_gate_owner(self):
        wait = {"decision": "WAIT", "reason": "BIAS_ABSTAIN"}
        gate = {
            "allowed": False, "owner": "MARKET_TRUTH",
            "stage": "MARKET_TRUTH", "reason": "BIAS_ABSTAIN",
        }
        self.assertEqual(
            launcher._blocking_stage(wait, False, gate), "MARKET_TRUTH",
        )

    def test_timing_retry_block_is_not_mislabeled_structural(self):
        state = SimpleNamespace(
            entry_gate_outcome={"allowed": True, "reason": "PASS"},
            entry_timing_attempt_status="EXPIRED",
        )
        with patch.object(
            launcher.entry_lifecycle, "can_create_attempt",
            return_value=(False, "STALE_PROOF_REUSE"),
        ):
            result, allowed, gate, blocked = launcher._apply_timing_retry_gate(
                state, {"decision": "GO", "reason": "PASS"}, True,
            )
        self.assertTrue(blocked)
        self.assertFalse(allowed)
        self.assertEqual(result["decision"], "WAIT")
        self.assertEqual(gate["owner"], "TIMING")
        self.assertEqual(gate["stage"], "TIMING_ATTEMPT_GATE")

    def test_non_go_wait_never_enters_structural_validator(self):
        result = {"decision": "WAIT", "reason": "BIAS_NOT_READY"}
        state = SimpleNamespace(wstrade_live_armed=False)
        with patch.object(
            active_launcher.base.entry_council,
            "validate_frozen_entry_contract",
        ) as validator, patch.object(
            active_launcher.edge, "authorize",
        ) as authorize:
            self.assertFalse(
                active_launcher._entry_quorum_ok(result, state, 1.0)
            )
        validator.assert_not_called()
        authorize.assert_not_called()
        self.assertEqual(state.entry_gate_outcome["owner"], "MARKET_TRUTH")
        self.assertEqual(
            state.entry_structural_contract["reason"],
            "NOT_APPLICABLE_UNTIL_GO",
        )

    def test_wait_clears_previous_decision_economics(self):
        state = SimpleNamespace(wstrade_live_armed=False)
        with patch.object(
            active_launcher.base.entry_council,
            "validate_frozen_entry_contract",
            return_value=(True, "PASS", {}),
        ), patch.object(
            active_launcher.edge, "authorize",
            return_value=(True, {
                "edge_class": "RESIDUAL_POSITIVE",
                "cost_ok": True,
                "execution_cost_contract": {"contract_id": "wave-a"},
            }),
        ):
            self.assertTrue(active_launcher._entry_quorum_ok(
                {"decision": "GO", "ignition": {}}, state, 1.0,
            ))
        self.assertEqual(state.entry_edge_class, "RESIDUAL_POSITIVE")

        self.assertFalse(active_launcher._entry_quorum_ok(
            {"decision": "WAIT", "reason": "BIAS_NOT_READY"}, state, 2.0,
        ))
        self.assertEqual(state.entry_edge_tier, {})
        self.assertIsNone(state.entry_edge_class)
        self.assertIsNone(state.entry_edge_cost_ok)
        self.assertEqual(state.entry_edge_updated_at, 0.0)
        self.assertEqual(state.entry_tier_s_volume_quality, {})

    def test_active_runtime_does_not_reinterpret_validated_ignition(self):
        result = {"decision": "GO", "ignition": {}}
        state = SimpleNamespace(wstrade_live_armed=False)
        with patch.object(
            active_launcher.base.entry_council,
            "validate_frozen_entry_contract",
            return_value=(True, "PASS", {}),
        ), patch.object(
            active_launcher.edge, "authorize",
            return_value=(True, {
                "edge_class": "RESIDUAL_POSITIVE",
                "cost_ok": True,
            }),
        ):
            self.assertTrue(
                active_launcher._entry_quorum_ok(result, state, 1.0)
            )
        self.assertEqual(state.entry_gate_outcome["reason"], "PASS")
        self.assertEqual(state.entry_gate_outcome["owner"], "ACTION")

    def test_active_runtime_preserves_timing_owner_from_edge(self):
        result = frozen_result(
            mode="PERSISTENT_METAORDER", proof="PERSISTENT_METAORDER",
        )
        state = SimpleNamespace(wstrade_live_armed=False)
        report = {
            "edge_class": "WAIT_EVIDENCE",
            "cost_ok": False,
            "hard_vetoes": [],
            "soft_wait_reasons": ["WAIT_PERSISTENT_FLOW_EFFICIENCY"],
        }
        with patch.object(
            active_launcher.base.entry_council,
            "validate_frozen_entry_contract",
            return_value=(True, "PASS", {}),
        ), patch.object(
            active_launcher.edge, "authorize", return_value=(False, report),
        ):
            self.assertFalse(
                active_launcher._entry_quorum_ok(result, state, 1.0)
            )
        self.assertEqual(state.entry_gate_outcome["owner"], "TIMING")
        self.assertEqual(
            state.entry_gate_outcome["reason"],
            "WAIT_PERSISTENT_FLOW_EFFICIENCY",
        )

    def test_active_runtime_enforces_live_contract_before_economics(self):
        result = frozen_result()
        state = SimpleNamespace(wstrade_live_armed=True)
        with patch.object(
            active_launcher.base.entry_council,
            "validate_frozen_entry_contract",
            return_value=(
                False,
                "ACQUISITION_HANDOFF_LIVE_AUTHORITY_DISABLED",
                {"entry_mode": "ACQUISITION_HANDOFF"},
            ),
        ) as validator, patch.object(
            active_launcher.edge, "authorize",
        ) as authorize:
            self.assertFalse(active_launcher._entry_quorum_ok(result, state, 1.0))

        validator.assert_called_once_with(
            result, authority_scope="LIVE", require_authority=True,
        )
        authorize.assert_not_called()
        self.assertEqual(
            state.entry_structural_contract["reason"],
            "ACQUISITION_HANDOFF_LIVE_AUTHORITY_DISABLED",
        )

    def test_launcher_and_edge_accept_same_frozen_ignition_proof(self):
        result = frozen_result()
        state = SimpleNamespace(wstrade_live_armed=False)
        self.assertTrue(_base_entry_quorum_ok(result, state, 1.0))
        self.assertTrue(entry_edge_tier.normal_contract_ok(result))
        self.assertEqual(state.entry_structural_contract["reason"], "PASS")

    def test_persistent_is_shadow_bootstrap_but_never_live_authority(self):
        result = frozen_result(
            mode="PERSISTENT_METAORDER", proof="PERSISTENT_METAORDER",
        )
        shadow = SimpleNamespace(wstrade_live_armed=False)
        live = SimpleNamespace(wstrade_live_armed=True)
        self.assertTrue(_base_entry_quorum_ok(result, shadow, 1.0))
        self.assertTrue(entry_edge_tier.normal_contract_ok(result))
        self.assertFalse(_base_entry_quorum_ok(result, live, 1.0))
        self.assertEqual(
            live.entry_structural_contract["reason"],
            "PERSISTENT_METAORDER_LIVE_AUTHORITY_DISABLED",
        )

    def test_proof_name_drift_fails_both_consumers(self):
        result = frozen_result()
        result["ignition"] = dict(result["ignition"])
        result["ignition"]["proof_type"] = "PERSISTENT_METAORDER"
        state = SimpleNamespace(wstrade_live_armed=False)
        self.assertFalse(_base_entry_quorum_ok(result, state, 1.0))
        self.assertFalse(entry_edge_tier.normal_contract_ok(result))

    def test_frozen_execution_policy_is_not_inferred_from_phase(self):
        result = frozen_result()
        result["phase"] = "RELEASE"
        result["execution_policy"] = "MAKER"
        valid, reason, detail = ignition_core.validate_frozen_entry_contract(
            result, authority_scope="SHADOW", require_authority=True,
        )
        self.assertTrue(valid, reason)
        self.assertEqual(detail["execution_policy"], "MAKER")

        result["execution_policy"] = ""
        valid, reason, _detail = ignition_core.validate_frozen_entry_contract(
            result, authority_scope="SHADOW", require_authority=True,
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "FROZEN_EXECUTION_POLICY_INVALID")

    def test_timing_retry_wait_is_sealed_as_wait_action(self):
        result = frozen_result()
        state = SimpleNamespace(
            entry_timing_attempt_status="EXPIRED",
            entry_gate_outcome={"allowed": True, "reason": "PASS"},
            wstrade_live_armed=False,
            execution_allowed=False,
        )
        with patch.object(
            launcher.entry_lifecycle, "can_create_attempt",
            return_value=(False, "PROOF_ALREADY_USED"),
        ):
            final, quorum_ok, gate, blocked = (
                launcher._apply_timing_retry_gate(state, result, True)
            )
        self.assertTrue(blocked)
        self.assertFalse(quorum_ok)
        self.assertEqual(final["decision"], "WAIT")
        self.assertEqual(gate["reason"], "TIMING_RETRY_BLOCKED")

        bundle = launcher._authority_contract_bundle(
            state, final, quorum_ok, final["causal_episode_id"],
        )
        self.assertEqual(
            bundle["contracts"]["ACTION"]["action"],
            "WAIT_INFORMATION",
        )


if __name__ == "__main__":
    unittest.main()
