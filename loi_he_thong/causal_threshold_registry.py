"""Phase-4 provenance for constants that can affect trading authority.

The registry is descriptive and immutable.  It does not tune a threshold or
authorize a decision; it makes the market question, owner and evidential
status of each active boundary explicit so replay cannot silently treat an
engineering prior as calibrated alpha.
"""

from types import MappingProxyType


VERSION = "CAUSAL_THRESHOLD_REGISTRY_V1"
CLASSES = frozenset({
    "SAFETY_INVARIANT", "DATA_QUALITY_RULE", "ENGINEERING_PRIOR",
    "EMPIRICAL_ALPHA", "RISK_POLICY",
})


def _spec(value, unit, category, owner, question, *, tunable=False,
          provenance="SOURCE_CONTRACT"):
    if category not in CLASSES:
        raise ValueError("UNKNOWN_THRESHOLD_CLASS:%s" % category)
    return MappingProxyType({
        "value": value,
        "unit": unit,
        "category": category,
        "owner": owner,
        "question": question,
        "tunable_by_ablation": bool(tunable),
        "provenance": provenance,
    })


# Only boundaries on the active Bias -> Ignition -> Entry -> Guardian -> Risk
# route belong here.  Metadata-only historical priors such as 13/20/35 bps are
# deliberately excluded because they have no authority.
THRESHOLDS = MappingProxyType({
    "bias.minimum_support": _spec(
        0.55, "support", "ENGINEERING_PRIOR", "bias_council",
        "Is background direction sufficiently supported?", tunable=True,
    ),
    "bias.borderline_support": _spec(
        0.50, "support", "ENGINEERING_PRIOR", "bias_council",
        "Is a weak pre-bias observation worth recording?", tunable=True,
    ),
    "ignition.follow_window": _spec(
        600, "ms", "ENGINEERING_PRIOR", "ignition_core",
        "Did the follower respond inside the same causal wave?", tunable=True,
    ),
    "ignition.evidence_gap": _spec(
        300, "ms", "DATA_QUALITY_RULE", "ignition_core",
        "Can two executed-flow observations be joined without a gap?",
    ),
    "ignition.episode_lifetime": _spec(
        5000, "ms", "DATA_QUALITY_RULE", "ignition_core",
        "Can evidence still belong to the bounded causal episode?",
    ),
    "ignition.lead_floor": _spec(
        100.0, "ms", "DATA_QUALITY_RULE", "ignition_core",
        "Is measured event ordering larger than timing uncertainty?",
    ),
    "entry.max_consumed_fraction": _spec(
        0.35, "fraction", "ENGINEERING_PRIOR", "ignition_core",
        "Is enough of the shared wave still unconsumed?", tunable=True,
    ),
    "entry.minimum_flow_imbalance": _spec(
        0.20, "imbalance", "ENGINEERING_PRIOR", "entry_thesis_gate",
        "Is current directional executed flow material?", tunable=True,
    ),
    "entry.minimum_cash_progress": _spec(
        0.15, "bps", "ENGINEERING_PRIOR", "entry_thesis_gate",
        "Is aggressive cash flow converting into price?", tunable=True,
    ),
    "entry.perp_lead_veto": _spec(
        3.0, "bps", "ENGINEERING_PRIOR", "entry_edge_tier",
        "Has the derivative move outrun executable cash evidence?", tunable=True,
    ),
    "execution.proof_max_age": _spec(
        1.5, "seconds", "DATA_QUALITY_RULE", "execution_causal_revalidation",
        "Is the frozen causal proof still current at submit?",
    ),
    "execution.bbo_max_age": _spec(
        1.0, "seconds", "DATA_QUALITY_RULE", "execution_causal_revalidation",
        "Is the executable BBO still fresh at submit?",
    ),
    "cost.unverified_fee_per_side": _spec(
        9.0, "bps", "RISK_POLICY", "verified_cost_model",
        "What conservative fee applies when account commission is unverified?",
    ),
    "cost.taker_minimum_net": _spec(
        6.0, "bps", "RISK_POLICY", "verified_cost_model",
        "What minimum net reserve must a taker entry retain?",
    ),
    "cost.maker_minimum_net": _spec(
        2.0, "bps", "RISK_POLICY", "verified_cost_model",
        "What minimum net reserve must a maker entry retain?",
    ),
    "guardian.minimum_adverse_price": _spec(
        1.5, "bps", "ENGINEERING_PRIOR", "guardian_s_tier",
        "Is adverse cash displacement material enough to investigate?",
        tunable=True,
    ),
    "guardian.minimum_deterioration": _spec(
        0.55, "seconds", "ENGINEERING_PRIOR", "guardian_s_tier",
        "Has causal deterioration persisted beyond transient noise?",
        tunable=True,
    ),
    "guardian.trend_deterioration": _spec(
        3.0, "seconds", "ENGINEERING_PRIOR", "guardian_s_tier",
        "Has an established trend thesis failed to recover?", tunable=True,
    ),
    "risk.hard_stop": _spec(
        "0.35-0.55", "percent", "SAFETY_INVARIANT", "hard_risk",
        "What exchange-side loss boundary remains final authority?",
    ),
    "risk.daily_real_loss": _spec(
        0.60, "USDT", "RISK_POLICY", "hard_risk",
        "When must real-money trading lock for the day?",
    ),
})


def value(name):
    return THRESHOLDS[name]["value"]


def snapshot():
    return {
        name: dict(spec)
        for name, spec in sorted(THRESHOLDS.items())
    }


def validate():
    owners = {}
    for name, spec in THRESHOLDS.items():
        if not name or spec["category"] not in CLASSES:
            raise ValueError("INVALID_THRESHOLD_SPEC:%s" % name)
        if not spec["owner"] or not spec["question"]:
            raise ValueError("THRESHOLD_WITHOUT_QUESTION_OWNER:%s" % name)
        owners.setdefault(spec["owner"], []).append(name)
    return {name: tuple(sorted(values)) for name, values in owners.items()}

