"""Ignition Core V1: Predict -> Probe -> Prove live entry authority.

Bias is frozen before the first impulse bucket.  Futures may create an alert,
but only independent cash price plus executed flow can prove an entry.  PROBE
is evidence state only; this module never sizes or submits an order.
"""

from collections import deque
import hashlib
import json
import time

from loi_he_thong import ignition_signals


VERSION = "IGNITION_CORE_V1"
BIAS_MIN_CONF = 0.55
BORDERLINE_BIAS_MIN_CONF = 0.50
BIAS_MAX_AGE = 3.0
BIAS_MIN_PRE_IMPULSE_AGE = 1.0
EVAL_THROTTLE = 0.10
FOLLOW_MAX_MS = 600
EVIDENCE_GAP_MS = 300
EPISODE_MAX_MS = 5_000
PERSISTENT_WAVE_GAP_MS = 3_000
PERSISTENT_WAVE_MAX_MS = 15_000
LEAD_FLOOR_MS = 100.0
MAX_CONSUMED_FRACTION = 0.35
MIN_ACCEPTANCE_MS = 400
ATR_MAX_AGE_SECONDS = 120.0
FAILED_REVERSION_MAX_MS = 700
MATERIAL_PRICE_BPS = 0.15
OI_BUILD_MIN_PCT = 0.02
OI_SAMPLE_MAX_SECONDS = 20.0
MIN_VOL_BTC_BY_VENUE = {
    "spot": ignition_signals.MIN_QTY["binance_spot"],
    "coinbase": ignition_signals.MIN_QTY["coinbase_spot"],
    "futures": ignition_signals.MIN_QTY["futures"],
}
CASH = frozenset(("binance_spot", "coinbase_spot"))
ECONOMIC_CONTRACT_VERSION = "ENTRY_ECONOMICS_V3"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sign(side):
    return 1.0 if str(side).upper() == "LONG" else -1.0


def _bps(current, reference):
    current, reference = _f(current), _f(reference)
    return (current - reference) / reference * 10_000.0 if current > 0.0 and reference > 0.0 else 0.0


def _efficiency_window(rows, side, start_ms, end_ms):
    """Summarize one bounded receive-time executed-flow window.

    Quote volume is directional aggressive volume, while price progress is
    measured from the last observed trade before the window (when available)
    to the final trade inside it. Missing 100 ms rows mean no executed trade,
    not a fabricated feed gap; epoch/clock guards still fail the measurement.
    """
    # SignalEngine history is append-only receive-time order. Avoid sorting in
    # the hot evaluator; bounded scans over at most 64 rows preserve O(1).
    ordered = [
        row for row in rows if row.get("bucket_start_ms") is not None
    ]
    selected = [
        row for row in ordered
        if int(start_ms) <= int(row.get("bucket_start_ms", 0) or 0) < int(end_ms)
    ]
    if not selected:
        return None
    epochs = {int(row.get("epoch", -1) or -1) for row in selected}
    if len(epochs) != 1 or any(not row.get("clock_valid") for row in selected):
        return None
    epoch = next(iter(epochs))
    anchor = next((
        row for row in reversed(ordered)
        if int(row.get("bucket_start_ms", 0) or 0) < int(start_ms)
        and int(row.get("epoch", -1) or -1) == epoch
        and row.get("clock_valid")
        and _f(row.get("price")) > 0.0
    ), None)
    sign = _sign(side)
    buy_quote = sum(_f(row.get("buy_quote")) for row in selected)
    sell_quote = sum(_f(row.get("sell_quote")) for row in selected)
    directional_quote = buy_quote if sign > 0.0 else sell_quote
    total_qty = sum(
        _f(row.get("buy_qty")) + _f(row.get("sell_qty")) for row in selected
    )
    aligned_qty = sum(
        _f(row.get("buy_qty")) if sign > 0.0 else _f(row.get("sell_qty"))
        for row in selected
    )
    imbalance = (
        sign * (
            sum(_f(row.get("buy_qty")) for row in selected)
            - sum(_f(row.get("sell_qty")) for row in selected)
        ) / total_qty
        if total_qty > 0.0 else 0.0
    )
    first = _f((anchor or {}).get("price"))
    if first <= 0.0:
        first = next((
            _f(row.get("first_price")) for row in selected
            if _f(row.get("first_price")) > 0.0
        ), 0.0)
    last = next(
        (_f(row.get("price")) for row in reversed(selected)
         if _f(row.get("price")) > 0.0),
        0.0,
    )
    progress = sign * _bps(last, first) if first > 0.0 and last > 0.0 else 0.0
    material = bool(
        total_qty >= ignition_signals.MIN_QTY[str(selected[-1].get("venue"))]
        and aligned_qty > 0.0 and imbalance >= 0.20 and directional_quote > 0.0
    )
    efficiency = progress / max(directional_quote / 1_000_000.0, 1e-9)
    return {
        "start_ms": int(start_ms), "end_ms": int(end_ms),
        "epoch": epoch, "material": material,
        "observed_trade_buckets": len(selected),
        "expected_buckets": max(1, int((end_ms - start_ms) / ignition_signals.BUCKET_MS)),
        "directional_quote": round(directional_quote, 6),
        "total_qty": round(total_qty, 8), "imbalance": round(imbalance, 6),
        "price_progress_bps": round(progress, 6),
        "efficiency_bps_per_million": round(efficiency, 6),
    }


def _flow_efficiency_snapshot(histories, side, cash_venues):
    """Return current marginal conversion without reusing episode progress.

    Prefer three sliding 500 ms windows. Persistent metaorders with sparse
    trades may use three contiguous 1 s windows from the same exact 100 ms
    executed-flow rows; the lower resolution is explicit in telemetry.
    """
    venues = {}
    for venue in sorted(set(cash_venues or ()) & CASH):
        rows = list(histories.get(venue, ()))
        latest_end_ms = max(
            (int(row.get("receive_time_ms", 0) or 0) for row in rows),
            default=0,
        )
        resolution_ms = 500
        windows = [
            _efficiency_window(
                rows, side,
                latest_end_ms - resolution_ms * (3 - index),
                latest_end_ms - resolution_ms * (2 - index),
            )
            for index in range(3)
        ] if latest_end_ms > 0 else [None, None, None]
        valid = [row for row in windows if row is not None and row.get("material")]
        source = "SLIDING_500MS_EXECUTED_FLOW"
        if len(valid) != 3 and latest_end_ms > 0:
            fallback_resolution_ms = 1_000
            fallback = [
                _efficiency_window(
                    rows, side,
                    latest_end_ms - fallback_resolution_ms * (3 - index),
                    latest_end_ms - fallback_resolution_ms * (2 - index),
                )
                for index in range(3)
            ]
            fallback_valid = [
                row for row in fallback
                if row is not None and row.get("material")
            ]
            if len(fallback_valid) == 3:
                windows, valid = fallback, fallback_valid
                resolution_ms = fallback_resolution_ms
                source = "PERSISTENT_1S_EXECUTED_FLOW_FALLBACK"
        diagnostics = {
            "flow_change_ratio": None,
            "progress_change_ratio": None,
            "flow_collapsed": False,
            "progress_collapsed": False,
            "absorbed_before_burst": False,
            "conversion_survived": False,
        }
        if len(valid) != 3:
            state = "UNKNOWN"
            classification_reason = "INSUFFICIENT_CONTIGUOUS_MATERIAL_WINDOWS"
        else:
            old, previous, current = valid
            material_progress = MATERIAL_PRICE_BPS * 0.70
            progress_decay_1 = previous["price_progress_bps"] < old["price_progress_bps"] * 0.70
            progress_decay_2 = current["price_progress_bps"] < previous["price_progress_bps"] * 0.70
            efficiency_decay_1 = previous["efficiency_bps_per_million"] < old["efficiency_bps_per_million"] * 0.70
            efficiency_decay_2 = current["efficiency_bps_per_million"] < previous["efficiency_bps_per_million"] * 0.70
            flow_persists = current["directional_quote"] >= previous["directional_quote"]
            flow_collapsed = (
                previous["directional_quote"] > 0.0
                and current["directional_quote"]
                < previous["directional_quote"] * 0.70
            )
            progress_collapsed = (
                previous["price_progress_bps"] >= material_progress
                and current["price_progress_bps"]
                < previous["price_progress_bps"] * 0.70
            )
            no_progress = current["price_progress_bps"] < material_progress
            repeated_no_progress = all(
                row["price_progress_bps"] < material_progress
                for row in (old, previous, current)
            )
            absorbed_before_burst = bool(
                old["price_progress_bps"] < material_progress
                and previous["price_progress_bps"] < material_progress
                and current["price_progress_bps"] >= material_progress
                and current["directional_quote"]
                > max(old["directional_quote"], previous["directional_quote"])
            )
            conversion_survived = bool(
                previous["price_progress_bps"] >= material_progress
                and current["price_progress_bps"] >= material_progress
                and current["efficiency_bps_per_million"] > 0.0
            )
            diagnostics = {
                "flow_change_ratio": round(
                    current["directional_quote"]
                    / previous["directional_quote"], 6
                ) if previous["directional_quote"] > 0.0 else None,
                "progress_change_ratio": round(
                    current["price_progress_bps"]
                    / previous["price_progress_bps"], 6
                ) if previous["price_progress_bps"] > 0.0 else None,
                "flow_collapsed": flow_collapsed,
                "progress_collapsed": progress_collapsed,
                "absorbed_before_burst": absorbed_before_burst,
                "conversion_survived": conversion_survived,
            }
            # A prettier price/volume ratio cannot hide that both absolute
            # flow and absolute marginal progress collapsed.  This is a soft
            # timing state downstream, not an absorption veto.
            if flow_collapsed and progress_collapsed:
                state = "FADING"
                classification_reason = "ABSOLUTE_FLOW_AND_PROGRESS_COLLAPSED"
            # Two non-converting cash windows followed by one large print are
            # not yet a durable metaorder.  A later window or an independent
            # cash venue may confirm it without resetting the causal wave.
            elif absorbed_before_burst:
                state = "REACCELERATION_UNCONFIRMED"
                classification_reason = "ONE_BURST_AFTER_TWO_NON_CONVERTING_WINDOWS"
            elif flow_persists and no_progress and progress_decay_1 and progress_decay_2:
                state = "EXHAUSTED"
                classification_reason = "PERSISTENT_FLOW_WITH_PROGRESS_DECAY"
            elif flow_persists and repeated_no_progress:
                state = "ABSORBED"
                classification_reason = "PERSISTENT_FLOW_WITHOUT_PRICE_PROGRESS"
            elif (progress_decay_2 and efficiency_decay_2) or (
                progress_decay_1 and efficiency_decay_1
            ):
                state = "DECAYING"
                classification_reason = "MARGINAL_CONVERSION_DECAYING"
            elif conversion_survived:
                state = "CONTINUING_CONFIRMED"
                classification_reason = "TWO_CONTIGUOUS_CONVERTING_WINDOWS"
            else:
                state = "UNKNOWN"
                classification_reason = "NO_DURABLE_CONVERSION_CLASSIFICATION"
        venues[venue] = {
            "state": state,
            "classification_reason": classification_reason,
            "diagnostics": diagnostics,
            "windows": windows,
            "window_resolution_ms": resolution_ms,
            "measurement_source": source,
            "previous_conversion_bps": (
                valid[-2]["price_progress_bps"] if len(valid) == 3 else None
            ),
            "marginal_conversion_now_bps": (
                valid[-1]["price_progress_bps"] if len(valid) == 3 else None
            ),
            "policy": "THREE_CONTIGUOUS_WINDOWS_EXACT_EXECUTED_FLOW_NO_EPISODE_FALLBACK",
        }
    return {
        "version": "FLOW_EFFICIENCY_V4_SURVIVAL_CONFIRMATION",
        "side": side,
        "venues": venues,
        "authority": "ENTRY_COMPOSITE_ONLY",
    }


def _oi_state_snapshot(state):
    """Freeze the REST OI observation visible in this scheduler turn."""
    updated_at = _f(getattr(state, "open_interest_updated_at", 0.0))
    value = _f(getattr(state, "open_interest", 0.0))
    previous = _f(getattr(state, "prev_open_interest", 0.0))
    return {
        "value": value if value > 0.0 else None,
        "updated_at": updated_at if updated_at > 0.0 else None,
        "previous_value": previous if previous > 0.0 else None,
        "change_pct": (
            round(_f(getattr(state, "open_interest_change_pct", 0.0)), 6)
            if updated_at > 0.0 and previous > 0.0 else None
        ),
        "sample_window_seconds": (
            round(_f(getattr(state, "open_interest_change_window_seconds", 0.0)), 4)
            if updated_at > 0.0 and previous > 0.0 else None
        ),
    }


def _oi_verification(
    oi, before=None, after=None, *, episode_started_ms=None, decision_time=None
):
    """Verify that OI actually refreshed inside this causal episode.

    A REST sample can be young while still predating the impulse.  Such a
    sample is useful context, but it is not causal confirmation.  Timestamps
    received after ``decision_time`` are also forbidden from retroactively
    changing the decision.
    """
    oi = dict(oi or {})
    before, after = dict(before or {}), dict(after or {})
    intent = str(oi.get("intent") or "NEUTRAL").upper()
    before_ts = _f(before.get("updated_at"))
    after_ts = _f(after.get("updated_at"))
    started_s = _f(episode_started_ms) / 1000.0
    decision_s = _f(decision_time)
    available = bool(
        before_ts > 0.0 and after_ts > 0.0
        and before.get("value") is not None and after.get("value") is not None
    )
    no_lookahead = bool(decision_s <= 0.0 or after_ts <= decision_s + 1e-6)
    refreshed = bool(available and after_ts > before_ts and no_lookahead)
    inside_episode = bool(
        refreshed
        and (started_s <= 0.0 or after_ts >= started_s)
        and (started_s <= 0.0 or after_ts - started_s <= OI_SAMPLE_MAX_SECONDS)
    )
    age = decision_s - after_ts if decision_s > 0.0 and after_ts > 0.0 else None
    timely = bool(
        inside_episode and age is not None
        and 0.0 <= age <= OI_SAMPLE_MAX_SECONDS
    )
    aligned = bool(oi.get("aligned_with_entry", True))
    if not available or not no_lookahead:
        status = "UNAVAILABLE"
    elif after_ts == before_ts:
        status = "UNCHANGED_UNKNOWN"
    elif not timely:
        status = "STALE_UNKNOWN"
    elif not aligned:
        status = "FRESH_CONFLICT"
    elif intent == "POSITION_BUILD":
        status = "FRESH_POSITION_BUILD"
    elif intent == "UNWIND":
        status = "FRESH_UNWIND"
    else:
        status = "UNCHANGED_UNKNOWN"
    verified = status.startswith("FRESH_")
    return {
        "version": "OI_EPISODE_VERIFICATION_V2_CAUSAL_REFRESH",
        "status": status,
        "fresh": verified,
        "market_snapshot_fresh": bool(oi.get("fresh")),
        "intent": intent,
        "intent_source": oi.get("intent_source"),
        "frozen_updated_at": oi.get("frozen_oi_updated_at"),
        "live_updated_at": oi.get("live_oi_updated_at"),
        "live_age_seconds": oi.get("live_oi_age_seconds"),
        "sample_window_seconds": oi.get("sample_window_seconds"),
        "episode_started_ms": episode_started_ms,
        "decision_time": decision_time,
        "episode_before": before,
        "episode_after": after,
        "refresh_observed": refreshed,
        "inside_causal_window": inside_episode,
        "no_lookahead": no_lookahead,
        "same_snapshot": bool(available and after_ts == before_ts),
        "policy": "EPISODE_REFRESH_REQUIRED_STALE_OR_UNCHANGED_IS_UNKNOWN",
    }


def _persistent_oi_before_snapshot(state, candidate_id):
    """Bind the first visible OI sample to one persistent causal candidate."""
    binding = dict(getattr(state, "_persistent_oi_episode_binding", {}) or {})
    if binding.get("candidate_id") == candidate_id:
        return dict(binding.get("before") or {})
    before = _oi_state_snapshot(state)
    state._persistent_oi_episode_binding = {
        "candidate_id": candidate_id,
        "before": before,
    }
    return before


def _wait(now, side, reason, phase="ARMED", episode=None, freshness=None):
    ignition = dict(episode or {})
    return {
        "version": VERSION, "decision": "WAIT", "entry_mode": "NONE",
        "execution_policy": "NONE", "phase": phase, "confidence": 0.0,
        "reason": reason, "side": side, "s_votes": _compat_votes(False, ignition),
        "ignition": ignition, "causal": _causal(ignition),
        "causal_episode_id": ignition.get("causal_episode_id"),
        "freshness": dict(freshness or {}), "ts": now,
    }


def _compat_votes(passed, ignition):
    status = "PASS" if passed else "WAIT"
    cash = list(ignition.get("cash_venues") or ())
    proof = ignition.get("proof_type")
    move_alias = {
        "binance_spot": "spot", "coinbase_spot": "coinbase",
        "futures": "futures",
    }
    moves = {
        move_alias.get(name, name): value
        for name, value in (ignition.get("venue_moves_bps") or {}).items()
    }
    price_metrics = {
        "supporters": ["spot" if x == "binance_spot" else "coinbase" for x in cash],
        "strong_supporters": ["spot" if x == "binance_spot" else "coinbase" for x in cash],
        "opponents": list(ignition.get("cash_opponents") or ()),
        "moves": moves,
        "compatibility_only": True,
    }
    flow_metrics = {
        "supporters": ["spot" if x == "binance_spot" else "coinbase" if x == "coinbase_spot" else x
                       for x in ignition.get("supporting_venues") or ()],
        "strong_supporters": list(ignition.get("supporting_venues") or ()),
        "strong_opponents": list(ignition.get("cash_opponents") or ()),
        "venues": dict(ignition.get("flow_by_venue") or {}),
        "volume_floor_btc_by_venue": dict(MIN_VOL_BTC_BY_VENUE),
        "volume_floor_btc": min(MIN_VOL_BTC_BY_VENUE.values()),
        "compatibility_only": True,
    }
    return {
        "S1_cross_venue_price_acceptance": {
            "status": status, "reason": "IGNITION_CASH_PRICE_AUTHORITY",
            "confidence": 0.70 if passed else 0.0, "metrics": price_metrics,
        },
        "S2_multi_venue_executed_flow": {
            "status": status, "reason": "IGNITION_EXECUTED_FLOW_PROOF",
            "confidence": 0.70 if passed else 0.0, "metrics": flow_metrics,
        },
        "S3_causal_response_validator": {
            "status": status, "reason": proof or "IGNITION_NOT_PROVED",
            "confidence": 0.72 if passed else 0.0,
            "metrics": {"proof_type": proof, "compatibility_only": True},
        },
    }


def _causal(ignition):
    cash = list(ignition.get("cash_venues") or ())
    aliases = ["spot" if x == "binance_spot" else "coinbase" for x in cash]
    proposer = ignition.get("proposer")
    handoff_status = None
    if proposer in CASH:
        handoff_status = "CASH_IGNITION"
    elif proposer == "futures":
        handoff_status = "FUTURES_ALERT_CASH_PROVED"
    return {
        "evidence_groups": {
            "cash_price": aliases, "cash_flow": aliases,
            "derivative_price": ["futures"] if ignition.get("futures_response") else [],
            "derivative_flow": ["futures"] if ignition.get("futures_response") else [],
            "policy": "IGNITION_CASH_AUTHORITY_FUTURES_NEVER_SELF_OPENS",
        },
        "handoff": {
            "status": handoff_status,
            "proposer": proposer, "leader": ignition.get("leader"),
        },
        "persistence": {
            "ok": bool(ignition.get("proof_type")),
            "policy": "FAILED_REVERSION_OR_TWO_100MS_METAORDER_BUCKETS",
        },
        "oi_intent": dict(ignition.get("oi_intent") or {}),
        "ignition": ignition,
    }


def _freshness(state, now):
    values = {
        "binance_spot_price_age": now - _f(getattr(state, "thoi_gian_tick_cuoi", 0.0)),
        "binance_spot_flow_age": now - _f(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0)),
        "coinbase_price_age": now - _f(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0)),
        "coinbase_flow_age": now - _f(getattr(state, "coinbase_flow_3s_ts", 0.0)),
        "futures_price_age": now - _f(getattr(state, "execution_price_time", 0.0)),
        "futures_flow_age": now - _f(getattr(state, "thoi_gian_dong_tien_futures_cuoi", 0.0)),
    }
    values["binance_spot_ready"] = all(
        0.0 <= values[name] <= 2.5
        for name in ("binance_spot_price_age", "binance_spot_flow_age")
    )
    coinbase_age = max(values["coinbase_price_age"], values["coinbase_flow_age"])
    values["coinbase_mode"] = (
        "FRESH" if 0.0 <= coinbase_age < 2.5
        else "DEGRADED" if 0.0 <= coinbase_age <= 5.0
        else "STALE"
    )
    values["futures_ready"] = all(
        0.0 <= values[name] <= 2.5
        for name in ("futures_price_age", "futures_flow_age")
    )
    return values


def _bias_snapshot(state, signal):
    signal_s = _f(signal.get("receive_time_ms")) / 1000.0
    history = getattr(state, "_ignition_bias_snapshots", None)
    if isinstance(history, deque):
        # Fixed-size reverse scan is bounded O(1). The minimum age prevents the
        # same impulse from creating Bias and proving Ignition a few ms later.
        for cached in reversed(history):
            age = signal_s - _f(cached.get("captured_at"))
            if BIAS_MIN_PRE_IMPULSE_AGE <= age <= BIAS_MAX_AGE:
                return dict(cached)
    return {"direction": "ABSTAIN", "confidence": 0.0, "updated_at": 0.0}


def _remember_bias(state, now=None):
    history = getattr(state, "_ignition_bias_snapshots", None)
    if not isinstance(history, deque):
        history = deque(maxlen=40)
        state._ignition_bias_snapshots = history
    council = getattr(state, "bias_council", None) or {}
    memory = council.get("direction_memory") or {}
    story = council.get("story") or {}
    seats = council.get("s_votes") or {}
    price_seat = seats.get("S1_cross_price") or {}
    flow_seat = seats.get("S3_multi_flow") or {}
    oi_seat = seats.get("S2_price_x_oi") or {}
    compact_votes = {
        name: {
            "vote": str((seat or {}).get("vote") or "ABSTAIN"),
            "confidence": _f((seat or {}).get("confidence")),
            "reason": str((seat or {}).get("reason") or "UNKNOWN"),
        }
        for name, seat in seats.items() if isinstance(seat, dict)
    }
    row = {
        "direction": str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper(),
        "confidence": _f(getattr(state, "bias_confidence", 0.0)),
        "raw_direction": str(council.get("raw_bias") or "ABSTAIN").upper(),
        "raw_confidence": _f(council.get("raw_confidence")),
        "s_votes": compact_votes,
        "updated_at": _f(getattr(state, "bias_updated_at", 0.0)),
        "version": getattr(state, "bias_version", None),
        "captured_at": time.time() if now is None else float(now),
        # Keep the causal context that produced the direction.  Copy only the
        # compact fields consumed by Ignition so the frozen row is immutable
        # and bounded instead of retaining the live council dictionary.
        "direction_context": {
            "phase": str(memory.get("phase") or "UNKNOWN"),
            "context_side": str(memory.get("context_side") or "ABSTAIN"),
            "candidate_side": str(memory.get("candidate_side") or "ABSTAIN"),
            "hysteresis": str(council.get("hysteresis") or "UNKNOWN"),
            "story": str(story.get("name") or council.get("reason") or "UNKNOWN"),
            "price_vote": str(price_seat.get("vote") or "ABSTAIN"),
            "flow_vote": str(flow_seat.get("vote") or "ABSTAIN"),
            "oi_regime": str((oi_seat.get("metrics") or {}).get("regime") or "UNKNOWN"),
            "oi_updated_at": _f(getattr(state, "open_interest_updated_at", 0.0)),
            "oi_value": _f(getattr(state, "open_interest", 0.0)),
            "oi_change_pct": _f(getattr(state, "open_interest_change_pct", 0.0)),
            "oi_change_window_seconds": _f(
                getattr(state, "open_interest_change_window_seconds", 0.0)
            ),
            "flow_price_trap": bool(
                str(story.get("name") or council.get("reason") or "")
                == "FLOW_NOT_CONVERTED_TO_PRICE"
            ),
        },
    }
    if not history or (
        history[-1].get("direction"), history[-1].get("confidence"),
        history[-1].get("updated_at"), history[-1].get("raw_direction"),
        history[-1].get("raw_confidence"), history[-1].get("s_votes"),
        history[-1].get("direction_context")
    ) != (
        row["direction"], row["confidence"], row["updated_at"],
        row["raw_direction"], row["raw_confidence"], row["s_votes"],
        row["direction_context"],
    ):
        history.append(row)


def _new_signals(state, histories):
    seen = getattr(state, "_ignition_seen_bucket", None)
    if not isinstance(seen, dict):
        seen = {}
        state._ignition_seen_bucket = seen
    rows = []
    for venue, history in histories.items():
        last_seen = int(seen.get(venue, -1))
        for row in history:
            token = int(row.get("bucket_start_ms", -1))
            if token > last_seen:
                rows.append(row)
        if history:
            seen[venue] = max(last_seen, int(history[-1].get("bucket_start_ms", -1)))
    rows.sort(key=lambda x: (int(x.get("receive_time_ms", 0)), str(x.get("venue", ""))))
    return rows


def _episode_id(signal):
    return "ign:%s:%s:%d" % (
        signal.get("venue"), signal.get("side"), int(signal.get("bucket_start_ms", 0))
    )


def _pending_reversal_identity(signal, bias):
    """Hash only immutable onset identity; later evidence cannot rewrite it."""
    onset = {
        "episode_id": _episode_id(signal),
        "side": str(signal.get("side") or "ABSTAIN").upper(),
        "start_receive_ms": int(signal.get("receive_time_ms", 0) or 0),
        "bucket_start_ms": int(signal.get("bucket_start_ms", 0) or 0),
        "proposer": str(signal.get("venue") or "UNKNOWN"),
        "epoch": int(signal.get("epoch", 0) or 0),
        "event_time_ms": int(signal.get("event_time_ms", 0) or 0),
        "price": round(_f(signal.get("price")), 10),
        "total_qty": round(_f(signal.get("total_qty")), 10),
        "imbalance": round(_f(signal.get("imbalance")), 8),
        "price_conversion_bps": round(
            _f(signal.get("price_conversion_bps")), 8
        ),
        "frozen_bias_direction": str(
            (bias or {}).get("direction") or "ABSTAIN"
        ).upper(),
        "frozen_bias_updated_at": _f((bias or {}).get("updated_at")),
    }
    encoded = json.dumps(
        onset, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return onset, hashlib.sha256(encoded).hexdigest()


def _start_pending_reversal(state, signal, bias, histories):
    """Remember counter-Bias onset without granting direction or Entry."""
    side = str(signal.get("side") or "ABSTAIN").upper()
    frozen_side = str((bias or {}).get("direction") or "ABSTAIN").upper()
    if (
        side not in ("LONG", "SHORT")
        or frozen_side not in ("LONG", "SHORT")
        or side == frozen_side
        or _f((bias or {}).get("confidence")) < BIAS_MIN_CONF
        or not signal.get("strong")
        or not signal.get("clock_valid")
    ):
        return None
    onset, identity_hash = _pending_reversal_identity(signal, bias)
    receive_ms = int(signal.get("receive_time_ms", 0) or 0)
    proposer = str(signal.get("venue") or "UNKNOWN")
    cash = [proposer] if proposer in CASH else []
    precursor = _precursor_cash_progress(state, signal)
    histories_at_onset = {
        name: tuple(
            row for row in rows
            if int(row.get("receive_time_ms", 0) or 0) <= receive_ms
        )
        for name, rows in (histories or {}).items()
    }
    pending = {
        "causal_episode_id": onset["episode_id"],
        "episode_id": onset["episode_id"],
        "episode_hash": identity_hash,
        "side": side,
        "start_ts": receive_ms / 1000.0,
        "last_ts": receive_ms / 1000.0,
        "started_receive_ms": receive_ms,
        "last_evidence_ms": receive_ms,
        "proposer": proposer,
        "leader": proposer,
        "signals": [dict(signal)],
        "epochs": {proposer: int(signal.get("epoch", 0) or 0)},
        "pre_impulse_bias_snapshot": dict(bias or {}),
        "bias_snapshot": dict(bias or {}),
        "onset_evidence": onset,
        "executed_flow_evidence": [dict(signal)],
        "flow_efficiency_at_onset": _flow_efficiency_snapshot(
            histories_at_onset, side, cash,
        ),
        "precursor_measurement": precursor,
        "displacement_onset": dict(precursor),
        "oi_before_snapshot": _oi_state_snapshot(state),
        "status": "PENDING_BIAS_FLIP",
        "authority": False,
        "policy": "SAME_EPISODE_BIAS_FLIP_NO_BUCKET_REPLAY_NO_LOOKAHEAD",
    }
    state._ignition_pending_reversal_episode = pending
    state._ignition_last_reject = "PENDING_REVERSAL_BIAS_CONFIRMATION"
    state._ignition_last_reject_payload = dict(pending)
    return pending


def _observe_pending_reversal(state, rows, histories):
    """Append only newly received, same-side material evidence."""
    handled = set()
    pending = getattr(state, "_ignition_pending_reversal_episode", None)
    for row in rows:
        token = (str(row.get("venue") or ""), int(row.get("bucket_start_ms", -1)))
        if isinstance(pending, dict):
            if (
                str(row.get("side") or "").upper() == pending.get("side")
                and _material_flow(row)
            ):
                pending["signals"].append(dict(row))
                pending["executed_flow_evidence"].append(dict(row))
                receive_ms = int(row.get("receive_time_ms", 0) or 0)
                pending["last_evidence_ms"] = receive_ms
                pending["last_ts"] = receive_ms / 1000.0
                pending["epochs"][str(row.get("venue") or "")] = int(
                    row.get("epoch", 0) or 0
                )
                handled.add(token)
            continue
        if not row.get("strong") or not row.get("clock_valid"):
            continue
        frozen = _bias_snapshot(state, row)
        pending = _start_pending_reversal(state, row, frozen, histories)
        if pending is not None:
            handled.add(token)
    return handled


def _current_bias_confirmation(state, side, now_s, onset_ms):
    """Capture current Bias at decision time; never manufacture past state."""
    updated_at = _f(getattr(state, "bias_updated_at", 0.0))
    if (
        str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
        != str(side).upper()
        or _f(getattr(state, "bias_confidence", 0.0)) < BIAS_MIN_CONF
        or updated_at <= 0.0
        or updated_at * 1000.0 < int(onset_ms)
        or updated_at > float(now_s) + 1e-6
    ):
        return None
    _remember_bias(state, now_s)
    history = getattr(state, "_ignition_bias_snapshots", None)
    if not isinstance(history, deque) or not history:
        return None
    current = dict(history[-1])
    if (
        str(current.get("direction") or "ABSTAIN").upper() != str(side).upper()
        or _f(current.get("confidence")) < BIAS_MIN_CONF
        or _f(current.get("updated_at")) * 1000.0 < int(onset_ms)
    ):
        return None
    return current


def _resolve_pending_reversal(state, histories, now_ms, allow_promotion=True):
    """Expire invalid onset or promote the exact object after a real Bias flip."""
    pending = getattr(state, "_ignition_pending_reversal_episode", None)
    if not isinstance(pending, dict):
        return None
    current_epochs = {
        name: int(rows[-1].get("epoch", 0) or 0)
        for name, rows in (histories or {}).items() if rows
    }
    epoch_changed = any(
        name in current_epochs and current_epochs[name] != int(epoch)
        for name, epoch in (pending.get("epochs") or {}).items()
    )
    clock_invalid = any(
        not bool(row.get("clock_valid"))
        for row in pending.get("executed_flow_evidence") or ()
    )
    age = int(now_ms) - int(pending.get("started_receive_ms", 0) or 0)
    gap = int(now_ms) - int(pending.get("last_evidence_ms", 0) or 0)
    if epoch_changed or clock_invalid or age > EPISODE_MAX_MS or gap > EVIDENCE_GAP_MS:
        reason = (
            "PENDING_REVERSAL_EPOCH_RESET" if epoch_changed else
            "PENDING_REVERSAL_CLOCK_INVALID" if clock_invalid else
            "PENDING_REVERSAL_TTL_EXPIRED" if age > EPISODE_MAX_MS else
            "PENDING_REVERSAL_EVIDENCE_GAP"
        )
        state._ignition_pending_reversal_episode = None
        state._ignition_last_reject = reason
        state._ignition_last_reject_payload = {
            "expired_episode_id": pending.get("episode_id"),
            "expired_episode_hash": pending.get("episode_hash"),
            "research_reject_reason": reason,
            "authority": False,
        }
        return None
    if not allow_promotion:
        state._ignition_last_reject_payload = dict(pending)
        return None
    confirmation = _current_bias_confirmation(
        state, pending.get("side"), now_ms / 1000.0,
        pending.get("started_receive_ms", 0),
    )
    if confirmation is None:
        state._ignition_last_reject = "PENDING_REVERSAL_BIAS_CONFIRMATION"
        state._ignition_last_reject_payload = dict(pending)
        return None
    # Keep identity, onset, signals, OI-before and hash untouched. Only attach
    # the later confirmation that makes the existing episode eligible.
    pending["bias_confirmation_snapshot"] = confirmation
    pending["bias_snapshot"] = confirmation
    pending["pending_reversal_promoted"] = True
    pending["status"] = "BIAS_FLIP_CONFIRMED"
    pending["promotion_ts"] = now_ms / 1000.0
    pending["flow_efficiency_at_promotion"] = _flow_efficiency_snapshot(
        histories, pending.get("side"), CASH,
    )
    state._ignition_pending_reversal_episode = None
    state._ignition_episode = pending
    return pending


def _bias_bucket_at(buckets, target, max_age=2.0):
    """Return one receive-time snapshot with a bounded direct lookup."""
    if not isinstance(buckets, dict):
        return None
    second = int(target)
    for offset in range(int(max_age) + 2):
        row = buckets.get(second - offset)
        if not isinstance(row, dict):
            continue
        age = target - _f(row.get("ts"))
        if 0.0 <= age <= max_age:
            return row
    return None


def _bias_price_epoch_matches(current, reference, source):
    """Do not measure precursor displacement across a venue reconnect."""
    try:
        current_epoch = int(
            ((current or {}).get("venue_epochs") or {}).get(source, 0) or 0
        )
        reference_epoch = int(
            ((reference or {}).get("venue_epochs") or {}).get(source, 0) or 0
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return current_epoch == reference_epoch


def _precursor_cash_progress(state, signal):
    """Measure cash displacement that happened before the proposer bucket.

    Ignition's venue-local anchor intentionally measures only the active
    episode.  This second, bounded view prevents a late proposer from resetting
    a wave that was already running for 3-15 seconds back to phase zero.
    """
    signal_s = _f(signal.get("receive_time_ms")) / 1000.0
    buckets = getattr(state, "bias_price_buckets", None)
    current = _bias_bucket_at(buckets, signal_s, 2.0)
    if current is None:
        return {
            "valid": False, "source": "BIAS_PRICE_BUCKETS_UNAVAILABLE",
            "progress_bps": 0.0, "horizon_seconds": None,
            "venue_moves_bps": {}, "cash_coverage": 0, "horizons": {},
            "continuity_status": "UNMEASURED_REQUIRES_EXECUTED_FLOW_PATH",
            "authority": False,
        }
    sign = _sign(signal.get("side"))
    best = None
    horizons = {}
    for horizon in (3, 6, 15):
        reference = _bias_bucket_at(buckets, signal_s - horizon, 2.0)
        if reference is None:
            horizons[str(horizon)] = {
                "valid": False, "reason": "REFERENCE_UNAVAILABLE",
                "progress_bps": 0.0, "venue_moves_bps": {},
                "cash_coverage": 0,
            }
            continue
        moves = {}
        for source, venue in (("spot", "binance_spot"), ("coinbase", "coinbase_spot")):
            if not _bias_price_epoch_matches(current, reference, source):
                continue
            current_price = _f(current.get(source))
            reference_price = _f(reference.get(source))
            if current_price > 0.0 and reference_price > 0.0:
                moves[venue] = sign * _bps(current_price, reference_price)
        if not moves:
            horizons[str(horizon)] = {
                "valid": False, "reason": "NO_SAME_EPOCH_CASH_PRICE",
                "progress_bps": 0.0, "venue_moves_bps": {},
                "cash_coverage": 0, "reference_ts": reference.get("ts"),
            }
            continue
        # Both cash venues have equal authority.  With one venue missing we
        # retain measurement coverage metadata instead of inventing agreement.
        progress = max(0.0, sum(moves.values()) / len(moves))
        candidate = {
            "valid": True,
            "source": "PRE_IGNITION_CASH_PROGRESS",
            "progress_bps": round(progress, 6),
            "horizon_seconds": horizon,
            "venue_moves_bps": {name: round(value, 6) for name, value in moves.items()},
            "cash_coverage": len(moves),
        }
        horizons[str(horizon)] = dict(candidate, reference_ts=reference.get("ts"))
        if best is None or progress > _f(best.get("progress_bps")):
            best = candidate
    base = best or {
        "valid": False, "source": "PRECURSOR_REFERENCE_UNAVAILABLE",
        "progress_bps": 0.0, "horizon_seconds": None,
        "venue_moves_bps": {}, "cash_coverage": 0,
    }
    return dict(
        base, horizons=horizons,
        continuity_status="UNMEASURED_REQUIRES_EXECUTED_FLOW_PATH",
        authority=False,
    )


def _research_reject_context(state, signal, bias, reason):
    """Group pre-authority rejects without creating a canonical opportunity."""
    receive_ms = int(signal.get("receive_time_ms", 0) or 0)
    side = str(signal.get("side") or "ABSTAIN").upper()
    previous = dict(getattr(state, "_ignition_research_reject", {}) or {})
    same_wave = bool(
        previous.get("side") == side
        and 0 <= receive_ms - int(previous.get("last_evidence_ms", 0) or 0)
        <= EVIDENCE_GAP_MS
    )
    candidate_id = (
        previous.get("research_candidate_id") if same_wave else
        "ign-research:%s:%s:%d" % (
            str(signal.get("venue") or "unknown"), side,
            int(signal.get("bucket_start_ms", receive_ms) or receive_ms),
        )
    )
    venues = set(previous.get("observed_venues") or ()) if same_wave else set()
    venues.add(str(signal.get("venue") or "unknown"))
    grouped = {
        "research_candidate_id": candidate_id,
        "side": side,
        "last_evidence_ms": receive_ms,
        "observed_venues": sorted(venues),
    }
    state._ignition_research_reject = grouped
    current = dict(getattr(state, "_ignition_last_reject_payload", {}) or {})
    payload = {
        "research_candidate_id": candidate_id,
        "research_candidate_transition": bool(
            current.get("research_candidate_transition") or not same_wave
        ),
        "research_only": True,
        "authority": False,
        "policy": "TELEMETRY_ONLY_NEVER_CREATES_CANONICAL_OPPORTUNITY",
        "bias_snapshot": dict(bias or {}),
        "research_side": side,
        "research_receive_time_ms": receive_ms,
        "proposer": signal.get("venue"),
        "leader": "UNKNOWN",
        "observed_venues": sorted(venues),
        "research_reject_reason": str(reason),
    }
    state._ignition_last_reject_payload = payload
    return payload


def _tee_borderline_pre_bias(state, rows):
    """Preserve strong onsets before Bias early-return consumes their buckets.

    ``_new_signals`` advances the venue cursors before the live Bias guards are
    evaluated.  Without this tee, a 0.50-0.55 frozen Bias onset was neither an
    Entry nor a research sample.  This path records it only; it never starts an
    Ignition episode and never changes the live decision reason.
    """
    for row in rows:
        if not bool(row.get("strong")) or not bool(row.get("clock_valid")):
            continue
        frozen = _bias_snapshot(state, row)
        side = str(row.get("side") or "ABSTAIN").upper()
        confidence = _f(frozen.get("confidence"))
        if (
            side in ("LONG", "SHORT")
            and side == str(frozen.get("direction") or "ABSTAIN").upper()
            and BORDERLINE_BIAS_MIN_CONF <= confidence < BIAS_MIN_CONF
        ):
            _research_reject_context(
                state, row, frozen, "BORDERLINE_PRE_BIAS_RESEARCH"
            )


def _research_payload(state):
    return dict(getattr(state, "_ignition_last_reject_payload", {}) or {})


def _start_episode(state, signal, histories=None):
    bias = _bias_snapshot(state, signal)
    side = str(signal.get("side") or "NEUTRAL").upper()
    if side != bias.get("direction") or _f(bias.get("confidence")) < BIAS_MIN_CONF:
        if _start_pending_reversal(state, signal, bias, histories or {}) is not None:
            return None
        state._ignition_last_reject = "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS"
        _research_reject_context(
            state, signal, bias, "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS"
        )
        return None
    context = bias.get("direction_context") or {}
    candidate = str(context.get("candidate_side") or "ABSTAIN").upper()
    if (
        str(context.get("phase") or "").upper() == "REVERSAL_CANDIDATE"
        and candidate in ("LONG", "SHORT")
        and candidate != side
    ):
        state._ignition_last_reject = "BIAS_REVERSAL_CANDIDATE_PENDING"
        _research_reject_context(
            state, signal, bias, "BIAS_REVERSAL_CANDIDATE_PENDING"
        )
        return None
    if bool(context.get("flow_price_trap")):
        state._ignition_last_reject = "FROZEN_BIAS_FLOW_PRICE_TRAP"
        _research_reject_context(
            state, signal, bias, "FROZEN_BIAS_FLOW_PRICE_TRAP"
        )
        return None
    episode = {
        "causal_episode_id": _episode_id(signal), "side": side,
        "proposer": signal.get("venue"), "leader": signal.get("venue"),
        "started_receive_ms": int(signal.get("receive_time_ms", 0)),
        "last_evidence_ms": int(signal.get("receive_time_ms", 0)),
        "bias_snapshot": bias, "signals": [dict(signal)],
        "epochs": {signal.get("venue"): int(signal.get("epoch", 0))},
        "precursor_measurement": _precursor_cash_progress(state, signal),
        "oi_before_snapshot": _oi_state_snapshot(state),
    }
    state._ignition_episode = episode
    return episode


def _tombstone(state, episode_id):
    order = getattr(state, "_ignition_tombstone_order", None)
    lookup = getattr(state, "_ignition_tombstones", None)
    if not isinstance(order, deque) or not isinstance(lookup, dict):
        order, lookup = deque(), {}
        state._ignition_tombstone_order = order
        state._ignition_tombstones = lookup
    if episode_id in lookup:
        return
    lookup[episode_id] = True
    order.append(episode_id)
    while len(order) > 256:
        lookup.pop(order.popleft(), None)


def _material_flow(row):
    venue = str(row.get("venue") or "")
    return bool(
        venue in ignition_signals.MIN_QTY
        and _f(row.get("total_qty")) >= ignition_signals.MIN_QTY[venue]
        and abs(_f(row.get("imbalance"))) >= 0.20
        and abs(_f(row.get("price_conversion_bps"))) >= MATERIAL_PRICE_BPS
        and row.get("clock_valid")
    )


def _venue_anchor(history, started_receive_ms):
    """Return a venue-local pre-episode reference; never borrow another venue's basis."""
    rows = [row for row in history if _f(row.get("price")) > 0.0 or _f(row.get("first_price")) > 0.0]
    before = [row for row in rows if int(row.get("receive_time_ms", 0)) < int(started_receive_ms)]
    if before:
        price = _f(before[-1].get("price"))
        if price > 0.0:
            return price, "PRE_EPISODE_CLOSE"
    for row in rows:
        if int(row.get("receive_time_ms", 0)) >= int(started_receive_ms):
            price = _f(row.get("first_price"))
            if price > 0.0:
                return price, "VENUE_EPISODE_FIRST_TRADE"
            price = _f(row.get("price"))
            if price > 0.0:
                return price, "VENUE_EPISODE_FIRST_CLOSE"
    return 0.0, "UNAVAILABLE"


def _failed_reversion(history, side, proposer_ms):
    """Prove material opposition -> excursion -> reclaim -> acceptance on one cash venue."""
    sign = _sign(side)
    rows = [
        row for row in history
        if int(row.get("receive_time_ms", 0)) >= int(proposer_ms)
    ]
    if len(rows) < 4:
        return None

    anchor, anchor_method = _venue_anchor(history, proposer_ms)
    if anchor <= 0.0:
        return None

    latest = rows[-1]
    latest_ms = int(latest.get("receive_time_ms", 0))
    for shock_index in range(len(rows) - 2, -1, -1):
        shock = rows[shock_index]
        shock_ms = int(shock.get("receive_time_ms", 0))
        if latest_ms - shock_ms > FAILED_REVERSION_MAX_MS:
            break
        if str(shock.get("side")) == str(side):
            continue
        # "strong" already requires venue MIN_QTY, adaptive quote surprise >= 1.50,
        # >= 0.20 imbalance, >= 0.15 bps conversion and a valid clock.
        if not shock.get("strong") or not _material_flow(shock):
            continue

        adverse_price = _f(shock.get("low")) if sign > 0.0 else _f(shock.get("high"))
        if adverse_price <= 0.0:
            adverse_price = _f(shock.get("price"))
        adverse_excursion = max(0.0, -sign * _bps(adverse_price, anchor))
        if adverse_excursion < 0.15:
            continue

        reclaim_index = None
        for index in range(shock_index + 1, len(rows) - 1):
            reclaim = rows[index]
            if int(reclaim.get("receive_time_ms", 0)) - shock_ms > FAILED_REVERSION_MAX_MS:
                break
            if str(reclaim.get("side")) != str(side) or not _material_flow(reclaim):
                continue
            if sign * _bps(_f(reclaim.get("price")), anchor) >= 0.0:
                reclaim_index = index
                break
        if reclaim_index is None:
            continue

        acceptance_rows = rows[reclaim_index + 1:]
        if not acceptance_rows or latest_ms - shock_ms > FAILED_REVERSION_MAX_MS:
            continue
        causal_path = rows[shock_index:]
        if any(
            int(later.get("receive_time_ms", 0))
            - int(earlier.get("receive_time_ms", 0)) > EVIDENCE_GAP_MS
            for earlier, later in zip(causal_path, causal_path[1:])
        ):
            continue
        reclaim_ms = int(rows[reclaim_index].get("receive_time_ms", 0))
        acceptance_ms = latest_ms - reclaim_ms
        if acceptance_ms < MIN_ACCEPTANCE_MS:
            continue
        if any(
            sign * _bps(_f(row.get("price")), anchor) < 0.0
            for row in acceptance_rows
            if _f(row.get("price")) > 0.0
        ):
            continue
        if any(
            str(row.get("side")) != str(side) and _material_flow(row)
            for row in acceptance_rows
        ):
            continue
        material_acceptance = [
            row for row in acceptance_rows
            if str(row.get("side")) == str(side) and _material_flow(row)
        ]
        if not material_acceptance:
            continue
        material_acceptance_ms = (
            int(material_acceptance[-1].get("receive_time_ms", 0)) - reclaim_ms
        )
        if material_acceptance_ms < MIN_ACCEPTANCE_MS:
            continue

        reclaim = rows[reclaim_index]
        proof = dict(latest)
        proof["_failed_reversion_evidence"] = {
            "version": "FAILED_REVERSION_V4",
            "venue": str(shock.get("venue") or latest.get("venue") or ""),
            "anchor_price": round(anchor, 10),
            "anchor_method": anchor_method,
            "shock_receive_time_ms": shock_ms,
            "shock_volume_btc": round(_f(shock.get("total_qty")), 8),
            "shock_surprise_ratio": round(_f(shock.get("surprise_ratio")), 6),
            "shock_signed_imbalance": round(sign * _f(shock.get("imbalance")), 6),
            "adverse_excursion_bps": round(adverse_excursion, 6),
            "reclaim_receive_time_ms": int(reclaim.get("receive_time_ms", 0)),
            "reclaim_bps": round(sign * _bps(_f(reclaim.get("price")), anchor), 6),
            "acceptance_receive_time_ms": latest_ms,
            "acceptance_bps": round(sign * _bps(_f(latest.get("price")), anchor), 6),
            "acceptance_buckets": len(acceptance_rows),
            "acceptance_material_buckets": len(material_acceptance),
            "acceptance_duration_ms": acceptance_ms,
            "material_acceptance_duration_ms": material_acceptance_ms,
        }
        return proof
    return None


def _proof(episode, histories):
    side = episode["side"]
    cash_venues = sorted({
        row["venue"] for row in episode["signals"]
        if row["venue"] in CASH and row["side"] == side
    })
    candidates = []
    proof_rank = {"METAORDER_CONTINUATION": 0, "FAILED_REVERSION": 1}
    for venue in cash_venues:
        venue_history = tuple(histories.get(venue, ()))
        rows = [
            row for row in venue_history
            if row.get("side") == side
            and int(row.get("receive_time_ms", 0)) >= episode["started_receive_ms"]
        ]
        if len(rows) >= 2 and rows[-1].get("strong") and rows[-2].get("strong"):
            if _f(rows[-1].get("flow_acceleration")) >= 0.0:
                first_bucket = int(rows[-2].get("bucket_start_ms", 0) or 0)
                second_bucket = int(rows[-1].get("bucket_start_ms", 0) or 0)
                intervening = [
                    row for row in venue_history
                    if first_bucket < int(row.get("bucket_start_ms", 0) or 0)
                    < second_bucket
                ]
                expected_between = max(
                    0,
                    (second_bucket - first_bucket)
                    // ignition_signals.BUCKET_MS - 1,
                )
                observed_between = len(intervening)
                missing_between = max(0, expected_between - observed_between)
                # Episode continuity on another venue cannot turn two
                # disconnected cash impulses into one persistent metaorder.
                # Reuse the existing causal evidence-decay contract.
                if (
                    second_bucket <= first_bucket
                    or second_bucket - first_bucket > EVIDENCE_GAP_MS
                    or missing_between > 0
                ):
                    continue
                proof = dict(rows[-1])
                proof["_metaorder_evidence"] = {
                    "version": "METAORDER_PROOF_QUALITY_V2_GAP_AUTHORITY",
                    "proof_buckets": [first_bucket, second_bucket],
                    "proof_bucket_gap_ms": second_bucket - first_bucket,
                    "proof_buckets_adjacent": bool(
                        second_bucket - first_bucket
                        == ignition_signals.BUCKET_MS
                    ),
                    "intervening_observed_buckets": len(intervening),
                    "intervening_nonmaterial_buckets": sum(
                        not _material_flow(row) for row in intervening
                    ),
                    "intervening_missing_buckets": missing_between,
                    "metadata_authority": True,
                    "proof_policy": "OBSERVED_BRIEF_PAUSE_ONLY_MAX_CAUSAL_DECAY",
                }
                candidates.append(("METAORDER_CONTINUATION", proof, venue))
        failed = _failed_reversion(venue_history, side, episode["started_receive_ms"])
        if failed is not None:
            candidates.append(("FAILED_REVERSION", failed, venue))

    if not candidates:
        return None, None, None
    candidates.sort(key=lambda item: (
        int(item[1].get("receive_time_ms", 0)),
        proof_rank[item[0]],
        item[2],
    ))
    proof_type, proof_signal, proof_venue = candidates[0]
    return proof_type, proof_signal, proof_venue


def _leader_from_rows(rows):
    first_by_venue = {}
    for row in rows:
        venue = str(row.get("venue"))
        current = first_by_venue.get(venue)
        if current is None or _f(row.get("corrected_event_time_ms")) < _f(current.get("corrected_event_time_ms")):
            first_by_venue[venue] = row
    if not first_by_venue:
        return "UNKNOWN", None
    ordered = sorted(first_by_venue.values(), key=lambda row: _f(row.get("corrected_event_time_ms")))
    first = ordered[0]
    if len(ordered) < 2:
        return str(first.get("venue")), None
    second = ordered[1]
    gap = _f(second.get("corrected_event_time_ms")) - _f(first.get("corrected_event_time_ms"))
    uncertainty = _f(first.get("clock_uncertainty_ms")) + _f(second.get("clock_uncertainty_ms"))
    lower = gap - uncertainty
    return (str(first.get("venue")) if lower >= LEAD_FLOOR_MS else "SIMULTANEOUS"), lower


def _leader(episode):
    return _leader_from_rows(episode["signals"])


def _cash_discovery(episode):
    """Side-independent cash venue timing; research metadata only.

    This deliberately does not call a one-episode observation empirical price
    discovery.  It records who arrived first only when both cash venues exist;
    correlation and 1-3 second acceptance remain recorder/replay work.
    """
    cash_rows = [row for row in episode["signals"] if row.get("venue") in CASH]
    venues = {str(row.get("venue")) for row in cash_rows}
    if len(venues) < 2:
        return {
            "status": "ONE_CASH_VENUE_ONLY", "leader": "UNKNOWN",
            "lead_lower_bound_ms": None, "authority": False,
            "confirmation": "INSUFFICIENT_CROSS_SPOT_RESPONSE",
        }
    leader, lower = _leader_from_rows(cash_rows)
    status = {
        "binance_spot": "BINANCE_SPOT_LED_CANDIDATE",
        "coinbase_spot": "COINBASE_SPOT_LED_CANDIDATE",
        "SIMULTANEOUS": "SIMULTANEOUS",
    }.get(leader, "UNKNOWN")
    return {
        "status": status, "leader": leader,
        "lead_lower_bound_ms": round(lower, 4) if lower is not None else None,
        "authority": False,
        "confirmation": "TIMING_ONLY_NO_1_3S_ACCEPTANCE",
    }


def _persistent_metaorder_shadow(histories, side, now_ms):
    """Bounded 1-6 s persistence measurement for the slow Entry lane.

    It deliberately reuses exact executed-flow buckets instead of inventing a
    second signal stack.  The fixed-size history keeps hot-path work bounded.
    This function only measures evidence; ``_persistent_entry_result`` owns
    the Bias/freshness/OI/phase authority checks.
    """
    cutoff = int(now_ms) - 6_000
    sign = _sign(side)
    venues = {}
    for venue in ("binance_spot", "coinbase_spot", "futures"):
        rows = [
            row for row in histories.get(venue, ())
            if int(row.get("receive_time_ms", 0)) >= cutoff
        ]
        seconds = {}
        for row in rows:
            key = int(row.get("receive_time_ms", 0)) // 1_000
            cell = seconds.setdefault(key, {
                "buy": 0.0, "sell": 0.0, "first": 0.0, "last": 0.0,
            })
            cell["buy"] += _f(row.get("buy_qty"))
            cell["sell"] += _f(row.get("sell_qty"))
            price = _f(row.get("price"))
            if price > 0.0:
                if cell["first"] <= 0.0:
                    cell["first"] = price
                cell["last"] = price
        second_keys = sorted(seconds)
        ordered = [seconds[key] for key in second_keys]
        contiguous = bool(
            len(second_keys) >= 3
            and all(b - a == 1 for a, b in zip(second_keys, second_keys[1:]))
        )
        aligned = opposed = observed = 0
        material_aligned = material_opposed = material_observed = 0
        first_price = last_price = 0.0
        first_receive_ms = last_receive_ms = 0
        first_aligned_ms = last_aligned_ms = 0
        total_buy = total_sell = 0.0
        epochs = set()
        clock_valid = True
        for cell in ordered:
            total = cell["buy"] + cell["sell"]
            if total <= 0.0:
                continue
            observed += 1
            imbalance = sign * (cell["buy"] - cell["sell"]) / total
            aligned += int(imbalance >= 0.20)
            opposed += int(imbalance <= -0.20)
            if total >= ignition_signals.MIN_QTY[venue]:
                material_observed += 1
                material_aligned += int(imbalance >= 0.20)
                material_opposed += int(imbalance <= -0.20)
            if first_price <= 0.0 and cell["first"] > 0.0:
                first_price = cell["first"]
            if cell["last"] > 0.0:
                last_price = cell["last"]
        for row in rows:
            receive_ms = int(row.get("receive_time_ms", 0) or 0)
            if receive_ms <= 0:
                continue
            first_receive_ms = first_receive_ms or receive_ms
            last_receive_ms = max(last_receive_ms, receive_ms)
            total_buy += _f(row.get("buy_qty"))
            total_sell += _f(row.get("sell_qty"))
            epochs.add(int(row.get("epoch", 0) or 0))
            clock_valid = bool(clock_valid and row.get("clock_valid"))
            total = _f(row.get("buy_qty")) + _f(row.get("sell_qty"))
            imbalance = (
                sign * (_f(row.get("buy_qty")) - _f(row.get("sell_qty"))) / total
                if total > 0.0 else 0.0
            )
            if imbalance >= 0.20:
                first_aligned_ms = first_aligned_ms or receive_ms
                last_aligned_ms = max(last_aligned_ms, receive_ms)
        progress = sign * _bps(last_price, first_price) if first_price > 0.0 else 0.0
        persistence = aligned / observed if observed else 0.0
        structural = bool(
            contiguous and material_observed >= 3 and material_aligned >= 2
            and material_aligned / material_observed >= (2.0 / 3.0)
            and material_opposed == 0 and progress >= 0.15
            and clock_valid and len(epochs) == 1
        )
        total_qty = total_buy + total_sell
        raw_imbalance = (
            (total_buy - total_sell) / total_qty if total_qty > 0.0 else 0.0
        )
        recent_rows = [
            row for row in rows
            if int(row.get("receive_time_ms", 0) or 0) >= int(now_ms) - 1_000
        ]
        recent_buy = sum(_f(row.get("buy_qty")) for row in recent_rows)
        recent_sell = sum(_f(row.get("sell_qty")) for row in recent_rows)
        recent_qty = recent_buy + recent_sell
        recent_imbalance = (
            sign * (recent_buy - recent_sell) / recent_qty
            if recent_qty > 0.0 else 0.0
        )
        recent_first = next(
            (_f(row.get("first_price")) for row in recent_rows
             if _f(row.get("first_price")) > 0.0), 0.0
        )
        recent_last = next(
            (_f(row.get("price")) for row in reversed(recent_rows)
             if _f(row.get("price")) > 0.0), 0.0
        )
        recent_progress = (
            sign * _bps(recent_last, recent_first)
            if recent_first > 0.0 and recent_last > 0.0 else 0.0
        )
        venues[venue] = {
            "observed_seconds": observed, "aligned_seconds": aligned,
            "opposed_seconds": opposed,
            "material_observed_seconds": material_observed,
            "material_aligned_seconds": material_aligned,
            "material_opposed_seconds": material_opposed,
            "contiguous_seconds": contiguous,
            "persistence_ratio": round(persistence, 6),
            "price_progress_bps": round(progress, 6),
            "structural_candidate": structural,
            "volume_btc": round(total_qty, 8),
            "signed_imbalance": round(sign * raw_imbalance, 6),
            "recent_1s_volume_btc": round(recent_qty, 8),
            "recent_1s_signed_imbalance": round(recent_imbalance, 6),
            "recent_1s_price_progress_bps": round(recent_progress, 6),
            "first_price": first_price or None,
            "last_price": last_price or None,
            "first_receive_ms": first_receive_ms or None,
            "last_receive_ms": last_receive_ms or None,
            "first_aligned_ms": first_aligned_ms or None,
            "last_aligned_ms": last_aligned_ms or None,
            "clock_valid": clock_valid,
            "epoch": next(iter(epochs)) if len(epochs) == 1 else None,
        }
    cash = [name for name in CASH if venues[name]["structural_candidate"]]
    futures_follow = venues["futures"]["structural_candidate"]
    opposing_cash = [
        name for name in CASH
        if venues[name]["material_opposed_seconds"] >= 2
    ]
    cash = [name for name in cash if name not in opposing_cash]
    cash.sort(key=lambda name: (
        int(venues[name].get("first_aligned_ms") or now_ms), name
    ))
    status = (
        "PERSISTENT_METAORDER_CANDIDATE" if cash and futures_follow else
        "WAIT_PERSISTENT_FUTURES_FOLLOW" if cash else "OBSERVING"
    )
    return {
        "version": "PERSISTENT_METAORDER_SHADOW_V1", "status": status,
        "cash_candidates": cash, "futures_follow": futures_follow,
        "opposing_cash_venues": sorted(opposing_cash),
        "proposer": cash[0] if cash else None,
        "venues": venues, "authority": False,
        "calibration_status": "BOOTSTRAP_UNVERIFIED",
        "policy": "MEASUREMENT_ONLY_AUTHORITY_CHECKS_DOWNSTREAM",
    }


def _persistent_metaorder_snapshot(histories, now_ms, previous=None):
    """Observe one stable slow causal wave independently from fast Ignition.

    A brief evidence lull does not create a second opportunity.  The wave ID
    survives at most three seconds without structural confirmation and never
    exceeds fifteen seconds.  This is lifecycle identity, not permission to
    trade through a lull.
    """
    sides = {
        side: _persistent_metaorder_shadow(histories, side, now_ms)
        for side in ("LONG", "SHORT")
    }
    candidates = [
        side for side, report in sides.items()
        if report.get("status") == "PERSISTENT_METAORDER_CANDIDATE"
    ]
    candidate_side = candidates[0] if len(candidates) == 1 else "ABSTAIN"
    status = (
        candidate_side + "_CANDIDATE" if candidate_side != "ABSTAIN" else
        "DIRECTION_CONFLICT" if len(candidates) > 1 else "OBSERVING"
    )
    identity = (status, candidate_side)
    previous_identity = (
        str((previous or {}).get("status", "")),
        str((previous or {}).get("candidate_side", "")),
    )
    previous = dict(previous or {})
    previous_wave_side = str(previous.get("wave_side") or "ABSTAIN")
    previous_started = int(previous.get("wave_started_at_ms", 0) or 0)
    previous_confirmed = int(previous.get("wave_last_confirmed_at_ms", 0) or 0)
    bridge_previous = bool(
        previous_wave_side in ("LONG", "SHORT")
        and previous_started > 0 and previous_confirmed > 0
        and 0 <= now_ms - previous_confirmed <= PERSISTENT_WAVE_GAP_MS
    )
    candidate_started_at_ms = None
    candidate_id = None
    wave_side = previous_wave_side if bridge_previous else "ABSTAIN"
    wave_started_at_ms = previous_started if bridge_previous else 0
    wave_last_confirmed_at_ms = previous_confirmed if bridge_previous else 0
    captured = bool(previous.get("captured", False)) if bridge_previous else False
    wave_expired = bool(
        bridge_previous
        and now_ms - previous_started > PERSISTENT_WAVE_MAX_MS
    )
    if candidate_side != "ABSTAIN":
        report = sides[candidate_side]
        evidence_start = min(
            (
                int((report.get("venues") or {}).get(name, {}).get("first_aligned_ms") or now_ms)
                for name in list(report.get("cash_candidates") or ()) + ["futures"]
            ),
            default=int(now_ms),
        )
        if not (bridge_previous and previous_wave_side == candidate_side):
            wave_side = candidate_side
            wave_started_at_ms = evidence_start
            captured = False
            wave_expired = False
        wave_last_confirmed_at_ms = int(now_ms)
        candidate_started_at_ms = wave_started_at_ms
        candidate_id = "pmeta:%s:%d" % (
            candidate_side, candidate_started_at_ms,
        )
    elif bridge_previous:
        candidate_started_at_ms = wave_started_at_ms
        candidate_id = "pmeta:%s:%d" % (wave_side, wave_started_at_ms)
    return {
        "version": "PERSISTENT_METAORDER_SHADOW_V2",
        "status": status,
        "candidate_side": candidate_side,
        "candidate_id": candidate_id,
        "candidate_started_at_ms": candidate_started_at_ms,
        "wave_side": wave_side,
        "wave_started_at_ms": wave_started_at_ms or None,
        "wave_last_confirmed_at_ms": wave_last_confirmed_at_ms or None,
        "wave_bridge_active": bool(candidate_side == "ABSTAIN" and bridge_previous),
        "wave_expired": wave_expired,
        "captured": captured,
        "sides": sides,
        "authority": False,
        "policy": "SHADOW_BOOTSTRAP_LIVE_EMPIRICAL_ONLY",
        "transition": identity != previous_identity,
    }


def _persistent_entry_result(state, snapshot, histories, freshness, now):
    """Convert a coherent slow wave into the same guarded Entry contract.

    This lane never changes Bias and never lets Futures propose direction.  It
    is bootstrap-authorized only in shadow; ``entry_edge_tier`` keeps future
    real execution behind exact-cohort empirical promotion.
    """
    side = str((snapshot or {}).get("candidate_side") or "ABSTAIN").upper()
    candidate_id = str((snapshot or {}).get("candidate_id") or "")
    if side not in ("LONG", "SHORT") or not candidate_id:
        return None
    if bool((snapshot or {}).get("captured")):
        return None
    if bool((snapshot or {}).get("wave_expired")):
        return None
    report = dict(((snapshot.get("sides") or {}).get(side)) or {})
    cash = list(report.get("cash_candidates") or ())
    proposer = str(report.get("proposer") or "")
    if not cash or not report.get("futures_follow") or proposer not in CASH:
        return None
    if report.get("opposing_cash_venues"):
        return None
    if not freshness.get("binance_spot_ready") or not freshness.get("futures_ready"):
        return None
    if freshness.get("coinbase_mode") == "STALE":
        return None
    if freshness.get("coinbase_mode") == "DEGRADED" and proposer != "binance_spot":
        return None

    wave_started_ms = int(snapshot.get("wave_started_at_ms") or 0)
    frozen = _bias_snapshot(state, {"receive_time_ms": wave_started_ms})
    current_side = str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    if (
        current_side != side
        or _f(getattr(state, "bias_confidence", 0.0)) < BIAS_MIN_CONF
        or str(frozen.get("direction") or "ABSTAIN").upper() != side
        or _f(frozen.get("confidence")) < BIAS_MIN_CONF
    ):
        return None
    context = dict(frozen.get("direction_context") or {})
    phase_name = str(context.get("phase") or "UNKNOWN").upper()
    context_side = str(context.get("context_side") or "ABSTAIN").upper()
    if (
        context_side != side
        or phase_name not in {
            "ESTABLISHED_TREND",
            "PULLBACK_AGAINST_CONTEXT",
            "CONTEXT_WITHOUT_CONFIRMATION",
        }
    ):
        return None
    if bool(context.get("flow_price_trap")):
        return None
    if (
        str(context.get("phase") or "").upper() == "REVERSAL_CANDIDATE"
        and str(context.get("candidate_side") or "ABSTAIN").upper() not in (
            "ABSTAIN", side,
        )
    ):
        return None

    venue_reports = report.get("venues") or {}
    names = set(cash) | {"futures"}
    moves = {
        name: _f((venue_reports.get(name) or {}).get("price_progress_bps"))
        for name in names
    }
    latest_evidence_ms = max(
        (
            int((venue_reports.get(name) or {}).get("last_receive_ms") or 0)
            for name in names
        ),
        default=int(now * 1000.0),
    )
    latest = {
        "receive_time_ms": latest_evidence_ms,
        "price_conversion_bps": max((moves.get(name, 0.0) for name in cash), default=0.0),
    }
    # The slow lane must not reset an already-running cash wave back to phase
    # zero.  Measure the same bounded 3/6/15s precursor used by fast Ignition
    # at the persistent wave's immutable start.  Epoch matching inside the
    # precursor measurement keeps reconnects/gaps fail-closed; an unavailable
    # precursor remains diagnostic and cannot invent displacement.
    precursor = _precursor_cash_progress(state, {
        "receive_time_ms": wave_started_ms,
        "side": side,
    })
    phase = _phase_measurement(
        state, side, cash, moves, latest,
        episode={"precursor_measurement": precursor},
    )
    consumed = _f(phase.get("consumed_fraction"), 1.0)
    if not phase.get("valid") or consumed > MAX_CONSUMED_FRACTION:
        return None
    oi = _oi_intent(state, side, now, frozen)
    oi_verification = _oi_verification(
        oi,
        _persistent_oi_before_snapshot(state, candidate_id),
        _oi_state_snapshot(state),
        episode_started_ms=wave_started_ms,
        decision_time=now,
    )
    oi_status = str(oi_verification.get("status") or "UNAVAILABLE")
    if oi_status == "FRESH_CONFLICT":
        return None
    # A directional context without confirmation is not a trend.  Do not let
    # one Binance cash impulse plus its correlated Futures echo turn it into a
    # trade.  Keep a narrow transition path for genuine price discovery: both
    # independent cash venues must sustain the wave and fresh OI must show new
    # positions building in the frozen Bias direction.  Established trends
    # and pullbacks retain the existing single-cash-anchor contract.
    provisional_context = phase_name == "CONTEXT_WITHOUT_CONFIRMATION"
    provisional_confirmed = bool(
        set(cash) == CASH
        and oi_status == "FRESH_POSITION_BUILD"
    )
    if provisional_context and not provisional_confirmed:
        state._ignition_last_reject = (
            "PROVISIONAL_CONTEXT_REQUIRES_DUAL_CASH_OI_BUILD"
        )
        return None
    # Falling OI describes forced closure, not fresh directional commitment.
    # It may continue as an economic wave, but only when both independent cash
    # venues show persistent executed-flow conversion. One cash venue plus
    # Futures is the common liquidation-aftershock shape and is not enough.
    unwind_confirmed = bool(
        oi_status != "FRESH_UNWIND" or set(cash) == CASH
    )
    if not unwind_confirmed:
        state._ignition_last_reject = "UNWIND_REQUIRES_DUAL_CASH_PERSISTENCE"
        return None

    flow_by_venue = {
        name: {
            "signed_imbalance": _f((venue_reports.get(name) or {}).get("signed_imbalance")),
            "volume_btc": _f((venue_reports.get(name) or {}).get("volume_btc")),
            "surprise_ratio": None,
            "flow_acceleration": None,
            "recent_1s_signed_imbalance": _f(
                (venue_reports.get(name) or {}).get(
                    "recent_1s_signed_imbalance"
                )
            ),
            "recent_1s_price_progress_bps": _f(
                (venue_reports.get(name) or {}).get(
                    "recent_1s_price_progress_bps"
                )
            ),
            "recent_1s_volume_btc": _f(
                (venue_reports.get(name) or {}).get("recent_1s_volume_btc")
            ),
        }
        for name in names
    }
    clock_quality = {
        name: {
            "uncertainty_ms": rows[-1].get("clock_uncertainty_ms"),
            "valid": rows[-1].get("clock_valid"),
            "epoch": rows[-1].get("epoch"),
        }
        for name, rows in histories.items() if rows and name in names
    }
    payload = {
        "causal_episode_id": candidate_id,
        "state": "PROVE",
        "side": side,
        "proposer": proposer,
        "leader": "COHERENT_CASH_WAVE",
        "lead_lower_bound_ms": None,
        "bias_snapshot": frozen,
        "proof_type": "PERSISTENT_METAORDER",
        "proof_venue": proposer,
        "cash_venues": cash,
        "cash_opponents": [],
        "supporting_venues": sorted(names),
        "futures_response": True,
        "futures_response_ms": int(
            (venue_reports.get("futures") or {}).get("first_aligned_ms") or 0
        ) or None,
        "futures_follow_ok": True,
        "futures_follow_invalidated": False,
        "futures_cash_response_ok": True,
        "last_evidence_ms": latest_evidence_ms,
        "flow_by_venue": flow_by_venue,
        "flow_efficiency": _flow_efficiency_snapshot(histories, side, cash),
        "venue_moves_bps": moves,
        "venue_anchor_prices": {
            name: (venue_reports.get(name) or {}).get("first_price")
            for name in names
        },
        "venue_anchor_policy": "PERSISTENT_WAVE_FIRST_EXECUTED_PRICE",
        "impulse_phase": "EARLY",
        "consumed_fraction": consumed,
        "phase_measurement": phase,
        "residual_edge_proxy_bps": 0.0,
        "residual_edge_source": "EMPIRICAL_GUARDIAN_OUTCOME_REQUIRED",
        "oi_intent": oi,
        "oi_verification_state": oi_verification,
        "economic_contract_version": ECONOMIC_CONTRACT_VERSION,
        "bias_context_quality": (
            "PROVISIONAL_DUAL_CASH_OI_BUILD"
            if provisional_context else "ESTABLISHED_OR_PULLBACK"
        ),
        "unwind_cash_independence": (
            "DUAL_CASH" if oi_status == "FRESH_UNWIND" else "NOT_REQUIRED"
        ),
        "causal_class": (
            "CASH_LED_UNWIND" if oi_status == "FRESH_UNWIND"
            else "ALIGNED_BUILD" if oi_status == "FRESH_POSITION_BUILD"
            else "PERSISTENT_CASH_WAVE"
        ),
        "persistent_evidence": report,
        "clock_quality": clock_quality,
    }
    confidence = min(0.90, 0.64 + 0.04 * len(payload["supporting_venues"]))
    state._ignition_pending_capture_id = candidate_id
    return {
        "version": VERSION,
        "decision": "GO",
        "entry_mode": "PERSISTENT_METAORDER",
        "execution_policy": "TAKER",
        "phase": "RELEASE",
        "confidence": round(confidence, 6),
        "reason": "PERSISTENT_METAORDER_PROVED",
        "price_threshold_bps": MATERIAL_PRICE_BPS,
        "side": side,
        "bias_confidence": _f(frozen.get("confidence")),
        "s_quorum": 2,
        "ignition": payload,
        "causal": _causal(payload),
        "s_votes": _compat_votes(True, payload),
        "freshness": freshness,
        "ts": now,
        "causal_episode_id": candidate_id,
    }


def _oi_intent(state, side, now, bias_snapshot=None):
    frozen = (bias_snapshot or {}).get("direction_context") or {}
    regime = str(frozen.get("oi_regime") or "UNKNOWN").upper()
    frozen_updated_at = _f(frozen.get("oi_updated_at"))
    frozen_age = now - frozen_updated_at
    live_updated_at = _f(getattr(state, "open_interest_updated_at", 0.0))
    live_age = now - live_updated_at
    frozen_intent = (
        "UNWIND" if regime in ("SHORT_COVERING", "LONG_LIQUIDATION_CLOSING") else
        "POSITION_BUILD" if regime in ("NEW_LONG_BUILD", "NEW_SHORT_BUILD") else "NEUTRAL"
    )
    expected_side = {
        "SHORT_COVERING": "LONG", "LONG_LIQUIDATION_CLOSING": "SHORT",
        "NEW_LONG_BUILD": "LONG", "NEW_SHORT_BUILD": "SHORT",
    }.get(regime)
    frozen_fresh = bool(
        frozen_updated_at > 0.0 and 0.0 <= frozen_age <= 6.0
    )
    live_fresh = bool(
        live_updated_at > 0.0 and 0.0 <= live_age <= 6.0
    )
    delta = _f(getattr(state, "open_interest_change_pct", 0.0))
    sample_window = _f(getattr(state, "open_interest_change_window_seconds", 0.0))
    delta_valid = bool(
        live_fresh and 0.0 < sample_window <= OI_SAMPLE_MAX_SECONDS
        and _f(getattr(state, "prev_open_interest", 0.0)) > 0.0
    )
    # A freshly observed material delta is closer to the ignition than the
    # frozen 60-second context.  It classifies build versus unwind, but never
    # invents LONG/SHORT direction; Bias remains direction authority.
    if delta_valid and delta >= OI_BUILD_MIN_PCT:
        intent, intent_source = "POSITION_BUILD", "REFRESHED_OI_DELTA"
        expected_side = str(side).upper()
    elif delta_valid and delta <= -OI_BUILD_MIN_PCT:
        intent, intent_source = "UNWIND", "REFRESHED_OI_DELTA"
        expected_side = str(side).upper()
    elif delta_valid and not frozen_fresh:
        # A current but immaterial OI move is better evidence than a stale
        # frozen regime.  It confirms NEUTRAL state only; direction remains
        # exclusively owned by the frozen Bias snapshot.
        intent, intent_source = "NEUTRAL", "REFRESHED_OI_DELTA_NEUTRAL"
        expected_side = None
    else:
        intent, intent_source = frozen_intent, "FROZEN_BIAS_OI_REGIME"
    fresh = live_fresh if intent_source.startswith("REFRESHED_OI_DELTA") else frozen_fresh
    aligned = expected_side in (None, str(side).upper())
    return {
        "intent": intent,
        "raw_regime": regime, "fresh": fresh,
        "age_seconds": round(max(0.0, live_age if intent_source.startswith("REFRESHED_OI_DELTA") else frozen_age), 4),
        "frozen_oi_updated_at": frozen_updated_at or None,
        "frozen_oi_age_seconds": round(max(0.0, frozen_age), 4) if frozen_updated_at > 0.0 else None,
        "live_oi_updated_at": live_updated_at or None,
        "live_oi_age_seconds": round(max(0.0, live_age), 4) if live_updated_at > 0.0 else None,
        "intent_source": intent_source,
        "change_pct": round(delta, 6) if delta_valid else None,
        "sample_window_seconds": round(sample_window, 4) if delta_valid else None,
        "side": side, "expected_side": expected_side, "aligned_with_entry": aligned,
        "causal_class": ("OI_STALE_CONTEXT" if not fresh else
            "ALIGNED_BUILD" if intent == "POSITION_BUILD" and aligned else
            "CASH_LED_UNWIND" if intent == "UNWIND" and aligned else
            "OI_DIRECTION_CONFLICT" if intent != "NEUTRAL" else "OI_NEUTRAL"
        ),
    }


def _phase_measurement(state, side, cash_venues, venue_moves, latest, episode=None):
    """Volatility-normalized displacement observed before proof.

    This is not a forecast of the final wave.  ATR supplies only a local scale;
    the measured numerator is cash displacement already visible at decision
    time.  Missing ATR fails closed instead of inventing a 20/35/65% phase.
    """
    spot = (_f(getattr(state, "best_bid", 0.0)) + _f(getattr(state, "best_ask", 0.0))) / 2.0
    atr = _f(getattr(state, "atr_1m", 0.0))
    event_now = _f((latest or {}).get("receive_time_ms")) / 1000.0
    atr_updated_at = _f(getattr(state, "atr_1m_updated_at", 0.0))
    atr_age = event_now - atr_updated_at
    atr_bps = atr / spot * 10_000.0 if spot > 0.0 and atr > 0.0 else 0.0
    episode_progress = max(
        (max(0.0, _f(venue_moves.get(name))) for name in cash_venues),
        default=0.0,
    )
    precursor = dict((episode or {}).get("precursor_measurement") or {})
    precursor_progress = max(0.0, _f(precursor.get("progress_bps"))) if precursor.get("valid") else 0.0
    progress = max(episode_progress, precursor_progress)
    if (
        atr_bps <= 0.0 or atr_updated_at <= 0.0
        # The latest completed 100ms bucket can precede the ATR assignment by
        # one scheduler turn. Treat only a material future timestamp as bad.
        or atr_age < -1.0 or atr_age > ATR_MAX_AGE_SECONDS
    ):
        return {
            "valid": False,
            "source": (
                "ATR_1M_UNAVAILABLE" if atr_bps <= 0.0 else
                "ATR_1M_TIMESTAMP_UNAVAILABLE" if atr_updated_at <= 0.0 else
                "ATR_1M_STALE"
            ),
            "atr_updated_at": atr_updated_at or None,
            "atr_age_seconds": round(max(0.0, atr_age), 4) if atr_updated_at > 0.0 else None,
            "cash_displacement_bps": round(progress, 6),
            "episode_cash_displacement_bps": round(episode_progress, 6),
            "precursor_cash_displacement_bps": round(precursor_progress, 6),
            "precursor_measurement": precursor,
            "phase_scale_bps": None, "consumed_fraction": 1.0,
        }
    consumed = max(0.0, min(1.5, progress / atr_bps))
    return {
        "valid": True,
        "source": "HYBRID_PRECURSOR_AND_EPISODE_CASH_DISPLACEMENT_OVER_ATR_1M",
        "cash_displacement_bps": round(progress, 6),
        "episode_cash_displacement_bps": round(episode_progress, 6),
        "precursor_cash_displacement_bps": round(precursor_progress, 6),
        "precursor_measurement": precursor,
        "phase_scale_bps": round(atr_bps, 6),
        "atr_updated_at": atr_updated_at,
        "atr_age_seconds": round(max(0.0, atr_age), 4),
        "consumed_fraction": round(consumed, 6),
        "latest_conversion_bps": round(
            max(0.0, _sign(side) * _f(latest.get("price_conversion_bps"))), 6
        ),
    }


def _result_from_episode(state, episode, histories, freshness, now):
    proof_type, proof_signal, proof_venue = _proof(episode, histories)
    leader, lead_lower = _leader(episode)
    cash_discovery = _cash_discovery(episode)
    episode["leader"] = leader
    side = episode["side"]
    persistent_snapshot = dict(
        getattr(state, "persistent_metaorder_shadow", {}) or {}
    )
    persistent_shadow = dict(
        (persistent_snapshot.get("sides") or {}).get(side) or {}
    )
    cash_signals = [row for row in episode["signals"] if row["venue"] in CASH and row["side"] == side]
    futures_signals = [row for row in episode["signals"] if row["venue"] == "futures" and row["side"] == side]
    cash_venues = sorted({row["venue"] for row in cash_signals})
    futures_response_ms = min((int(row["receive_time_ms"]) for row in futures_signals), default=0)
    futures_response = bool(futures_response_ms)
    cash_opponents = sorted({row["venue"] for row in episode["signals"] if row["venue"] in CASH and row["side"] != side})
    proposer_is_futures = episode["proposer"] == "futures"
    cash_response_ms = min((int(row["receive_time_ms"]) for row in cash_signals), default=0)
    futures_cash_ok = bool(
        not proposer_is_futures
        or (cash_response_ms and cash_response_ms - episode["started_receive_ms"] <= FOLLOW_MAX_MS)
    )
    futures_reversals = []
    previous_reversal_bucket = None
    for row in episode["signals"]:
        if row.get("venue") != "futures" or int(
            row.get("receive_time_ms", 0) or 0
        ) <= futures_response_ms:
            continue
        bucket = int(row.get("bucket_start_ms", 0) or 0)
        opposing = bool(row.get("side") != side and _material_flow(row))
        if not opposing:
            futures_reversals = []
            previous_reversal_bucket = None
            continue
        if (
            previous_reversal_bucket is None
            or bucket - previous_reversal_bucket != ignition_signals.BUCKET_MS
        ):
            futures_reversals = [row]
        else:
            futures_reversals.append(row)
        previous_reversal_bucket = bucket
    futures_reversal_confirmed = len(futures_reversals) >= 2
    futures_follow_ok = bool(
        proposer_is_futures
        or (futures_response_ms and futures_response_ms - episode["started_receive_ms"] <= FOLLOW_MAX_MS)
    ) and not futures_reversal_confirmed
    latest = proof_signal or episode["signals"][-1]
    venue_moves = {}
    venue_anchors = {}
    venue_anchor_methods = {}
    for venue, history in histories.items():
        if not history:
            continue
        anchor, anchor_method = _venue_anchor(history, episode["started_receive_ms"])
        if anchor <= 0.0:
            continue
        venue_anchors[venue] = round(anchor, 10)
        venue_anchor_methods[venue] = anchor_method
        venue_moves[venue] = round(
            _sign(side) * _bps(_f(history[-1].get("price")), anchor), 6
        )
    cash_move = max((venue_moves[name] for name in cash_venues if name in venue_moves), default=0.0)
    futures_move = venue_moves.get("futures")
    fair_value_gap = (
        max(0.0, cash_move - futures_move)
        if futures_move is not None else 0.0
    )
    phase_measurement = _phase_measurement(
        state, side, cash_venues, venue_moves, latest, episode,
    )
    consumed = _f(phase_measurement.get("consumed_fraction"), 1.0)
    # The cash/Futures gap measures handoff timing, not remaining alpha.  Using
    # it as edge inverted confirmation: better Futures follow meant less edge.
    # Completed Guardian outcomes are the only promotion authority.
    residual_proxy = 0.0
    oi = _oi_intent(state, side, now, episode.get("bias_snapshot"))
    oi_verification = _oi_verification(
        oi,
        episode.get("oi_before_snapshot"),
        _oi_state_snapshot(state),
        episode_started_ms=episode.get("started_receive_ms"),
        decision_time=now,
    )
    payload = {
        "causal_episode_id": episode["causal_episode_id"], "state": "PROBE",
        "side": side, "proposer": episode["proposer"], "leader": leader,
        "lead_lower_bound_ms": round(lead_lower, 4) if lead_lower is not None else None,
        "spot_price_discovery": cash_discovery,
        "persistent_metaorder_shadow": persistent_shadow,
        "bias_snapshot": dict(episode["bias_snapshot"]), "proof_type": proof_type,
        "proof_venue": proof_venue,
        "cash_venues": cash_venues, "cash_opponents": cash_opponents,
        "supporting_venues": sorted({row["venue"] for row in episode["signals"] if row["side"] == side}),
        "futures_response": futures_response,
        "futures_response_ms": futures_response_ms or None,
        "futures_follow_ok": futures_follow_ok,
        "futures_follow_invalidated": futures_reversal_confirmed,
        "futures_reversal_buckets": len(futures_reversals),
        "last_evidence_ms": int(episode.get("last_evidence_ms", 0) or 0),
        "futures_cash_response_ok": futures_cash_ok,
        "flow_by_venue": {row["venue"]: {
            "signed_imbalance": round(_sign(side) * _f(row.get("imbalance")), 6),
            "volume_btc": _f(row.get("total_qty")),
            "surprise_ratio": _f(row.get("surprise_ratio")),
            "flow_acceleration": _f(row.get("flow_acceleration")),
            "price_conversion_bps": round(
                _sign(side) * _f(row.get("price_conversion_bps")), 6
            ),
            "receive_time_ms": int(row.get("receive_time_ms", 0) or 0),
        } for row in episode["signals"][-3:]},
        "flow_efficiency": _flow_efficiency_snapshot(
            histories, side, cash_venues,
        ),
        "venue_moves_bps": venue_moves,
        "venue_anchor_prices": venue_anchors,
        "venue_anchor_methods": venue_anchor_methods,
        "venue_anchor_policy": "PER_VENUE_PRE_EPISODE",
        "failed_reversion_evidence": (
            dict(proof_signal.get("_failed_reversion_evidence") or {})
            if proof_type == "FAILED_REVERSION" and isinstance(proof_signal, dict) else {}
        ),
        "metaorder_proof_evidence": (
            dict(proof_signal.get("_metaorder_evidence") or {})
            if proof_type == "METAORDER_CONTINUATION"
            and isinstance(proof_signal, dict) else {}
        ),
        "impulse_phase": "EARLY" if consumed <= 0.35 else "MATURE",
        "consumed_fraction": consumed, "fair_value_gap_bps": round(fair_value_gap, 6),
        "handoff_gap_bps": round(fair_value_gap, 6),
        "phase_measurement": phase_measurement,
        "residual_edge_proxy_bps": round(residual_proxy, 6),
        "residual_edge_source": "EMPIRICAL_GUARDIAN_OUTCOME_REQUIRED",
        "oi_intent": oi,
        "oi_verification_state": oi_verification,
        "economic_contract_version": ECONOMIC_CONTRACT_VERSION,
        "pending_reversal_promoted": bool(
            episode.get("pending_reversal_promoted")
        ),
        "pending_reversal_episode_hash": episode.get("episode_hash"),
        "pending_reversal_original_onset": dict(
            episode.get("onset_evidence") or {}
        ),
        "pre_impulse_bias_snapshot": dict(
            episode.get("pre_impulse_bias_snapshot") or {}
        ),
        "bias_confirmation_snapshot": dict(
            episode.get("bias_confirmation_snapshot") or {}
        ),
        "clock_quality": {name: {
            "uncertainty_ms": rows[-1].get("clock_uncertainty_ms"),
            "valid": rows[-1].get("clock_valid"), "epoch": rows[-1].get("epoch"),
        } for name, rows in histories.items() if rows},
    }
    resolved_reversion_venue = None
    if proof_type == "FAILED_REVERSION" and isinstance(proof_signal, dict):
        resolved_reversion_venue = (
            (proof_signal.get("_failed_reversion_evidence") or {}).get("venue")
        )
    unresolved_cash_opponents = [
        venue for venue in cash_opponents if venue != resolved_reversion_venue
    ]
    payload["resolved_cash_opponents"] = (
        [resolved_reversion_venue] if resolved_reversion_venue else []
    )
    payload["unresolved_cash_opponents"] = unresolved_cash_opponents
    if unresolved_cash_opponents:
        state._ignition_episode = None
        return _wait(now, side, "OPPOSING_CASH_FLOW", "INVALID", payload, freshness)
    if freshness["coinbase_mode"] == "STALE":
        return _wait(now, side, "WAIT_STALE_COINBASE", "PROBE", payload, freshness)
    if not freshness["binance_spot_ready"] or not freshness["futures_ready"]:
        return _wait(now, side, "WAIT_FEED_GROUP_NOT_READY", "PROBE", payload, freshness)
    oi_status = str(oi_verification.get("status") or "UNAVAILABLE")
    strong_independent_cash_proof = bool(
        cash_venues and futures_cash_ok and proof_type is not None
    )
    if proposer_is_futures and not futures_cash_ok:
        return _wait(
            now, side, "WAIT_FUTURES_ALERT_CASH_RESPONSE",
            "PROBE", payload, freshness,
        )
    if (
        proposer_is_futures
        and oi_status not in {"FRESH_POSITION_BUILD", "FRESH_UNWIND"}
        and not strong_independent_cash_proof
    ):
        return _wait(
            now, side, "WAIT_FUTURES_PROPOSER_OI_REFRESH",
            "PRESSURE_BUILDING", payload, freshness,
        )
    if proposer_is_futures and oi_status == "FRESH_UNWIND":
        state._ignition_episode = None
        return _wait(
            now, side, "WAIT_FUTURES_PROPOSER_OI_UNWIND",
            "INVALID", payload, freshness,
        )
    if not proposer_is_futures and not futures_follow_ok:
        return _wait(now, side, "WAIT_CASH_IGNITION_FUTURES_RESPONSE", "PROBE", payload, freshness)
    if leader == "SIMULTANEOUS" and proof_type != "FAILED_REVERSION":
        return _wait(now, side, "WAIT_CAUSAL_LEADER_UNCERTAIN", "PROBE", payload, freshness)
    if proof_type is None:
        return _wait(now, side, "WAIT_IGNITION_PROOF", "PROBE", payload, freshness)
    if freshness["coinbase_mode"] == "DEGRADED" and not (
        episode["proposer"] == "binance_spot"
        and proof_venue == "binance_spot"
        and futures_follow_ok
    ):
        return _wait(
            now, side, "WAIT_DEGRADED_COINBASE_REQUIRES_BINANCE_CASH",
            "PROBE", payload, freshness,
        )
    if not phase_measurement.get("valid"):
        return _wait(now, side, "WAIT_PHASE_SCALE_UNAVAILABLE", "PROBE", payload, freshness)
    if oi_status == "FRESH_CONFLICT":
        state._ignition_episode = None
        return _wait(now, side, "OI_INTENT_DIRECTION_CONFLICT", "INVALID", payload, freshness)
    if consumed > MAX_CONSUMED_FRACTION:
        state._ignition_episode = None
        return _wait(now, side, "WAIT_IMPULSE_ALREADY_CONSUMED", "MATURE", payload, freshness)
    payload["state"] = "PROVE"
    execution = "MAKER" if proof_type == "FAILED_REVERSION" else "TAKER"
    confidence = min(0.92, 0.62 + 0.05 * len(payload["supporting_venues"]) + 0.05 * (proof_type == "FAILED_REVERSION"))
    result = {
        "version": VERSION, "decision": "GO", "entry_mode": "IGNITION",
        "execution_policy": execution,
        "phase": "ACCEPTANCE" if execution == "MAKER" else "RELEASE",
        "confidence": round(confidence, 6), "reason": "IGNITION_" + proof_type,
        "price_threshold_bps": MATERIAL_PRICE_BPS,
        "side": side, "bias_confidence": _f(episode["bias_snapshot"].get("confidence")),
        "s_quorum": 2, "ignition": payload, "causal": _causal(payload),
        "s_votes": _compat_votes(True, payload), "freshness": freshness, "ts": now,
        "causal_episode_id": episode["causal_episode_id"],
    }
    # GO is a proposal, not an execution capture. Keep the episode retryable
    # across verified pre-order failures (for example a transient BBO outage).
    # The launcher calls capture_episode only after a real/virtual fill exists.
    state._ignition_pending_capture_id = episode["causal_episode_id"]
    return result


def capture_episode(state, causal_episode_id, side=None, last_evidence_ms=None):
    """Irreversibly close an Ignition episode only after execution capture."""
    episode_id = str(causal_episode_id or "")
    if not episode_id:
        return False
    episode = getattr(state, "_ignition_episode", None)
    pending = str(getattr(state, "_ignition_pending_capture_id", "") or "")
    _tombstone(state, episode_id)
    resolved_side = str(
        side or (episode or {}).get("side") or getattr(state, "bias_state", "")
    ).upper()
    evidence_ms = int(
        last_evidence_ms
        or (episode or {}).get("last_evidence_ms", 0)
        or time.time() * 1000.0
    )
    state._ignition_cooldown_side = resolved_side
    state._ignition_cooldown_until_ms = evidence_ms + 5_000
    if isinstance(episode, dict) and str(episode.get("causal_episode_id") or "") == episode_id:
        state._ignition_episode = None
    persistent = dict(getattr(state, "persistent_metaorder_shadow", {}) or {})
    if str(persistent.get("candidate_id") or "") == episode_id:
        persistent["captured"] = True
        state.persistent_metaorder_shadow = persistent
    if not pending or pending == episode_id:
        state._ignition_pending_capture_id = None
    return True


def evaluate(state, now=None, side=None):
    now = time.time() if now is None else float(now)
    now_ms = int(now * 1000.0)
    side = str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    freshness = _freshness(state, now)
    histories = ignition_signals.snapshot(state, now_ms)
    previous_persistent = getattr(state, "persistent_metaorder_shadow", None)
    persistent_snapshot = _persistent_metaorder_snapshot(
        histories, now_ms, previous_persistent
    )
    state.persistent_metaorder_shadow = persistent_snapshot
    state.persistent_metaorder_updated_at = now
    rows = _new_signals(state, histories)
    # This payload describes only a proposer observed in this evaluator cycle;
    # never let an old research reject masquerade as fresh evidence.
    state._ignition_last_reject_payload = {}
    _tee_borderline_pre_bias(state, rows)
    episode = getattr(state, "_ignition_episode", None)
    pending_handled = _observe_pending_reversal(state, rows, histories)
    promoted = _resolve_pending_reversal(
        state, histories, now_ms, allow_promotion=episode is None,
    )
    if promoted is not None:
        episode = promoted

    if episode is not None:
        live_venues = ignition_signals.engine(state).venues
        epoch_changed = any(
            name in live_venues and int(live_venues[name].epoch) != int(epoch)
            for name, epoch in (episode.get("epochs") or {}).items()
        )
        if epoch_changed:
            state._ignition_episode = None
            episode = None
            state._ignition_last_reject = "EXECUTED_FLOW_EPOCH_RESET"

    if side not in ("LONG", "SHORT"):
        state._ignition_episode = None
        _remember_bias(state, now)
        return _wait(
            now, side, "BIAS_ABSTAIN",
            episode=_research_payload(state) or None, freshness=freshness,
        )
    if _f(getattr(state, "bias_confidence", 0.0)) < BIAS_MIN_CONF:
        state._ignition_episode = None
        _remember_bias(state, now)
        return _wait(
            now, side, "BIAS_CONFIDENCE_LOW",
            episode=_research_payload(state) or None, freshness=freshness,
        )
    bias_ts = _f(getattr(state, "bias_updated_at", 0.0))
    if bias_ts <= 0.0 or now - bias_ts > BIAS_MAX_AGE:
        state._ignition_episode = None
        return _wait(
            now, side, "BIAS_STALE",
            episode=_research_payload(state) or None, freshness=freshness,
        )

    if episode is not None:
        age = now_ms - int(episode.get("started_receive_ms", 0))
        gap = now_ms - int(episode.get("last_evidence_ms", 0))
        # A newly completed receive-time bucket is evidence that existed before
        # this evaluator wake-up. Consume it before applying decay; otherwise a
        # 1 ms scheduler delay can erase a valid 300 ms cash response.
        if age > EPISODE_MAX_MS or (gap > EVIDENCE_GAP_MS and not rows):
            reason = "IGNITION_EPISODE_SAFETY_EXPIRED" if age > EPISODE_MAX_MS else "IGNITION_EVIDENCE_DECAYED"
            state._ignition_episode = None
            episode = None
            state._ignition_last_reject = reason

    for row in rows:
        token = (
            str(row.get("venue") or ""),
            int(row.get("bucket_start_ms", -1)),
        )
        if token in pending_handled:
            continue
        if not row.get("clock_valid"):
            state._ignition_episode = None
            episode = None
            state._ignition_last_reject = "CLOCK_OR_EVENT_TIME_INVALID"
            continue
        if episode is not None and row.get("venue") in CASH | {"futures"}:
            if str(row.get("side")) != str(episode.get("side")) and _material_flow(row):
                episode["signals"].append(dict(row))
                episode["last_evidence_ms"] = int(row.get("receive_time_ms", now_ms))
                continue
        appended = False
        # A proposer/proof still needs adaptive surprise (``strong``), while
        # an independent follower only needs the already-established material
        # price+executed-flow contract.  Requiring another 2-deviation shock
        # here would mistake corroboration for a second proposer and create
        # needless misses.
        if (
            episode is not None
            and str(row.get("side")) == str(episode.get("side"))
            and _material_flow(row)
            and int(row.get("receive_time_ms", 0))
                - int(episode.get("started_receive_ms", 0)) <= EPISODE_MAX_MS
        ):
            episode["signals"].append(dict(row))
            episode["last_evidence_ms"] = int(row.get("receive_time_ms", now_ms))
            episode["epochs"][row.get("venue")] = int(row.get("epoch", 0))
            appended = True
        if not row.get("strong"):
            continue
        if episode is None:
            cooldown_side = str(getattr(state, "_ignition_cooldown_side", "") or "")
            cooldown_until = int(getattr(state, "_ignition_cooldown_until_ms", 0) or 0)
            if str(row.get("side")) == cooldown_side and int(row.get("receive_time_ms", 0)) <= cooldown_until:
                state._ignition_last_reject = "CAUSAL_EPISODE_ALREADY_CAPTURED"
                continue
            episode = _start_episode(state, row, histories)
            continue
        if not appended and str(row.get("side")) == str(episode.get("side")):
            if int(row.get("receive_time_ms", 0)) - int(episode.get("started_receive_ms", 0)) <= EPISODE_MAX_MS:
                episode["signals"].append(dict(row))
                episode["last_evidence_ms"] = int(row.get("receive_time_ms", now_ms))
                episode["epochs"][row.get("venue")] = int(row.get("epoch", 0))

    if episode is None:
        _remember_bias(state, now)
        persistent_result = _persistent_entry_result(
            state, persistent_snapshot, histories, freshness, now,
        )
        if persistent_result is not None:
            return persistent_result
        reason = str(getattr(state, "_ignition_last_reject", "WAIT_IGNITION") or "WAIT_IGNITION")
        research = _research_payload(state)
        return _wait(
            now, side, reason, episode=research or None, freshness=freshness
        )
    if now_ms - int(episode.get("last_evidence_ms", 0)) > EVIDENCE_GAP_MS:
        state._ignition_episode = None
        state._ignition_last_reject = "IGNITION_EVIDENCE_DECAYED"
        return _wait(
            now, side, "IGNITION_EVIDENCE_DECAYED", "INVALID",
            {"causal_episode_id": episode.get("causal_episode_id")}, freshness,
        )
    result = _result_from_episode(state, episode, histories, freshness, now)
    if result.get("decision") == "GO":
        return result
    persistent_result = _persistent_entry_result(
        state, persistent_snapshot, histories, freshness, now,
    )
    return persistent_result if persistent_result is not None else result


def update_state(state, now=None):
    now = time.time() if now is None else float(now)
    last = _f(getattr(state, "entry_shadow_updated_at", 0.0))
    if now - last < EVAL_THROTTLE:
        return getattr(state, "entry_shadow_council", None)
    result = evaluate(state, now=now)
    state.entry_shadow_council = result
    state.entry_shadow_decision = result["decision"]
    state.entry_shadow_confidence = result["confidence"]
    state.entry_shadow_phase = result["phase"]
    state.entry_shadow_mode = result["entry_mode"]
    state.entry_shadow_updated_at = now
    state.entry_shadow_version = VERSION
    return result
