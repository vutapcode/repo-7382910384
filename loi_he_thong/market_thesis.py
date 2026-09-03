"""Canonical market truth and post-entry thesis observation.

Ignition evidence enters here as an explanation, never as an execution or
safety decision.  The content-addressed snapshot is handed to later owners;
Guardian supplies subsequent canonical measurements to :func:`observe`; this
owner alone maps them to thesis truth.  The frozen entry truth is never
rewritten. PnL, best-R, holding time and account history are intentionally
absent from both contracts.
"""

import hashlib
import json

from loi_he_thong import authority_contracts


VERSION = "MARKET_THESIS_V3_AUTHORITY_SEPARATED"
OBSERVATION_VERSION = "MARKET_THESIS_OBSERVATION_V1"
OWNER = "MARKET_THESIS"
OBSERVATION_STATUSES = {
    "SUPPORT", "DIVERGENCE", "CONTROL_TRANSFER", "FALSIFY", "UNKNOWN",
}


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
    primary_aliases = {
        "binance_spot": "spot", "spot": "spot",
        "coinbase_spot": "coinbase", "coinbase": "coinbase",
    }
    proposed_primary = primary_aliases.get(
        str(ignition.get("proposer") or "").lower()
    )
    if primary_cash_anchor not in anchors:
        primary_cash_anchor = (
            proposed_primary if proposed_primary in anchors else None
        )
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
        "bias_context": {
            "direction": frozen.get("direction"),
            "confidence": frozen.get("confidence"),
            "context_side": (
                frozen.get("direction_context") or {}
            ).get("context_side"),
            "phase": (frozen.get("direction_context") or {}).get("phase"),
            "candidate_side": (
                frozen.get("direction_context") or {}
            ).get("candidate_side"),
            "hysteresis": (
                frozen.get("direction_context") or {}
            ).get("hysteresis"),
            "price_vote": (
                frozen.get("direction_context") or {}
            ).get("price_vote"),
            "flow_vote": (
                frozen.get("direction_context") or {}
            ).get("flow_vote"),
            "oi_regime": (
                frozen.get("direction_context") or {}
            ).get("oi_regime"),
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


def _stable_hash(value):
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _observation_unknown(contract, reason, canonical=None):
    canonical = dict(canonical or {})
    return {
        "version": OBSERVATION_VERSION,
        "status": "UNKNOWN",
        "reason": reason,
        "causal_episode_id": contract.get("causal_episode_id"),
        "entry_market_truth_hash": contract.get("contract_hash"),
        "observation_hash": _stable_hash(canonical),
        "observed_falsifiers": [],
        "old_thesis_falsified": False,
        "pnl_fields_used_for_thesis": False,
        "capital_fields_used_for_thesis": False,
        "immutable_entry_truth": True,
    }


def observe(contract, observation):
    """Classify later evidence against one sealed Entry market thesis.

    ``observation`` is a measurement adapter contract, not another vote. Price
    moves and flow imbalances are signed relative to the open position. Missing
    sources and discontinuities remain unknown; they never masquerade as a
    market falsification. Safety may independently close such a position.
    """
    contract = dict(contract or {})
    observation = dict(observation or {})
    if not (
        authority_contracts.verify(contract)
        and contract.get("layer") == "MARKET_TRUTH"
        and contract.get("owner") == OWNER
    ):
        return _observation_unknown(
            contract, "ENTRY_MARKET_THESIS_INVALID", {},
        )

    why = dict(contract.get("why_entry") or {})
    anchors = sorted({
        str(name).lower() for name in (why.get("cash_anchors") or ())
        if str(name).lower() in {"spot", "coinbase"}
    })
    primary = str(why.get("primary_cash_anchor") or "").lower()
    if primary not in anchors:
        primary = anchors[0] if len(anchors) == 1 else ""
    side = _u(contract.get("side"), "ABSTAIN")
    episode_id = str(contract.get("causal_episode_id") or "")

    source_health = {
        str(name).lower(): _u(value)
        for name, value in dict(observation.get("source_health") or {}).items()
    }
    price_horizons = {}
    for raw_horizon, raw_row in dict(
        observation.get("price_horizons") or {}
    ).items():
        try:
            horizon = str(float(raw_horizon))
        except (TypeError, ValueError):
            continue
        price_horizons[horizon] = {
            "moves": {
                str(name).lower(): round(_number(value), 8)
                for name, value in dict((raw_row or {}).get("moves") or {}).items()
                if str(name).lower() in {"spot", "coinbase", "futures"}
            },
            "threshold_bps": round(
                max(0.0, _number((raw_row or {}).get("threshold_bps"))), 8
            ),
        }
    flows = {
        str(name).lower(): round(_number(value), 8)
        for name, value in dict(
            observation.get("flow_signed_imbalances") or {}
        ).items()
        if str(name).lower() in {"spot", "coinbase", "futures"}
    }
    oi = dict(observation.get("oi") or {})
    canonical = {
        "version": str(observation.get("version") or "UNKNOWN"),
        "causal_episode_id": str(
            observation.get("causal_episode_id") or ""
        ) or None,
        "position_side": _u(observation.get("position_side"), "ABSTAIN"),
        "source_health": source_health,
        "price_horizons": price_horizons,
        "flow_signed_imbalances": flows,
        "oi": {
            "status": _u(oi.get("status")),
            "fresh": bool(oi.get("fresh")),
        },
        "gap_or_epoch_invalid": bool(
            observation.get("gap_or_epoch_invalid")
        ),
        "material_price_bps": round(
            max(0.0, _number(observation.get("material_price_bps"), 1.5)), 8
        ),
        "material_flow_imbalance": round(
            max(0.0, _number(
                observation.get("material_flow_imbalance"), 0.20,
            )), 8
        ),
    }
    if (
        canonical["causal_episode_id"] != episode_id
        or canonical["position_side"] != side
        or side not in {"LONG", "SHORT"}
    ):
        return _observation_unknown(
            contract, "THESIS_OBSERVATION_IDENTITY_MISMATCH", canonical,
        )
    if canonical["gap_or_epoch_invalid"]:
        return _observation_unknown(
            contract, "THESIS_OBSERVATION_DISCONTINUITY", canonical,
        )
    if not anchors or any(
        source_health.get(anchor) != "FRESH" for anchor in anchors
    ):
        return _observation_unknown(
            contract, "THESIS_OBSERVATION_SOURCE_UNKNOWN", canonical,
        )

    price_floor = canonical["material_price_bps"]
    flow_floor = canonical["material_flow_imbalance"]
    price_adverse = set()
    price_supportive = set()
    persistent_adverse = set()
    persistent_supportive = set()
    for raw_horizon, row in price_horizons.items():
        horizon = _number(raw_horizon)
        row_price_floor = row.get("threshold_bps") or price_floor
        for venue, move in row["moves"].items():
            if move <= -row_price_floor:
                price_adverse.add(venue)
                if horizon >= 3.0:
                    persistent_adverse.add(venue)
            elif move >= row_price_floor:
                price_supportive.add(venue)
                if horizon >= 3.0:
                    persistent_supportive.add(venue)
    flow_adverse = {name for name, value in flows.items() if value <= -flow_floor}
    flow_supportive = {name for name, value in flows.items() if value >= flow_floor}
    anchor_set = set(anchors)
    adverse_cash_price = price_adverse & anchor_set
    adverse_cash_flow = flow_adverse & anchor_set
    supportive_cash_price = price_supportive & anchor_set
    supportive_cash_flow = flow_supportive & anchor_set
    dual_adverse = bool(
        len(anchor_set) >= 2
        and anchor_set <= adverse_cash_price
        and anchor_set <= adverse_cash_flow
    )
    dual_supportive = bool(
        len(anchor_set) >= 2
        and anchor_set <= supportive_cash_price
        and anchor_set <= supportive_cash_flow
    )
    persistent_dual_adverse = bool(
        dual_adverse and anchor_set <= persistent_adverse
    )
    primary_adverse = bool(
        primary and primary in adverse_cash_price and primary in adverse_cash_flow
    )
    primary_persistent = bool(primary and primary in persistent_adverse)
    primary_supportive = bool(
        primary and primary in supportive_cash_price
        and primary in supportive_cash_flow
    )
    secondary_supports_old_side = bool(
        (anchor_set - {primary}) & supportive_cash_price
        and (anchor_set - {primary}) & supportive_cash_flow
    )
    secondary_adverse_evidence = bool(
        (anchor_set - {primary})
        & (adverse_cash_price | adverse_cash_flow)
    )
    opposite_oi_build = bool(
        canonical["oi"]["fresh"]
        and canonical["oi"]["status"] in {
            "ADVERSE", "FRESH_CONFLICT", "OPPOSITE_POSITION_BUILD",
            "FRESH_OPPOSITE_POSITION_BUILD",
        }
    )

    falsifiers = []
    if primary_adverse and primary_persistent:
        falsifiers.append("PRIMARY_CASH_STOPS_OR_REVERSES_CONVERSION")
    if persistent_dual_adverse:
        falsifiers.extend([
            "OPPOSITE_DUAL_CASH_CONTROL", "OPPOSITE_CASH_PRICE_ACCEPTANCE",
        ])
    if opposite_oi_build and primary_adverse:
        falsifiers.append("FRESH_OPPOSITE_POSITION_BUILD")
    falsifiers = [
        name for name in dict.fromkeys(falsifiers)
        if name in set(contract.get("falsifiers") or ())
    ]

    if persistent_dual_adverse:
        status = "CONTROL_TRANSFER"
        reason = "OPPOSITE_DUAL_CASH_CONTROL_PERSISTED"
        falsified = True
    elif (
        primary_adverse and not secondary_supports_old_side
        and (
            opposite_oi_build
            or (primary_persistent and secondary_adverse_evidence)
        )
    ):
        status = "FALSIFY"
        reason = "FROZEN_PRIMARY_CASH_THESIS_FALSIFIED"
        falsified = True
    elif adverse_cash_price or adverse_cash_flow or opposite_oi_build:
        status = "DIVERGENCE"
        reason = "ADVERSE_EVIDENCE_INCOMPLETE_OR_CONFLICTED"
        falsified = False
    elif dual_supportive or primary_supportive:
        status = "SUPPORT"
        reason = "FROZEN_CASH_THESIS_CURRENTLY_SUPPORTED"
        falsified = False
    else:
        status = "UNKNOWN"
        reason = "NO_MATERIAL_CURRENT_THESIS_EVIDENCE"
        falsified = False

    assert status in OBSERVATION_STATUSES
    return {
        "version": OBSERVATION_VERSION,
        "status": status,
        "reason": reason,
        "causal_episode_id": episode_id,
        "entry_market_truth_hash": contract.get("contract_hash"),
        "observation_hash": _stable_hash(canonical),
        "observed_falsifiers": falsifiers,
        "old_thesis_falsified": falsified,
        "primary_cash_anchor": primary or None,
        "cash_anchors": anchors,
        "evidence": {
            "price_adverse": sorted(price_adverse),
            "flow_adverse": sorted(flow_adverse),
            "persistent_price_adverse": sorted(persistent_adverse),
            "price_supportive": sorted(price_supportive),
            "flow_supportive": sorted(flow_supportive),
            "fresh_opposite_position_build": opposite_oi_build,
        },
        "pnl_fields_used_for_thesis": False,
        "capital_fields_used_for_thesis": False,
        "immutable_entry_truth": True,
    }
