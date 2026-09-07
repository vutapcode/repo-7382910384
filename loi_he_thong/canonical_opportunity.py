"""Canonical causal-episode identity for the active Tier-S Entry Council.

This module does not create signals, confidence, edge or vetoes. It groups
causal observations, deduplicates qualified GO episodes, and owns the
reserve -> execute -> commit handoff. A reservation is not consumption:
temporary execution failures may retry the same opportunity.
"""
import time

VERSION = "CANONICAL_ENTRY_OPPORTUNITY_V8_MARKET_TIMING_SEPARATED"
CAUSAL_PHASES = {
    "PROBE", "EARLY", "MATURE", "ACCEPTANCE", "RELEASE",
    "PRESSURE_BUILDING", "WAIT_CHASE",
}


def _signature(result, market_truth_wave=None):
    result = result or {}
    truth = dict(market_truth_wave or {})
    market_wave_id = str(
        truth.get("market_wave_id")
        or truth.get("causal_wave_id")
        or result.get("market_wave_id")
        or ""
    )
    if market_wave_id:
        return (market_wave_id,)
    episode_id = str(result.get("causal_episode_id") or "")
    if episode_id:
        return (episode_id,)
    return (str(result.get("side", "ABSTAIN") or "ABSTAIN").upper(),)


def _candidate(result):
    result = result or {}
    side = str(result.get("side", "ABSTAIN") or "ABSTAIN").upper()
    phase = str(result.get("phase", "ARMED") or "ARMED").upper()
    return bool(
        side in ("LONG", "SHORT")
        and (result.get("decision") == "GO" or phase in CAUSAL_PHASES)
    )


def _reset_active(state):
    # Strategy identity may reset on a gap, but an execution reservation is an
    # order-lifecycle fact. Only the execution/reconciliation boundary may
    # release or commit it after proving whether a fill exists.
    state.canonical_opportunity_active = False
    state.canonical_opportunity_signature = None
    state.canonical_opportunity_active_qualified = False
    state.canonical_opportunity_last_evidence_at = 0.0
    state.canonical_opportunity_active_episode_id = None


def _snapshot(state, *, active, new, qualified_now, transition,
              causal_episode_id, grace_active, signature=None):
    qualified_ever = bool(
        getattr(state, "canonical_opportunity_active_qualified", False)
    )
    row = {
        "active": bool(active),
        "new": bool(new),
        "qualified": qualified_ever,
        "qualified_now": bool(qualified_now),
        "qualified_ever": qualified_ever,
        "qualification_transition": bool(transition),
        "opportunity_id": int(
            getattr(state, "canonical_opportunity_count", 0) or 0
        ),
        "causal_episode_id": causal_episode_id,
        "market_wave_id": causal_episode_id,
        "grace_active": bool(grace_active),
        "grace_seconds": None,
    }
    if signature is not None:
        row["signature"] = signature
    return row


def bind_result_identity(result, observation):
    """Bind only a candidate accepted by the canonical wave owner.

    ``observe`` deliberately keeps the previous opportunity active when a new
    candidate has not been verified by Market Truth.  The launcher must not
    then copy that previous episode id onto the rejected candidate: doing so
    changes a LONG timing observation into a SHORT episode (or vice versa) and
    lets the old lifecycle/TTL explain the new market evidence.

    Return the immutable candidate record plus the economic opportunity id it
    may legitimately use.  An unverified candidate stays observable but is not
    attached to the previous opportunity.
    """
    bound = dict(result or {})
    observation = dict(observation or {})
    candidate_wave = str(
        bound.get("market_wave_id")
        or ((bound.get("market_truth_wave_lifecycle") or {}).get(
            "market_wave_id"
        ))
        or ((bound.get("market_truth_wave_lifecycle") or {}).get(
            "causal_wave_id"
        ))
        or ""
    )
    canonical_wave = str(
        observation.get("market_wave_id")
        or observation.get("causal_episode_id")
        or ""
    )
    rejected = str(observation.get("candidate_rejected") or "")
    if rejected:
        bound["canonical_opportunity_link_status"] = rejected
        return bound, None
    if canonical_wave:
        if candidate_wave and candidate_wave != canonical_wave:
            # This should be unreachable for an accepted candidate. Fail
            # closed without corrupting provenance or borrowing an old id.
            bound["canonical_opportunity_link_status"] = (
                "CANONICAL_EPISODE_ID_MISMATCH"
            )
            return bound, None
        bound["market_wave_id"] = canonical_wave
    bound["canonical_opportunity_link_status"] = "LINKED"
    return bound, observation.get("opportunity_id")


def observe(state, result, qualified=False, now=None, market_truth_wave=None):
    """Observe the canonical council result without changing its authority."""
    now = time.time() if now is None else float(now)
    market_truth_wave = dict(
        market_truth_wave
        or (result or {}).get("market_truth_wave_lifecycle")
        or {}
    )
    truth_status = str(market_truth_wave.get("status") or "UNKNOWN").upper()
    truth_wave_id = str(
        market_truth_wave.get("market_wave_id")
        or market_truth_wave.get("causal_wave_id") or ""
    )
    truth_identity = str(
        market_truth_wave.get("identity_authority")
        or "EXPLICIT_TYPED_MARKET_TRUTH"
    ).upper()
    go = bool((result or {}).get("decision") == "GO")
    candidate = _candidate(result)
    previous_active = bool(
        getattr(state, "canonical_opportunity_active", False)
    )
    previous_id = int(
        getattr(state, "canonical_opportunity_count", 0) or 0
    )
    previous_episode = getattr(
        state, "canonical_opportunity_active_episode_id", None
    )
    if truth_status == "FALSIFIED":
        reason = str(
            market_truth_wave.get("falsifier")
            or market_truth_wave.get("reason")
            or "MARKET_TRUTH_CAUSAL_FALSIFIER"
        )
        invalidates_active = bool(
            previous_active
            and (
                not truth_wave_id
                or truth_wave_id == str(previous_episode or "")
            )
        )
        if invalidates_active:
            _reset_active(state)
        active = bool(getattr(state, "canonical_opportunity_active", False))
        row = _snapshot(
            state, active=active, new=False, qualified_now=False,
            transition=False,
            causal_episode_id=(
                getattr(state, "canonical_opportunity_active_episode_id", None)
                if active else None
            ),
            grace_active=False,
        )
        if invalidates_active and previous_id > 0:
            row["invalidation"] = {
                "opportunity_id": previous_id,
                "causal_wave_id": previous_episode,
                "reason": reason,
            }
        if candidate:
            row["candidate_rejected"] = "MARKET_TRUTH_WAVE_TERMINAL"
        return row
    if not candidate:
        active = bool(getattr(state, "canonical_opportunity_active", False))
        return _snapshot(
            state, active=active, new=False, qualified_now=False,
            transition=False,
            causal_episode_id=(
                getattr(state, "canonical_opportunity_active_episode_id", None)
                if active else None
            ),
            grace_active=False,
        )

    signature = _signature(result, market_truth_wave)
    previous = tuple(
        getattr(state, "canonical_opportunity_signature", ()) or ()
    )
    if (
        previous_active and signature != previous
        and not (
            truth_status == "ACTIVE"
            and truth_wave_id
            and truth_wave_id == str(signature[0])
            and truth_identity != "TIMING_EPISODE_FALLBACK"
        )
    ):
        row = _snapshot(
            state, active=True, new=False, qualified_now=False,
            transition=False, causal_episode_id=previous_episode,
            grace_active=False, signature=previous,
        )
        row["candidate_rejected"] = "MARKET_TRUTH_WAVE_UNVERIFIED"
        return row
    is_new = bool(
        not getattr(state, "canonical_opportunity_active", False)
        or signature != previous
    )
    invalidation = None
    if is_new:
        if previous_active and previous_id > 0:
            invalidation = {
                "opportunity_id": previous_id,
                "causal_wave_id": previous_episode,
                "reason": "MARKET_TRUTH_ACTIVE_WAVE_SUPERSEDED",
            }
        state.canonical_opportunity_count = int(
            getattr(state, "canonical_opportunity_count", 0) or 0
        ) + 1
        state.canonical_opportunity_active = True
        state.canonical_opportunity_signature = signature
        state.canonical_opportunity_active_qualified = False
        state.canonical_opportunity_active_episode_id = (
            truth_wave_id
            or (result or {}).get("market_wave_id")
            or (result or {}).get("causal_episode_id")
            or f"tier-s:{int(getattr(state, 'canonical_opportunity_count', 0) or 0)}"
        )
    state.canonical_opportunity_last_timing_episode_id = (
        (result or {}).get("causal_episode_id")
    )
    state.canonical_opportunity_last_evidence_at = now
    if go:
        state.canonical_opportunity_last_go_at = now

    qualified_now = bool(go and qualified)
    qualification_transition = bool(qualified_now and not bool(
        getattr(state, "canonical_opportunity_active_qualified", False)
    ))
    if qualification_transition:
        state.canonical_opportunity_qualified = int(
            getattr(state, "canonical_opportunity_qualified", 0) or 0
        ) + 1
        state.canonical_opportunity_active_qualified = True

    row = _snapshot(
        state, active=True, new=is_new, qualified_now=qualified_now,
        transition=qualification_transition,
        causal_episode_id=getattr(
            state, "canonical_opportunity_active_episode_id", None
        ),
        grace_active=False, signature=signature,
    )
    if invalidation:
        row["invalidation"] = invalidation
    return row


def _clear_reservation(state):
    state.canonical_reserved_opportunity_id = 0
    state.canonical_reserved_at = 0.0
    state.canonical_reserved_context = {}


def _reservation_context(state, opportunity_id, now):
    result = dict(getattr(state, "entry_shadow_council", {}) or {})
    ignition = dict(result.get("ignition") or {})
    edge = dict(result.get("edge_tier") or {})
    cost_contract = dict(
        result.get("execution_cost_contract")
        or edge.get("execution_cost_contract")
        or {}
    )
    episode_id = (
        result.get("causal_episode_id")
        or ignition.get("causal_episode_id")
        or getattr(state, "canonical_opportunity_active_episode_id", None)
    )
    return {
        "opportunity_id": int(opportunity_id),
        "causal_episode_id": episode_id,
        "market_wave_id": (
            result.get("market_wave_id")
            or (result.get("market_truth_wave_lifecycle") or {}).get(
                "market_wave_id"
            )
            or getattr(state, "canonical_opportunity_active_episode_id", None)
        ),
        "side": str(
            result.get("side") or getattr(state, "bias_state", "ABSTAIN")
        ).upper(),
        "proof_type": str(ignition.get("proof_type") or ""),
        "proof_venue": str(ignition.get("proof_venue") or ""),
        "execution_policy": str(result.get("execution_policy") or "").upper(),
        "phase": str(result.get("phase") or "").upper(),
        "result_ts": float(result.get("ts", 0.0) or 0.0),
        "reserved_at": float(now),
        "execution_cost_contract": cost_contract,
        "authority_basis": str(result.get("authority_basis") or ""),
        "authority_dependencies": dict(
            result.get("authority_dependencies") or {}
        ),
        "authority_proof_hash": str(
            result.get("authority_proof_hash") or ""
        ),
        "active_opportunity_id": int(
            getattr(state, "canonical_opportunity_count", 0) or 0
        ),
        "epochs": {
            str(name): int((row or {}).get("epoch", 0) or 0)
            for name, row in (ignition.get("clock_quality") or {}).items()
            if isinstance(row, dict) and int((row or {}).get("epoch", 0) or 0) > 0
        },
    }


def reserve(state, opportunity_id, now=None):
    """Reserve one GO without consuming it; duplicate attempts are rejected until release."""
    opportunity_id = int(opportunity_id or 0)
    state.canonical_last_reserve_reject = None
    consumed = int(
        getattr(state, "canonical_last_consumed_opportunity_id", 0) or 0
    )
    if opportunity_id <= 0:
        state.canonical_last_reserve_reject = "INVALID_OPPORTUNITY_ID"
        return False
    if opportunity_id <= consumed:
        state.canonical_last_reserve_reject = "OPPORTUNITY_ALREADY_CONSUMED"
        return False

    reserved = int(
        getattr(state, "canonical_reserved_opportunity_id", 0) or 0
    )
    if reserved == opportunity_id:
        state.canonical_last_reserve_reject = "OPPORTUNITY_ALREADY_RESERVED"
        return False
    if reserved > opportunity_id:
        state.canonical_last_reserve_reject = "OLDER_THAN_ACTIVE_RESERVATION"
        return False
    if reserved and opportunity_id > reserved:
        state.canonical_last_reserve_reject = "ACTIVE_RESERVATION_HELD"
        return False

    now = time.time() if now is None else float(now)
    state.canonical_reserved_opportunity_id = opportunity_id
    state.canonical_reserved_at = now
    state.canonical_reserved_context = _reservation_context(
        state, opportunity_id, now
    )
    state.canonical_opportunity_reserved = int(
        getattr(state, "canonical_opportunity_reserved", 0) or 0
    ) + 1
    return True


def release(state, opportunity_id, reason="EXECUTION_NOT_CAPTURED"):
    """Release a reservation after a transient/non-fill execution outcome."""
    opportunity_id = int(opportunity_id or 0)
    reserved = int(
        getattr(state, "canonical_reserved_opportunity_id", 0) or 0
    )
    if opportunity_id <= 0 or reserved != opportunity_id:
        return False
    state.canonical_last_release_reason = str(reason or "EXECUTION_NOT_CAPTURED")
    state.canonical_last_released_opportunity_id = opportunity_id
    state.canonical_opportunity_released = int(
        getattr(state, "canonical_opportunity_released", 0) or 0
    ) + 1
    _clear_reservation(state)
    return True


def commit(state, opportunity_id):
    """Consume an opportunity only after execution has produced a captured fill."""
    opportunity_id = int(opportunity_id or 0)
    consumed = int(
        getattr(state, "canonical_last_consumed_opportunity_id", 0) or 0
    )
    reserved = int(
        getattr(state, "canonical_reserved_opportunity_id", 0) or 0
    )
    if opportunity_id <= 0 or opportunity_id <= consumed:
        return False
    if reserved and reserved != opportunity_id:
        return False
    state.canonical_last_consumed_opportunity_id = opportunity_id
    state.canonical_last_committed_opportunity_id = opportunity_id
    state.canonical_opportunity_committed = int(
        getattr(state, "canonical_opportunity_committed", 0) or 0
    ) + 1
    if reserved == opportunity_id:
        _clear_reservation(state)
    return True


def claim(state, opportunity_id):
    """Backward-compatible launcher API: claim now means reserve, not consume."""
    return reserve(state, opportunity_id)


def mark_captured(state, opportunity_id):
    """Record one captured execution and atomically commit its reservation."""
    opportunity_id = int(opportunity_id or 0)
    last = int(
        getattr(state, "canonical_last_captured_opportunity_id", 0) or 0
    )
    if opportunity_id <= 0 or opportunity_id <= last:
        return False
    if not commit(state, opportunity_id):
        return False
    state.canonical_last_captured_opportunity_id = opportunity_id
    state.canonical_opportunity_captured = int(
        getattr(state, "canonical_opportunity_captured", 0) or 0
    ) + 1
    if int(getattr(state, "canonical_opportunity_count", 0) or 0) == opportunity_id:
        _reset_active(state)
    return True
