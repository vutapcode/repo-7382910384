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
    price_metrics = {
        "supporters": ["spot" if x == "binance_spot" else "coinbase" for x in cash],
        "strong_supporters": ["spot" if x == "binance_spot" else "coinbase" for x in cash],
        "opponents": list(ignition.get("cash_opponents") or ()),
        "moves": dict(ignition.get("venue_moves_bps") or {}),
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
    row = {
        "direction": str(getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper(),
        "confidence": _f(getattr(state, "bias_confidence", 0.0)),
        "updated_at": _f(getattr(state, "bias_updated_at", 0.0)),
        "version": getattr(state, "bias_version", None),
        "captured_at": time.time() if now is None else float(now),
    }
    if not history or (
        history[-1].get("direction"), history[-1].get("confidence"), history[-1].get("updated_at")
    ) != (row["direction"], row["confidence"], row["updated_at"]):
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


def _start_episode(state, signal):
    bias = _bias_snapshot(state, signal)
    side = str(signal.get("side") or "NEUTRAL").upper()
    if side != bias.get("direction") or _f(bias.get("confidence")) < BIAS_MIN_CONF:
        state._ignition_last_reject = "IGNITION_NOT_ALIGNED_WITH_FROZEN_BIAS"
        return None
    episode = {
        "causal_episode_id": _episode_id(signal), "side": side,
        "proposer": signal.get("venue"), "leader": signal.get("venue"),
        "started_receive_ms": int(signal.get("receive_time_ms", 0)),
        "last_evidence_ms": int(signal.get("receive_time_ms", 0)),
        "bias_snapshot": bias, "signals": [dict(signal)],
        "epochs": {signal.get("venue"): int(signal.get("epoch", 0))},
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
        and abs(_f(row.get("price_conversion_bps"))) >= 0.15
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
        if latest_ms - shock_ms > EVIDENCE_GAP_MS:
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
            if int(reclaim.get("receive_time_ms", 0)) - shock_ms > EVIDENCE_GAP_MS:
                break
            if str(reclaim.get("side")) != str(side) or not _material_flow(reclaim):
                continue
            if sign * _bps(_f(reclaim.get("price")), anchor) >= 0.0:
                reclaim_index = index
                break
        if reclaim_index is None:
            continue

        acceptance_rows = rows[reclaim_index + 1:]
        if not acceptance_rows or latest_ms - shock_ms > EVIDENCE_GAP_MS:
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

        reclaim = rows[reclaim_index]
        proof = dict(latest)
        proof["_failed_reversion_evidence"] = {
            "version": "FAILED_REVERSION_V2",
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
        return None, None
    candidates.sort(key=lambda item: (
        int(item[1].get("receive_time_ms", 0)),
        proof_rank[item[0]],
        item[2],
    ))
    proof_type, proof_signal, _venue = candidates[0]
    return proof_type, proof_signal


def _leader(episode):
    first_by_venue = {}
    for row in episode["signals"]:
        venue = str(row.get("venue"))
        current = first_by_venue.get(venue)
        if current is None or _f(row.get("corrected_event_time_ms")) < _f(current.get("corrected_event_time_ms")):
            first_by_venue[venue] = row
    ordered = sorted(first_by_venue.values(), key=lambda row: _f(row.get("corrected_event_time_ms")))
    first = ordered[0]
    if len(ordered) < 2:
        return str(first.get("venue")), None
    second = ordered[1]
    gap = _f(second.get("corrected_event_time_ms")) - _f(first.get("corrected_event_time_ms"))
    uncertainty = _f(first.get("clock_uncertainty_ms")) + _f(second.get("clock_uncertainty_ms"))
    lower = gap - uncertainty
    return (str(first.get("venue")) if lower >= LEAD_FLOOR_MS else "SIMULTANEOUS"), lower


def _oi_intent(state, side, now):
    council = getattr(state, "bias_council", None) or {}
    seat = ((council.get("s_votes") or {}).get("S2_price_x_oi") or {})
    metrics = seat.get("metrics") or {}
    regime = str(metrics.get("regime") or "UNKNOWN").upper()
    age = now - _f(getattr(state, "open_interest_updated_at", 0.0))
    return {
        "intent": "UNWIND" if regime in ("SHORT_COVERING", "LONG_LIQUIDATION_CLOSING") else
                  "POSITION_BUILD" if regime in ("NEW_LONG_BUILD", "NEW_SHORT_BUILD") else "NEUTRAL",
        "raw_regime": regime, "fresh": age <= 6.0, "age_seconds": round(max(0.0, age), 4),
        "side": side,
    }


def _result_from_episode(state, episode, histories, freshness, now):
    proof_type, proof_signal = _proof(episode, histories)
    leader, lead_lower = _leader(episode)
    episode["leader"] = leader
    side = episode["side"]
    cash_signals = [row for row in episode["signals"] if row["venue"] in CASH and row["side"] == side]
    futures_signals = [row for row in episode["signals"] if row["venue"] == "futures" and row["side"] == side]
    cash_venues = sorted({row["venue"] for row in cash_signals})
    futures_response = bool(futures_signals)
    cash_opponents = sorted({row["venue"] for row in episode["signals"] if row["venue"] in CASH and row["side"] != side})
    proposer_is_futures = episode["proposer"] == "futures"
    cash_response_ms = min((int(row["receive_time_ms"]) for row in cash_signals), default=0)
    futures_cash_ok = bool(
        not proposer_is_futures
        or (cash_response_ms and cash_response_ms - episode["started_receive_ms"] <= FOLLOW_MAX_MS)
    )
    latest = proof_signal or episode["signals"][-1]
    acceleration = _f(latest.get("flow_acceleration"))
    consumed = 0.20 if acceleration > 0.0 and len(episode["signals"]) <= 4 else 0.35 if acceleration >= 0.0 else 0.65
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
    conversion = max(0.0, _sign(side) * _f(latest.get("price_conversion_bps")))
    residual_proxy = max(fair_value_gap, conversion * max(0.0, (1.0 - consumed) / max(consumed, 0.05)))
    oi = _oi_intent(state, side, now)
    payload = {
        "causal_episode_id": episode["causal_episode_id"], "state": "PROBE",
        "side": side, "proposer": episode["proposer"], "leader": leader,
        "lead_lower_bound_ms": round(lead_lower, 4) if lead_lower is not None else None,
        "bias_snapshot": dict(episode["bias_snapshot"]), "proof_type": proof_type,
        "cash_venues": cash_venues, "cash_opponents": cash_opponents,
        "supporting_venues": sorted({row["venue"] for row in episode["signals"] if row["side"] == side}),
        "futures_response": futures_response, "futures_cash_response_ok": futures_cash_ok,
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
        "residual_edge_proxy_bps": round(residual_proxy, 6), "oi_intent": oi,
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
    if proposer_is_futures and not futures_cash_ok:
        return _wait(now, side, "WAIT_FUTURES_ALERT_CASH_RESPONSE", "PROBE", payload, freshness)
    if leader == "SIMULTANEOUS" and proof_type != "FAILED_REVERSION":
        return _wait(now, side, "WAIT_CAUSAL_LEADER_UNCERTAIN", "PROBE", payload, freshness)
    if proof_type is None:
        return _wait(now, side, "WAIT_IGNITION_PROOF", "PROBE", payload, freshness)
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
        if str(row.get("side")) == str(episode.get("side")):
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
