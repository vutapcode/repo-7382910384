"""Canonical temporal envelope shared by live evidence, recorder and replay.

This module answers one question only: *when did an event become usable by the
bot, and how trustworthy is any cross-stream ordering claim?*  Exchange event
time describes the market; availability time is the no-lookahead authority.
"""

from __future__ import annotations

import hashlib
import json
import time


VERSION = "MARKET_EVENT_CONTRACT_V1"
SOURCE_HEALTH = frozenset({
    "FRESH", "STALE", "DEGRADED", "DEAD", "CONTRADICTORY", "UNKNOWN",
})


def _i(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def deterministic_event_id(
    source, stream, epoch, sequence_start, sequence_end,
    exchange_event_time_ms, available_time_ms, payload=None,
):
    """Return a stable identity without pretending timestamps are sequences."""
    if sequence_start is not None or sequence_end is not None:
        identity = [
            str(source), str(stream), _i(epoch),
            sequence_start, sequence_end,
        ]
    else:
        identity = [
            str(source), str(stream), _i(epoch),
            _i(exchange_event_time_ms), _i(available_time_ms), payload or {},
        ]
    raw = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return "me:" + hashlib.sha256(raw).hexdigest()[:24]


def temporal_measurement(
    *, clock_uncertainty_ms=0.0, batching_uncertainty_ms=0.0,
    clock_valid=True, source_health="FRESH",
):
    health = str(source_health or "UNKNOWN").upper()
    if health not in SOURCE_HEALTH:
        health = "UNKNOWN"
    clock = max(0.0, _f(clock_uncertainty_ms))
    batching = max(0.0, _f(batching_uncertainty_ms))
    valid = bool(clock_valid) and health not in {
        "STALE", "DEAD", "CONTRADICTORY", "UNKNOWN",
    }
    return {
        "clock_uncertainty_ms": round(clock, 4),
        "batching_uncertainty_ms": round(batching, 4),
        "temporal_uncertainty_ms": round(clock + batching, 4),
        "temporal_status": "MEASURED" if valid else "UNSAFE_OR_UNKNOWN",
        "source_health": health,
    }


def build_envelope(
    *, source, stream, exchange_event_time_ms, receive_time_ms,
    available_time_ms=None, receive_time_monotonic_ns=None,
    available_time_monotonic_ns=None, epoch=0, event_id=None,
    sequence_start=None, sequence_end=None, previous_sequence=None,
    source_health="FRESH", payload_version="RAW_V1",
    clock_offset_ms=0.0, clock_jitter_ms=0.0,
    clock_uncertainty_ms=0.0, batching_uncertainty_ms=0.0,
    clock_valid=True, payload=None,
):
    """Build the common envelope; compatibility aliases stay explicit."""
    receive_ms = _i(receive_time_ms)
    event_ms = _i(exchange_event_time_ms, receive_ms)
    available_ms = max(receive_ms, _i(available_time_ms, receive_ms))
    receive_mono = _i(
        receive_time_monotonic_ns,
        time.monotonic_ns(),
    )
    available_mono = max(
        receive_mono,
        _i(available_time_monotonic_ns, receive_mono),
    )
    measurement = temporal_measurement(
        clock_uncertainty_ms=clock_uncertainty_ms,
        batching_uncertainty_ms=batching_uncertainty_ms,
        clock_valid=clock_valid,
        source_health=source_health,
    )
    identity = event_id or deterministic_event_id(
        source, stream, epoch, sequence_start, sequence_end,
        event_ms, available_ms, payload,
    )
    return {
        "event_contract_version": VERSION,
        "event_id": identity,
        "exchange_event_time_ms": event_ms,
        # Compatibility field used by V6 consumers.
        "event_time_ms": event_ms,
        "receive_time_ms": receive_ms,
        "receive_time_monotonic_ns": receive_mono,
        "available_time_ms": available_ms,
        "available_time_monotonic_ns": available_mono,
        "epoch": _i(epoch),
        "sequence_start": sequence_start,
        "sequence_end": sequence_end,
        "previous_sequence": previous_sequence,
        "source_health": measurement["source_health"],
        "payload_version": str(payload_version or "UNKNOWN"),
        "clock_offset_ms": round(_f(clock_offset_ms), 4),
        "clock_jitter_ms": round(max(0.0, _f(clock_jitter_ms)), 4),
        **measurement,
    }


def available_time_ms(record):
    """Read V7 availability, safely falling back for historical V6 rows."""
    payload = dict(record.get("payload") or {})
    return _i(
        record.get("available_time_ms"),
        payload.get("batch_available_time_ms")
        or record.get("receive_time_ms")
        or record.get("event_time_ms"),
    )
