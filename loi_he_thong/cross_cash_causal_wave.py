"""Authority-free receive-time observation of cross-cash causal waves.

This observer answers one research question only: did executed cash flow lead
and sustain price conversion, and did the other independent cash venue join
the same still-living process?  It never creates Bias, Entry direction, or an
execution permission.  Time bounds freshness; temporal proximity alone is
never evidence that two roots are the same wave.
"""

import hashlib


VERSION = "CROSS_CASH_CAUSAL_WAVE_V1"
CASH = ("binance_spot", "coinbase_spot")
MIN_PRICE_BPS = 0.15
MAX_OBSERVATION_AGE_MS = 5_000
MIN_QTY = {"binance_spot": 0.015, "coinbase_spot": 0.002}


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _available(row):
    return int(
        row.get("available_time_ms")
        or row.get("receive_time_ms")
        or 0
    )


def _price(row, field="price"):
    return _f(row.get(field))


def _bps(current, reference):
    return (
        (current - reference) / reference * 10_000.0
        if current > 0.0 and reference > 0.0 else 0.0
    )


def _sign(side):
    return 1.0 if side == "LONG" else -1.0


def _healthy(row):
    return bool(
        row
        and row.get("clock_valid")
        and str(row.get("source_health") or "UNKNOWN").upper() == "FRESH"
    )


def _material(row):
    venue = str(row.get("venue") or row.get("source") or "")
    side = str(row.get("side") or "NEUTRAL").upper()
    return bool(
        venue in CASH
        and side in {"LONG", "SHORT"}
        and _healthy(row)
        and _f(row.get("total_qty")) >= MIN_QTY[venue]
        and abs(_f(row.get("imbalance"))) >= 0.20
        and abs(_f(row.get("signed_quote"))) > 0.0
    )


def _row_identity(row):
    return str(
        row.get("evidence_id")
        or row.get("event_id")
        or "%s:%s:%s" % (
            row.get("venue") or row.get("source"),
            int(row.get("epoch", 0) or 0),
            int(row.get("bucket_start_ms", 0) or 0),
        )
    )


def _venue_observation(rows, now_ms):
    rows = sorted(
        (dict(row) for row in (rows or ()) if _available(row) > 0),
        key=lambda row: (_available(row), int(row.get("bucket_start_ms", 0) or 0)),
    )
    if not rows:
        return {"state": "UNKNOWN", "reason": "NO_EXECUTED_FLOW"}
    latest = rows[-1]
    age_ms = int(now_ms) - _available(latest)
    if age_ms < 0 or age_ms > MAX_OBSERVATION_AGE_MS or not _healthy(latest):
        return {
            "state": "UNKNOWN", "reason": "SOURCE_NOT_CURRENT",
            "epoch": int(latest.get("epoch", 0) or 0), "age_ms": age_ms,
        }

    material_indexes = [
        index for index, row in enumerate(rows) if _material(row)
    ]
    # A newer root without a post-flow response is still unresolved.  It must
    # not erase the newest root whose response and hold are already observable.
    completed_indexes = [
        index for index in material_indexes
        if sum(
            1 for row in rows[index + 1:]
            if int(row.get("epoch", 0) or 0)
            == int(rows[index].get("epoch", 0) or 0)
            and _available(row) > _available(rows[index])
            and _healthy(row)
        ) >= 2
    ]
    root_index = (
        completed_indexes[-1] if completed_indexes
        else material_indexes[-1] if material_indexes else None
    )
    if root_index is None:
        return {
            "state": "UNKNOWN", "reason": "NO_MATERIAL_FLOW_ROOT",
            "epoch": int(latest.get("epoch", 0) or 0), "age_ms": age_ms,
        }
    root = rows[root_index]
    side = str(root.get("side") or "NEUTRAL").upper()
    epoch = int(root.get("epoch", 0) or 0)
    after = [
        row for row in rows[root_index + 1:]
        if int(row.get("epoch", 0) or 0) == epoch
        and _available(row) > _available(root)
        and _healthy(row)
    ]
    previous = rows[root_index - 1] if root_index > 0 else {}
    root_price = _price(root)
    pre_flow_move = _sign(side) * _bps(
        _price(root, "first_price"), _price(previous),
    )
    base = {
        "venue": str(root.get("venue") or root.get("source") or ""),
        "side": side,
        "epoch": epoch,
        "flow_onset_available_ms": _available(root),
        "root_evidence_id": _row_identity(root),
        "root_price": root_price,
        "pre_flow_price_move_bps": round(pre_flow_move, 6),
        "age_ms": age_ms,
        "authority": False,
    }
    if not after:
        return {
            **base, "state": "UNKNOWN", "reason": "POST_FLOW_RESPONSE_PENDING",
        }

    response = after[0]
    response_move = _sign(side) * _bps(_price(response), root_price)
    response_flow_aligned = bool(
        _material(response) and str(response.get("side") or "").upper() == side
    )
    base.update({
        "response_available_ms": _available(response),
        "post_flow_response_bps": round(response_move, 6),
        "response_flow_aligned": response_flow_aligned,
    })
    if pre_flow_move >= MIN_PRICE_BPS and response_move < MIN_PRICE_BPS:
        return {
            **base, "state": "PRICE_LED_CHASE",
            "reason": "PRICE_MOVED_BEFORE_FLOW_WITHOUT_POST_FLOW_CONVERSION",
        }
    if len(after) < 2:
        return {
            **base, "state": "UNKNOWN", "reason": "RESPONSE_HOLD_PENDING",
        }

    hold = after[1]
    hold_move = _sign(side) * _bps(_price(hold), root_price)
    base.update({
        "hold_available_ms": _available(hold),
        "response_hold_bps": round(hold_move, 6),
    })
    if (
        response_move >= MIN_PRICE_BPS
        and hold_move >= MIN_PRICE_BPS
        and response_flow_aligned
    ):
        return {
            **base, "state": "FLOW_LED_CONVERSION",
            "reason": "FLOW_PRECEDED_SURVIVING_PRICE_RESPONSE",
            "conversion_held": True,
        }
    if response_move <= 0.0 and hold_move <= 0.0:
        return {
            **base, "state": "FLOW_NONCONVERSION",
            "reason": "MATERIAL_FLOW_FAILED_TO_MOVE_OR_HOLD_PRICE",
            "reclaimed_past_root": hold_move <= -MIN_PRICE_BPS,
        }
    return {
        **base, "state": "UNKNOWN",
        "reason": "FLOW_PRICE_ORDERING_UNRESOLVED",
    }


def _wave_id(side, roots):
    ordered = sorted(
        (str(venue), str(row.get("root_evidence_id") or ""))
        for venue, row in roots.items()
    )
    raw = "%s|%s" % (side, "|".join("%s=%s" % item for item in ordered))
    return "cash-wave:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _same_side_conversions(observations):
    conversions = {
        venue: row for venue, row in observations.items()
        if str(row.get("state") or "") == "FLOW_LED_CONVERSION"
        and row.get("conversion_held")
    }
    sides = {str(row.get("side") or "") for row in conversions.values()}
    if len(conversions) == 2 and len(sides) == 1:
        return sides.pop(), conversions
    return None, conversions


def _event(name, wave, reason=None):
    before, after = {
        "CAUSAL_WAVE_OPENED": ("UNOBSERVED", wave.get("state")),
        "CAUSAL_WAVE_UPDATED": ("CONTROL_PERSISTING", wave.get("state")),
        "CAUSAL_WAVE_TERMINATED": ("CONTROL_PERSISTING", "FALSIFIED"),
    }.get(name, ("UNKNOWN", wave.get("state")))
    payload = {
        "version": VERSION,
        "causal_wave_id": wave.get("causal_wave_id"),
        "side": wave.get("side"),
        "state": wave.get("state"),
        "cash_roots": dict(wave.get("cash_roots") or {}),
        "venue_epochs": dict(wave.get("venue_epochs") or {}),
        "authority": False,
        "state_transition": {
            "machine": "CROSS_CASH_CAUSAL_WAVE",
            "owner": "CROSS_CASH_CAUSAL_WAVE",
            "authority": False,
            "state_before": before,
            "state_after": after,
            "trigger": reason or name,
            "causal_wave_id": wave.get("causal_wave_id"),
        },
    }
    if reason:
        payload["reason"] = reason
    return name, payload


def observe(state, histories, now_ms):
    """Update the bounded shadow observation and return recorder events."""
    observations = {
        venue: _venue_observation((histories or {}).get(venue, ()), now_ms)
        for venue in CASH
    }
    active = dict(getattr(state, "_cross_cash_causal_wave_active", {}) or {})
    events = []

    if active:
        current_epochs = {
            venue: int((observations.get(venue) or {}).get("epoch", 0) or 0)
            for venue in CASH
        }
        expected = dict(active.get("venue_epochs") or {})
        epoch_break = any(
            expected.get(venue) and current_epochs.get(venue)
            and int(expected[venue]) != int(current_epochs[venue])
            for venue in CASH
        )
        side, conversions = _same_side_conversions(observations)
        opposite_control = bool(
            side in {"LONG", "SHORT"} and side != active.get("side")
        )
        nonconversion_reclaim = any(
            str(row.get("side") or "") == str(active.get("side") or "")
            and str(row.get("state") or "") == "FLOW_NONCONVERSION"
            and bool(row.get("reclaimed_past_root"))
            for row in observations.values()
        )
        if epoch_break or opposite_control or nonconversion_reclaim:
            reason = (
                "VENUE_EPOCH_BREAK" if epoch_break else
                "OPPOSITE_DUAL_CASH_CONTROL" if opposite_control else
                "FLOW_NONCONVERSION_WITH_RECLAIM"
            )
            terminated = {
                **active, "state": "FALSIFIED",
                "terminated_at_ms": int(now_ms),
                "termination_reason": reason,
                "authority": False,
            }
            events.append(_event("CAUSAL_WAVE_TERMINATED", terminated, reason))
            active = {}

    side, conversions = _same_side_conversions(observations)
    if active and side == active.get("side"):
        old_roots = dict(active.get("cash_roots") or {})
        roots = {venue: dict(row) for venue, row in conversions.items()}
        active.update({
            "state": "CONTROL_PERSISTING",
            "cash_roots": roots,
            "venue_epochs": {
                venue: int(row.get("epoch", 0) or 0)
                for venue, row in roots.items()
            },
            "last_observed_at_ms": int(now_ms),
            "join_basis": (
                "INDEPENDENT_FLOW_LED_CONVERSION_SURVIVED_WITHOUT_"
                "INTERVENING_CAUSAL_FALSIFIER"
            ),
        })
        if old_roots != roots:
            events.append(_event("CAUSAL_WAVE_UPDATED", active))
    elif not active and side in {"LONG", "SHORT"}:
        roots = {venue: dict(row) for venue, row in conversions.items()}
        active = {
            "version": VERSION,
            "causal_wave_id": _wave_id(side, roots),
            "side": side,
            "state": "CONTROL_PERSISTING",
            "opened_at_ms": min(
                int(row.get("flow_onset_available_ms", now_ms) or now_ms)
                for row in roots.values()
            ),
            "last_observed_at_ms": int(now_ms),
            "cash_roots": roots,
            "venue_epochs": {
                venue: int(row.get("epoch", 0) or 0)
                for venue, row in roots.items()
            },
            "join_basis": (
                "INDEPENDENT_FLOW_LED_CONVERSION_SURVIVED_WITHOUT_"
                "INTERVENING_CAUSAL_FALSIFIER"
            ),
            "time_is_identity_proof": False,
            "authority": False,
        }
        events.append(_event("CAUSAL_WAVE_OPENED", active))

    aggregate_state = (
        str(active.get("state")) if active else
        "FLOW_NONCONVERSION" if any(
            row.get("state") == "FLOW_NONCONVERSION"
            for row in observations.values()
        ) else
        "PRICE_LED_CHASE" if any(
            row.get("state") == "PRICE_LED_CHASE"
            for row in observations.values()
        ) else
        "FLOW_LED_CONVERSION" if any(
            row.get("state") == "FLOW_LED_CONVERSION"
            for row in observations.values()
        ) else "UNKNOWN"
    )
    snapshot = {
        "version": VERSION,
        "state": aggregate_state,
        "causal_wave_id": active.get("causal_wave_id"),
        "side": active.get("side", "ABSTAIN"),
        "venue_observations": observations,
        "active_wave": dict(active),
        "observed_at_ms": int(now_ms),
        "authority": False,
        "entry_authority": False,
        "policy": (
            "AVAILABILITY_ORDER_AND_SURVIVING_CONVERSION_NOT_TIME_PROXIMITY"
        ),
    }
    state._cross_cash_causal_wave_active = active
    state.cross_cash_causal_wave_shadow = snapshot
    state._cross_cash_causal_wave_events = events
    return snapshot
