import unittest

from loi_he_thong import forensic_telemetry


class ForensicTelemetryTests(unittest.TestCase):
    def test_wait_separates_unknown_falsified_owner_and_action(self):
        snapshot = {
            "cycle_id": "decision-1",
            "decision_time_ms": 1000,
            "causal_episode_id": "wave-1",
            "timing_attempt_id": "timing-1",
            "economic_opportunity_id": 7,
            "inputs": {
                "open_interest": {"fresh": False},
                "distance_to_boundary": {"boundaries": {
                    "economic_reserve": {
                        "owner": "entry_economics_v2",
                        "question": "Does net edge clear cost?",
                        "observed": True,
                        "observed_value": -1.0,
                        "threshold": 0.0,
                        "distance_to_boundary": -1.0,
                    },
                }},
                "ignition": {"current_execution_proof": {
                    "proof_hash": "proof-1",
                }},
            },
            "output": {
                "decision": "WAIT", "reason": "WAIT_STALE_COINBASE",
                "authorization_status": "BLOCKED",
                "blocking_reason": "WAIT_STALE_COINBASE",
                "blocking_reasons": ["WAIT_STALE_COINBASE"],
                "diagnostic_reasons": ["PRICE_QUORUM_FAIL"],
                "entry_gate_outcome": {
                    "allowed": False, "owner": "TIMING",
                    "stage": "TIMING_NOW", "reason": "WAIT_STALE_COINBASE",
                },
                "cost": {"cost_ok": None},
            },
            "authority_contracts": {
                "version": "FOUR_AUTHORITY_CONTRACTS_V1",
                "bundle_hash": "bundle-1",
                "contracts": {
                    "MARKET_TRUTH": {
                        "owner": "MARKET_THESIS", "status": "SUPPORTED",
                        "contract_hash": "truth-1",
                    },
                },
            },
        }
        dossier = forensic_telemetry.build_decision_dossier(snapshot)
        self.assertEqual(dossier["decision"]["action"], "WAIT")
        self.assertEqual(dossier["blockers"][0]["owner"], "TIMING")
        self.assertEqual(
            dossier["blockers"][0]["epistemic_status"], "UNKNOWN",
        )
        self.assertIn("boundary:economic_reserve", dossier["falsified"])
        self.assertIn("timing_attempt_executable_now", dossier["unknowns"])
        self.assertEqual(
            dossier["ignored_evidence"][0]["disposition"],
            "DIAGNOSTIC_NON_BLOCKING",
        )
        self.assertTrue(any(
            row["ref"] == "proof-1"
            for row in dossier["market_evidence"]["evidence_refs"]
        ))

    def test_go_is_not_reclassified_by_forensic_projection(self):
        snapshot = {
            "cycle_id": "decision-2", "inputs": {},
            "output": {
                "decision": "GO", "reason": "PASS",
                "authorization_status": "AUTHORIZED",
                "blocking_reasons": [],
                "entry_gate_outcome": {"allowed": True, "owner": "ACTION"},
                "cost": {"cost_ok": True},
            },
        }
        dossier = forensic_telemetry.build_decision_dossier(snapshot)
        self.assertEqual(dossier["decision"]["action"], "GO")
        self.assertEqual(dossier["blockers"], [])
        self.assertTrue(dossier["advisory_only"])


if __name__ == "__main__":
    unittest.main()
