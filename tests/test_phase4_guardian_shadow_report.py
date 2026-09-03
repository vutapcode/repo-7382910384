import copy
import unittest

from loi_he_thong import authority_contracts, market_thesis
from ops import phase4_guardian_shadow_report as report


def _bundle(truth):
    episode = truth["causal_episode_id"]
    return authority_contracts.bundle(
        truth,
        authority_contracts.seal(
            "ACTION", "ENTRY_ACTION_POLICY", episode,
            {"action": "ACT_TAKER_NOW"},
        ),
        authority_contracts.seal(
            "EXECUTION", "EXECUTION_REVALIDATION", episode,
            {"execution_action": "EXECUTE"},
        ),
        authority_contracts.seal(
            "SAFETY", "MAINNET_SAFETY", episode,
            {"safety_state": "SAFE"},
        ),
    )


class Phase4GuardianReportTests(unittest.TestCase):
    def test_empty_evidence_fails_closed(self):
        result = report.build_report([])
        self.assertEqual(result["cutover_decision"], "KEEP_LEGACY_GUARDIAN")
        self.assertIn("NO_SHARED_THESIS_SHADOW_ROWS", result["blockers"])
        self.assertIn(
            "EXECUTABLE_COUNTERFACTUAL_GUARDIAN_REPLAY_REQUIRED",
            result["blockers"],
        )

    def test_recomputes_same_frozen_truth_without_poisoning_safety_exit(self):
        source = {
            "decision": "GO", "reason": "IGNITION_PROVED", "side": "LONG",
            "causal_episode_id": "episode-report", "ignition": {
                "side": "LONG", "causal_episode_id": "episode-report",
                "proof_type": "PERSISTENT_METAORDER",
                "proposer": "binance_spot",
                "cash_venues": ["binance_spot", "coinbase_spot"],
            },
        }
        truth = market_thesis.build(source)
        canonical = {
            "version": "GUARDIAN_CANONICAL_OBSERVATION_V1",
            "causal_episode_id": "episode-report", "position_side": "LONG",
            "source_health": {
                "spot": "FRESH", "coinbase": "FRESH", "futures": "FRESH",
            },
            "price_horizons": {"3.0": {"threshold_bps": 1.5, "moves": {
                "spot": -2.0, "coinbase": -2.0, "futures": -2.0,
            }}},
            "flow_signed_imbalances": {
                "spot": -0.4, "coinbase": -0.4, "futures": -0.4,
            },
            "oi": {"status": "NEUTRAL", "fresh": True},
            "gap_or_epoch_invalid": False,
        }
        observed = market_thesis.observe(truth, canonical)
        bundle = _bundle(truth)
        rows = [{
            "available_time_ms": 1_000, "event_time_ms": 1_000,
            "code_version": "code", "config_version": "config",
            "payload": {
                "event": "POSITION_STATE", "cycle_id": "position-1",
                "causal_episode_id": "episode-report",
                "authority_contracts": bundle,
                "guardian_state": {
                    "decision": "HOLD",
                    "canonical_thesis_event": canonical,
                    "shared_thesis_observation": observed,
                    "shared_thesis_shadow": {
                        "version": "GUARDIAN_SHARED_THESIS_SHADOW_V1",
                        "decision": "EXIT", "authority": False,
                    },
                },
            },
        }]
        first = report.build_report(copy.deepcopy(rows))
        second = report.build_report(copy.deepcopy(rows))
        self.assertEqual(first["report_hash"], second["report_hash"])
        self.assertEqual(first["determinism"]["recompute_failures"], 0)
        self.assertEqual(
            first["determinism"]["recorded_observation_mismatches"], 0,
        )
        self.assertEqual(first["status_counts"], {"CONTROL_TRANSFER": 1})
        self.assertEqual(first["decision_pairs"], {"HOLD->EXIT": 1})
        self.assertEqual(first["cutover_decision"], "KEEP_LEGACY_GUARDIAN")


if __name__ == "__main__":
    unittest.main()
