"""Immutable Entry-to-Guardian market thesis contract.

Entry owns the explanation that justified taking risk.  Guardian owns whether
later evidence still satisfies or falsifies that explanation.  PnL, best-R,
holding time and account history are intentionally absent from this module.
"""

VERSION = "MARKET_THESIS_V2"


def _u(value, default="UNKNOWN"):
    return str(value or default).upper()


def build(result, *, primary_cash_anchor=None, cash_anchors=()):
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    frozen = dict(ignition.get("bias_snapshot") or {})
    transition = dict(ignition.get("transition_authority") or {})
    oi = dict(ignition.get("oi_verification_state") or {})
    raw_oi = dict(ignition.get("oi_intent") or {})
    proof = _u(ignition.get("proof_type"))
    side = _u(
        result.get("side") or ignition.get("side") or frozen.get("direction"),
        "ABSTAIN",
    )
    transition_confirmed = bool(
        ignition.get("transition_confirmed")
        and _u(transition.get("status")) == "REVERSAL_CONFIRMED"
    )
    oi_state = _u(oi.get("status") or raw_oi.get("intent"))
    if transition_confirmed:
        mechanism = "CASH_CONTROL_TRANSFER"
        expected_sequence = [
            "OLD_SIDE_FAILURE", "CASH_RECLAIM", "NEW_SIDE_CONVERSION",
            "INDEPENDENT_CASH_ACCEPTANCE",
        ]
    elif proof == "PERSISTENT_METAORDER":
        mechanism = "CASH_METAORDER"
        expected_sequence = [
            "PRIMARY_CASH_AGGRESSION", "PRICE_CONVERSION",
            "INDEPENDENT_CASH_ACCEPTANCE",
        ]
    elif proof == "FAILED_REVERSION":
        mechanism = "FAILED_REVERSION_CONTINUATION"
        expected_sequence = [
            "OPPOSING_ATTEMPT", "REVERSION_FAILURE", "CASH_REACCEPTANCE",
        ]
    else:
        mechanism = "CASH_IGNITION"
        expected_sequence = [
            "CASH_IMPULSE", "PRICE_CONVERSION", "CROSS_VENUE_ACCEPTANCE",
        ]

    anchors = sorted({
        str(value).lower() for value in cash_anchors
        if str(value).lower() in {"spot", "coinbase"}
    })
    supporting = [
        "EXECUTED_CASH_FLOW", "CASH_PRICE_CONVERSION",
    ]
    if len(anchors) >= 2:
        supporting.append("INDEPENDENT_DUAL_CASH_ACCEPTANCE")
    if transition_confirmed:
        supporting.append("OLD_SIDE_FAILURE_AND_NEW_SIDE_CONTROL")
    if oi_state not in {"UNKNOWN", "UNCHANGED_UNKNOWN", "STALE_UNKNOWN"}:
        supporting.append("FRESH_OI_CONTEXT")

    competing = ["DERIVATIVE_DISLOCATION", "FLOW_NON_CONVERSION"]
    if "UNWIND" in oi_state or "LIQUIDATION" in oi_state or "COVER" in oi_state:
        competing.append("FORCED_UNWIND_TAIL")

    return {
        "version": VERSION,
        "side": side,
        "mechanism": mechanism,
        "why_entry": {
            "proof_type": proof,
            "proposer": _u(ignition.get("proposer")),
            "primary_cash_anchor": primary_cash_anchor,
            "cash_anchors": anchors,
            "authority_basis": result.get("authority_basis"),
        },
        "supporting_evidence": supporting,
        "competing_hypotheses": competing,
        "falsifiers": [
            "PRIMARY_CASH_STOPS_OR_REVERSES_CONVERSION",
            "OPPOSITE_DUAL_CASH_CONTROL",
            "OPPOSITE_CASH_PRICE_ACCEPTANCE",
            "FRESH_OPPOSITE_POSITION_BUILD",
        ],
        "expected_next_observation": [
            "PRIMARY_CASH_CONTROL_PERSISTS",
            "SECONDARY_CASH_DOES_NOT_ACCEPT_OPPOSITE_SIDE",
            "PRICE_CONTINUES_CONVERTING_WHILE_EXECUTED_FLOW_PERSISTS",
        ],
        "expected_sequence": expected_sequence,
        "oi_context": oi_state,
        "expiry_semantics": {
            "mode": "EVIDENCE_DRIVEN",
            "time_alone_falsifies": False,
            "unknown_source_health_returns": "UNKNOWN",
        },
        "pnl_independent": True,
        "capital_policy_separate": True,
    }
