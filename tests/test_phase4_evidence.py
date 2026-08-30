import unittest

from loi_he_thong import causal_threshold_registry as registry
from loi_he_thong import decision_boundary_evidence
from loi_he_thong import entry_thesis_gate
from loi_he_thong import execution_causal_revalidation
from loi_he_thong import ignition_core
from loi_he_thong import verified_cost_model
from ops import phase4_promotion_manifest


class ThresholdRegistryTests(unittest.TestCase):
    def test_every_threshold_has_question_owner_and_valid_class(self):
        owners = registry.validate()
        self.assertIn("ignition_core", owners)
        self.assertIn("hard_risk", owners)
        self.assertTrue(set(registry.snapshot()))

    def test_shared_active_contracts_do_not_drift(self):
        self.assertEqual(registry.value("bias.minimum_support"), ignition_core.BIAS_MIN_CONF)
        self.assertEqual(registry.value("bias.minimum_support"), entry_thesis_gate.BIAS_MIN_CONF)
        self.assertEqual(registry.value("ignition.follow_window"), ignition_core.FOLLOW_MAX_MS)
        self.assertEqual(
            registry.value("ignition.follow_window"),
            execution_causal_revalidation.FOLLOW_MAX_MS,
        )
        self.assertEqual(
            registry.value("entry.max_consumed_fraction"),
            ignition_core.MAX_CONSUMED_FRACTION,
        )
        self.assertEqual(
            registry.value("entry.minimum_cash_progress"),
            ignition_core.MATERIAL_PRICE_BPS,
        )
        self.assertEqual(
            registry.value("cost.unverified_fee_per_side"),
            verified_cost_model.DEFAULT_FALLBACK_FEE_BPS_PER_SIDE,
        )


class BoundaryEvidenceTests(unittest.TestCase):
    def test_signed_distance_uses_passing_side_and_preserves_unknown(self):
        result = {
            "bias_confidence": 0.56,
            "ignition": {
                "consumed_fraction": 0.34,
                "futures_response_ms": None,
            },
        }
        edge = {
            "expected_net_bps_model": 8.0,
            "min_net_edge_bps": 6.0,
            "entry_thesis_audit": {"questions": {
                "q1_bias": {"confidence": 0.56},
                "q3_flow_efficiency": {
                    "cash_flow_strength": 0.19,
                    "recent_cash_progress_bps": 0.20,
                    "material_price_bps": 0.15,
                },
                "q5_maturity": {"shared_wave_consumed": 0.34},
            }},
            "spot_perp_basis": {"lead_bps": 2.5},
        }
        rows = decision_boundary_evidence.build(result, edge)["boundaries"]
        self.assertAlmostEqual(rows["bias_support"]["distance_to_boundary"], 0.01)
        self.assertAlmostEqual(rows["flow_imbalance"]["distance_to_boundary"], -0.01)
        self.assertAlmostEqual(rows["wave_consumed"]["distance_to_boundary"], 0.01)
        self.assertAlmostEqual(rows["perp_lead"]["distance_to_boundary"], 0.5)
        self.assertEqual(rows["economic_reserve"]["distance_to_boundary"], 2.0)
        self.assertFalse(rows["futures_follow_age"]["observed"])
        self.assertIsNone(rows["futures_follow_age"]["distance_to_boundary"])


def canonical_report(parameter, value, *, net=10.0, fp=0.10):
    return {
        "strategy_authority": "IGNITION_CORE_V1",
        "wal_identity": "wal-1",
        "candidate_population_hash": "population-1",
        "schema_version": "v3",
        "inference_version": "v2",
        "cost_contract_version": "cost-v1",
        "guardian_version": "guardian-v10",
        "fill_model_version": "fill-v1",
        "authority_parameters": {parameter: value},
        "causal_wave_matched": True,
        "no_lookahead": True,
        "feed_clean": True,
        "executable_fill_replayed": True,
        "guardian_replayed": True,
        "rejected_candidates_adjudicated": True,
        "maker_fill_feasibility_checked": True,
        "candidate_count": 100,
        "guardian_net_bps_total": net,
        "false_positive_rate": fp,
        "deterministic_hash": "repeatable",
        "repeat_hash": "repeatable",
    }


class Phase4ManifestTests(unittest.TestCase):
    def test_rejects_noncanonical_mirror_and_missing_guardian(self):
        baseline = canonical_report("follow_max_ms", 600)
        candidate = canonical_report("follow_max_ms", 700, net=12.0)
        candidate["strategy_authority"] = "RETIRED_WHALE_EXPERIMENT_NON_AUTHORITY"
        candidate["guardian_replayed"] = False
        report = phase4_promotion_manifest.build_manifest(
            baseline, candidate, requested_variable="follow_max_ms",
        )
        self.assertEqual(report["decision"], "REJECT_UNPROVEN")
        self.assertIn("CANDIDATE_NOT_CANONICAL_LIVE_AUTHORITY", report["blockers"])
        self.assertIn(
            "MISSING_CANONICAL_EVIDENCE:guardian_replayed", report["blockers"]
        )

    def test_rejects_more_than_one_changed_authority_variable(self):
        baseline = canonical_report("follow_max_ms", 600)
        baseline["authority_parameters"]["consumed"] = 0.35
        candidate = canonical_report("follow_max_ms", 700, net=12.0)
        candidate["authority_parameters"]["consumed"] = 0.40
        report = phase4_promotion_manifest.build_manifest(baseline, candidate)
        self.assertEqual(report["decision"], "REJECT_UNPROVEN")
        self.assertIn(
            "ABLATION_MUST_CHANGE_EXACTLY_ONE_AUTHORITY_VARIABLE",
            report["blockers"],
        )

    def test_promotes_only_complete_one_variable_net_improvement(self):
        baseline = canonical_report("follow_max_ms", 600)
        candidate = canonical_report("follow_max_ms", 700, net=12.0, fp=0.09)
        first = phase4_promotion_manifest.build_manifest(
            baseline, candidate, requested_variable="follow_max_ms",
        )
        second = phase4_promotion_manifest.build_manifest(
            baseline, candidate, requested_variable="follow_max_ms",
        )
        self.assertEqual(first["decision"], "PROMOTE")
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])


if __name__ == "__main__":
    unittest.main()
