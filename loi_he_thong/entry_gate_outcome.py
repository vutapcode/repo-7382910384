"""Canonical ownership for the active Entry authorization boundary.

This module classifies an already-computed result.  It does not create market
direction, add a veto, or change an authorization decision.  Its only job is
to preserve which existing owner produced PASS/WAIT/REJECT so recorder data
cannot label an Economics or Timing rejection as a Structural failure.
"""

VERSION = "ENTRY_GATE_OUTCOME_V3_PENDING_TRANSITION_OWNER"

OWNERS = {
    "MARKET_TRUTH", "STRUCTURAL", "TIMING", "THESIS", "ECONOMICS",
    "EXECUTION", "SAFETY", "ACTION",
}

_ECONOMIC_REASONS = {
    "EMPIRICAL_FORWARD_EDGE_FAIL",
    "EMPIRICAL_OUTCOME_FALSIFIER",
    "EMPIRICAL_ALPHA_NOT_READY",
    "EDGE_COST_FAIL",
    "FORWARD_EDGE_FAIL",
}

_STRUCTURAL_REASONS = {
    "IGNITION_CONTRACT_FAIL",
    "ENTRY_AUTHORITY_CONTRACT_FAIL",
}

_THESIS_MARKERS = (
    "ABSOR", "EXHAUST", "NONCONVERSION", "PERP_LED", "LIQUIDATION",
    "UNWIND", "OPPOSITE", "CONTRADICTION", "CROSS_VENUE",
)

_MARKET_TRUTH_MARKERS = (
    "BIAS", "ALIGN", "CONTRADICTION", "OPPOSITE", "ABSOR", "EXHAUST",
    "NONCONVERSION", "PERP_LED", "LIQUIDATION", "UNWIND",
)


def _pending_transition_reason(result):
    """Expose a counter-wave wait without pretending Bias rejected it.

    Ignition can retain an authority-free reversal episode while top-level
    Bias is ABSTAIN. In that state the first unresolved question is the fast
    transition proof carried by the episode, not whether Bias emitted a side.
    """
    ignition = dict((result or {}).get("ignition") or {})
    explicit = str(
        ignition.get("research_reject_reason") or ""
    ).upper()
    if explicit.startswith("PENDING_REVERSAL_"):
        return explicit
    if (
        str(ignition.get("status") or "").upper()
        in {"PENDING_BIAS_FLIP", "CONTROL_TRANSFER_WAITING_FRESH_CASH"}
        and str(ignition.get("side") or "ABSTAIN").upper()
            in {"LONG", "SHORT"}
        and str(ignition.get("background_side") or "ABSTAIN").upper()
            in {"LONG", "SHORT"}
        and not bool(
            ignition.get("transition_confirmed")
            or (ignition.get("transition_authority") or {}).get("confirmed")
        )
    ):
        if bool(ignition.get("new_side_evidence_stale")):
            return "PENDING_REVERSAL_CONTROL_TRANSFER_WAITING_FRESH_CASH"
        return "PENDING_REVERSAL_BIAS_CONFIRMATION"
    return None


def outcome(allowed, owner, stage, reason, detail=None):
    owner = str(owner or "ACTION").upper()
    if owner not in OWNERS:
        owner = "ACTION"
    reason = str(reason or ("PASS" if allowed else "UNATTRIBUTED_REJECT")).upper()
    if not allowed and reason == "PASS":
        reason = "UNATTRIBUTED_REJECT"
    return {
        "version": VERSION,
        "allowed": bool(allowed),
        "owner": owner,
        "stage": str(stage or "UNKNOWN").upper(),
        "reason": reason,
        "detail": dict(detail or {}) if isinstance(detail, dict) else detail,
    }


def structural(allowed, reason, detail=None):
    return outcome(
        allowed, "ACTION" if allowed else "STRUCTURAL",
        "AUTHORIZED" if allowed else "FROZEN_ENTRY_CONTRACT",
        reason, detail,
    )


def from_entry_decision(result):
    """Attribute an Ignition/Council WAIT before structural validation."""
    result = dict(result or {})
    reason = str(result.get("reason") or "ENTRY_NOT_PROPOSED").upper()
    pending_reason = _pending_transition_reason(result)
    if pending_reason:
        return outcome(
            False, "TIMING", "FAST_TRANSITION", pending_reason, {
                "entry_decision": result.get("decision", "WAIT"),
                "entry_reason": reason,
                "causal_episode_id": (
                    (result.get("ignition") or {}).get("causal_episode_id")
                    or result.get("causal_episode_id")
                ),
                "policy": (
                    "COUNTER_WAVE_IS_OBSERVED_BUT_HAS_NO_ENTRY_AUTHORITY_"
                    "UNTIL_TRANSITION_PROOF"
                ),
            },
        )
    owner = (
        "MARKET_TRUTH"
        if any(marker in reason for marker in _MARKET_TRUTH_MARKERS)
        else "TIMING"
    )
    return outcome(
        False, owner,
        "MARKET_TRUTH" if owner == "MARKET_TRUTH" else "TIMING_NOW",
        reason, {"entry_decision": result.get("decision", "WAIT")},
    )


def from_edge_report(allowed, report, *, live=False):
    """Attribute the existing Edge result without changing its decision."""
    report = dict(report or {})
    if allowed:
        return outcome(True, "ACTION", "AUTHORIZED", "PASS", {})

    hard = [str(value or "").upper() for value in report.get("hard_vetoes") or ()]
    soft = [str(value or "").upper() for value in report.get("soft_wait_reasons") or ()]

    if hard:
        reason = hard[0]
        if reason in _STRUCTURAL_REASONS:
            owner, stage = "STRUCTURAL", "FROZEN_ENTRY_CONTRACT"
        elif reason in _ECONOMIC_REASONS:
            owner, stage = "ECONOMICS", "FORWARD_EDGE"
        else:
            owner, stage = "THESIS", "CAUSAL_THESIS"
        return outcome(False, owner, stage, reason, {
            "hard_vetoes": hard,
            "soft_wait_reasons": soft,
        })

    if soft:
        reason = soft[0]
        owner = (
            "THESIS" if any(marker in reason for marker in _THESIS_MARKERS)
            else "TIMING"
        )
        return outcome(False, owner, "CAUSAL_THESIS" if owner == "THESIS" else "TIMING_NOW",
                       reason, {"soft_wait_reasons": soft})

    if live and not bool(report.get("live_empirical_ok")):
        return outcome(False, "ECONOMICS", "EMPIRICAL_PROMOTION",
                       "EMPIRICAL_ALPHA_NOT_READY", {
                           "edge_class": report.get("edge_class"),
                           "cost_ok": report.get("cost_ok"),
                       })

    if not bool(report.get("cost_ok")) and not bool(
        report.get("bootstrap_shadow_allowed")
    ):
        return outcome(False, "ECONOMICS", "FROZEN_COST",
                       "EDGE_COST_FAIL", {
                           "edge_class": report.get("edge_class"),
                           "expected_net_bps_model": report.get(
                               "expected_net_bps_model"
                           ),
                           "minimum_net_edge_bps": report.get(
                               "min_net_edge_bps"
                           ),
                       })

    return outcome(False, "ACTION", "AUTHORIZATION", "ACTION_NOT_AUTHORIZED", {
        "edge_class": report.get("edge_class"),
        "cost_ok": report.get("cost_ok"),
        "bootstrap_shadow_allowed": report.get("bootstrap_shadow_allowed"),
        "live_empirical_ok": report.get("live_empirical_ok"),
    })
