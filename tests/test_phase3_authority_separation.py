import copy
import math
import unittest
from types import SimpleNamespace

import mainnet_tier_s_shadow_launcher as launcher
from loi_he_thong import authority_contracts
from loi_he_thong import execution_causal_revalidation
from loi_he_thong import mainnet_safety
from loi_he_thong import market_thesis
from recorder.replay import DeterministicReplay


def result(decision="GO", reason="IGNITION_PROVED", policy="TAKER"):
    return {
        "decision": decision,
        "reason": reason,
        "side": "LONG",
        "execution_policy": policy,
        "causal_episode_id": "episode-3",
        "authority_basis": "BIAS_ALIGNED",
        "ignition": {
            "causal_episode_id": "episode-3",
            "side": "LONG",
            "proof_type": "PERSISTENT_METAORDER",
            "proposer": "binance_spot",
            "cash_venues": ["binance_spot", "coinbase_spot"],
            "oi_verification_state": {"status": "UNCHANGED_UNKNOWN"},
            "clock_quality": {
                "binance_spot": {
                    "source_health": "FRESH", "epoch": 2,
                    "temporal_uncertainty_ms": 12.0,
                },
            },
        },
    }


class Phase3AuthoritySeparationTests(unittest.TestCase):
    def test_contract_hash_detects_downstream_rewrite(self):
        contract = authority_contracts.seal(
            "ACTION", "OWNER", "episode-3", {"action": "WAIT_INFORMATION"}
        )
        self.assertTrue(authority_contracts.verify(contract))
        contract["action"] = "ACT_TAKER_NOW"
        self.assertFalse(authority_contracts.verify(contract))

    def test_contract_telemetry_cannot_crash_decision_on_bad_measurement(self):
        contract = authority_contracts.seal(
            "MARKET_TRUTH", "OWNER", "episode-3",
            {"measurement": math.nan},
        )
        self.assertEqual(contract["measurement"], "NON_FINITE_MEASUREMENT")
        self.assertTrue(authority_contracts.verify(contract))

    def test_market_truth_taxonomy_does_not_emit_other_owner_actions(self):
        supported = market_thesis.build(result())
        self.assertEqual(supported["status"], "SUPPORTED")
        self.assertEqual(supported["knowledge_state"], "SUPPORTED")
        self.assertIn(
            "DUAL_CASH_CROSS_VENUE_CORROBORATION",
            supported["supporting_evidence"],
        )
        for forbidden in ("action", "execution_action", "safety_action"):
            self.assertNotIn(forbidden, supported)

        source_unknown = market_thesis.build(result(
            decision="WAIT", reason="SHADOW_FEED_NOT_READY",
        ))
        self.assertEqual(source_unknown["status"], "UNKNOWN")
        self.assertEqual(source_unknown["knowledge_state"], "UNKNOWN_SOURCE")

        contradicted = market_thesis.build(result(
            decision="WAIT", reason="PRICE_AND_FLOW_OPPOSE",
        ))
        self.assertEqual(contradicted["status"], "DIVERGING")
        self.assertEqual(contradicted["knowledge_state"], "CONTRADICTED")

        falsified_input = result(decision="WAIT", reason="EXPLICIT_BREAK")
        falsified_input["market_truth_status"] = "FALSIFIED"
        falsified = market_thesis.build(falsified_input)
        self.assertEqual(falsified["status"], "FALSIFIED")
        self.assertEqual(falsified["knowledge_state"], "FALSIFIED")

    def test_action_owner_maps_existing_decision_without_mutating_it(self):
        candidate = result()
        before = copy.deepcopy(candidate)
        action = launcher._action_contract(candidate, True, "episode-3")
        self.assertEqual(action["action"], "ACT_TAKER_NOW")
        self.assertEqual(candidate, before)
        self.assertNotIn("status", action)
        self.assertNotIn("safety_action", action)
        wait = launcher._action_contract(
            result(decision="WAIT", reason="WAIT_IGNITION"),
            False, "episode-3",
        )
        self.assertEqual(wait["action"], "WAIT_INFORMATION")

    def test_execution_owner_does_not_rewrite_market_thesis(self):
        execution = execution_causal_revalidation.execution_contract(
            False, "POST_PROOF_CASH_PRICE_FLOW_REVERSAL", "episode-3"
        )
        self.assertEqual(execution["execution_action"], "CANCEL")
        self.assertNotIn("status", execution)
        self.assertNotIn("safety_state", execution)

    def test_source_unknown_and_system_unsafe_are_distinct(self):
        missing = SimpleNamespace(mainnet_shadow_health={
            "spot_price": True, "coinbase_price": False,
            "futures_price": True, "spot_flow": True, "futures_flow": True,
            "operational_blockers": [],
        })
        source = mainnet_safety.safety_contract(missing, "episode-3")
        self.assertEqual(source["safety_state"], "UNKNOWN_SOURCE")
        self.assertNotIn("status", source)
        unsafe = SimpleNamespace(mainnet_shadow_health={
            "spot_price": True, "coinbase_price": True,
            "futures_price": True, "spot_flow": True, "futures_flow": True,
            "operational_blockers": ["journal_stalled"],
        })
        system = mainnet_safety.safety_contract(unsafe, "episode-3")
        self.assertEqual(system["safety_state"], "SYSTEM_UNSAFE")
        self.assertEqual(system["safety_action"], "SEAL_NEW_ENTRY")

    def test_bundle_requires_four_owners_and_one_episode(self):
        state = SimpleNamespace(mainnet_shadow_health={})
        candidate = result()
        before = copy.deepcopy(candidate)
        bundle = launcher._authority_contract_bundle(
            state, candidate, True, "episode-3"
        )
        self.assertTrue(authority_contracts.verify_bundle(bundle))
        self.assertEqual(
            set(bundle["contracts"]), authority_contracts.LAYERS
        )
        self.assertEqual(candidate, before)
        self.assertEqual(
            bundle["contracts"]["MARKET_TRUTH"]["status"], "SUPPORTED"
        )
        self.assertEqual(
            bundle["contracts"]["SAFETY"]["safety_state"], "UNKNOWN_SOURCE"
        )
        with self.assertRaisesRegex(ValueError, "CAUSAL_EPISODE_ID_MISMATCH"):
            authority_contracts.bundle(
                bundle["contracts"]["MARKET_TRUTH"],
                bundle["contracts"]["ACTION"],
                execution_causal_revalidation.pending_contract("other"),
                bundle["contracts"]["SAFETY"],
            )

    def test_legacy_reader_never_promotes_old_fields(self):
        legacy = authority_contracts.read_journal_bundle({
            "decision": "GO",
            "entry_causal_thesis": {"market_thesis": {"status": "SUPPORTED"}},
        })
        self.assertTrue(legacy["compatibility_only"])
        self.assertFalse(legacy["authority_eligible"])
        self.assertIsNone(legacy["bundle"])

    def test_replay_distinguishes_valid_contracts_from_legacy_fields(self):
        state = SimpleNamespace(mainnet_shadow_health={})
        bundle = launcher._authority_contract_bundle(
            state, result(), True, "episode-3"
        )
        rows = [
            {
                "stream": "bot_event", "event_time_ms": 1_000,
                "receive_time_ms": 1_000, "available_time_ms": 1_000,
                "payload": {
                    "event": "DECISION_EVALUATED",
                    "authority_contracts": bundle,
                },
            },
            {
                "stream": "bot_event", "event_time_ms": 1_001,
                "receive_time_ms": 1_001, "available_time_ms": 1_001,
                "payload": {"event": "ENTRY", "decision": "GO"},
            },
        ]
        summary = DeterministicReplay().run(rows)
        self.assertEqual(summary["authority_contract_rows"], 1)
        self.assertEqual(summary["authority_contract_invalid"], 0)
        self.assertEqual(summary["legacy_authority_rows"], 1)

    def test_guardian_observation_cannot_mutate_entry_truth(self):
        truth = market_thesis.build(result())
        original = copy.deepcopy(truth)
        position = SimpleNamespace(
            side="LONG",
            entry_causal_thesis={
                "market_thesis": truth,
                "primary_cash_anchor": "spot",
                "cash_anchors": ["spot", "coinbase"],
            },
        )
        state = SimpleNamespace(thoi_gian_coinbase_ticker_cuoi=100.0)
        launcher.guardian_s._entry_thesis_break(
            state, position, 100.0,
            {"metrics": {"horizons": {}}},
            {"metrics": {"venues": []}},
            {"status": "NEUTRAL"},
        )
        self.assertEqual(truth, original)
        self.assertTrue(authority_contracts.verify(truth))

    def test_live_execution_replaces_only_execution_owner(self):
        state = SimpleNamespace(mainnet_shadow_health={})
        candidate = result()
        bundle = launcher._authority_contract_bundle(
            state, candidate, True, "episode-3"
        )
        candidate["authority_contracts"] = bundle
        replacement = execution_causal_revalidation.execution_contract(
            True, "PASS", "episode-3"
        )
        updated = launcher.live_execution._replace_execution_contract(
            candidate, replacement
        )
        self.assertTrue(authority_contracts.verify_bundle(updated))
        for layer in ("MARKET_TRUTH", "ACTION", "SAFETY"):
            self.assertEqual(
                updated["contracts"][layer]["contract_hash"],
                bundle["contracts"][layer]["contract_hash"],
            )
        self.assertEqual(
            updated["contracts"]["EXECUTION"]["execution_action"], "EXECUTE"
        )


if __name__ == "__main__":
    unittest.main()
