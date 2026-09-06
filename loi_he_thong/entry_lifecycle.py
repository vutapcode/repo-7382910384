"""Recorder-only identities for wave, timing attempt and economics.

The active strategy already owns causal wave and canonical opportunity IDs.
This module only links them to the immutable current execution proof; it has no
Entry, Execution, Guardian or Risk authority.
"""

import hashlib
import json

VERSION = "ENTRY_LIFECYCLE_V2_RETRYABLE_TIMING_ATTEMPTS"

_EXPIRING_TIMING_REASONS = frozenset({
    "WAIT_CASH_IGNITION_FUTURES_RESPONSE",
    "WAIT_FUTURES_ALERT_CASH_RESPONSE",
    "WAIT_STALE_COINBASE",
    "WAIT_FEED_GROUP_NOT_READY",
    "WAIT_CURRENT_CASH_CONVERSION",
    "WAIT_CAUSAL_LEADER_UNCERTAIN",
    "IGNITION_EVIDENCE_DECAYED",
    "IGNITION_EPISODE_SAFETY_EXPIRED",
    "CLOCK_OR_EVENT_TIME_INVALID",
    "EXECUTED_FLOW_EPOCH_RESET",
})


def _proof_and_epochs(result):
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    dependencies = dict(result.get("authority_dependencies") or {})
    proof = dict(
        dependencies.get("current_execution_proof")
        or ignition.get("current_execution_proof")
        or {}
    )
    epochs = dict(dependencies.get("causal_epochs") or {})
    if not epochs:
        epochs = {
            str(name): int((row or {}).get("epoch", 0) or 0)
            for name, row in dict(ignition.get("clock_quality") or {}).items()
            if isinstance(row, dict) and int((row or {}).get("epoch", 0) or 0) > 0
        }
    if not epochs:
        epochs = dict(
            (ignition.get("causal_wave_snapshot") or {}).get("epochs") or {}
        )
    return proof, {
        str(name): int(value or 0)
        for name, value in sorted(epochs.items())
        if int(value or 0) > 0
    }


def _timing_payload(result):
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    proof, epochs = _proof_and_epochs(result)
    episode_id = str(
        result.get("causal_episode_id")
        or ignition.get("causal_episode_id") or ignition.get("episode_id") or ""
    )
    side = str(result.get("side") or ignition.get("side") or "ABSTAIN").upper()
    proof_identity = str(
        proof.get("proof_hash") or proof.get("evidence_id") or ""
    )
    if not episode_id or side not in {"LONG", "SHORT"} or not proof_identity:
        return None
    return {
        "causal_wave_id": episode_id,
        "side": side,
        "current_execution_proof": proof_identity,
        "proof_observed_at_ms": int(proof.get("observed_at_ms", 0) or 0),
        "venue_epochs": epochs,
    }


def timing_attempt_id(result):
    payload = _timing_payload(result)
    if payload is None:
        return None
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return "timing:" + hashlib.sha256(encoded).hexdigest()


def wave_still_alive(state, causal_wave_id):
    if not causal_wave_id:
        return False
    active_id = getattr(state, "canonical_opportunity_active_episode_id", None)
    if active_id != causal_wave_id:
        return False
    if not getattr(state, "canonical_opportunity_active", False):
        return False
    return True

def can_create_attempt(state, result):
    payload = _timing_payload(result)
    if not payload:
        return False, "INVALID_PAYLOAD"
    
    causal_wave_id = payload.get("causal_wave_id")
    if not wave_still_alive(state, causal_wave_id):
        return False, "WAVE_NO_LONGER_ALIVE"

    previous_terminal = bool(getattr(state, "entry_timing_attempt_terminal", False))
    previous_id = str(getattr(state, "entry_timing_attempt_id", "") or "")
    if previous_id and not previous_terminal:
        return False, "PARALLEL_ACTIVE_ATTEMPT"

    attempt_id = timing_attempt_id(result)
    expired_ids = list(getattr(state, "entry_timing_expired_attempt_ids", ()) or ())
    if attempt_id in expired_ids:
        return False, "STALE_PROOF_REUSE"

    previous_identity = dict(getattr(state, "entry_timing_attempt_identity", {}) or {})
    if previous_identity and payload.get("causal_wave_id") == previous_identity.get("causal_wave_id"):
        if dict(payload.get("venue_epochs") or {}) != dict(previous_identity.get("venue_epochs") or {}):
            return False, "CAUSAL_EPOCH_CHANGED_WITHIN_WAVE"

    return True, "ALLOWED"

def observe(state, result, gate_outcome, *, economic_opportunity_id=None):
    """Observe sequential timing attempts without changing authorization."""
    result = dict(result or {})
    gate = dict(gate_outcome or {})
    attempt_id = timing_attempt_id(result)
    attempt_identity = _timing_payload(result)
    reason = str(gate.get("reason") or result.get("reason") or "UNKNOWN").upper()
    expiring = reason in _EXPIRING_TIMING_REASONS
    previous_id = str(getattr(state, "entry_timing_attempt_id", "") or "")
    previous_identity = dict(
        getattr(state, "entry_timing_attempt_identity", {}) or {}
    )
    previous_terminal = bool(
        getattr(state, "entry_timing_attempt_terminal", False)
    )
    expired_ids = list(
        getattr(state, "entry_timing_expired_attempt_ids", ()) or ()
    )
    events = []

    epoch_cross = bool(
        attempt_identity and previous_identity
        and attempt_identity.get("causal_wave_id")
        == previous_identity.get("causal_wave_id")
        and dict(attempt_identity.get("venue_epochs") or {})
        != dict(previous_identity.get("venue_epochs") or {})
    )
    if epoch_cross:
        expiring = True
        reason = "CAUSAL_EPOCH_CHANGED_WITHIN_WAVE"
        # An epoch change must produce a new causal wave, not merely a new
        # timing proof under the old identity.
        attempt_id = None
        attempt_identity = None

    def expire(active_id, expiry_reason):
        nonlocal previous_terminal
        if not active_id or active_id in expired_ids:
            return
        expired_ids.append(active_id)
        del expired_ids[:-64]
        state.entry_timing_expired_attempt_ids = list(expired_ids)
        state.entry_timing_attempt_terminal = True
        state.entry_timing_attempt_status = "EXPIRED"
        previous_terminal = True
        events.append(("TIMING_ATTEMPT_EXPIRED", {
            "timing_attempt_id": active_id,
            "result": expiry_reason,
            "gate_outcome": gate,
        }))

    if expiring and previous_id and not previous_terminal and (
        attempt_id in {None, previous_id}
    ):
        expire(previous_id, reason)

    stale_reuse = bool(attempt_id and attempt_id in expired_ids)
    if stale_reuse:
        reuse_identity = (attempt_id, reason)
        if reuse_identity != getattr(
            state, "entry_timing_attempt_last_reuse_identity", None
        ):
            state.entry_timing_attempt_last_reuse_identity = reuse_identity
            events.append(("TIMING_ATTEMPT_REUSE_REJECTED", {
                "timing_attempt_id": attempt_id,
                "result": "EXPIRED_PROOF_CANNOT_REOPEN_ATTEMPT",
                "gate_outcome": gate,
            }))
        attempt_id = None
        attempt_identity = None

    if attempt_id and attempt_id != previous_id:
        if previous_id and not previous_terminal:
            events.append(("TIMING_ATTEMPT_CLOSED", {
                "timing_attempt_id": previous_id,
                "result": "SUPERSEDED_BY_FRESH_EXECUTION_PROOF",
            }))
        state.entry_timing_attempt_id = attempt_id
        state.entry_timing_attempt_identity = dict(attempt_identity or {})
        state.entry_timing_attempt_terminal = False
        state.entry_timing_attempt_status = "OPEN"
        state.entry_timing_attempt_last_wait_identity = None
        previous_terminal = False
        events.append(("TIMING_ATTEMPT_OPENED", {
            "timing_attempt_id": attempt_id,
            "identity": attempt_identity,
        }))

    if attempt_id and not previous_terminal:
        if expiring:
            expire(attempt_id, reason)
        elif gate.get("allowed"):
            state.entry_timing_attempt_terminal = True
            state.entry_timing_attempt_status = "PASSED"
            events.append(("TIMING_ATTEMPT_PASSED", {
                "timing_attempt_id": attempt_id,
                "gate_outcome": gate,
            }))
        elif str(gate.get("owner") or "").upper() == "TIMING":
            state.entry_timing_attempt_status = "WAIT"
            wait_identity = (
                attempt_id, str(gate.get("reason") or "UNKNOWN")
            )
            if wait_identity != getattr(
                state, "entry_timing_attempt_last_wait_identity", None
            ):
                state.entry_timing_attempt_last_wait_identity = wait_identity
                events.append(("TIMING_ATTEMPT_WAIT", {
                    "timing_attempt_id": attempt_id,
                    "gate_outcome": gate,
                }))
        else:
            state.entry_timing_attempt_terminal = True
            state.entry_timing_attempt_status = "CLOSED"
            events.append(("TIMING_ATTEMPT_CLOSED", {
                "timing_attempt_id": attempt_id,
                "result": "REJECTED",
                "gate_outcome": gate,
            }))

    economic_id = (
        int(economic_opportunity_id or 0) or None
    )
    previous_link = getattr(state, "entry_economic_opportunity_link", None)
    link_identity = (attempt_id, economic_id)
    if attempt_id and economic_id and link_identity != getattr(
        state, "entry_economic_opportunity_link", None
    ):
        previous_attempt, previous_economic = (
            previous_link if isinstance(previous_link, tuple)
            and len(previous_link) == 2 else (None, None)
        )
        if previous_economic != economic_id:
            events.append(("ECONOMIC_OPPORTUNITY_OPENED", {
                "economic_opportunity_id": economic_id,
                "canonical_opportunity_id": economic_id,
                "causal_wave_id": (
                    (attempt_identity or {}).get("causal_wave_id")
                ),
            }))
        elif previous_attempt != attempt_id:
            events.append(("ECONOMIC_OPPORTUNITY_REPRICED", {
                "economic_opportunity_id": economic_id,
                "canonical_opportunity_id": economic_id,
                "previous_timing_attempt_id": previous_attempt,
                "timing_attempt_id": attempt_id,
                "reason": "FRESH_EXECUTION_PROOF",
            }))
        state.entry_economic_opportunity_link = link_identity
        events.append(("ECONOMIC_OPPORTUNITY_LINKED", {
            "timing_attempt_id": attempt_id,
            "economic_opportunity_id": economic_id,
            "canonical_opportunity_id": economic_id,
        }))

    return {
        "version": VERSION,
        "causal_wave_id": (_timing_payload(result) or {}).get("causal_wave_id"),
        "timing_attempt_id": attempt_id,
        "economic_opportunity_id": economic_id,
        "status": (
            getattr(state, "entry_timing_attempt_status", "UNOBSERVED")
            if attempt_id else (
                "EXPIRED" if stale_reuse or expiring else "UNOBSERVED"
            )
        ),
        "events": events,
    }
