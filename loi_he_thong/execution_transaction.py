"""Canonical live execution/protection transaction state.

This module owns physical execution lifecycle facts only.  It never interprets
market direction or the entry thesis.  A filled position is either explicitly
``UNPROTECTED_EXPOSURE`` or exchange-verified ``POSITION_PROTECTED``; an order
ACK alone can never bridge those states.
"""

from copy import deepcopy
import time


VERSION = "EXECUTION_PROTECTION_TRANSACTION_V1"
MAX_TRANSITIONS = 32

TERMINAL_STATES = frozenset({
    "NO_POSITION",
    "FLAT_VERIFIED",
    "POSITION_CLOSED",
})

RECOVERY_STATES = frozenset({
    "ORDER_SENT",
    "ACK_KNOWN",
    "EXECUTION_UNKNOWN",
    "PARTIAL_FILL_CONFIRMED",
    "FILL_CONFIRMED",
    "UNPROTECTED_EXPOSURE",
    "PROTECTION_SENT",
    "PROTECTION_ACKNOWLEDGED",
    "PROTECTION_VERIFICATION_FAILED",
    "EMERGENCY_FLATTEN_SENT",
    "RECOVERY_REQUIRED",
    "INVARIANT_BROKEN",
})

_ALLOWED = {
    "INTENT_CREATED": {
        "ORDER_SENT", "NO_POSITION", "RECOVERY_REQUIRED",
        "INVARIANT_BROKEN",
    },
    "ORDER_SENT": {
        "ACK_KNOWN", "FILL_CONFIRMED", "PARTIAL_FILL_CONFIRMED",
        "EXECUTION_UNKNOWN", "NO_POSITION", "RECOVERY_REQUIRED",
        "INVARIANT_BROKEN",
    },
    "ACK_KNOWN": {
        "FILL_CONFIRMED", "PARTIAL_FILL_CONFIRMED", "EXECUTION_UNKNOWN",
        "NO_POSITION", "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "EXECUTION_UNKNOWN": {
        "FILL_CONFIRMED", "PARTIAL_FILL_CONFIRMED", "NO_POSITION",
        "RECOVERY_REQUIRED", "FLAT_VERIFIED", "INVARIANT_BROKEN",
    },
    "PARTIAL_FILL_CONFIRMED": {
        "UNPROTECTED_EXPOSURE", "EMERGENCY_FLATTEN_SENT",
        "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "FILL_CONFIRMED": {
        "UNPROTECTED_EXPOSURE", "EMERGENCY_FLATTEN_SENT",
        "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "UNPROTECTED_EXPOSURE": {
        "PROTECTION_SENT", "EMERGENCY_FLATTEN_SENT", "RECOVERY_REQUIRED",
        "INVARIANT_BROKEN",
    },
    "PROTECTION_SENT": {
        "PROTECTION_ACKNOWLEDGED", "PROTECTION_VERIFIED",
        "PROTECTION_VERIFICATION_FAILED", "EXECUTION_UNKNOWN",
        "EMERGENCY_FLATTEN_SENT", "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "PROTECTION_ACKNOWLEDGED": {
        "PROTECTION_VERIFIED", "PROTECTION_VERIFICATION_FAILED",
        "EXECUTION_UNKNOWN", "EMERGENCY_FLATTEN_SENT",
        "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "PROTECTION_VERIFICATION_FAILED": {
        "EMERGENCY_FLATTEN_SENT", "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "PROTECTION_VERIFIED": {
        "POSITION_PROTECTED", "EMERGENCY_FLATTEN_SENT",
        "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "POSITION_PROTECTED": {
        "POSITION_CLOSED", "EMERGENCY_FLATTEN_SENT",
        "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "EMERGENCY_FLATTEN_SENT": {
        "FLAT_VERIFIED", "RECOVERY_REQUIRED", "INVARIANT_BROKEN",
    },
    "RECOVERY_REQUIRED": {
        "FLAT_VERIFIED", "POSITION_PROTECTED", "POSITION_CLOSED",
        "EMERGENCY_FLATTEN_SENT",
        "INVARIANT_BROKEN",
    },
    "INVARIANT_BROKEN": {"RECOVERY_REQUIRED", "FLAT_VERIFIED"},
    "NO_POSITION": set(),
    "FLAT_VERIFIED": set(),
    "POSITION_CLOSED": set(),
}


def _now(wall_time=None, monotonic_time=None):
    return (
        time.time() if wall_time is None else float(wall_time),
        time.monotonic() if monotonic_time is None else float(monotonic_time),
    )


def _bounded_append(transaction, row):
    rows = list(transaction.get("transitions") or ())
    rows.append(row)
    transaction["transitions"] = rows[-MAX_TRANSITIONS:]


def begin(
    state, *, intent_id, side, quantity, wall_time=None,
    monotonic_time=None, metadata=None,
):
    """Start one physical transaction, refusing to overwrite an active one."""
    existing = getattr(state, "wstrade_execution_transaction", None)
    if isinstance(existing, dict) and str(existing.get("state")) not in TERMINAL_STATES:
        state.wstrade_execution_transaction_invariant_error = (
            "ACTIVE_TRANSACTION_REPLACEMENT_REFUSED"
        )
        state.wstrade_execution_recovery_required = True
        state.execution_unknown = True
        return deepcopy(existing)

    wall, mono = _now(wall_time, monotonic_time)
    transaction = {
        "version": VERSION,
        "transaction_id": str(intent_id),
        "intent_id": str(intent_id),
        "side": str(side).upper(),
        "quantity": float(quantity),
        "state": "INTENT_CREATED",
        "started_at": wall,
        "started_monotonic": mono,
        "updated_at": wall,
        "updated_monotonic": mono,
        "sequence": 1,
        "invariant_ok": True,
        "metadata": dict(metadata or {}),
        "transitions": [],
    }
    _bounded_append(transaction, {
        "sequence": 1,
        "state": "INTENT_CREATED",
        "wall_time": wall,
        "monotonic_time": mono,
        "detail": {},
    })
    state.wstrade_execution_transaction = transaction
    return deepcopy(transaction)


def transition(
    state, next_state, *, wall_time=None, monotonic_time=None, detail=None,
):
    """Advance the transaction without ever hiding an impossible transition.

    An invalid transition latches recovery instead of raising through an
    emergency protection path.  The caller may continue flatten/reconciliation,
    while the invariant failure remains visible and new entries stay sealed.
    """
    transaction = getattr(state, "wstrade_execution_transaction", None)
    if not isinstance(transaction, dict):
        state.wstrade_execution_transaction_invariant_error = "MISSING_TRANSACTION"
        state.wstrade_execution_recovery_required = True
        state.execution_unknown = True
        return None

    current = str(transaction.get("state") or "")
    target = str(next_state or "").upper()
    wall, mono = _now(wall_time, monotonic_time)
    idempotent = target == current
    if not idempotent and target not in _ALLOWED.get(current, set()):
        message = f"INVALID_TRANSITION:{current}->{target}"
        transaction["invariant_ok"] = False
        transaction["invariant_error"] = message
        transaction["state"] = "INVARIANT_BROKEN"
        state.wstrade_execution_transaction_invariant_error = message
        state.wstrade_execution_recovery_required = True
        state.execution_unknown = True
        target = "INVARIANT_BROKEN"

    sequence = int(transaction.get("sequence", 0) or 0) + 1
    transaction.update({
        "state": target,
        "sequence": sequence,
        "updated_at": wall,
        "updated_monotonic": mono,
    })
    info = dict(detail or {})

    if target == "ORDER_SENT":
        transaction["order_sent_at"] = wall
        transaction["order_sent_monotonic"] = mono
        if info.get("decision_to_submit_ms") is not None:
            transaction["decision_to_submit_ms"] = max(
                0.0, float(info["decision_to_submit_ms"])
            )
    if target == "ACK_KNOWN":
        sent_mono = transaction.get("order_sent_monotonic")
        if sent_mono is not None:
            transaction["submit_to_ack_ms"] = max(
                0.0, (mono - float(sent_mono)) * 1000.0
            )
        if info.get("client_order_id"):
            transaction["entry_client_order_id"] = str(
                info["client_order_id"]
            )
        if info.get("order_id") is not None:
            transaction["entry_order_id"] = info["order_id"]

    if target in {"FILL_CONFIRMED", "PARTIAL_FILL_CONFIRMED"}:
        transaction["fill_confirmed_at"] = wall
        transaction["fill_confirmed_monotonic"] = mono
        if info.get("exchange_fill_time_ms") is not None:
            transaction["exchange_fill_time_ms"] = info["exchange_fill_time_ms"]
    fill_mono = transaction.get("fill_confirmed_monotonic")
    if fill_mono is not None:
        elapsed = max(0.0, (mono - float(fill_mono)) * 1000.0)
        if target == "PROTECTION_SENT":
            transaction["fill_to_protection_submit_ms"] = elapsed
        elif target == "PROTECTION_ACKNOWLEDGED":
            transaction["fill_to_protection_ack_ms"] = elapsed
        elif target == "PROTECTION_VERIFIED":
            transaction["fill_to_protection_verified_ms"] = elapsed

    if target == "EXECUTION_UNKNOWN":
        transaction["execution_unknown_started_at"] = wall
        transaction["execution_unknown_started_monotonic"] = mono
    unknown_mono = transaction.get("execution_unknown_started_monotonic")
    if (
        unknown_mono is not None
        and target != "EXECUTION_UNKNOWN"
        and transaction.get("execution_unknown_duration_ms") is None
    ):
        transaction["execution_unknown_duration_ms"] = max(
            0.0, (mono - float(unknown_mono)) * 1000.0
        )
        transaction["execution_unknown_resolved_at"] = wall

    if target == "PROTECTION_SENT" and info.get("client_algo_id"):
        transaction["protection_client_algo_id"] = str(
            info["client_algo_id"]
        )
    if target in {"PROTECTION_ACKNOWLEDGED", "PROTECTION_VERIFIED"}:
        if info.get("algo_id") is not None:
            transaction["protection_algo_id"] = info["algo_id"]
    if target == "EMERGENCY_FLATTEN_SENT" and info.get("client_order_id"):
        transaction["emergency_flatten_client_order_id"] = str(
            info["client_order_id"]
        )
    if idempotent:
        info["idempotent"] = True

    _bounded_append(transaction, {
        "sequence": sequence,
        "state": target,
        "wall_time": wall,
        "monotonic_time": mono,
        "detail": info,
    })
    state.wstrade_execution_transaction = transaction
    return deepcopy(transaction)


def snapshot(state):
    transaction = getattr(state, "wstrade_execution_transaction", None)
    return deepcopy(transaction) if isinstance(transaction, dict) else None


def requires_reconciliation(transaction):
    if not isinstance(transaction, dict):
        return False
    return str(transaction.get("state") or "") not in TERMINAL_STATES
