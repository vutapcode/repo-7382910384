"""Canonical market-truth snapshot for the active Tier-S causal path.

Ignition evidence enters here as an explanation, never as an execution or
safety decision.  The content-addressed snapshot is handed to later owners;
Guardian may report subsequent observations but must not rewrite this entry
truth.  PnL, best-R, holding time and account history are intentionally absent.
"""

from loi_he_thong import authority_contracts


VERSION = "MARKET_THESIS_V3_AUTHORITY_SEPARATED"
OWNER = "MARKET_THESIS"


def _u(value, default="UNKNOWN"):
    return str(value or default).upper()


def _knowledge_state(result):
    """Conservative observation taxonomy; this has no GO/WAIT authority."""
    result = dict(result or {})
    decision = _u(result.get("decision"), "WAIT")
    reason = _u(result.get("reason"))
    explicit = _u(result.get("market_truth_status"), "")
    if explicit == "FALSIFIED":
        return "FALSIFIED", "FALSIFIED"
    if decision == "GO":
        return "SUPPORTED", "SUPPORTED"
    if any(token in reason for token in (
        "STALE", "GAP", "EPOCH", "CLOCK", "FEED_NOT_READY",
        "EXTERNAL_UNAVAILABLE", "SOURCE_UNAVAILABLE",
    )):
        return "UNKNOWN", "UNKNOWN_SOURCE"
    if any(token in reason for token in (
        "CONTRADICTION", "OPPOSE", "NOT_ALIGNED", "CONTROL_TRANSFER_FAILED",
    )):
        return "DIVERGING", "CONTRADICTED"
    return "UNKNOWN", "UNKNOWN_MARKET"


def _source_health(ignition, knowledge_state):
    clock = dict(ignition.get("clock_quality") or {})
    sources = {}
    for venue, row in clock.items():
        row = dict(row or {})
        sources[str(venue)] = {
            "status": _u(
                row.get("source_health") or row.get("temporal_status"),
                "UNKNOWN",
            ),
            "epoch": row.get("epoch"),
            "temporal_uncertainty_ms": row.get("temporal_uncertainty_ms"),
        }
    if knowledge_state == "UNKNOWN_SOURCE" or not sources:
        overall = "UNKNOWN"
    elif all(row.get("status") == "FRESH" for row in sources.values()):
        overall = "FRESH"
    else:
        overall = "DEGRADED"
    return {
        "overall": overall,
        "sources": sources,
    }


def build(result, *, primary_cash_anchor=None, cash_anchors=()):
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    frozen = dict(ignition.get("bias_snapshot") or {})
    transition = dict(ignition.get("transition_authority") or {})
    oi = dict(ignition.get("oi_verification_state") or {})
    raw_oi = dict(ignition.get("oi_intent") or {})
    proof = _u(ignition.get("proof_type"))
    causal_episode_id = str(
        result.get("causal_episode_id")
        or ignition.get("causal_episode_id") or ""
    )
    status, knowledge_state = _knowledge_state(result)
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
            "DUAL_CASH_CROSS_VENUE_CORROBORATION",
        ]
    elif proof == "PERSISTENT_METAORDER":
        mechanism = "CASH_METAORDER"
        expected_sequence = [
            "PRIMARY_CASH_AGGRESSION", "PRICE_CONVERSION",
            "DUAL_CASH_CROSS_VENUE_CORROBORATION",
        ]
    elif proof == "FAILED_REVERSION":
        mechanism = "FAILED_REVERSION_CONTINUATION"
        expected_sequence = [
            "OPPOSING_ATTEMPT", "REVERSION_FAILURE", "CASH_REACCEPTANCE",
        ]
    elif proof != "UNKNOWN":
        mechanism = "CASH_IGNITION"
        expected_sequence = [
            "CASH_IMPULSE", "PRICE_CONVERSION", "CROSS_VENUE_ACCEPTANCE",
        ]
    else:
        mechanism = "UNRESOLVED_MARKET_MECHANISM"
        expected_sequence = []

    if not cash_anchors:
        aliases = {
            "binance_spot": "spot", "spot": "spot",
            "coinbase_spot": "coinbase", "coinbase": "coinbase",
        }
        cash_anchors = tuple(
            aliases[str(value).lower()]
            for value in (ignition.get("cash_venues") or ())
            if str(value).lower() in aliases
        )
    anchors = sorted({
        str(value).lower() for value in cash_anchors
        if str(value).lower() in {"spot", "coinbase"}
    })
    has_cash_evidence = bool(
        anchors and (
            proof != "UNKNOWN"
            or (ignition.get("current_cash_conversion") or {}).get("confirmed")
        )
    )
    supporting = (
        ["EXECUTED_CASH_FLOW", "CASH_PRICE_CONVERSION"]
        if has_cash_evidence else []
    )
    if has_cash_evidence and len(anchors) >= 2:
        supporting.append("DUAL_CASH_CROSS_VENUE_CORROBORATION")
    if transition_confirmed:
        supporting.append("OLD_SIDE_FAILURE_AND_NEW_SIDE_CONTROL")
    if oi_state not in {"UNKNOWN", "UNCHANGED_UNKNOWN", "STALE_UNKNOWN"}:
        supporting.append("FRESH_OI_CONTEXT")

    competing = ["DERIVATIVE_DISLOCATION", "FLOW_NON_CONVERSION"]
    if "UNWIND" in oi_state or "LIQUIDATION" in oi_state or "COVER" in oi_state:
        competing.append("FORCED_UNWIND_TAIL")

    expected_next = [
        "PRIMARY_CASH_CONTROL_PERSISTS",
        "SECONDARY_CASH_DOES_NOT_ACCEPT_OPPOSITE_SIDE",
        "PRICE_CONTINUES_CONVERTING_WHILE_EXECUTED_FLOW_PERSISTS",
    ]
    payload = {
        "version": VERSION,
        "status": status,
        "knowledge_state": knowledge_state,
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
        "competing_explanations": competing,
        # Historical reader compatibility. New consumers use the neutral name
        # above and must verify the sealed contract before granting authority.
        "competing_hypotheses": competing,
        "falsifiers": [
            "PRIMARY_CASH_STOPS_OR_REVERSES_CONVERSION",
            "OPPOSITE_DUAL_CASH_CONTROL",
            "OPPOSITE_CASH_PRICE_ACCEPTANCE",
            "FRESH_OPPOSITE_POSITION_BUILD",
        ],
        "expected_next_observations": expected_next,
        "expected_next_observation": expected_next,
        "expected_sequence": expected_sequence,
        "oi_context": oi_state,
        "source_health": _source_health(ignition, knowledge_state),
        "expiry_semantics": {
            "mode": "EVIDENCE_DRIVEN",
            "time_alone_falsifies": False,
            "unknown_source_health_returns": "UNKNOWN",
        },
        "pnl_independent": True,
        "capital_policy_separate": True,
    }
    return authority_contracts.seal(
        "MARKET_TRUTH", OWNER, causal_episode_id, payload,
    )
