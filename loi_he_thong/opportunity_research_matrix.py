"""Research-only taxonomy for causal opportunities rejected by live authority.

This module is deliberately descriptive.  It cannot open, veto, size or exit
an order; it only turns already-computed Bias/Ignition evidence into stable
cohort labels for later canonical replay and Guardian counterfactuals.
"""

VERSION = "OPPORTUNITY_RESEARCH_MATRIX_V1"
BIAS_MIN_CONF = 0.55
BORDERLINE_MIN_CONF = 0.50
CASH = frozenset(("binance_spot", "coinbase_spot"))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bias_band(confidence, frozen):
    if not frozen:
        return "UNOBSERVED"
    if confidence >= BIAS_MIN_CONF:
        return "STRONG"
    if confidence >= BORDERLINE_MIN_CONF:
        return "BORDERLINE"
    return "WEAK"


def _bias_source(snapshot):
    context = dict((snapshot or {}).get("direction_context") or {})
    hysteresis = str(context.get("hysteresis") or "UNKNOWN").upper()
    story = str(context.get("story") or "UNKNOWN").upper()
    raw_direction = str((snapshot or {}).get("raw_direction") or "ABSTAIN").upper()
    direction = str((snapshot or {}).get("direction") or "ABSTAIN").upper()
    if "COVERAGE" in hysteresis or "COVERAGE" in story:
        return "COVERAGE_DEGRADED"
    if "HOLD" in hysteresis or raw_direction != direction:
        return "HYSTERESIS_HOLD"
    return "RAW" if raw_direction == direction else "FROZEN_PUBLISHED"


def _leader_class(ignition):
    leader = str((ignition or {}).get("leader") or "UNKNOWN").lower()
    if leader in CASH:
        return "CASH"
    if leader == "futures":
        return "FUTURES"
    if leader == "simultaneous":
        return "SIMULTANEOUS"
    return "UNKNOWN"


def _slow_acceptance(persistent, side):
    side_report = dict(((persistent or {}).get("sides") or {}).get(side) or {})
    cash = sorted(set(side_report.get("cash_candidates") or ()))
    futures_follow = bool(side_report.get("futures_follow"))
    if cash and futures_follow:
        status = "CASH_PERSISTENT_DUAL" if len(cash) >= 2 else "CASH_PERSISTENT_SINGLE"
    elif futures_follow:
        status = "FUTURES_PERSISTENT"
    else:
        status = "NONE"
    return {
        "status": status,
        "cash_venues": cash,
        "futures_follow": futures_follow,
        "source": "PERSISTENT_METAORDER_1_6S",
    }


def _oi_class(ignition, slow):
    oi = dict((ignition or {}).get("oi_intent") or {})
    intent = str(oi.get("intent") or "NEUTRAL").upper()
    leader = _leader_class(ignition)
    proposer = str((ignition or {}).get("proposer") or "UNKNOWN").lower()
    cash_accepted = str(slow.get("status") or "").startswith("CASH_PERSISTENT")
    if not oi or not bool(oi.get("fresh")):
        return "OI_STALE_OR_UNOBSERVED"
    if intent == "POSITION_BUILD":
        return (
            "POSITION_BUILD_CONFIRMED"
            if bool(oi.get("aligned_with_entry", True)) else "OI_DIRECTION_CONFLICT"
        )
    if intent != "UNWIND":
        return "OI_NEUTRAL"
    if proposer == "futures" or leader == "FUTURES":
        return "UNWIND_FUTURES_LED"
    if leader == "SIMULTANEOUS":
        return (
            "UNWIND_CASH_ACCEPTED"
            if cash_accepted else "UNWIND_SIMULTANEOUS_UNRESOLVED"
        )
    if proposer in CASH and cash_accepted:
        return "UNWIND_CASH_ACCEPTED"
    return "UNWIND_CASH_UNPROVED"


def _proof_class(ignition):
    proof = str((ignition or {}).get("proof_type") or "").upper()
    if proof == "METAORDER_CONTINUATION":
        return "METAORDER"
    if proof == "FAILED_REVERSION":
        return "FAILED_REVERSION"
    return "NONE"


def build(state, result, edge_report=None):
    """Build a bounded diagnostic snapshot without mutating runtime state."""
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    persistent = dict(
        result.get("persistent_metaorder_shadow")
        or getattr(state, "persistent_metaorder_shadow", {}) or {}
    )
    frozen = dict(ignition.get("bias_snapshot") or {})
    council = dict(getattr(state, "bias_council", {}) or {})
    frozen_available = bool(frozen)
    confidence = _f(
        frozen.get("confidence")
        if frozen_available else getattr(state, "bias_confidence", 0.0)
    )
    raw_confidence = _f(
        frozen.get("raw_confidence")
        if frozen_available else council.get("raw_confidence")
    )
    side = str(result.get("side") or "ABSTAIN").upper()
    slow = _slow_acceptance(persistent, side)
    leader = _leader_class(ignition)
    phase_measurement = dict(ignition.get("phase_measurement") or {})
    precursor = dict(phase_measurement.get("precursor_measurement") or {})
    consumed = ignition.get("consumed_fraction")
    simultaneous_acceptance = (
        "SIMULTANEOUS_CASH_ACCEPTANCE"
        if leader == "SIMULTANEOUS"
        and str(slow.get("status") or "").startswith("CASH_PERSISTENT")
        else "NOT_OBSERVED"
    )
    observed_conditions = []
    if leader == "SIMULTANEOUS":
        observed_conditions.append("LEADER_UNRESOLVED")
    if consumed is not None and _f(consumed) > 0.35:
        observed_conditions.append("IMPULSE_MATURE")
    if _proof_class(ignition) == "NONE":
        observed_conditions.append("PROOF_INCOMPLETE")
    if _oi_class(ignition, slow) == "UNWIND_FUTURES_LED":
        observed_conditions.append("FUTURES_LED_UNWIND")

    votes = dict(frozen.get("s_votes") or council.get("s_votes") or {})
    compact_votes = {
        name: {
            "vote": str((row or {}).get("vote") or "ABSTAIN"),
            "confidence": _f((row or {}).get("confidence")),
            "reason": str((row or {}).get("reason") or "UNKNOWN"),
        }
        for name, row in votes.items() if isinstance(row, dict)
    }
    return {
        "version": VERSION,
        "authority": False,
        "policy": "RECORDER_ONLY_NEVER_OPENS_VETOES_SIZES_OR_EXITS",
        "research_candidate_id": ignition.get("research_candidate_id"),
        "transition": bool(ignition.get("research_candidate_transition")),
        "pre_bias": {
            "band": _bias_band(confidence, frozen_available),
            "borderline": bool(
                frozen_available
                and BORDERLINE_MIN_CONF <= confidence < BIAS_MIN_CONF
            ),
            "snapshot_source": "FROZEN_PRE_IMPULSE" if frozen_available else "CURRENT_NOT_PRE_IMPULSE",
            "direction": str(
                frozen.get("direction")
                if frozen_available else getattr(state, "bias_state", "ABSTAIN")
            ).upper(),
            "confidence": round(confidence, 6),
            "raw_direction": str(
                frozen.get("raw_direction")
                if frozen_available else council.get("raw_bias", "ABSTAIN")
            ).upper(),
            "raw_confidence": round(raw_confidence, 6),
            "source": _bias_source(frozen) if frozen_available else "CURRENT_ONLY",
            "hysteresis": str(
                ((frozen.get("direction_context") or {}).get("hysteresis"))
                if frozen_available else council.get("hysteresis", "UNKNOWN")
            ),
            "votes": compact_votes,
        },
        "leader": leader,
        "slow_acceptance": slow,
        "simultaneous_cash_acceptance": simultaneous_acceptance,
        "oi_class": _oi_class(ignition, slow),
        "precursor": {
            "winner_horizon_seconds": precursor.get("horizon_seconds"),
            "horizons": dict(precursor.get("horizons") or {}),
            "continuity": str(
                precursor.get("continuity_status")
                or "UNMEASURED_REQUIRES_EXECUTED_FLOW_PATH"
            ),
        },
        "phase": str(ignition.get("impulse_phase") or result.get("phase") or "UNKNOWN").upper(),
        "proof": _proof_class(ignition),
        "first_blocking_gate": (
            str(result.get("reason") or "UNKNOWN")
            if result.get("decision") != "GO" else None
        ),
        "observed_secondary_conditions": observed_conditions,
        "execution": "UNMEASURED_REQUIRES_CANONICAL_FILL_REPLAY",
        "economics": "UNMEASURED_REQUIRES_FROZEN_COST_AND_GUARDIAN_EXIT",
        "guardian": "NO_COUNTERFACTUAL",
        "edge_class_metadata": (edge_report or {}).get("edge_class"),
    }
