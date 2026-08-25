"""Ignition Core V1: Predict -> Probe -> Prove live entry authority.

Bias is frozen before the first impulse bucket.  Futures may create an alert,
but only independent cash price plus executed flow can prove an entry.  PROBE
is evidence state only; this module never sizes or submits an order.
"""

from collections import deque
import time

from loi_he_thong import ignition_signals


VERSION = "IGNITION_CORE_V1"
BIAS_MIN_CONF = 0.55
BIAS_MAX_AGE = 3.0
BIAS_MIN_PRE_IMPULSE_AGE = 1.0
EVAL_THROTTLE = 0.10
FOLLOW_MAX_MS = 600
EVIDENCE_GAP_MS = 300
EPISODE_MAX_MS = 5_000
LEAD_FLOOR_MS = 100.0
MAX_CONSUMED_FRACTION = 0.35
MIN_ACCEPTANCE_MS = 400
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
    row = {
        "direction": str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper(),
        "confidence": _f(getattr(state, "bias_confidence", 0.0)),
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
            "flow_price_trap": bool(
                str(story.get("name") or council.get("reason") or "")
                == "FLOW_NOT_CONVERTED_TO_PRICE"
            ),
        },
    }
    if not history or (
        history[-1].get("direction"), history[-1].get("confidence"),
        history[-1].get("updated_at"), history[-1].get("direction_context")
    ) != (
        row["direction"], row["confidence"], row["updated_at"],
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
            "venue_moves_bps": {}, "cash_coverage": 0,
        }
    sign = _sign(signal.get("side"))
    best = None
    for horizon in (3, 6, 15):
        reference = _bias_bucket_at(buckets, signal_s - horizon, 2.0)
        if reference is None:
            continue
        moves = {}
        for source, venue in (("spot", "binance_spot"), ("coinbase", "coinbase_spot")):
            current_price = _f(current.get(source))
            reference_price = _f(reference.get(source))
            if current_price > 0.0 and reference_price > 0.0:
                moves[venue] = sign * _bps(current_price, reference_price)
        if not moves:
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
        if best is None or progress > _f(best.get("progress_bps")):
            best = candidate
    return best or {
        "valid": False, "source": "PRECURSOR_REFERENCE_UNAVAILABLE",
        "progress_bps": 0.0, "horizon_seconds": None,
        "venue_moves_bps": {}, "cash_coverage": 0,
    }


def _start_episode(state, signal):
    bias = _bias_snapshot(state, signal)
    side = str(signal.get("side") or "NEUTRAL").upper()
    if side != bias.get("direction") or _f(bias.get("confidence")) < BIAS_MIN_CONF:
        state._ignition_last_reject = "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS"
        return None
    context = bias.get("direction_context") or {}
    candidate = str(context.get("candidate_side") or "ABSTAIN").upper()
    if (
        str(context.get("phase") or "").upper() == "REVERSAL_CANDIDATE"
        and candidate in ("LONG", "SHORT")
        and candidate != side
    ):
        state._ignition_last_reject = "BIAS_REVERSAL_CANDIDATE_PENDING"
        return None
    if bool(context.get("flow_price_trap")):
        state._ignition_last_reject = "FROZEN_BIAS_FLOW_PRICE_TRAP"
        return None
    episode = {
        "causal_episode_id": _episode_id(signal), "side": side,
        "proposer": signal.get("venue"), "leader": signal.get("venue"),
        "started_receive_ms": int(signal.get("receive_time_ms", 0)),
        "last_evidence_ms": int(signal.get("receive_time_ms", 0)),
        "bias_snapshot": bias, "signals": [dict(signal)],
        "epochs": {signal.get("venue"): int(signal.get("epoch", 0))},
        "precursor_measurement": _precursor_cash_progress(state, signal),
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
        rows = [
            row for row in histories.get(venue, ())
            if row.get("side") == side
            and int(row.get("receive_time_ms", 0)) >= episode["started_receive_ms"]
        ]
        if len(rows) >= 2 and rows[-1].get("strong") and rows[-2].get("strong"):
            if _f(rows[-1].get("flow_acceleration")) >= 0.0:
                candidates.append(("METAORDER_CONTINUATION", rows[-1], venue))
        failed = _failed_reversion(histories.get(venue, ()), side, episode["started_receive_ms"])
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
    """Bounded 1-6 s persistence measurement; never Entry authority.

    It deliberately reuses exact executed-flow buckets instead of inventing a
    second signal stack.  The fixed-size history keeps hot-path work bounded.
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
        first_price = last_price = 0.0
        for cell in ordered:
            total = cell["buy"] + cell["sell"]
            if total <= 0.0:
                continue
            observed += 1
            imbalance = sign * (cell["buy"] - cell["sell"]) / total
            aligned += int(imbalance >= 0.20)
            opposed += int(imbalance <= -0.20)
            if first_price <= 0.0 and cell["first"] > 0.0:
                first_price = cell["first"]
            if cell["last"] > 0.0:
                last_price = cell["last"]
        progress = sign * _bps(last_price, first_price) if first_price > 0.0 else 0.0
        persistence = aligned / observed if observed else 0.0
        structural = bool(
            contiguous and observed >= 3 and aligned >= 2
            and persistence >= (2.0 / 3.0)
            and opposed == 0 and progress >= 0.15
        )
        venues[venue] = {
            "observed_seconds": observed, "aligned_seconds": aligned,
            "opposed_seconds": opposed,
            "contiguous_seconds": contiguous,
            "persistence_ratio": round(persistence, 6),
            "price_progress_bps": round(progress, 6),
            "structural_candidate": structural,
        }
    cash = [name for name in CASH if venues[name]["structural_candidate"]]
    futures_follow = venues["futures"]["structural_candidate"]
    status = (
        "PERSISTENT_METAORDER_CANDIDATE" if cash and futures_follow else
        "WAIT_PERSISTENT_FUTURES_FOLLOW" if cash else "OBSERVING"
    )
    return {
        "version": "PERSISTENT_METAORDER_SHADOW_V1", "status": status,
        "cash_candidates": sorted(cash), "futures_follow": futures_follow,
        "venues": venues, "authority": False,
        "calibration_status": "BOOTSTRAP_UNVERIFIED",
        "policy": "TELEMETRY_ONLY_NEVER_OPENS_OR_VETOES",
    }


def _persistent_metaorder_snapshot(histories, now_ms, previous=None):
    """Observe slow cash persistence independently from 100 ms ignition.

    This is deliberately recorder-only.  Evaluating both sides here makes a
    1-6 second metaorder visible even when no two-deviation proposer ever
    creates an Ignition episode.
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
    return {
        "version": "PERSISTENT_METAORDER_SHADOW_V2",
        "status": status,
        "candidate_side": candidate_side,
        "sides": sides,
        "authority": False,
        "policy": "RECORDER_ONLY_NEVER_OPENS_OR_VETOES",
        "transition": identity != previous_identity,
    }


def _oi_intent(state, side, now, bias_snapshot=None):
    frozen = (bias_snapshot or {}).get("direction_context") or {}
    regime = str(frozen.get("oi_regime") or "UNKNOWN").upper()
    oi_updated_at = _f(getattr(state, "open_interest_updated_at", 0.0))
    age = now - oi_updated_at
    frozen_intent = (
        "UNWIND" if regime in ("SHORT_COVERING", "LONG_LIQUIDATION_CLOSING") else
        "POSITION_BUILD" if regime in ("NEW_LONG_BUILD", "NEW_SHORT_BUILD") else "NEUTRAL"
    )
    expected_side = {
        "SHORT_COVERING": "LONG", "LONG_LIQUIDATION_CLOSING": "SHORT",
        "NEW_LONG_BUILD": "LONG", "NEW_SHORT_BUILD": "SHORT",
    }.get(regime)
    fresh = bool(oi_updated_at > 0.0 and 0.0 <= age <= 6.0)
    delta = _f(getattr(state, "open_interest_change_pct", 0.0))
    sample_window = _f(getattr(state, "open_interest_change_window_seconds", 0.0))
    delta_valid = bool(
        fresh and 0.0 < sample_window <= OI_SAMPLE_MAX_SECONDS
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
    else:
        intent, intent_source = frozen_intent, "FROZEN_BIAS_OI_REGIME"
    aligned = expected_side in (None, str(side).upper())
    return {
        "intent": intent,
        "raw_regime": regime, "fresh": fresh, "age_seconds": round(max(0.0, age), 4),
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
    atr_bps = atr / spot * 10_000.0 if spot > 0.0 and atr > 0.0 else 0.0
    episode_progress = max(
        (max(0.0, _f(venue_moves.get(name))) for name in cash_venues),
        default=0.0,
    )
    precursor = dict((episode or {}).get("precursor_measurement") or {})
    precursor_progress = max(0.0, _f(precursor.get("progress_bps"))) if precursor.get("valid") else 0.0
    progress = max(episode_progress, precursor_progress)
    if atr_bps <= 0.0:
        return {
            "valid": False, "source": "ATR_1M_UNAVAILABLE",
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
    futures_follow_ok = bool(
        proposer_is_futures
        or (futures_response_ms and futures_response_ms - episode["started_receive_ms"] <= FOLLOW_MAX_MS)
    )
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
        "futures_cash_response_ok": futures_cash_ok,
        "flow_by_venue": {row["venue"]: {
            "signed_imbalance": round(_sign(side) * _f(row.get("imbalance")), 6),
            "volume_btc": _f(row.get("total_qty")),
            "surprise_ratio": _f(row.get("surprise_ratio")),
            "flow_acceleration": _f(row.get("flow_acceleration")),
        } for row in episode["signals"][-3:]},
        "venue_moves_bps": venue_moves,
        "venue_anchor_prices": venue_anchors,
        "venue_anchor_methods": venue_anchor_methods,
        "venue_anchor_policy": "PER_VENUE_PRE_EPISODE",
        "failed_reversion_evidence": (
            dict(proof_signal.get("_failed_reversion_evidence") or {})
            if proof_type == "FAILED_REVERSION" and isinstance(proof_signal, dict) else {}
        ),
        "impulse_phase": "EARLY" if consumed <= 0.35 else "MATURE",
        "consumed_fraction": consumed, "fair_value_gap_bps": round(fair_value_gap, 6),
        "handoff_gap_bps": round(fair_value_gap, 6),
        "phase_measurement": phase_measurement,
        "residual_edge_proxy_bps": round(residual_proxy, 6),
        "residual_edge_source": "EMPIRICAL_GUARDIAN_OUTCOME_REQUIRED",
        "oi_intent": oi,
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
    if proposer_is_futures and not oi.get("fresh"):
        return _wait(
            now, side, "WAIT_FUTURES_PROPOSER_OI_REFRESH",
            "PRESSURE_BUILDING", payload, freshness,
        )
    if proposer_is_futures and oi.get("intent") == "UNWIND":
        state._ignition_episode = None
        return _wait(
            now, side, "WAIT_FUTURES_PROPOSER_OI_UNWIND",
            "INVALID", payload, freshness,
        )
    if proposer_is_futures and not futures_cash_ok:
        return _wait(now, side, "WAIT_FUTURES_ALERT_CASH_RESPONSE", "PROBE", payload, freshness)
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
    if oi.get("fresh") and not oi.get("aligned_with_entry", True):
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
    _tombstone(state, episode["causal_episode_id"])
    state._ignition_cooldown_side = side
    state._ignition_cooldown_until_ms = int(episode["last_evidence_ms"]) + 5_000
    state._ignition_episode = None
    return result


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
    episode = getattr(state, "_ignition_episode", None)

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

    if side not in ("LONG", "SHORT") or _f(getattr(state, "bias_confidence", 0.0)) < BIAS_MIN_CONF:
        state._ignition_episode = None
        _remember_bias(state, now)
        return _wait(now, side, "BIAS_NOT_FRESH_OR_CONFIDENT", freshness=freshness)
    bias_ts = _f(getattr(state, "bias_updated_at", 0.0))
    if bias_ts <= 0.0 or now - bias_ts > BIAS_MAX_AGE:
        state._ignition_episode = None
        return _wait(now, side, "BIAS_NOT_FRESH_OR_CONFIDENT", freshness=freshness)

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
        if not row.get("clock_valid"):
            state._ignition_episode = None
            episode = None
            state._ignition_last_reject = "CLOCK_OR_EVENT_TIME_INVALID"
            continue
        if episode is not None and row.get("venue") in CASH:
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
            episode = _start_episode(state, row)
            continue
        if not appended and str(row.get("side")) == str(episode.get("side")):
            if int(row.get("receive_time_ms", 0)) - int(episode.get("started_receive_ms", 0)) <= EPISODE_MAX_MS:
                episode["signals"].append(dict(row))
                episode["last_evidence_ms"] = int(row.get("receive_time_ms", now_ms))
                episode["epochs"][row.get("venue")] = int(row.get("epoch", 0))

    if episode is None:
        _remember_bias(state, now)
        reason = str(getattr(state, "_ignition_last_reject", "WAIT_IGNITION") or "WAIT_IGNITION")
        return _wait(now, side, reason, freshness=freshness)
    if now_ms - int(episode.get("last_evidence_ms", 0)) > EVIDENCE_GAP_MS:
        state._ignition_episode = None
        state._ignition_last_reject = "IGNITION_EVIDENCE_DECAYED"
        return _wait(
            now, side, "IGNITION_EVIDENCE_DECAYED", "INVALID",
            {"causal_episode_id": episode.get("causal_episode_id")}, freshness,
        )
    return _result_from_episode(state, episode, histories, freshness, now)


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
