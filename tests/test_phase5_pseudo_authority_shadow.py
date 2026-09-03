import unittest

from loi_he_thong import phase5_pseudo_authority_shadow as phase5


def replay_report(**overrides):
    base = {
        "wal_identity": "wal-1",
        "candidate_population_hash": "population-1",
        "causal_wave_set_hash": "waves-1",
        "frozen_cost_hash": "cost-1",
        "guardian_version": "guardian-current",
        "fill_model_version": "fill-current",
        "causal_wave_matched": True,
        "executable_fill_replayed": True,
        "frozen_cost_applied": True,
        "guardian_replayed": True,
        "feed_valid": True,
        "no_lookahead": True,
        "deterministic_hash": "same",
        "repeat_hash": "same",
        "guardian_net_bps_total": 10.0,
        "economic_miss_count": 2,
        "false_entry_count": 1,
        "guardian_capture_ratio": 0.80,
        "hard_stop_rate": 0.05,
    }
    base.update(overrides)
    return base


def phase4_ready(**overrides):
    value = {
        "shared_thesis_trace_deterministic": True,
        "completed_positions_with_sealed_thesis": 2,
        "same_wal_guardian_comparison": True,
        "capture_and_hard_stop_quality_proven": True,
    }
    value.update(overrides)
    return value


class Phase5RegistryTests(unittest.TestCase):
    def test_registry_contains_nine_independent_pseudo_rules(self):
        expected = {
            "guardian_pseudo_confidence",
            "microstructure_regime_multiplier",
            "displacement_dominance_label",
            "bias_weighted_support",
            "dual_venue_corroboration",
            "oi_identity_inference",
            "fixed_execution_priors",
            "duplicate_entry_causal_validators",
            "auto_promotion_runtime_authority",
        }
        self.assertEqual(set(phase5.validate_registry()), expected)
        for row in phase5.registry().values():
            self.assertIn(row["evidence_state"], {"UNKNOWN", "PRIOR_ONLY"})

    def test_phase4_gate_is_fail_closed(self):
        result = phase5.phase4_gate_status({
            "shared_thesis_trace_deterministic": True,
        })
        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["same_wal_guardian_comparison"])
        self.assertFalse(result["checks"]["capture_and_hard_stop_quality_proven"])


class Phase5ShadowAblationTests(unittest.TestCase):
    def test_shadow_ablation_never_mutates_active_decision(self):
        active = {"decision": "GO", "reason": "ACTIVE"}
        original = dict(active)
        record = phase5.build_shadow_ablation(
            "microstructure_regime_multiplier",
            active_rule_result=active,
            shadow_disabled_result={"decision": "WAIT", "reason": "DISABLED"},
            baseline_report=replay_report(),
            counterfactual_report=replay_report(),
            phase4_evidence=phase4_ready(),
        )
        self.assertEqual(active, original)
        self.assertEqual(record["ablation"]["active_rule_result"]["decision"], "GO")
        self.assertEqual(
            record["ablation"]["shadow_disabled_result"]["decision"], "WAIT"
        )
        self.assertFalse(record["authority_changed"])
        self.assertFalse(record["runtime_mutation_allowed"])
        self.assertFalse(record["promotion_allowed"])

    def test_phase4_block_keeps_complete_comparison_prior_only(self):
        record = phase5.build_shadow_ablation(
            "guardian_pseudo_confidence",
            active_rule_result={"decision": "EXIT"},
            shadow_disabled_result={"decision": "HOLD"},
            baseline_report=replay_report(),
            counterfactual_report=replay_report(),
            phase4_evidence={
                "shared_thesis_trace_deterministic": True,
                "completed_positions_with_sealed_thesis": 0,
                "same_wal_guardian_comparison": False,
                "capture_and_hard_stop_quality_proven": False,
            },
        )
        self.assertEqual(record["evidence_state"], "PRIOR_ONLY")
        self.assertIn("PHASE4_GATE_NOT_SATISFIED", record["blockers"])
        self.assertEqual(record["removal_recommendation"], "NOT_ELIGIBLE")

    def test_mismatched_wal_is_unknown_not_evidence(self):
        record = phase5.build_shadow_ablation(
            "bias_weighted_support",
            active_rule_result={"decision": "GO"},
            shadow_disabled_result={"decision": "WAIT"},
            baseline_report=replay_report(wal_identity="wal-a"),
            counterfactual_report=replay_report(wal_identity="wal-b"),
            phase4_evidence=phase4_ready(),
        )
        self.assertEqual(record["evidence_state"], "UNKNOWN")
        self.assertIn("MISMATCHED_COMPARABILITY:wal_identity", record["blockers"])

    def test_mfe_or_chart_only_cannot_satisfy_outcome_evidence(self):
        baseline = replay_report()
        counterfactual = replay_report()
        for field in (
            "guardian_net_bps_total", "economic_miss_count", "false_entry_count",
            "guardian_capture_ratio", "hard_stop_rate",
        ):
            baseline.pop(field)
            counterfactual.pop(field)
        baseline["mfe_bps"] = 50.0
        counterfactual["mfe_bps"] = 70.0
        record = phase5.build_shadow_ablation(
            "fixed_execution_priors",
            active_rule_result={"decision": "WAIT"},
            shadow_disabled_result={"decision": "GO"},
            baseline_report=baseline,
            counterfactual_report=counterfactual,
            phase4_evidence=phase4_ready(),
        )
        self.assertEqual(record["evidence_state"], "PRIOR_ONLY")
        self.assertTrue(any(x.startswith("MISSING_OUTCOME:") for x in record["blockers"]))
        self.assertFalse(record["promotion_allowed"])

    def test_each_rule_can_be_abated_independently(self):
        records = []
        for rule_id in phase5.validate_registry():
            record = phase5.build_shadow_ablation(
                rule_id,
                active_rule_result={"decision": "WAIT", "rule": rule_id},
                shadow_disabled_result={"decision": "WAIT", "rule": rule_id},
                baseline_report=replay_report(),
                counterfactual_report=replay_report(),
                phase4_evidence=phase4_ready(),
            )
            records.append(record)
            self.assertEqual(record["rule_id"], rule_id)
            self.assertEqual(record["evidence_state"], "SHADOW_MEASURED_REVIEW_REQUIRED")
            self.assertFalse(record["authority_changed"])
            self.assertFalse(record["promotion_allowed"])
        self.assertEqual(len(records), 9)


if __name__ == "__main__":
    unittest.main()
