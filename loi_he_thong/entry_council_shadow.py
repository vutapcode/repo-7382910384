"""Testnet/shadow entry council: GO / WAIT / REJECT only.

This module never enqueues or executes an order.  It is intentionally independent
from structure/zone/orderbook heuristics and reads only expensive-to-fake
cross-venue price/flow evidence plus the already-produced bias council output.
"""

from collections import deque
import time

VERSION = "ENTRY_COUNCIL_SHADOW_V1"
LOOKBACK_SECONDS = 1.5
MIN_PRICE_MOVE_BPS = 0.50
MAX_PRICE_MOVE_BPS = 2.50
MIN_FLOW_IMBALANCE = 0.08
STRONG_FLOW_IMBALANCE = 0.25
BASIS_EXPANSION_CAUTION_BPS = 4.0
BIAS_MIN_CONFIDENCE = 0.55
BIAS_MAX_AGE_SECONDS = 12.0
FUTURES_MAX_AGE_SECONDS = 5.0
COINBASE_MAX_AGE_SECONDS = 5.0
EVAL_THROTTLE_SECONDS = 0.10


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def _mid(bid, ask):
    bid = float(bid or 0.0)
    ask = float(ask or 0.0)
    if bid > 0.0 and ask > bid:
        return (bid + ask) / 2.0
    return max(bid, ask)


def _bps_change(current, previous):
    current = float(current or 0.0)
    previous = float(previous or 0.0)
    if current <= 0.0 or previous <= 0.0:
        return None
    return (current - previous) / previous * 10000.0


def _direction(side):
    return 1.0 if side == "LONG" else -1.0


def _vote(status="WAIT", confidence=0.0, reason="", **metrics):
    return {
        "status": status,
        "confidence": round(_clamp(confidence), 6),
        "reason": reason,
        "metrics": metrics,
    }


def _latest_futures_mainnet(state, now):
    rows = getattr(state, "danh_sach_khop_lenh_futures", None) or ()
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        price = float(row.get("gia", 0.0) or 0.0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0, 0.0
    if price <= 0.0 or ts <= 0.0 or now - ts > FUTURES_MAX_AGE_SECONDS:
        return 0.0, ts
    return price, ts


def _history(state):
    history = getattr(state, "entry_shadow_price_history", None)
    if history is None:
        history = deque(maxlen=128)
        state.entry_shadow_price_history = history
    return history


def _reference(history, now):
    target = now - LOOKBACK_SECONDS
    ref = None
    for row in history:
        if float(row.get("ts", 0.0) or 0.0) <= target:
            ref = row
        else:
            break
    return ref


def _append_history(state, row):
    history = _history(state)
    last_ts = float(history[-1].get("ts", 0.0) or 0.0) if history else 0.0
    if row["ts"] - last_ts >= 0.08:
        history.append(row)
    return history


def _price_threshold_bps(state, spot):
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    atr_bps = atr / spot * 10000.0 if spot > 0.0 and atr > 0.0 else 0.0
    dynamic = atr_bps * 0.05
    return max(MIN_PRICE_MOVE_BPS, min(MAX_PRICE_MOVE_BPS, dynamic or MIN_PRICE_MOVE_BPS))


def _flow_imbalance(buy, sell):
    buy = float(buy or 0.0)
    sell = float(sell or 0.0)
    total = buy + sell
    if total <= 0.0:
        return 0.0, 0.0
    return (buy - sell) / total, total


def _spot_flow(state):
    return _flow_imbalance(
        getattr(state, "current_cvd_buy_3s", 0.0),
        getattr(state, "current_cvd_sell_3s", 0.0),
    )


def _futures_flow(state, now):
    cutoff_ms = (now - 3.0) * 1000.0
    buy = sell = 0.0
    newest = 0.0
    for row in list(getattr(state, "danh_sach_khop_lenh_futures", ()) or ()):
        try:
            ts_ms = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts_ms < cutoff_ms:
                continue
            qty = float(row.get("khoi_luong", 0.0) or 0.0)
            newest = max(newest, ts_ms / 1000.0)
            if bool(row.get("ban_chu_dong", False)):
                sell += qty
            else:
                buy += qty
        except (AttributeError, TypeError, ValueError):
            continue
    imb, total = _flow_imbalance(buy, sell)
    if newest <= 0.0 or now - newest > FUTURES_MAX_AGE_SECONDS:
        return 0.0, 0.0
    return imb, total


def _coinbase_flow(state, now):
    ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    total = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    cvd = float(getattr(state, "coinbase_cvd_3s", 0.0) or 0.0)
    if ts <= 0.0 or now - ts > COINBASE_MAX_AGE_SECONDS or total <= 0.0:
        return 0.0, 0.0
    return cvd / total, total


def _s1_price_acceptance(cur, ref, side, threshold_bps):
    if ref is None:
        return _vote(reason="PRICE_WARMUP")
    d = _direction(side)
    rows = []
    for venue in ("spot", "coinbase", "futures"):
        move = _bps_change(cur.get(venue), ref.get(venue))
        if move is None:
            continue
        rows.append((venue, move * d))
    supporters = [row for row in rows if row[1] >= threshold_bps]
    opponents = [row for row in rows if row[1] <= -threshold_bps]
    metrics = {
        "signed_moves_bps": {name: round(value, 4) for name, value in rows},
        "threshold_bps": round(threshold_bps, 4),
        "supporters": [name for name, _ in supporters],
        "opponents": [name for name, _ in opponents],
    }
    if len(opponents) >= 2:
        strength = sum(min(abs(v) / threshold_bps, 3.0) for _, v in opponents) / len(opponents)
        return _vote("REJECT", 0.62 + 0.12 * min(strength, 2.0), "PRICE_MULTI_VENUE_OPPOSES", **metrics)
    if len(supporters) >= 2:
        strength = sum(min(v / threshold_bps, 3.0) for _, v in supporters) / len(supporters)
        confidence = 0.56 + 0.10 * (len(supporters) - 2) + 0.14 * min(strength - 1.0, 2.0)
        return _vote("PASS", confidence, "PRICE_ACCEPTED_MULTI_VENUE", **metrics)
    return _vote("WAIT", 0.25, "PRICE_NOT_YET_ACCEPTED", **metrics)


def _s2_multi_flow(state, now, side):
    d = _direction(side)
    spot_imb, spot_total = _spot_flow(state)
    fut_imb, fut_total = _futures_flow(state, now)
    cb_imb, cb_total = _coinbase_flow(state, now)
    rows = []
    for venue, imb, total in (
        ("spot", spot_imb, spot_total),
        ("futures", fut_imb, fut_total),
        ("coinbase", cb_imb, cb_total),
    ):
        if total > 0.0:
            rows.append((venue, imb * d))
    supporters = [row for row in rows if row[1] >= MIN_FLOW_IMBALANCE]
    opponents = [row for row in rows if row[1] <= -MIN_FLOW_IMBALANCE]
    metrics = {
        "signed_imbalances": {name: round(value, 4) for name, value in rows},
        "supporters": [name for name, _ in supporters],
        "opponents": [name for name, _ in opponents],
    }
    if len(opponents) >= 2:
        strength = sum(abs(v) for _, v in opponents) / len(opponents)
        return _vote("REJECT", 0.64 + 0.25 * min(strength, 1.0), "FLOW_MULTI_VENUE_OPPOSES", **metrics)
    if len(supporters) >= 2:
        strength = sum(v for _, v in supporters) / len(supporters)
        confidence = 0.56 + 0.10 * (len(supporters) - 2) + 0.28 * min(strength, 1.0)
        return _vote("PASS", confidence, "FLOW_MULTI_VENUE_SUPPORTS", **metrics)
    return _vote("WAIT", 0.22, "FLOW_INSUFFICIENT_CONSENSUS", **metrics)


def _s3_flow_to_price(s1, s2):
    price_moves = list((s1.get("metrics") or {}).get("signed_moves_bps", {}).values())
    flow_values = list((s2.get("metrics") or {}).get("signed_imbalances", {}).values())
    if not price_moves or not flow_values:
        return _vote(reason="RESPONSE_INSUFFICIENT_DATA")
    positive_flow = [value for value in flow_values if value >= MIN_FLOW_IMBALANCE]
    negative_flow = [value for value in flow_values if value <= -MIN_FLOW_IMBALANCE]
    price_positive = [value for value in price_moves if value > 0.0]
    price_negative = [value for value in price_moves if value < 0.0]
    mean_flow = sum(positive_flow) / len(positive_flow) if positive_flow else 0.0
    mean_price = sum(price_positive) / len(price_positive) if price_positive else 0.0
    metrics = {
        "supporting_flow_mean": round(mean_flow, 4),
        "positive_price_venues": len(price_positive),
        "negative_price_venues": len(price_negative),
    }
    if len(negative_flow) >= 2 and len(price_negative) >= 2:
        return _vote("REJECT", 0.74, "FLOW_AND_PRICE_OPPOSE", **metrics)
    if len(positive_flow) >= 1 and len(price_positive) >= 2:
        strength = _clamp(mean_flow / STRONG_FLOW_IMBALANCE)
        return _vote("PASS", 0.54 + 0.28 * strength, "FLOW_CONVERTS_TO_PRICE", **metrics)
    if len(positive_flow) >= 2 and not price_positive:
        return _vote("REJECT", 0.68, "BUYSELL_PRESSURE_ABSORBED_AGAINST_BIAS", **metrics)
    return _vote("WAIT", 0.22, "RESPONSE_NOT_CLEAR", **metrics)


def _a1_spot_lead(cur, ref, side):
    if ref is None:
        return _vote(reason="LEAD_WARMUP")
    d = _direction(side)
    spot = _bps_change(cur.get("spot"), ref.get("spot"))
    fut = _bps_change(cur.get("futures"), ref.get("futures"))
    if spot is None or fut is None:
        return _vote(reason="LEAD_MISSING_PRICE")
    spot *= d
    fut *= d
    if spot > 0.0 and spot >= max(0.35, fut * 0.85):
        ratio = spot / max(abs(fut), 0.35)
        return _vote("PASS", 0.52 + 0.10 * min(max(ratio - 1.0, 0.0), 2.0),
                    "SPOT_LEADS_OR_MATCHES_PERP", spot_bps=spot, futures_bps=fut)
    if fut > max(spot + 2.0, 3.0):
        return _vote("REJECT", 0.58, "PERP_RUNS_AHEAD_OF_SPOT", spot_bps=spot, futures_bps=fut)
    return _vote("WAIT", 0.20, "NO_CLEAR_LEAD", spot_bps=spot, futures_bps=fut)


def _a2_basis(cur, ref, side):
    spot = float(cur.get("spot", 0.0) or 0.0)
    fut = float(cur.get("futures", 0.0) or 0.0)
    if ref is None or spot <= 0.0 or fut <= 0.0:
        return _vote(reason="BASIS_WARMUP")
    ref_spot = float(ref.get("spot", 0.0) or 0.0)
    ref_fut = float(ref.get("futures", 0.0) or 0.0)
    if ref_spot <= 0.0 or ref_fut <= 0.0:
        return _vote(reason="BASIS_MISSING_REFERENCE")
    current_basis = (fut - spot) / spot * 10000.0
    reference_basis = (ref_fut - ref_spot) / ref_spot * 10000.0
    expansion = (current_basis - reference_basis) * _direction(side)
    metrics = {
        "basis_bps": round(current_basis, 4),
        "basis_change_signed_bps": round(expansion, 4),
    }
    if expansion > BASIS_EXPANSION_CAUTION_BPS:
        return _vote("REJECT", _clamp(0.50 + expansion / 40.0), "BASIS_CHASING_CAUTION", **metrics)
    return _vote("PASS", 0.54, "BASIS_SANE", **metrics)


def evaluate(state, now=None, side=None):
    now = time.time() if now is None else float(now)
    side = str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    bias_conf = float(getattr(state, "bias_confidence", 0.0) or 0.0)
    bias_ts = float(getattr(state, "bias_updated_at", 0.0) or 0.0)

    if side not in ("LONG", "SHORT"):
        return {
            "version": VERSION, "decision": "WAIT", "confidence": 0.0,
            "reason": "BIAS_ABSTAIN", "side": side, "s_votes": {}, "a_votes": {}, "ts": now,
        }
    if bias_conf < BIAS_MIN_CONFIDENCE or bias_ts <= 0.0 or now - bias_ts > BIAS_MAX_AGE_SECONDS:
        return {
            "version": VERSION, "decision": "WAIT", "confidence": 0.0,
            "reason": "BIAS_NOT_FRESH_OR_CONFIDENT", "side": side,
            "bias_confidence": bias_conf, "s_votes": {}, "a_votes": {}, "ts": now,
        }

    spot = _mid(getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0))
    cb = float(getattr(state, "coinbase_price", 0.0) or 0.0)
    cb_ts = float(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0) or 0.0)
    if cb_ts <= 0.0 or now - cb_ts > COINBASE_MAX_AGE_SECONDS:
        cb = 0.0
    fut, fut_ts = _latest_futures_mainnet(state, now)

    current = {"ts": now, "spot": spot, "coinbase": cb, "futures": fut}
    history = _append_history(state, current)
    ref = _reference(history, now)
    threshold = _price_threshold_bps(state, spot)

    s1 = _s1_price_acceptance(current, ref, side, threshold)
    s2 = _s2_multi_flow(state, now, side)
    s3 = _s3_flow_to_price(s1, s2)
    a1 = _a1_spot_lead(current, ref, side)
    a2 = _a2_basis(current, ref, side)

    s_votes = {
        "S1_cross_venue_price_acceptance": s1,
        "S2_multi_venue_executed_flow": s2,
        "S3_flow_to_price_response": s3,
    }
    a_votes = {
        "A1_spot_leads_perp": a1,
        "A2_basis_sanity": a2,
    }

    passes = [vote for vote in s_votes.values() if vote["status"] == "PASS"]
    rejects = [vote for vote in s_votes.values() if vote["status"] == "REJECT"]

    if len(rejects) >= 2:
        decision, reason = "REJECT", "MULTIPLE_S_TIER_REJECT"
        confidence = sum(v["confidence"] for v in rejects) / len(rejects)
    elif len(passes) >= 2 and not rejects:
        decision, reason = "GO", "S_TIER_QUORUM"
        confidence = sum(v["confidence"] for v in passes) / len(passes)
        confidence = 0.82 * confidence + 0.18 * bias_conf
        for vote in a_votes.values():
            if vote["status"] == "PASS":
                confidence += 0.025 * vote["confidence"]
            elif vote["status"] == "REJECT":
                confidence -= 0.04 * vote["confidence"]
        confidence = _clamp(confidence)
    elif rejects:
        decision, reason = "WAIT", "ONE_S_TIER_REJECT"
        confidence = max(v["confidence"] for v in rejects)
    else:
        decision, reason = "WAIT", "S_TIER_QUORUM_NOT_READY"
        confidence = sum(v["confidence"] for v in passes) / len(passes) if passes else 0.0

    return {
        "version": VERSION,
        "decision": decision,
        "confidence": round(_clamp(confidence), 6),
        "reason": reason,
        "side": side,
        "bias_confidence": round(bias_conf, 6),
        "s_quorum": len(passes),
        "s_votes": s_votes,
        "a_votes": a_votes,
        "price_threshold_bps": round(threshold, 4),
        "futures_price_ts": fut_ts,
        "ts": now,
    }


def update_state(state, now=None):
    """Update shadow telemetry only on Testnet.  Never sends an order."""
    if not bool(getattr(state, "_api_is_testnet", False)):
        return None
    now = time.time() if now is None else float(now)
    last = float(getattr(state, "entry_shadow_updated_at", 0.0) or 0.0)
    if now - last < EVAL_THROTTLE_SECONDS:
        return getattr(state, "entry_shadow_council", None)
    result = evaluate(state, now=now)
    state.entry_shadow_council = result
    state.entry_shadow_decision = result["decision"]
    state.entry_shadow_confidence = result["confidence"]
    state.entry_shadow_updated_at = now
    state.entry_shadow_version = VERSION
    return result
