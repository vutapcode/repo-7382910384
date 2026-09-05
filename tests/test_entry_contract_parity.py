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


if __name__ == "__main__":
    unittest.main()
