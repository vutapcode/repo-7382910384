"""Canonical causal-episode identity for the active Tier-S Entry Council.

This module does not create signals, confidence, edge or vetoes.  It only
groups PRESSURE/ACCEPTANCE/RELEASE observations, deduplicates an already
authorized GO, and records empirical funnel counters. Short-lived evidence is
intentionally not persisted across a process/data gap.
"""
import time

VERSION = "CANONICAL_ENTRY_OPPORTUNITY_V4_QUALIFICATION_TRANSITIONS"
WAIT_GRACE_SECONDS = 5.0
CAUSAL_PHASES = {
    "PROBE", "EARLY", "MATURE", "ACCEPTANCE", "RELEASE",
    "PRESSURE_BUILDING", "WAIT_CHASE",
}


def _signature(result):
    result = result or {}
    episode_id = str(result.get("causal_episode_id") or "")
    if episode_id:
        return (episode_id,)
    # Mode/phase may evolve inside one impulse. Side is the stable causal identity;
    # claim() still guarantees only one execution attempt for the whole episode.
    return (str(result.get("side", "ABSTAIN") or "ABSTAIN").upper(),)


def _hard_reset(result):
    reason = str((result or {}).get("reason") or "").upper()
    return bool(
        (result or {}).get("data_gap")
        or "STALE" in reason
        or "GAP" in reason
        or reason in {"PRICE_AND_FLOW_OPPOSE", "WAIT_EXTERNAL_UNAVAILABLE"}
    )


def _candidate(result):
    result = result or {}
    side = str(result.get("side", "ABSTAIN") or "ABSTAIN").upper()
    phase = str(result.get("phase", "ARMED") or "ARMED").upper()
    return bool(
        side in ("LONG", "SHORT")
        and (result.get("decision") == "GO" or phase in CAUSAL_PHASES)
        and not _hard_reset(result)
    )


def _reset_active(state):
    state.canonical_opportunity_active = False
    state.canonical_opportunity_signature = None
    state.canonical_opportunity_active_qualified = False
    state.canonical_opportunity_wait_since = 0.0
    state.canonical_opportunity_last_evidence_at = 0.0
    state.canonical_opportunity_active_episode_id = None


def _snapshot(state, *, active, new, qualified_now, transition,
              causal_episode_id, grace_active, signature=None):
    """Expose current-vs-latched qualification without changing authority."""
    qualified_ever = bool(
        getattr(state, "canonical_opportunity_active_qualified", False)
    )
    row = {
        "active": bool(active),
        "new": bool(new),
        # Backward-compatible funnel field: one qualification per episode.
        "qualified": qualified_ever,
        "qualified_now": bool(qualified_now),
        "qualified_ever": qualified_ever,
        "qualification_transition": bool(transition),
        "opportunity_id": int(
            getattr(state, "canonical_opportunity_count", 0) or 0
        ),
        "causal_episode_id": causal_episode_id,
        "grace_active": bool(grace_active),
        "grace_seconds": WAIT_GRACE_SECONDS,
    }
    if signature is not None:
        row["signature"] = signature
    return row


def observe(state, result, qualified=False, now=None):
    """Observe the canonical council result without changing its behavior."""
    now = time.time() if now is None else float(now)
    go = bool((result or {}).get("decision") == "GO")
    candidate = _candidate(result)
    if _hard_reset(result):
        _reset_active(state)
        return _snapshot(
            state, active=False, new=False, qualified_now=False,
            transition=False, causal_episode_id=None, grace_active=False,
        )

    if not candidate:
        active = bool(getattr(state, "canonical_opportunity_active", False))
        last_evidence = float(
            getattr(state, "canonical_opportunity_last_evidence_at", 0.0) or 0.0
        )
        grace = bool(
            active and last_evidence > 0.0
            and now - last_evidence <= WAIT_GRACE_SECONDS
        )
        if not grace:
            _reset_active(state)
        elif float(getattr(state, "canonical_opportunity_wait_since", 0.0) or 0.0) <= 0.0:
            state.canonical_opportunity_wait_since = now
        return _snapshot(
            state, active=grace, new=False, qualified_now=False,
            transition=False,
            causal_episode_id=(
                getattr(state, "canonical_opportunity_active_episode_id", None)
                if grace else None
            ),
            grace_active=grace,
        )

    signature = _signature(result)
    previous = tuple(
        getattr(state, "canonical_opportunity_signature", ()) or ()
    )
    is_new = bool(
        not getattr(state, "canonical_opportunity_active", False)
        or signature != previous
    )
    if is_new:
        state.canonical_opportunity_count = int(
            getattr(state, "canonical_opportunity_count", 0) or 0
        ) + 1
        state.canonical_opportunity_active = True
        state.canonical_opportunity_signature = signature
        state.canonical_opportunity_active_qualified = False
        state.canonical_opportunity_active_episode_id = (
            (result or {}).get("causal_episode_id")
            or f"tier-s:{int(getattr(state, 'canonical_opportunity_count', 0) or 0)}"
        )
    state.canonical_opportunity_last_evidence_at = now
    if go:
        state.canonical_opportunity_last_go_at = now
    state.canonical_opportunity_wait_since = 0.0

    qualified_now = bool(go and qualified)
    qualification_transition = bool(qualified_now and not bool(
        getattr(state, "canonical_opportunity_active_qualified", False)
    ))
    if qualification_transition:
        state.canonical_opportunity_qualified = int(
            getattr(state, "canonical_opportunity_qualified", 0) or 0
        ) + 1
        state.canonical_opportunity_active_qualified = True

    return _snapshot(
        state, active=True, new=is_new, qualified_now=qualified_now,
        transition=qualification_transition,
        causal_episode_id=(
            getattr(state, "canonical_opportunity_active_episode_id", None)
        ),
        grace_active=False, signature=signature,
    )


def claim(state, opportunity_id):
    """Allow at most one execution attempt for one canonical GO episode."""
    opportunity_id = int(opportunity_id or 0)
    consumed = int(
        getattr(state, "canonical_last_consumed_opportunity_id", 0) or 0
    )
    if opportunity_id <= 0 or opportunity_id <= consumed:
        return False
    state.canonical_last_consumed_opportunity_id = opportunity_id
    return True


def mark_captured(state, opportunity_id):
    opportunity_id = int(opportunity_id or 0)
    last = int(
        getattr(state, "canonical_last_captured_opportunity_id", 0) or 0
    )
    if opportunity_id <= 0 or opportunity_id <= last:
        return False
    state.canonical_last_captured_opportunity_id = opportunity_id
    state.canonical_opportunity_captured = int(
        getattr(state, "canonical_opportunity_captured", 0) or 0
    ) + 1
    return True
