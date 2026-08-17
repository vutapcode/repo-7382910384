"""Direction-only S/A bias council. No entry, setup, zone or timing logic."""
from collections import deque
import time

VERSION = "BIAS_COUNCIL_V2"
LOOKBACK = 15.0
CB_MAX_AGE = 30.0
FUT_MAX_AGE = 10.0
MIN_MOVE_PCT = 0.015
MIN_OI_RISE_PCT = 0.015
MIN_FLOW_IMB = 0.05
LIGHT_FLOW_TRIGGER = 0.20
STRONG_OPPOSITION = 0.75


def _clamp(x):
    return max(0.0, min(1.0, float(x)))


def _mid(bid, ask):
    bid, ask = float(bid or 0.0), float(ask or 0.0)
    return (bid + ask) / 2.0 if bid > 0.0 and ask > bid else max(bid, ask)


def _chg(cur, old):
    cur, old = float(cur or 0.0), float(old or 0.0)
    return None if cur <= 0.0 or old <= 0.0 else (cur - old) / old * 100.0


def _vote(side="ABSTAIN", confidence=0.0, reason="", **metrics):
    return {"vote": side, "confidence": round(_clamp(confidence), 6),
            "reason": reason, "metrics": metrics}


def _last_fut_trade(state, now):
    rows = getattr(state, "danh_sach_khop_lenh_futures", None) or ()
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        if ts <= 0.0 or now - ts > FUT_MAX_AGE:
            return 0.0
        return float(row.get("gia", 0.0) or 0.0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0


def _futures_price(state, now):
    # Never use execution BBO on Testnet as a market oracle.
    if not bool(getattr(state, "_api_is_testnet", False)):
        ts = float(getattr(state, "execution_price_time", 0.0) or 0.0)
        if ts > 0.0 and now - ts <= FUT_MAX_AGE:
            px = _mid(getattr(state, "execution_best_bid", 0.0),
                      getattr(state, "execution_best_ask", 0.0))
            if px > 0.0:
                return px, "FUTURES_MAINNET_BBO"
    px = _last_fut_trade(state, now)
    return px, ("FUTURES_MAINNET_TRADE" if px > 0.0 else "MISSING")


def _threshold(state, spot):
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    dyn = atr / spot * 100.0 * 0.15 if spot > 0.0 and atr > 0.0 else 0.0
    return max(MIN_MOVE_PCT, dyn)


def _side(change, threshold):
    if change is None or abs(change) < threshold:
        return "ABSTAIN"
    return "LONG" if change > 0.0 else "SHORT"


def _excess(value, threshold, span=2.0):
    if value is None or threshold <= 0.0:
        return 0.0
    return _clamp((abs(value) / threshold - 1.0) / span)


def _s1(cur, ref, threshold):
    if ref is None:
        return _vote(reason="WARMUP_PRICE_HISTORY")
    rows, changes = [], {}
    for venue in ("spot", "coinbase", "futures"):
        c = _chg(cur.get(venue), ref.get(venue))
        changes[venue] = c
        d = _side(c, threshold)
        if d != "ABSTAIN":
            rows.append((venue, d, c))
    longs = [r for r in rows if r[1] == "LONG"]
    shorts = [r for r in rows if r[1] == "SHORT"]
    if len(longs) >= 2 and not shorts:
        agreed, side, reason = longs, "LONG", "MULTI_VENUE_PRICE_UP"
    elif len(shorts) >= 2 and not longs:
        agreed, side, reason = shorts, "SHORT", "MULTI_VENUE_PRICE_DOWN"
    elif longs and shorts:
        return _vote(reason="CROSS_VENUE_PRICE_CONFLICT", changes=changes)
    else:
        return _vote(reason="PRICE_MOVE_SMALL_OR_INCOMPLETE", changes=changes)
    strength = sum(_excess(r[2], threshold) for r in agreed) / len(agreed)
    conf = 0.55 + 0.12 * max(0, len(agreed) - 2) + 0.25 * strength
    return _vote(side, conf, reason, changes=changes, agreeing=len(agreed),
                 strength=strength, threshold_pct=threshold)


def _s2(cur, ref, threshold):
    if ref is None:
        return _vote(reason="WARMUP_OI_HISTORY")
    pc, oc = _chg(cur.get("spot"), ref.get("spot")), _chg(cur.get("oi"), ref.get("oi"))
    if pc is None or oc is None:
        return _vote(reason="MISSING_PRICE_OR_OI", price_pct=pc, oi_pct=oc)
    if oc < MIN_OI_RISE_PCT:
        return _vote(reason="OI_NOT_BUILDING", price_pct=pc, oi_pct=oc)
    side = _side(pc, threshold)
    if side == "ABSTAIN":
        return _vote(reason="PRICE_MOVE_TOO_SMALL", price_pct=pc, oi_pct=oc)
    ps = _excess(pc, threshold)
    os = _clamp((oc / MIN_OI_RISE_PCT - 1.0) / 3.0)
    return _vote(side, 0.55 + 0.20 * ps + 0.20 * os, "NEW_POSITION_BUILD",
                 price_pct=pc, oi_pct=oc, price_strength=ps, oi_strength=os)


def _spot_flow(state, now):
    buy = sell = 0.0
    cutoff = now - 60.0
    for r in list(getattr(state, "flow_1s_buffer", ())) or ():
        try:
            if float(r.get("ts", 0.0) or 0.0) >= cutoff:
                buy += float(r.get("buy", 0.0) or 0.0)
                sell += float(r.get("sell", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            pass
    total = buy + sell
    return ((buy - sell) / total if total > 0.0 else 0.0), total


def _fut_flow(state, now):
    buy = sell = newest = 0.0
    cutoff = (now - 60.0) * 1000.0
    for r in list(getattr(state, "danh_sach_khop_lenh_futures", ())) or ():
        try:
            ts = float(r.get("thoi_gian_ms", 0.0) or 0.0)
            if ts < cutoff:
                continue
            qty = float(r.get("khoi_luong", 0.0) or 0.0)
            newest = max(newest, ts / 1000.0)
            if bool(r.get("ban_chu_dong", False)):
                sell += qty
            else:
                buy += qty
        except (AttributeError, TypeError, ValueError):
            pass
    total = buy + sell
    return ((buy - sell) / total if total > 0.0 else 0.0), total, newest


def _flow_side(x):
    return "ABSTAIN" if abs(x) < MIN_FLOW_IMB else ("LONG" if x > 0.0 else "SHORT")


def _light_flow(state):
    buy = float(getattr(state, "current_cvd_buy_3s", 0.0) or 0.0)
    sell = float(getattr(state, "current_cvd_sell_3s", 0.0) or 0.0)
    total = buy + sell
    if total <= 0.0:
        return False, 0.0, total
    imb = (buy - sell) / total
    p90 = float(getattr(state, "vol_pct90", 0.0) or 0.0)
    floor = max(0.05, 0.35 * p90) if p90 > 0.0 else 0.05
    return abs(imb) >= LIGHT_FLOW_TRIGGER and total >= floor, imb, total


def _s3(state, now):
    si, st = _spot_flow(state, now)
    fi, ft, fts = _fut_flow(state, now)
    cb = float(getattr(state, "coinbase_cvd_1m", 0.0) or 0.0)
    cbts = float(getattr(state, "thoi_gian_coinbase_cuoi", 0.0) or 0.0)
    rows = []
    if st > 0.0 and _flow_side(si) != "ABSTAIN":
        rows.append(("spot", _flow_side(si), _clamp(abs(si) / 0.35)))
    if ft > 0.0 and fts > 0.0 and now - fts <= FUT_MAX_AGE and _flow_side(fi) != "ABSTAIN":
        rows.append(("futures", _flow_side(fi), _clamp(abs(fi) / 0.35)))
    if cbts > 0.0 and now - cbts <= CB_MAX_AGE and abs(cb) >= 0.5:
        rows.append(("coinbase", "LONG" if cb > 0.0 else "SHORT", _clamp(abs(cb) / 3.0)))
    longs, shorts = [r for r in rows if r[1] == "LONG"], [r for r in rows if r[1] == "SHORT"]
    if len(longs) >= 2 and not shorts:
        agreed, side, reason = longs, "LONG", "MULTI_VENUE_BUY_FLOW"
    elif len(shorts) >= 2 and not longs:
        agreed, side, reason = shorts, "SHORT", "MULTI_VENUE_SELL_FLOW"
    elif longs and shorts:
        return _vote(reason="MULTI_VENUE_FLOW_CONFLICT", venues=rows)
    else:
        return _vote(reason="INSUFFICIENT_FLOW_CONSENSUS", venues=rows)
    strength = sum(r[2] for r in agreed) / len(agreed)
    conf = 0.52 + 0.12 * max(0, len(agreed) - 2) + 0.30 * strength
    return _vote(side, conf, reason, venues=rows, agreeing=len(agreed), strength=strength)


def _a1(state, spot, futures):
    funding = float(getattr(state, "funding_rate", 0.0) or 0.0)
    if spot <= 0.0 or futures <= 0.0:
        return _vote(reason="MISSING_BASIS_PRICE")
    basis = (futures - spot) / spot * 10000.0
    if basis >= 8.0 and funding > 0.0:
        return _vote("SHORT", _clamp(0.45 + min(abs(basis), 40.0) / 100.0),
                     "LONG_CROWDING", basis_bps=basis, funding=funding)
    if basis <= -8.0 and funding < 0.0:
        return _vote("LONG", _clamp(0.45 + min(abs(basis), 40.0) / 100.0),
                     "SHORT_CROWDING", basis_bps=basis, funding=funding)
    return _vote(reason="CROWDING_NEUTRAL", basis_bps=basis, funding=funding)


def _a2(cur, ref, threshold):
    if ref is None:
        return _vote(reason="WARMUP_LEAD_HISTORY")
    sr, fr = _chg(cur.get("spot"), ref.get("spot")), _chg(cur.get("futures"), ref.get("futures"))
    if sr is None or fr is None or abs(sr) < threshold:
        return _vote(reason="NO_CLEAR_SPOT_LEAD", spot_pct=sr, futures_pct=fr)
    same = (sr > 0.0 and fr >= 0.0) or (sr < 0.0 and fr <= 0.0)
    if not same or abs(sr) < max(abs(fr) * 1.20, threshold):
        return _vote(reason="PERP_NOT_LAGGING_SPOT", spot_pct=sr, futures_pct=fr)
    ratio = abs(sr) / max(abs(fr), threshold * 0.25)
    return _vote("LONG" if sr > 0.0 else "SHORT",
                 _clamp(0.45 + 0.10 * min(max(ratio - 1.0, 0.0), 2.0)),
                 "SPOT_LEADS_PERP", spot_pct=sr, futures_pct=fr, lead_ratio=ratio)


def _consensus(sv, av):
    longs = [v for v in sv.values() if v["vote"] == "LONG"]
    shorts = [v for v in sv.values() if v["vote"] == "SHORT"]
    if len(longs) >= 2 and not any(v["confidence"] >= STRONG_OPPOSITION for v in shorts):
        side, supporters, opponents = "LONG", longs, shorts
    elif len(shorts) >= 2 and not any(v["confidence"] >= STRONG_OPPOSITION for v in longs):
        side, supporters, opponents = "SHORT", shorts, longs
    else:
        return "ABSTAIN", 0.0, max(len(longs), len(shorts)),                ("S_CONFLICT" if longs and shorts else "INSUFFICIENT_S_QUORUM")
    conf = sum(v["confidence"] for v in supporters) / len(supporters)
    if len(supporters) == 3:
        conf += 0.05
    conf -= sum(0.10 * v["confidence"] for v in opponents)
    for v in av.values():
        conf += (0.05 if v["vote"] == side else -0.05 if v["vote"] in ("LONG", "SHORT") else 0.0) * v["confidence"]
    return side, _clamp(conf), len(supporters), "S_QUORUM"


def evaluate(state, now=None, force_full=False):
    now = time.time() if now is None else float(now)
    spot = _mid(getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0))
    cb = float(getattr(state, "coinbase_price", 0.0) or 0.0)
    cbts = float(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0) or 0.0)
    if cbts <= 0.0 or now - cbts > CB_MAX_AGE:
        cb = 0.0
    futures, fut_source = _futures_price(state, now)
    oi = float(getattr(state, "open_interest", 0.0) or 0.0)

    hist = getattr(state, "bias_price_history", None)
    if hist is None:
        hist = deque(maxlen=48)
        state.bias_price_history = hist
    ref = None
    for row in hist:
        if float(row.get("ts", 0.0) or 0.0) <= now - LOOKBACK:
            ref = row
    cur = {"ts": now, "spot": spot, "coinbase": cb, "futures": futures, "oi": oi}
    hist.append(cur)

    threshold = _threshold(state, spot)
    sv = {"S1_cross_price": _s1(cur, ref, threshold),
          "S2_price_x_oi": _s2(cur, ref, threshold)}
    flow_trigger, light_imb, light_total = _light_flow(state)
    full = force_full or flow_trigger or any(v["vote"] != "ABSTAIN" for v in sv.values())
    sv["S3_multi_flow"] = _s3(state, now) if full else _vote(
        reason="DEFERRED_LIGHT_SCOUT", light_flow_imbalance=light_imb, light_flow_total=light_total)
    mode = "FULL" if full else "LIGHT"

    au = {"A1_funding_basis": _a1(state, spot, futures),
          "A2_spot_lead": _a2(cur, ref, threshold)}
    bias, confidence, quorum, reason = _consensus(sv, au)
    return {"version": VERSION, "bias": bias, "confidence": round(confidence, 6),
            "quorum": quorum, "reason": reason, "mode": mode,
            "s_votes": sv, "a_votes": au, "futures_price_source": fut_source, "ts": now}


def update_state(state, now=None, force_full=False):
    result = evaluate(state, now=now, force_full=force_full)
    state.bias_state = result["bias"]
    state.bias_confidence = result["confidence"]
    state.bias_council = result
    state.bias_updated_at = result["ts"]
    state.bias_version = VERSION
    # Legacy OI+funding bias must not influence scoring once council is active.
    state.macro_bias = "NEUTRAL"
    return result
