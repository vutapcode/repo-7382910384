"""Phase-5 pseudo-authority research contract.

This module is deliberately shadow-only.  It never changes GO/WAIT/EXIT,
Guardian authority, Hard Risk, Mainnet state, or any strategy threshold.  It
exists only to make one-variable ablations comparable once Phase-4 shared-
thesis evidence is available.
"""

from copy import deepcopy


VERSION = "PHASE5_PSEUDO_AUTHORITY_SHADOW_V1"
POLICY = "SHADOW_ONLY_NO_RUNTIME_AUTHORITY"
ABLATION_MODES = ("ACTIVE_RULE", "RULE_SHADOW_DISABLED")

_REQUIRED_RULE_FIELDS = {
    "owner", "consumers", "current_authority", "target_semantics",
    "evidence_state",
}

_RULES = {
    "guardian_pseudo_confidence": {
        "owner": "3_thuc_thi/ve_si_lenh/guardian_s_tier.py",
        "consumers": ("guardian_s_tier state transitions",),
        "current_authority": "ACTIVE_GUARDIAN_INTERNAL",
        "target_semantics": "TIMING_METADATA_ONLY_PENDING_PHASE4_EVIDENCE",
        "evidence_state": "PRIOR_ONLY",
    },
    "microstructure_regime_multiplier": {
        "owner": "loi_he_thong/microstructure_regime.py::classify",
        "consumers": ("entry threshold/cost/expectancy adapters",),
        "current_authority": "ACTIVE_ADAPTIVE_FACTORS",
        "target_semantics": "FEATURE_ONLY_UNTIL_MATCHED_REPLAY_PROOF",
        "evidence_state": "PRIOR_ONLY",
    },
    "displacement_dominance_label": {
        "owner": "loi_he_thong/flow_lead_engine.py::analyze",
        "consumers": ("microstructure_regime", "entry context consumers"),
        "current_authority": "CONTEXT_ONLY_NO_EVENT_ORDERING_AUTHORITY",
        "target_semantics": "DISPLACEMENT_DOMINANCE_NOT_CAUSAL_LEAD",
        "evidence_state": "PRIOR_ONLY",
    },
    "bias_weighted_support": {
        "owner": "2_suy_luan_mapping/bias_council.py",
        "consumers": ("Bias direction support", "MarketThesis bias context"),
        "current_authority": "ACTIVE_SUPPORT_SCORE",
        "target_semantics": "SUPPORT_SCORE_NOT_REALIZED_PROBABILITY",
        "evidence_state": "PRIOR_ONLY",
    },
    "dual_venue_corroboration": {
        "owner": "2_suy_luan_mapping/bias_council.py::flow_family_consensus",
        "consumers": ("Bias", "Ignition/MarketThesis corroboration consumers"),
        "current_authority": "PARTIAL_CAUSAL_FAMILY_DEDUP",
        "target_semantics": "ONE_ROOT_EVIDENCE_ID_COUNTS_ONCE",
        "evidence_state": "PRIOR_ONLY",
    },
    "oi_identity_inference": {
        "owner": "Bias/microstructure OI interpretation",
        "consumers": ("Bias context", "microstructure_regime", "Guardian context"),
        "current_authority": "ACTIVE_CONTEXT_AND_ADAPTATION",
        "target_semantics": "OI_EXPANSION_CONTRACTION_OBSERVATION_ONLY",
        "evidence_state": "PRIOR_ONLY",
    },
    "fixed_execution_priors": {
        "owner": "loi_he_thong/verified_cost_model.py",
        "consumers": ("Entry economics", "frozen cost plan", "shadow execution"),
        "current_authority": "ACTIVE_ENGINEERING_PRIORS_WHEN_UNVERIFIED",
        "target_semantics": "ENGINEERING_PRIOR_NOT_EMPIRICAL_ALPHA",
        "evidence_state": "PRIOR_ONLY",
    },
    "duplicate_entry_causal_validators": {
        "owner": "Entry pre-Action validators / Phase4 handoff boundary",
        "consumers": ("Entry qualification", "pre-submit dependency checks"),
        "current_authority": "POST_ACTION_REJUDGMENT_REMOVED_MONITOR_REMAINDERS",
        "target_semantics": "ONE_QUESTION_ONE_OWNER",
        "evidence_state": "PRIOR_ONLY",
    },
    "auto_promotion_runtime_authority": {
        "owner": "loi_he_thong/auto_promotion.py::PromotionController",
        "consumers": ("promotion callback", "runtime armed state"),
        "current_authority": "CAN_INVOKE_RUNTIME_PROMOTION_AFTER_GATES",
        "target_semantics": "EVIDENCE_GATE_MANUAL_CUTOVER_ONLY",
        "evidence_state": "PRIOR_ONLY",
    },
}

_COMPARABILITY_FIELDS = (
    "wal_identity",
    "candidate_population_hash",
    "causal_wave_set_hash",
    "frozen_cost_hash",
    "guardian_version",
    "fill_model_version",
)

_REQUIRED_BOOLEAN_EVIDENCE = (
    "causal_wave_matched",
    "executable_fill_replayed",
    "frozen_cost_applied",
    "guardian_replayed",
    "feed_valid",
    "no_lookahead",
)

_REQUIRED_OUTCOME_FIELDS = (
    "guardian_net_bps_total",
    "economic_miss_count",
    "false_entry_count",
    "guardian_capture_ratio",
    "hard_stop_rate",
)


def registry():
    """Return an immutable-by-copy registry for Phase-5 research."""
    return deepcopy(_RULES)


def validate_registry():
    if len(_RULES) != 9:
        raise ValueError("PHASE5_RULE_COUNT_MISMATCH")
    for rule_id, row in _RULES.items():
        missing = _REQUIRED_RULE_FIELDS - set(row)
        if missing:
            raise ValueError(f"PHASE5_RULE_FIELDS_MISSING:{rule_id}:{sorted(missing)}")
        if row["evidence_state"] not in {"UNKNOWN", "PRIOR_ONLY"}:
            raise ValueError(f"PHASE5_PREPARATION_CANNOT_PREPROMOTE:{rule_id}")
    return tuple(sorted(_RULES))


def phase4_gate_status(evidence=None):
    """Evaluate the hard gate before any Phase-5 authority removal."""
    evidence = dict(evidence or {})
    checks = {
        "shared_thesis_trace_deterministic": bool(
            evidence.get("shared_thesis_trace_deterministic", False)
        ),
        "completed_positions_with_sealed_thesis": int(
            evidence.get("completed_positions_with_sealed_thesis", 0) or 0
        ) > 0,
        "same_wal_guardian_comparison": bool(
            evidence.get("same_wal_guardian_comparison", False)
        ),
        "capture_and_hard_stop_quality_proven": bool(
            evidence.get("capture_and_hard_stop_quality_proven", False)
        ),
    }
    return {
        "version": VERSION,
        "ready": all(checks.values()),
        "checks": checks,
        "policy": "NO_PHASE5_AUTHORITY_CHANGE_UNTIL_ALL_TRUE",
    }


def _deterministic(report):
    report = dict(report or {})
    first = report.get("deterministic_hash")
    repeat = report.get("repeat_hash")
    return bool(first and repeat and first == repeat)


def _comparability_blockers(baseline, counterfactual):
    baseline = dict(baseline or {})
    counterfactual = dict(counterfactual or {})
    blockers = []
    for field in _COMPARABILITY_FIELDS:
        left = baseline.get(field)
        right = counterfactual.get(field)
        if not left or not right:
            blockers.append(f"MISSING_COMPARABILITY:{field}")
        elif left != right:
            blockers.append(f"MISMATCHED_COMPARABILITY:{field}")
    for field in _REQUIRED_BOOLEAN_EVIDENCE:
        if baseline.get(field) is not True or counterfactual.get(field) is not True:
            blockers.append(f"MISSING_CANONICAL_EVIDENCE:{field}")
    if not _deterministic(baseline) or not _deterministic(counterfactual):
        blockers.append("DETERMINISM_UNPROVEN")
    return blockers


def build_shadow_ablation(
    rule_id,
    *,
    active_rule_result,
    shadow_disabled_result,
    baseline_report,
    counterfactual_report,
    phase4_evidence=None,
):
    """Build one non-authoritative ACTIVE_RULE vs disabled comparison.

    The caller owns replay execution.  This function only validates that the
    two reports are comparable and emits a research record.  It cannot promote,
    remove, disable, or mutate the active rule.
    """
    if rule_id not in _RULES:
        raise KeyError(f"UNKNOWN_PHASE5_RULE:{rule_id}")

    baseline = deepcopy(dict(baseline_report or {}))
    counterfactual = deepcopy(dict(counterfactual_report or {}))
    active_snapshot = deepcopy(active_rule_result)
    disabled_snapshot = deepcopy(shadow_disabled_result)
    gate = phase4_gate_status(phase4_evidence)
    blockers = _comparability_blockers(baseline, counterfactual)

    missing_outcomes = [
        name for name in _REQUIRED_OUTCOME_FIELDS
        if name not in baseline or name not in counterfactual
    ]
    if missing_outcomes:
        blockers.extend(f"MISSING_OUTCOME:{name}" for name in missing_outcomes)

    if not gate["ready"]:
        blockers.append("PHASE4_GATE_NOT_SATISFIED")

    comparability_broken = any(
        value.startswith("MISSING_COMPARABILITY:")
        or value.startswith("MISMATCHED_COMPARABILITY:")
        or value.startswith("MISSING_CANONICAL_EVIDENCE:")
        or value == "DETERMINISM_UNPROVEN"
        for value in blockers
    )
    if comparability_broken:
        evidence_state = "UNKNOWN"
    elif blockers:
        evidence_state = "PRIOR_ONLY"
    else:
        evidence_state = "SHADOW_MEASURED_REVIEW_REQUIRED"

    return {
        "version": VERSION,
        "policy": POLICY,
        "rule_id": rule_id,
        "rule": deepcopy(_RULES[rule_id]),
        "ablation": {
            "baseline_mode": "ACTIVE_RULE",
            "counterfactual_mode": "RULE_SHADOW_DISABLED",
            "active_rule_result": active_snapshot,
            "shadow_disabled_result": disabled_snapshot,
        },
        "phase4_gate": gate,
        "evidence_state": evidence_state,
        "blockers": tuple(sorted(set(blockers))),
        "baseline_report": baseline,
        "counterfactual_report": counterfactual,
        "authority_changed": False,
        "runtime_mutation_allowed": False,
        "promotion_allowed": False,
        "removal_recommendation": (
            "NOT_ELIGIBLE" if blockers else "MANUAL_REVIEW_ONLY"
        ),
    }
