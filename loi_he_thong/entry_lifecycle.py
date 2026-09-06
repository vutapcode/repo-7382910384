"""Recorder-only identities for wave, timing attempt and economics.

The active strategy already owns causal wave and canonical opportunity IDs.
This module only links them to the immutable current execution proof; it has no
Entry, Execution, Guardian or Risk authority.
"""

import hashlib
import json

VERSION = "ENTRY_LIFECYCLE_V1"


def _timing_payload(result):
    result = dict(result or {})
    ignition = dict(result.get("ignition") or {})
    dependencies = dict(result.get("authority_dependencies") or {})
    proof = dict(dependencies.get("current_execution_proof") or {})
    episode_id = str(
        result.get("causal_episode_id")
        or ignition.get("causal_episode_id") or ignition.get("episode_id") or ""
    )
    side = str(result.get("side") or ignition.get("side") or "ABSTAIN").upper()
    proof_identity = str(
        proof.get("proof_hash") or proof.get("evidence_id") or ""
    )
    epochs = {
        str(name): int(value or 0)
        for name, value in sorted(
            dict(dependencies.get("causal_epochs") or {}).items()
        )
    }
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


def observe(state, result, gate_outcome, *, economic_opportunity_id=None):
    """Return recorder events for one GO proof without changing authorization."""
    result = dict(result or {})
    gate = dict(gate_outcome or {})
    attempt_id = timing_attempt_id(result) if result.get("decision") == "GO" else None
    previous_id = str(getattr(state, "entry_timing_attempt_id", "") or "")
    previous_terminal = bool(
        getattr(state, "entry_timing_attempt_terminal", False)
    )
    events = []

    if attempt_id and attempt_id != previous_id:
        if previous_id and not previous_terminal:
            events.append(("TIMING_ATTEMPT_CLOSED", {
                "timing_attempt_id": previous_id,
                "result": "SUPERSEDED_BY_FRESH_EXECUTION_PROOF",
            }))
        state.entry_timing_attempt_id = attempt_id
        state.entry_timing_attempt_terminal = False
        state.entry_timing_attempt_status = "OPEN"
        previous_terminal = False
        events.append(("TIMING_ATTEMPT_OPENED", {
            "timing_attempt_id": attempt_id,
            "identity": _timing_payload(result),
        }))

    if attempt_id and not previous_terminal:
        if gate.get("allowed"):
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
    link_identity = (attempt_id, economic_id)
    if attempt_id and economic_id and link_identity != getattr(
        state, "entry_economic_opportunity_link", None
    ):
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
            if attempt_id else "UNOBSERVED"
        ),
        "events": events,
    }
