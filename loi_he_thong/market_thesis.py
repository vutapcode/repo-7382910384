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


VERSION = "MARKET_THESIS_V5_POSITION_LINEAGE"
OBSERVATION_VERSION = "MARKET_THESIS_OBSERVATION_V4_THESIS_LIFECYCLE"
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


def _entry_cash_lineage(result, side):
    """Freeze the authority-free root identity already observed at Entry."""
    snapshot = dict((result or {}).get("cross_cash_causal_wave") or {})
    active = dict(snapshot.get("active_wave") or {})
    aliases = {
        "binance_spot": "spot", "spot": "spot",
        "coinbase_spot": "coinbase", "coinbase": "coinbase",
    }
    roots = {}
    origin_rows = dict(
        active.get("origin_cash_roots") or active.get("cash_roots") or {}
    )
    observed_rows = dict(active.get("cash_roots") or {})
    for raw_name, raw_row in origin_rows.items():
        name = aliases.get(str(raw_name).lower())
        row = dict(raw_row or {})
        if name:
            roots[name] = row
    valid = bool(
        str(active.get("causal_wave_id") or "")
        and _u(active.get("side"), "ABSTAIN") == side
        and _u(active.get("state")) == "CONTROL_PERSISTING"
        and set(roots) == {"spot", "coinbase"}
        and all(
            _u(row.get("side"), "ABSTAIN") == side
            and _u(row.get("state")) == "FLOW_LED_CONVERSION"
            and row.get("conversion_held") is True
            and str(row.get("root_evidence_id") or "")
            for row in roots.values()
        )
    )
    return {
        "version": "ENTRY_CASH_LINEAGE_V1",
        "status": "BOUND" if valid else "UNBOUND",
        "reason": (
            "EXACT_CROSS_CASH_WAVE_FROZEN"
            if valid else "NO_EXACT_CROSS_CASH_WAVE_AT_ENTRY"
        ),
        "causal_wave_id": (
            str(active.get("causal_wave_id")) if valid else None
        ),
        "side": side,
        "root_evidence_ids": (
            {name: str(row.get("root_evidence_id"))
             for name, row in roots.items()} if valid else {}
        ),
        "entry_observed_root_evidence_ids": ({
            aliases[str(name).lower()]: str(
                (row or {}).get("root_evidence_id") or ""
            )
            for name, row in observed_rows.items()
            if str(name).lower() in aliases
        } if valid else {}),
        "venue_epochs": (
            {name: int(row.get("epoch", 0) or 0)
             for name, row in roots.items()} if valid else {}
        ),
        "observed_at_ms": int(snapshot.get("observed_at_ms", 0) or 0),
        "authority": False,
        "entry_authority": False,
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
    market_wave_id = str(
        result.get("market_wave_id")
        or (result.get("bias_acquisition_handoff") or {}).get(
            "causal_wave_id"
        )
        or causal_episode_id
        or ""
    )
    entry_cash_lineage = _entry_cash_lineage(result, side)
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
    elif proof in {"PERSISTENT_METAORDER", "METAORDER_CONTINUATION"}:
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
        "market_wave_id": market_wave_id or None,
        "entry_cash_lineage": entry_cash_lineage,
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
        "challenge": "UNKNOWN",
        "incumbent_thesis": {
            "state": "UNKNOWN", "reason": reason,
            "market_wave_id": contract.get("market_wave_id"),
            "terminal": False,
        },
        "challenger_process": {
            "state": "UNKNOWN", "reason": reason,
            "authority": False,
        },
        "causal_episode_id": contract.get("causal_episode_id"),
        "entry_market_truth_hash": contract.get("contract_hash"),
        "observation_hash": _stable_hash(canonical),
        "observed_falsifiers": [],
        "old_thesis_falsified": False,
        "pnl_fields_used_for_thesis": False,
        "capital_fields_used_for_thesis": False,
        "immutable_entry_truth": True,
    }


def observe(contract, observation, *, state=None):
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
    raw_position_cash = dict(
        observation.get("position_cash_wave") or {}
    )
    raw_position_segments = [
        dict(row or {})
        for row in (raw_position_cash.get("segments") or ())[:3]
    ]
    raw_cross_cash = dict(observation.get("cross_cash_wave") or {})
    raw_lineage = dict(raw_position_cash.get("causal_lineage") or {})
    raw_terminal = dict(raw_lineage.get("terminal_evidence") or {})
    raw_terminal_reclaims = {}
    for raw_name, raw_row in dict(
        raw_terminal.get("termination_evidence") or {}
    ).items():
        name = {
            "binance_spot": "spot", "spot": "spot",
            "coinbase_spot": "coinbase", "coinbase": "coinbase",
        }.get(str(raw_name).lower())
        if name:
            raw_row = dict(raw_row or {})
            raw_terminal_reclaims[name] = {
                "side": _u(raw_row.get("side"), "ABSTAIN"),
                "state": _u(raw_row.get("state")),
                "reclaimed_past_root": bool(
                    raw_row.get("reclaimed_past_root")
                ),
                "root_evidence_id": str(
                    raw_row.get("root_evidence_id") or ""
                ) or None,
            }
    raw_position_identity = dict(
        raw_position_cash.get("position_identity") or {}
    )
    derivative_context = dict(observation.get("derivative_context") or {})
    raw_ownership = dict(observation.get("control_ownership") or {})
    raw_handoff = dict(raw_ownership.get("acquisition_handoff") or {})
    sealed_handoff = dict(raw_handoff.get("sealed_payload") or {})
    canonical = {
        "version": str(observation.get("version") or "UNKNOWN"),
        "causal_episode_id": str(
            observation.get("causal_episode_id") or ""
        ) or None,
        "position_cycle_id": str(
            observation.get("position_cycle_id") or ""
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
        "position_cash_wave": {
            "version": str(raw_position_cash.get("version") or "UNKNOWN"),
            "observation_scope": str(
                raw_position_cash.get("observation_scope") or ""
            ) or None,
            "previous_side": _u(
                raw_position_cash.get("previous_side"), "ABSTAIN",
            ),
            "raw_side": _u(
                raw_position_cash.get("raw_side"), "ABSTAIN",
            ),
            "candidate_side": _u(
                raw_position_cash.get("candidate_side"), "ABSTAIN",
            ),
            "wave_state": _u(raw_position_cash.get("wave_state")),
            "phase": _u(raw_position_cash.get("phase")),
            "control_transfer_confirmed": bool(
                raw_position_cash.get("control_transfer_confirmed")
            ),
            "old_side_still_converts": bool(
                raw_position_cash.get("old_side_still_converts")
            ),
            "old_side_failure_evidence": bool(
                raw_position_cash.get("old_side_failure_evidence")
            ),
            "old_side_still_viable": bool(
                raw_position_cash.get("old_side_still_viable")
            ),
            "liquidity_state": _u(
                raw_position_cash.get("liquidity_state")
            ),
            "observed_at_ms": int(_number(
                raw_position_cash.get("observed_at_ms"), 0.0,
            )),
            "gap_or_epoch_invalid": bool(
                raw_position_cash.get("gap_or_epoch_invalid")
            ),
            "segments": [{
                "state": _u(row.get("state")),
                "side": _u(row.get("side"), "ABSTAIN"),
                "price_side": _u(row.get("price_side"), "ABSTAIN"),
                "flow_side": _u(row.get("flow_side"), "ABSTAIN"),
                "newer_ts": row.get("newer_ts"),
                "older_ts": row.get("older_ts"),
            } for row in raw_position_segments],
            "position_identity": {
                "position_cycle_id": str(
                    raw_position_identity.get("position_cycle_id") or ""
                ) or None,
                "market_wave_id": str(
                    raw_position_identity.get("market_wave_id") or ""
                ) or None,
                "market_truth_hash": str(
                    raw_position_identity.get("market_truth_hash") or ""
                ) or None,
                "entry_mechanism": _u(
                    raw_position_identity.get("entry_mechanism")
                ),
            },
            "causal_lineage": {
                "entry_causal_wave_id": str(
                    raw_lineage.get("entry_causal_wave_id") or ""
                ) or None,
                "entry_side": _u(
                    raw_lineage.get("entry_side"), "ABSTAIN"
                ),
                "current_causal_wave_id": str(
                    raw_lineage.get("current_causal_wave_id") or ""
                ) or None,
                "current_side": _u(
                    raw_lineage.get("current_side"), "ABSTAIN"
                ),
                "current_state": _u(raw_lineage.get("current_state")),
                "lineage_relation": _u(
                    raw_lineage.get("lineage_relation")
                ),
                "incumbent_terminal": bool(
                    raw_lineage.get("incumbent_terminal")
                ),
                "terminal_evidence": {
                    "causal_wave_id": str(
                        raw_terminal.get("causal_wave_id") or ""
                    ) or None,
                    "side": _u(raw_terminal.get("side"), "ABSTAIN"),
                    "termination_reason": _u(
                        raw_terminal.get("termination_reason")
                    ),
                    "terminated_at_ms": int(_number(
                        raw_terminal.get("terminated_at_ms"), 0.0,
                    )),
                    "termination_evidence": raw_terminal_reclaims,
                    "authority": False,
                },
                "authority": False,
            },
            "authority": False,
        },
        "cross_cash_wave": {
            "version": str(raw_cross_cash.get("version") or "UNKNOWN"),
            "state": _u(raw_cross_cash.get("state")),
            "side": _u(raw_cross_cash.get("side"), "ABSTAIN"),
            "causal_wave_id": str(
                raw_cross_cash.get("causal_wave_id") or ""
            ) or None,
            "observed_at_ms": int(_number(
                raw_cross_cash.get("observed_at_ms"), 0.0,
            )),
            "authority": False,
        },
        "position_market_wave_id": str(
            observation.get("position_market_wave_id") or ""
        ) or None,
        "position_market_truth_hash": str(
            observation.get("position_market_truth_hash") or ""
        ) or None,
        "derivative_context": {
            "oi_regime": _u(derivative_context.get("oi_regime")),
            "oi_change_pct": derivative_context.get("oi_change_pct"),
            "liquidation_phase": _u(
                derivative_context.get("liquidation_phase")
            ),
            "authority": False,
            "can_create_direction": False,
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
    market_wave_id = str(
        contract.get("market_wave_id") or episode_id or ""
    )
    position_identity = canonical["position_cash_wave"][
        "position_identity"
    ]
    if (
        canonical["position_market_wave_id"] != market_wave_id
        or canonical["position_market_truth_hash"]
            != contract.get("contract_hash")
        or position_identity.get("position_cycle_id")
            != canonical.get("position_cycle_id")
        or position_identity.get("market_wave_id") != market_wave_id
        or position_identity.get("market_truth_hash")
            != contract.get("contract_hash")
        or position_identity.get("entry_mechanism")
            != _u(contract.get("mechanism"))
    ):
        return _observation_unknown(
            contract, "POSITION_THESIS_IDENTITY_MISMATCH", canonical,
        )
    known_terminal = (
        _wave_tombstones(state).get(market_wave_id) if state is not None
        else None
    )
    if canonical["gap_or_epoch_invalid"] and not known_terminal:
        return _observation_unknown(
            contract, "THESIS_OBSERVATION_DISCONTINUITY", canonical,
        )
    position_cash = canonical["position_cash_wave"]
    if (
        position_cash.get("observation_scope")
        == "POSITION_RELATIVE_CASH_WAVE"
        and position_cash.get("gap_or_epoch_invalid")
        and not known_terminal
    ):
        return _observation_unknown(
            contract, "POSITION_CASH_WAVE_DISCONTINUITY", canonical,
        )
    if (
        position_cash.get("observation_scope")
        == "POSITION_RELATIVE_CASH_WAVE"
        and position_cash.get("previous_side") != side
    ):
        return _observation_unknown(
            contract, "POSITION_CASH_WAVE_IDENTITY_MISMATCH", canonical,
        )
    if not known_terminal and (not anchors or any(
        source_health.get(anchor) != "FRESH" for anchor in anchors
    )):
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
    )
    position_wave_age_ms = (
        canonical["observed_at_ms"]
        - int(position_cash.get("observed_at_ms", 0) or 0)
    )
    position_relative_current = bool(
        position_cash.get("observation_scope")
        == "POSITION_RELATIVE_CASH_WAVE"
        and position_cash.get("previous_side") == side
        and position_cash.get("observed_at_ms", 0) > 0
        and 0 <= position_wave_age_ms
            <= cross_cash_causal_wave.MAX_OBSERVATION_AGE_MS
    )
    frozen_wave = canonical["position_wave"]
    frozen_wave_falsified = bool(
        frozen_wave.get("causal_wave_id") == market_wave_id
        and frozen_wave.get("status") == "FALSIFIED"
        and frozen_wave.get("falsifier")
    )
    position_wave_state = str(
        position_cash.get("wave_state") or "UNKNOWN"
    ).upper()
    cross_wave_state = str(
        canonical["cross_cash_wave"].get("state") or "UNKNOWN"
    ).upper()
    derivative = canonical["derivative_context"]
    oi_contraction = derivative.get("oi_regime") in {
        "CONTRACTION", "CONTRACTING", "UNWIND",
        "PRICE_UP_OI_CONTRACTION", "PRICE_DOWN_OI_CONTRACTION",
    }
    latest_position_segment = next(iter(
        position_cash.get("segments") or ()
    ), {})
    entry_lineage = dict(contract.get("entry_cash_lineage") or {})
    lineage = dict(position_cash.get("causal_lineage") or {})
    entry_cross_id = str(entry_lineage.get("causal_wave_id") or "")
    terminal = dict(lineage.get("terminal_evidence") or {})
    terminal_reason = _u(terminal.get("termination_reason"))
    terminal_reclaims = dict(terminal.get("termination_evidence") or {})
    primary_reclaim = dict(terminal_reclaims.get(primary) or {})
    primary_reclaim_proven = bool(
        primary
        and primary_reclaim.get("side") == side
        and primary_reclaim.get("state") == "FLOW_NONCONVERSION"
        and primary_reclaim.get("reclaimed_past_root")
        and primary_reclaim.get("root_evidence_id")
    )
    relation = _u(lineage.get("lineage_relation"))
    lineage_bound = bool(
        entry_lineage.get("status") == "BOUND"
        and entry_cross_id
        and _u(entry_lineage.get("side"), "ABSTAIN") == side
        and lineage.get("entry_causal_wave_id") == entry_cross_id
        and lineage.get("entry_side") == side
    )

    distinct_opposing_wave = bool(
        wave_current
        and cash_wave.get("causal_wave_id")
        and cash_wave.get("causal_wave_id") != entry_cross_id
        and cash_wave.get("side") in {"LONG", "SHORT"}
        and cash_wave.get("side") != side
    )
    if distinct_opposing_wave and roots_prove_control:
        challenger_state = "PERSISTING"
        challenger_reason = "DISTINCT_DUAL_CASH_FLOW_LED_WAVE"
    elif distinct_opposing_wave:
        challenger_state = "EMERGING"
        challenger_reason = "OPPOSING_PROCESS_NOT_YET_PERSISTING"
    elif position_wave_state == "FAKEOUT_ABSORBED":
        challenger_state = "FAILED_ATTACK"
        challenger_reason = "OPPOSITE_EXECUTED_FLOW_ABSORBED"
    elif cross_wave_state == "PRICE_LED_CHASE" or (
        latest_position_segment.get("state")
        == "PRICE_ACCEPTED_FLOW_UNRESOLVED"
    ):
        challenger_state = "PRICE_LED"
        challenger_reason = "PRICE_MOVED_WITHOUT_CAUSAL_CASH_OWNERSHIP"
    elif position_wave_state in {"TRANSITION", "CONTROL_TRANSFER"}:
        challenger_state = "EMERGING"
        challenger_reason = "POSITION_RELATIVE_TRANSITION_UNCONFIRMED"
    elif position_relative_current:
        challenger_state = "NONE"
        challenger_reason = "NO_DISTINCT_CHALLENGER_PROCESS"
    else:
        challenger_state = "UNKNOWN"
        challenger_reason = "CHALLENGER_EVIDENCE_UNAVAILABLE"

    mechanism = _u(contract.get("mechanism"))
    terminal_market_reason = str(known_terminal or "")
    incumbent_reason = "EXACT_ENTRY_LINEAGE_UNRESOLVED"
    if known_terminal or frozen_wave_falsified:
        incumbent_state = "FAILED"
        incumbent_reason = (
            terminal_market_reason
            or str(frozen_wave.get("falsifier") or "MARKET_TRUTH_TERMINAL")
        )
    elif not lineage_bound or not position_relative_current:
        incumbent_state = "UNKNOWN"
        incumbent_reason = "EXACT_ENTRY_LINEAGE_NOT_CURRENT"
    elif lineage.get("incumbent_terminal"):
        if terminal.get("causal_wave_id") != entry_cross_id:
            incumbent_state = "UNKNOWN"
            incumbent_reason = "TERMINAL_EVIDENCE_IDENTITY_MISMATCH"
        elif terminal_reason == "VENUE_EPOCH_BREAK":
            incumbent_state = "UNKNOWN"
            incumbent_reason = "ENTRY_LINEAGE_BROKEN_BY_SOURCE_EPOCH"
        elif (
            terminal_reason == "FLOW_NONCONVERSION_WITH_RECLAIM"
            and not primary_reclaim_proven
        ):
            incumbent_state = "ERODING"
            incumbent_reason = "PRIMARY_CASH_RECLAIM_NOT_PROVEN"
        elif (
            mechanism == "FAILED_REVERSION_CONTINUATION"
            and terminal_reason == "FLOW_NONCONVERSION_WITH_RECLAIM"
            and challenger_state != "PERSISTING"
        ):
            incumbent_state = "ERODING"
            incumbent_reason = "REVERSION_ACCEPTANCE_NOT_YET_SURVIVING"
        else:
            incumbent_state = "FAILED"
            incumbent_reason = "%s_%s" % (mechanism, terminal_reason)
    elif relation == "SAME_CAUSAL_WAVE":
        if position_wave_state in {"CONTROL_ERODING", "ABSORPTION"}:
            incumbent_state = "ERODING"
            incumbent_reason = "EXACT_WAVE_FLOW_NO_LONGER_CONVERTING"
        else:
            incumbent_state = "ALIVE"
            incumbent_reason = "EXACT_ENTRY_CAUSAL_WAVE_PERSISTS"
    elif relation == "NEW_SAME_SIDE_WAVE":
        incumbent_state = "UNKNOWN"
        incumbent_reason = "NEW_SAME_SIDE_WAVE_CANNOT_RESURRECT_ENTRY_WAVE"
    else:
        incumbent_state = "UNKNOWN"
        incumbent_reason = "ENTRY_CAUSAL_WAVE_NOT_OBSERVED"

    if incumbent_state == "FAILED" and state is not None and not known_terminal:
        _remember_wave_falsifier(state, market_wave_id, incumbent_reason)

    opposing_control_proven = challenger_state == "PERSISTING"
    position_takeover_proven = bool(
        incumbent_state == "FAILED" and opposing_control_proven
    )
    if incumbent_state == "FAILED" and challenger_state == "PERSISTING":
        status = "CONTROL_TRANSFER"
        reason = "INCUMBENT_FAILED_AND_CHALLENGER_PERSISTING"
        challenge = "TAKEOVER_PROVEN"
        falsified = True
    elif incumbent_state == "FAILED":
        status = "FALSIFY"
        reason = "EXACT_INCUMBENT_THESIS_CAUSALLY_FAILED"
        challenge = "OLD_THESIS_CAUSALLY_DEAD"
        falsified = True
    elif incumbent_state == "ERODING":
        status = "DIVERGENCE"
        reason = "EXACT_INCUMBENT_THESIS_ERODING"
        challenge = "OLD_CONTROL_ERODING"
        falsified = False
    elif incumbent_state == "ALIVE" and challenger_state in {
        "NONE", "FAILED_ATTACK", "PRICE_LED",
    }:
        status = "SUPPORT"
        reason = "EXACT_INCUMBENT_THESIS_STILL_ALIVE"
        if challenger_state == "FAILED_ATTACK":
            challenge = "FAKEOUT_ABSORBED"
        elif challenger_state == "PRICE_LED":
            challenge = "PRICE_LED_CHASE"
        elif position_wave_state in {"PULLBACK", "LULL"}:
            challenge = "SQUEEZE_ONLY" if oi_contraction else "PULLBACK"
        else:
            challenge = "INCUMBENT_ALIVE"
        falsified = False
    elif incumbent_state == "ALIVE":
        status = "DIVERGENCE"
        reason = "INCUMBENT_ALIVE_BUT_CHALLENGER_CONTESTING"
        challenge = "UNPROVEN_TRANSITION"
        falsified = False
    else:
        status = "UNKNOWN"
        reason = incumbent_reason
        challenge = (
            "NEW_SAME_SIDE_WAVE"
            if relation == "NEW_SAME_SIDE_WAVE" else "UNKNOWN"
        )
        falsified = False

    falsifiers = []
    if falsified:
        falsifiers.append("PRIMARY_CASH_STOPS_OR_REVERSES_CONVERSION")
    if status == "CONTROL_TRANSFER":
        falsifiers.extend([
            "OPPOSITE_DUAL_CASH_CONTROL", "OPPOSITE_CASH_PRICE_ACCEPTANCE",
        ])
    if falsified and opposite_oi_build:
        falsifiers.append("FRESH_OPPOSITE_POSITION_BUILD")
    falsifiers = [
        name for name in dict.fromkeys(falsifiers)
        if name in set(contract.get("falsifiers") or ())
    ]

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
        "challenge": challenge,
        "incumbent_thesis": {
            "state": incumbent_state,
            "reason": incumbent_reason,
            "market_wave_id": market_wave_id,
            "entry_causal_wave_id": entry_cross_id or None,
            "lineage_relation": relation,
            "mechanism": mechanism,
            "terminal": incumbent_state == "FAILED",
        },
        "challenger_process": {
            "state": challenger_state,
            "reason": challenger_reason,
            "causal_wave_id": cash_wave.get("causal_wave_id"),
            "side": cash_wave.get("side"),
            "authority": False,
        },
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
            "position_cash_wave": position_cash,
            "position_cash_wave_current": position_relative_current,
            "position_takeover_proven": position_takeover_proven,
            "cross_cash_wave": canonical["cross_cash_wave"],
            "derivative_context": derivative,
            "position_wave": frozen_wave,
            "position_wave_falsified": frozen_wave_falsified,
            "entry_cash_lineage": entry_lineage,
            "lineage_bound": lineage_bound,
            "snapshot_deterioration_can_falsify_alone": False,
        },
        "pnl_fields_used_for_thesis": False,
        "capital_fields_used_for_thesis": False,
        "immutable_entry_truth": True,
    }
