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

from loi_he_thong import authority_contracts, cross_cash_causal_wave


VERSION = "MARKET_THESIS_V4_CAUSAL_CONTROL_OWNERSHIP"
OBSERVATION_VERSION = "MARKET_THESIS_OBSERVATION_V2"
WAVE_LIFECYCLE_VERSION = "MARKET_TRUTH_WAVE_LIFECYCLE_V3_IDENTITY_SEPARATED"
OWNER = "MARKET_THESIS"
MAX_WAVE_TOMBSTONES = 256
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
    if any(token in reason for token in (
        "STALE", "GAP", "EPOCH", "CLOCK", "FEED_NOT_READY",
        "EXTERNAL_UNAVAILABLE", "SOURCE_UNAVAILABLE",
    )):
        return "UNKNOWN", "UNKNOWN_SOURCE"
    if any(token in reason for token in (
        "CONTRADICTION", "OPPOSE", "NOT_ALIGNED", "CONTROL_TRANSFER_FAILED",
    )):
        return "DIVERGING", "CONTRADICTED"
    if decision == "GO":
        ignition = dict(result.get("ignition") or {})
        current_cash = dict(
            ignition.get("current_cash_conversion") or {}
        )
        accepted = {
            str(value) for value in current_cash.get(
                "accepted_cash_venues", ()
            )
        }
        proof = _u(ignition.get("proof_type"))
        health = _source_health(ignition, "UNKNOWN_MARKET")
        if health.get("overall") != "FRESH":
            return "UNKNOWN", "UNKNOWN_SOURCE"
        if (
            current_cash.get("confirmed")
            and accepted & {"binance_spot", "coinbase_spot"}
            and proof != "UNKNOWN"
        ):
            return "SUPPORTED", "SUPPORTED"
        # A proposal is an Action-layer fact, not evidence that Market Truth
        # is supported.  Missing causal cash fields remain unknown instead of
        # being upgraded merely because an upstream decision says GO.
        return "UNKNOWN", "UNKNOWN_MARKET"
    return "UNKNOWN", "UNKNOWN_MARKET"


def _wave_tombstones(state):
    raw = getattr(state, "market_truth_wave_tombstones", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _remember_wave_falsifier(state, episode_id, falsifier):
    """Keep terminal Market Truth across later handoff replacement.

    The ledger is deliberately bounded and keyed only by immutable causal-wave
    identity. Timing, Economics and Execution cannot write it.
    """
    episode_id = str(episode_id or "")
    if not episode_id:
        return
    rows = _wave_tombstones(state)
    if episode_id not in rows and len(rows) >= MAX_WAVE_TOMBSTONES:
        rows.pop(next(iter(rows)))
    rows[episode_id] = str(falsifier or "MARKET_TRUTH_CAUSAL_FALSIFIER")
    state.market_truth_wave_tombstones = rows


def wave_lifecycle(state, result):
    """Publish the sole typed alive/falsified view of one causal wave.

    Timing, Economics and Execution may observe this contract but cannot
    terminate the market process. Source unavailability remains UNKNOWN; it
    can fail execution safety independently without masquerading as a market
    falsifier.
    """
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    wave = dict(ignition.get("causal_wave_snapshot") or {})
    timing_episode_id = str(
        result.get("causal_episode_id")
        or ignition.get("causal_episode_id")
        or wave.get("causal_wave_id")
        or ""
    )
    side = _u(result.get("side") or ignition.get("side"), "ABSTAIN")
    # The launcher freezes the acquisition handoff onto the decision before
    # any downstream owner runs.  Reading ``state.bias_acquisition_handoff``
    # here would create a second observation time: Bias can advance between
    # Entry evaluation and Market Truth classification, leaving Opportunity,
    # Lifecycle and Recorder with mutually inconsistent wave identities.
    #
    # Missing evidence is intentionally UNKNOWN.  Do not fall back to mutable
    # state, because an absent frozen handoff is not permission to borrow a
    # newer one.
    handoff = dict(result.get("bias_acquisition_handoff") or {})
    transition = dict(ignition.get("transition_authority") or {})
    handoff_status = str(handoff.get("status") or "").upper()
    handoff_wave_id = str(handoff.get("causal_wave_id") or "")
    handoff_owns_side = bool(
        handoff_status == "SEALED"
        and str(handoff.get("side") or "").upper() == side
        and handoff_wave_id
    )
    handoff_terminates_wave = bool(
        handoff_wave_id
        and handoff_status.startswith(("TERMINATED_", "INVALIDATED_"))
    )
    transition_owns_side = bool(
        str(transition.get("status") or "").upper() == "REVERSAL_CONFIRMED"
        and str(transition.get("control_ownership_state") or "").upper()
            == "CONTROL_OWNED"
        and str(transition.get("side") or "").upper() == side
    )
    if handoff_owns_side or handoff_terminates_wave:
        # A terminal handoff no longer owns direction, but it still owns the
        # immutable identity of the wave it falsifies.  Falling back to the
        # timing-attempt id here would strand the old canonical opportunity.
        market_wave_id = handoff_wave_id
        identity_authority = "BIAS_CASH_WAVE_OWNERSHIP"
    elif transition_owns_side and timing_episode_id:
        market_wave_id = timing_episode_id
        identity_authority = "FAST_TRANSITION_CONTROL_OWNERSHIP"
    else:
        # Compatibility only. A timing episode can be observed as active, but
        # it cannot replace another Market Wave merely because its id changed.
        market_wave_id = timing_episode_id
        identity_authority = "TIMING_EPISODE_FALLBACK"
    row = {
        "version": WAVE_LIFECYCLE_VERSION,
        "owner": OWNER,
        "causal_wave_id": market_wave_id or None,
        "market_wave_id": market_wave_id or None,
        "timing_episode_id": timing_episode_id or None,
        "identity_authority": identity_authority,
        "status": "UNKNOWN",
        "falsifier": None,
        "time_alone_falsifies": False,
    }
    if not market_wave_id:
        row["reason"] = "NO_CAUSAL_WAVE_ID"
        return row

    tombstone = _wave_tombstones(state).get(market_wave_id)
    if tombstone:
        row.update(
            status="FALSIFIED",
            reason="MARKET_TRUTH_TERMINAL_WAVE",
            falsifier=str(tombstone),
        )
        return row

    if str(handoff.get("causal_wave_id") or "") == market_wave_id:
        handoff_status = str(handoff.get("status") or "UNKNOWN").upper()
        if handoff_status == "SEALED":
            row.update(status="ACTIVE", reason="BIAS_CASH_WAVE_OWNED")
        elif handoff_status.startswith(("TERMINATED_", "INVALIDATED_")):
            falsifier = str(
                handoff.get("termination_reason")
                or handoff.get("invalidation_reason")
                or handoff_status
            )
            row.update(
                status="FALSIFIED", reason="BIAS_CAUSAL_FALSIFIER",
                falsifier=falsifier,
            )
            _remember_wave_falsifier(state, market_wave_id, falsifier)
        return row

    contradictions = dict(wave.get("contradictions") or {})
    if contradictions.get("opposing_cash_control"):
        row.update(
            status="FALSIFIED",
            reason="OPPOSITE_DUAL_CASH_CONTROL",
            falsifier="OPPOSITE_DUAL_CASH_CONTROL",
        )
        _remember_wave_falsifier(
            state, market_wave_id, "OPPOSITE_DUAL_CASH_CONTROL",
        )
        return row

    _status, knowledge = _knowledge_state(result)
    if knowledge == "UNKNOWN_SOURCE":
        row.update(status="UNKNOWN", reason=knowledge)
    else:
        # A current identified wave stays alive until Market Truth observes a
        # causal contradiction. GO/WAIT, economics and elapsed time are not
        # lifecycle evidence.
        row.update(status="ACTIVE", reason="CAUSAL_WAVE_NOT_FALSIFIED")
    return row


def _source_health(ignition, knowledge_state):
    clock = dict(ignition.get("clock_quality") or {})
    sources = {}
    for venue, row in clock.items():
        row = dict(row or {})
        explicit_status = row.get("source_health") or row.get(
            "temporal_status"
        )
        if explicit_status is not None:
            status = _u(explicit_status, "UNKNOWN")
        elif row.get("valid") is True or row.get("clock_valid") is True:
            status = "FRESH"
        elif row.get("valid") is False or row.get("clock_valid") is False:
            status = "DEGRADED"
        else:
            status = "UNKNOWN"
        sources[str(venue)] = {
            "status": status,
            "epoch": row.get("epoch"),
            "temporal_uncertainty_ms": row.get(
                "temporal_uncertainty_ms", row.get("uncertainty_ms")
            ),
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
    ownership_state = transition.get("control_ownership_state")
    ownership_confirmed = bool(
        ownership_state == "CONTROL_OWNED"
        or (
            ownership_state is None
            and transition.get("new_side_cash_control_confirmed")
        )
    )
    transition_confirmed = bool(
        ignition.get("transition_confirmed")
        and _u(transition.get("status")) == "REVERSAL_CONFIRMED"
        and ownership_confirmed
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
    raw_wave = dict(observation.get("cash_control_wave") or {})
    cash_roots = {}
    root_aliases = {
        "binance_spot": "spot", "spot": "spot",
        "coinbase_spot": "coinbase", "coinbase": "coinbase",
    }
    for raw_name, raw_row in dict(raw_wave.get("cash_roots") or {}).items():
        name = root_aliases.get(str(raw_name).lower())
        if not name:
            continue
        raw_row = dict(raw_row or {})
        cash_roots[name] = {
            "side": _u(raw_row.get("side"), "ABSTAIN"),
            "state": _u(raw_row.get("state")),
            "conversion_held": bool(raw_row.get("conversion_held")),
            "root_evidence_id": str(
                raw_row.get("root_evidence_id") or ""
            ) or None,
            "epoch": raw_row.get("epoch"),
        }
    position_wave = dict(observation.get("position_wave") or {})
    raw_ownership = dict(observation.get("control_ownership") or {})
    raw_handoff = dict(raw_ownership.get("acquisition_handoff") or {})
    sealed_handoff = dict(raw_handoff.get("sealed_payload") or {})
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
        "observed_at_ms": int(
            _number(observation.get("observed_at_ms"), 0.0)
        ),
        "cash_control_wave": {
            "version": str(raw_wave.get("version") or "UNKNOWN"),
            "causal_wave_id": str(
                raw_wave.get("causal_wave_id") or ""
            ) or None,
            "side": _u(raw_wave.get("side"), "ABSTAIN"),
            "state": _u(raw_wave.get("state")),
            "observed_at_ms": int(
                _number(raw_wave.get("observed_at_ms"), 0.0)
            ),
            "cash_roots": cash_roots,
        },
        "position_wave": {
            "causal_wave_id": str(
                position_wave.get("causal_wave_id") or ""
            ) or None,
            "status": _u(position_wave.get("status")),
            "falsifier": str(position_wave.get("falsifier") or "") or None,
        },
        "control_ownership": {
            "current_bias_side": _u(
                raw_ownership.get("current_bias_side"), "ABSTAIN",
            ),
            "acquisition_handoff": {
                "status": _u(raw_handoff.get("status")),
                "sealed": raw_handoff.get("sealed") is True,
                "authority": raw_handoff.get("authority"),
                "entry_authority": raw_handoff.get("entry_authority"),
                "causal_wave_id": str(
                    raw_handoff.get("causal_wave_id") or ""
                ) or None,
                "handoff_hash": str(
                    raw_handoff.get("handoff_hash") or ""
                ) or None,
                "side": _u(raw_handoff.get("side"), "ABSTAIN"),
                "first_converting_segment_onset_ms": raw_handoff.get(
                    "first_converting_segment_onset_ms"
                ),
                "ownership_completed_ms": raw_handoff.get(
                    "ownership_completed_ms"
                ),
                "bias_version": raw_handoff.get("bias_version"),
                "sealed_payload": sealed_handoff,
            },
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

    # Price/flow horizons describe current deterioration, but they do not
    # identify a new causal process.  Control transfers only when the
    # independent cross-cash observer supplies a fresh, distinct opposing
    # wave whose two roots each show flow-led conversion that survived.
    cash_wave = canonical["cash_control_wave"]
    required_cash_roots = {"spot", "coinbase"}
    current_ms = canonical["observed_at_ms"]
    wave_ms = int(cash_wave.get("observed_at_ms", 0) or 0)
    wave_age_ms = current_ms - wave_ms
    wave_current = bool(
        current_ms > 0 and wave_ms > 0
        and 0 <= wave_age_ms <= cross_cash_causal_wave.MAX_OBSERVATION_AGE_MS
    )
    roots_prove_control = bool(
        required_cash_roots <= set(cash_wave.get("cash_roots") or {})
        and all(
            row.get("side") == cash_wave.get("side")
            and row.get("state") == "FLOW_LED_CONVERSION"
            and row.get("conversion_held")
            and row.get("root_evidence_id")
            for row in (cash_wave.get("cash_roots") or {}).values()
        )
    )
    ownership = canonical["control_ownership"]
    handoff = ownership["acquisition_handoff"]
    sealed = dict(handoff.get("sealed_payload") or {})
    handoff_side = _u(sealed.get("side"), "ABSTAIN")
    handoff_digest = _stable_hash(sealed) if sealed else ""
    handoff_segments = [
        dict(row or {}) for row in (sealed.get("segment_evidence") or ())[:2]
    ]
    handoff_roots = set(sealed.get("directional_cash_roots") or ())
    handoff_onset_ms = int(
        _number(sealed.get("first_converting_segment_onset_ms"), 0.0)
    )
    handoff_completed_ms = int(
        _number(sealed.get("ownership_completed_ms"), 0.0)
    )
    ownership_handoff_valid = bool(
        handoff.get("status") == "SEALED"
        and handoff.get("sealed") is True
        and handoff.get("authority") is False
        and handoff.get("entry_authority") is False
        and handoff_side in {"LONG", "SHORT"}
        and sealed.get("version") == "CASH_CONTROL_ACQUISITION_HANDOFF_V1"
        and handoff.get("side") == handoff_side
        and handoff.get("handoff_hash") == handoff_digest
        and handoff.get("causal_wave_id")
            == "cash-acquisition:%s" % handoff_digest[:20]
        and handoff_roots
            == {"BINANCE_SPOT_CASH", "COINBASE_USD_CASH"}
        and int(_number(sealed.get("temporal_persistence_segments"), 0.0))
            >= 2
        and len(handoff_segments) == 2
        and all(
            _u(segment.get("state")) == "CONVERTING"
            and _u(segment.get("side"), "ABSTAIN") == handoff_side
            and _u((segment.get("price") or {}).get("vote"), "ABSTAIN")
                == handoff_side
            and _u((segment.get("flow") or {}).get("vote"), "ABSTAIN")
                == handoff_side
            for segment in handoff_segments
        )
        and handoff_onset_ms > 0
        and handoff_completed_ms > handoff_onset_ms
        and all(
            handoff.get(name) == sealed.get(name)
            for name in (
                "side", "first_converting_segment_onset_ms",
                "ownership_completed_ms", "bias_version",
            )
        )
        and all(
            name in dict(sealed.get("venue_epochs") or {})
            for name in ("spot", "coinbase")
        )
        and ownership.get("current_bias_side") == handoff_side
    )
    opposing_control_proven = bool(
        wave_current
        and cash_wave.get("causal_wave_id")
        and cash_wave.get("causal_wave_id") != episode_id
        and cash_wave.get("side") in {"LONG", "SHORT"}
        and cash_wave.get("side") != side
        and cash_wave.get("state") == "CONTROL_PERSISTING"
        and roots_prove_control
        and ownership_handoff_valid
        and handoff_side == cash_wave.get("side")
    )
    frozen_wave = canonical["position_wave"]
    frozen_wave_falsified = bool(
        frozen_wave.get("causal_wave_id") == episode_id
        and frozen_wave.get("status") == "FALSIFIED"
        and frozen_wave.get("falsifier")
    )

    falsifiers = []
    if frozen_wave_falsified:
        falsifiers.append("PRIMARY_CASH_STOPS_OR_REVERSES_CONVERSION")
    if opposing_control_proven:
        falsifiers.extend([
            "OPPOSITE_DUAL_CASH_CONTROL", "OPPOSITE_CASH_PRICE_ACCEPTANCE",
        ])
    if frozen_wave_falsified and opposite_oi_build:
        falsifiers.append("FRESH_OPPOSITE_POSITION_BUILD")
    falsifiers = [
        name for name in dict.fromkeys(falsifiers)
        if name in set(contract.get("falsifiers") or ())
    ]

    if opposing_control_proven:
        status = "CONTROL_TRANSFER"
        reason = "DISTINCT_OPPOSING_CASH_WAVE_OWNS_CONTROL"
        falsified = True
    elif frozen_wave_falsified:
        status = "FALSIFY"
        reason = "FROZEN_MARKET_WAVE_CAUSALLY_FALSIFIED"
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
            "snapshot_persistent_dual_adverse": persistent_dual_adverse,
            "snapshot_primary_adverse": primary_adverse,
            "snapshot_primary_persistent": primary_persistent,
            "snapshot_secondary_adverse": secondary_adverse_evidence,
            "snapshot_secondary_supports_old_side": (
                secondary_supports_old_side
            ),
            "cash_control_wave": cash_wave,
            "cash_control_wave_current": wave_current,
            "cash_control_roots_proven": roots_prove_control,
            "control_ownership": ownership,
            "control_ownership_handoff_valid": ownership_handoff_valid,
            "opposing_cash_control_proven": opposing_control_proven,
            "position_wave": frozen_wave,
            "position_wave_falsified": frozen_wave_falsified,
            "snapshot_deterioration_can_falsify_alone": False,
        },
        "pnl_fields_used_for_thesis": False,
        "capital_fields_used_for_thesis": False,
        "immutable_entry_truth": True,
    }
