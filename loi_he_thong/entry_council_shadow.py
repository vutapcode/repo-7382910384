"""Causal Testnet entry council: intent -> acceptance -> release -> GO.

Only expensive-to-fake evidence can authorize an entry:
S1 cross-venue price acceptance, S2 multi-venue executed flow.
S3 is a causal response validator, never an independent quorum seat.
The fast lane is intentionally rare: 3/3 strong price acceptance + one strong
executed-flow venue + no strong opposing flow + high bias confidence.
"""
from collections import deque
import time

VERSION = "ENTRY_COUNCIL_CAUSAL_V2"
LOOKBACK = 1.50
FAST_LOOKBACK = 0.35
INTENT_LOOKBACK = 0.80
MIN_PRICE_BPS = 0.50
MAX_PRICE_BPS = 2.50
MIN_FLOW_IMB = 0.08
STRONG_FLOW_IMB = 0.25
MIN_VOL_BTC = 0.02
BIAS_MIN_CONF = 0.55
FAST_BIAS_MIN_CONF = 0.72
BIAS_MAX_AGE = 3.0
SPOT_MAX_AGE = 3.0
CB_MAX_AGE = 5.0
FUT_MAX_AGE = 5.0
EVAL_THROTTLE = 0.10


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def _mid(bid, ask):
    bid, ask = float(bid or 0.0), float(ask or 0.0)
    return (bid + ask) / 2.0 if bid > 0 and ask > bid else max(bid, ask)


def _bps(cur, ref):
    cur, ref = float(cur or 0.0), float(ref or 0.0)
    return None if cur <= 0 or ref <= 0 else (cur - ref) / ref * 10000.0


def _sign(side):
    return 1.0 if str(side).upper() == "LONG" else -1.0


def _flow_imb(buy, sell):
    buy, sell = float(buy or 0.0), float(sell or 0.0)
    total = buy + sell
    return ((buy - sell) / total if total > 0 else 0.0), total


def _latest_fut(state, now):
    rows = getattr(state, "danh_sach_khop_lenh_futures", None) or ()
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        px = float(row.get("gia", 0.0) or 0.0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0, 0.0
    return (px, ts) if px > 0 and ts > 0 and now - ts <= FUT_MAX_AGE else (0.0, ts)


def _prices(state, now):
    spot_ts = float(getattr(state, "thoi_gian_tick_cuoi", 0.0) or 0.0)
    spot = _mid(getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0))
    if spot_ts <= 0 or now - spot_ts > SPOT_MAX_AGE:
        spot = 0.0

    cb_ts = float(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0) or 0.0)
    cb = float(getattr(state, "coinbase_price", 0.0) or 0.0)
    if cb_ts <= 0 or now - cb_ts > CB_MAX_AGE:
        cb = 0.0

    fut, fut_ts = _latest_fut(state, now)
    return {
        "ts": now, "spot": spot, "coinbase": cb, "futures": fut
    }, {
        "spot": spot_ts, "coinbase": cb_ts, "futures": fut_ts
    }


def _history(state, name, maxlen=256):
    hist = getattr(state, name, None)
    if hist is None:
        hist = deque(maxlen=maxlen)
        setattr(state, name, hist)
    return hist


def _append_sample(hist, row, min_gap=0.08):
    last = float(hist[-1].get("ts", 0.0) or 0.0) if hist else 0.0
    if row["ts"] - last >= min_gap:
        hist.append(row)


def _ref(hist, now, age):
    target = now - age
    out = None
    for row in hist:
        if float(row.get("ts", 0.0) or 0.0) <= target:
            out = row
        else:
            break
    return out


def _threshold_bps(state, spot):
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    atr_bps = atr / spot * 10000.0 if atr > 0 and spot > 0 else 0.0
    return max(MIN_PRICE_BPS, min(MAX_PRICE_BPS, atr_bps * 0.05 or MIN_PRICE_BPS))


def _spot_flow(state, now):
    ts = float(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0) or 0.0)
    if ts <= 0 or now - ts > FUT_MAX_AGE:
        return 0.0, 0.0
    return _flow_imb(
        getattr(state, "current_cvd_buy_3s", 0.0),
        getattr(state, "current_cvd_sell_3s", 0.0),
    )


def _futures_flow(state, now):
    cutoff = (now - 3.0) * 1000.0
    buy = sell = newest = 0.0
    for row in list(getattr(state, "danh_sach_khop_lenh_futures", ()) or ()):
        try:
            ts = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts < cutoff:
                continue
            qty = float(row.get("khoi_luong", 0.0) or 0.0)
            newest = max(newest, ts / 1000.0)
            if bool(row.get("ban_chu_dong", False)):
                sell += qty
            else:
                buy += qty
        except (AttributeError, TypeError, ValueError):
            continue
    if newest <= 0 or now - newest > FUT_MAX_AGE:
        return 0.0, 0.0
    return _flow_imb(buy, sell)


def _coinbase_flow(state, now):
    ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    total = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    cvd = float(getattr(state, "coinbase_cvd_3s", 0.0) or 0.0)
    if ts <= 0 or now - ts > CB_MAX_AGE or total <= 0:
        return 0.0, 0.0
    return cvd / total, total


def _flow_snapshot(state, now, side):
    d = _sign(side)
    p90 = float(getattr(state, "vol_pct90", 0.0) or 0.0)
    floor = max(MIN_VOL_BTC, min(0.10, 0.02 * p90))
    raw = {
        "spot": _spot_flow(state, now),
        "futures": _futures_flow(state, now),
        "coinbase": _coinbase_flow(state, now),
    }
    venues = {}
    support = []
    oppose = []
    strong_support = []
    strong_oppose = []
    for venue, (imb, vol) in raw.items():
        if vol < floor:
            continue
        signed = imb * d
        venues[venue] = {"signed_imbalance": round(signed, 4), "volume_btc": round(vol, 8)}
        if signed >= MIN_FLOW_IMB:
            support.append(venue)
        if signed <= -MIN_FLOW_IMB:
            oppose.append(venue)
        if signed >= STRONG_FLOW_IMB:
            strong_support.append(venue)
        if signed <= -STRONG_FLOW_IMB:
            strong_oppose.append(venue)
    return {
        "ts": now, "venues": venues, "supporters": support, "opponents": oppose,
        "strong_supporters": strong_support, "strong_opponents": strong_oppose,
        "volume_floor_btc": round(floor, 8)
    }


def _price_snapshot(current, ref, side, threshold):
    d = _sign(side)
    moves = {}
    supporters, opponents, strong = [], [], []
    if ref is None:
        return {"moves": {}, "supporters": [], "opponents": [], "strong_supporters": []}
    for venue in ("spot", "coinbase", "futures"):
        move = _bps(current.get(venue), ref.get(venue))
        if move is None:
            continue
        signed = move * d
        moves[venue] = round(signed, 4)
        if signed >= threshold:
            supporters.append(venue)
        if signed <= -threshold:
            opponents.append(venue)
        if signed >= threshold * 1.50:
            strong.append(venue)
    return {"moves": moves, "supporters": supporters, "opponents": opponents, "strong_supporters": strong}


def _flow_mean(row):
    vals = [float(v.get("signed_imbalance", 0.0)) for v in (row.get("venues") or {}).values()]
    return sum(vals) / len(vals) if vals else 0.0


def _intent_and_release(flow_hist, price_hist, now, side, threshold):
    """Read the causal sequence, not another independent vote."""
    recent = [r for r in flow_hist if now - float(r.get("ts", 0.0)) <= INTENT_LOOKBACK]
    if not recent:
        return {"intent": False, "failed_opposition": False, "release": False}

    cur_mean = _flow_mean(recent[-1])
    old_means = [_flow_mean(r) for r in recent[:-1]]
    prior = sum(old_means) / len(old_means) if old_means else 0.0
    intent = cur_mean >= MIN_FLOW_IMB and cur_mean >= prior + 0.04

    failed_opposition = any(m <= -0.12 for m in old_means)
    release = False
    if failed_opposition and cur_mean >= 0.12:
        ref = _ref(price_hist, now, FAST_LOOKBACK)
        if ref:
            d = _sign(side)
            spot = _bps(price_hist[-1].get("spot"), ref.get("spot"))
            cb = _bps(price_hist[-1].get("coinbase"), ref.get("coinbase"))
            signed = [x * d for x in (spot, cb) if x is not None]
            release = len(signed) >= 1 and max(signed) >= threshold * 0.60

    return {
        "intent": intent,
        "failed_opposition": failed_opposition,
        "release": release,
        "current_flow_mean": round(cur_mean, 4),
        "prior_flow_mean": round(prior, 4),
    }


def _handoff(current, ref, side, threshold):
    if ref is None:
        return {"status": "WARMUP"}
    d = _sign(side)
    moves = {}
    for venue in ("spot", "coinbase", "futures"):
        x = _bps(current.get(venue), ref.get(venue))
        if x is not None:
            moves[venue] = x * d
    spot = moves.get("spot", 0.0)
    cb = moves.get("coinbase", 0.0)
    fut = moves.get("futures", 0.0)
    if fut > max(3.0, spot + 2.0) and spot < threshold and cb < threshold:
        return {"status": "WAIT_CHASE", "moves": {k: round(v, 4) for k, v in moves.items()}}
    if spot >= threshold * 0.70 and (cb >= threshold * 0.50 or fut >= threshold * 0.50):
        return {"status": "SPOT_HANDOFF", "moves": {k: round(v, 4) for k, v in moves.items()}}
    return {"status": "NEUTRAL", "moves": {k: round(v, 4) for k, v in moves.items()}}


def evaluate(state, now=None, side=None):
    now = time.time() if now is None else float(now)
    side = str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    bias_conf = float(getattr(state, "bias_confidence", 0.0) or 0.0)
    bias_ts = float(getattr(state, "bias_updated_at", 0.0) or 0.0)

    if side not in ("LONG", "SHORT"):
        return {"version": VERSION, "decision": "WAIT", "entry_mode": "NONE", "phase": "ARMED",
                "confidence": 0.0, "reason": "BIAS_ABSTAIN", "side": side, "s_votes": {}, "ts": now}
    if bias_conf < BIAS_MIN_CONF or bias_ts <= 0 or now - bias_ts > BIAS_MAX_AGE:
        return {"version": VERSION, "decision": "WAIT", "entry_mode": "NONE", "phase": "ARMED",
                "confidence": 0.0, "reason": "BIAS_NOT_FRESH_OR_CONFIDENT", "side": side, "s_votes": {}, "ts": now}

    current, freshness = _prices(state, now)
    threshold = _threshold_bps(state, current["spot"])
    price_hist = _history(state, "entry_shadow_price_history", 256)
    _append_sample(price_hist, current)
    ref = _ref(price_hist, now, LOOKBACK)
    fast_ref = _ref(price_hist, now, FAST_LOOKBACK)

    flow = _flow_snapshot(state, now, side)
    flow_hist = _history(state, "entry_causal_flow_history", 256)
    _append_sample(flow_hist, flow)

    p = _price_snapshot(current, ref, side, threshold)
    pf = _price_snapshot(current, fast_ref, side, threshold)
    seq = _intent_and_release(flow_hist, price_hist, now, side, threshold)
    handoff = _handoff(current, ref, side, threshold)

    s1_status = "PASS" if len(p["supporters"]) >= 2 and not p["opponents"] else (
        "REJECT" if len(p["opponents"]) >= 2 else "WAIT"
    )
    s2_status = "PASS" if len(flow["supporters"]) >= 2 and not flow["opponents"] else (
        "REJECT" if len(flow["opponents"]) >= 2 else "WAIT"
    )
    s1 = {"status": s1_status, "confidence": round(_clamp(0.56 + .08 * max(0, len(p["supporters"])-2)), 6),
          "reason": "CROSS_VENUE_PRICE", "metrics": p}
    s2 = {"status": s2_status, "confidence": round(_clamp(0.56 + .08 * max(0, len(flow["supporters"])-2)), 6),
          "reason": "MULTI_VENUE_EXECUTED_FLOW", "metrics": flow}
    s3_status = "PASS" if seq["release"] or (s1_status == "PASS" and s2_status == "PASS") else "WAIT"
    s3 = {"status": s3_status, "confidence": 0.62 if s3_status == "PASS" else 0.20,
          "reason": "FAILED_OPPOSITION_RELEASE" if seq["release"] else "FLOW_TO_PRICE_CAUSAL",
          "metrics": seq}

    phase = "ARMED"
    if handoff["status"] == "WAIT_CHASE":
        phase = "WAIT_CHASE"
    elif seq["release"]:
        phase = "RELEASE"
    elif s1_status == "PASS":
        phase = "ACCEPTANCE"
    elif seq["intent"] or len(flow["supporters"]) >= 1:
        phase = "PRESSURE_BUILDING"

    decision, mode, reason = "WAIT", "NONE", "CAUSAL_SEQUENCE_NOT_READY"
    confidence = max(s1["confidence"] if s1_status == "PASS" else 0.0,
                     s2["confidence"] if s2_status == "PASS" else 0.0)

    if s1_status == "REJECT" and s2_status == "REJECT":
        decision, reason, phase = "REJECT", "PRICE_AND_FLOW_OPPOSE", "FAILED"
        confidence = (s1["confidence"] + s2["confidence"]) / 2.0
    elif handoff["status"] == "WAIT_CHASE":
        reason = "WAIT_CHASE_PERP_AHEAD_OF_SPOT"
    elif s1_status == "PASS" and s2_status == "PASS":
        decision, mode, reason = "GO", "NORMAL", "CAUSAL_PRICE_FLOW_QUORUM"
        phase = "RELEASE" if seq["release"] else "ACCEPTANCE"
        confidence = _clamp(0.82 * ((s1["confidence"] + s2["confidence"]) / 2.0) + 0.18 * bias_conf)
        if seq["release"]:
            confidence = _clamp(confidence + 0.05)
    else:
        fast_ok = (
            len(pf["strong_supporters"]) == 3
            and len(flow["strong_supporters"]) >= 1
            and not flow["strong_opponents"]
            and bias_conf >= FAST_BIAS_MIN_CONF
        )
        if fast_ok:
            decision, mode, reason, phase = "GO", "FAST", "GO_FAST_3VENUE_ACCEPTANCE", "RELEASE"
            confidence = _clamp(0.70 + 0.15 * bias_conf + 0.05 * min(len(flow["strong_supporters"]), 2))
        elif s1_status == "REJECT" or s2_status == "REJECT":
            reason = "ONE_INDEPENDENT_S_REJECT"

    return {
        "version": VERSION, "decision": decision, "entry_mode": mode, "phase": phase,
        "confidence": round(_clamp(confidence), 6), "reason": reason, "side": side,
        "bias_confidence": round(bias_conf, 6),
        "s_quorum": int(s1_status == "PASS") + int(s2_status == "PASS"),
        "s_votes": {
            "S1_cross_venue_price_acceptance": s1,
            "S2_multi_venue_executed_flow": s2,
            "S3_causal_response_validator": s3,
        },
        "causal": {"sequence": seq, "handoff": handoff},
        "freshness": freshness, "price_threshold_bps": round(threshold, 4), "ts": now,
    }


def update_state(state, now=None):
    """Testnet telemetry/decision only. Never submits an order."""
    if not bool(getattr(state, "_api_is_testnet", False)):
        return None
    now = time.time() if now is None else float(now)
    last = float(getattr(state, "entry_shadow_updated_at", 0.0) or 0.0)
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
