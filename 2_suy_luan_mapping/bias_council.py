"""Bias council only: LONG / SHORT / ABSTAIN + confidence. No entry logic."""
from collections import deque
import time

VERSION = "BIAS_COUNCIL_V1"

def _mid(bid, ask):
    bid, ask = float(bid or 0), float(ask or 0)
    return (bid + ask) / 2 if bid > 0 and ask > bid else max(bid, ask)

def _chg(cur, old):
    cur, old = float(cur or 0), float(old or 0)
    return None if cur <= 0 or old <= 0 else (cur - old) / old * 100.0

def _vote(side="ABSTAIN", confidence=0.0, reason="", **metrics):
    return {"vote": side, "confidence": round(max(0.0, min(1.0, confidence)), 6),
            "reason": reason, "metrics": metrics}

def _fut_price(state, now):
    rows = getattr(state, "danh_sach_khop_lenh_futures", None) or ()
    if not rows:
        return 0.0
    try:
        row = rows[-1]
        ts = float(row.get("thoi_gian_ms", 0) or 0) / 1000.0
        if ts <= 0 or now - ts > 10:
            return 0.0
        return float(row.get("gia", 0) or 0)
    except (AttributeError, TypeError, ValueError, IndexError):
        return 0.0

def _spot_flow(state, now, seconds=60):
    cutoff = now - seconds
    buy = sell = 0.0
    for row in list(getattr(state, "flow_1s_buffer", ()) or ()):
        try:
            if float(row.get("ts", 0) or 0) < cutoff:
                continue
            buy += float(row.get("buy", 0) or 0)
            sell += float(row.get("sell", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
    total = buy + sell
    return ((buy - sell) / total if total > 0 else 0.0), total

def _fut_flow(state, now, seconds=60):
    cutoff = (now - seconds) * 1000.0
    buy = sell = 0.0
    newest = 0.0
    for row in list(getattr(state, "danh_sach_khop_lenh_futures", ()) or ()):
        try:
            ts = float(row.get("thoi_gian_ms", 0) or 0)
            if ts < cutoff:
                continue
            qty = float(row.get("khoi_luong", 0) or 0)
            newest = max(newest, ts / 1000.0)
            if bool(row.get("ban_chu_dong", False)):
                sell += qty
            else:
                buy += qty
        except (AttributeError, TypeError, ValueError):
            continue
    total = buy + sell
    return ((buy - sell) / total if total > 0 else 0.0), total, newest

def _flow_side(x):
    if abs(x) < 0.05:
        return "ABSTAIN"
    return "LONG" if x > 0 else "SHORT"

def evaluate(state, now=None, force_full=False):
    now = time.time() if now is None else float(now)
    spot = _mid(getattr(state, "best_bid", 0), getattr(state, "best_ask", 0))
    cb = float(getattr(state, "coinbase_price", 0) or 0)
    cb_ts = float(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0) or 0)
    if cb_ts <= 0 or now - cb_ts > 30:
        cb = 0.0
    fut = _fut_price(state, now)
    oi = float(getattr(state, "open_interest", 0) or 0)

    hist = getattr(state, "bias_price_history", None)
    if hist is None:
        hist = deque(maxlen=48)
        state.bias_price_history = hist
    ref = None
    for row in hist:
        if float(row["ts"]) <= now - 15:
            ref = row
    cur = {"ts": now, "spot": spot, "coinbase": cb, "futures": fut, "oi": oi}
    hist.append(cur)

    s_votes = {}
    threshold = max(0.015, (float(getattr(state, "atr_1m", 0) or 0) / spot * 15.0) if spot > 0 else 0.0)

    # S1: at least 2 independent venues move same direction.
    dirs = []
    changes = {}
    if ref:
        for name in ("spot", "coinbase", "futures"):
            c = _chg(cur[name], ref.get(name))
            changes[name] = c
            if c is not None and abs(c) >= threshold:
                dirs.append("LONG" if c > 0 else "SHORT")
    if dirs.count("LONG") >= 2 and dirs.count("SHORT") == 0:
        s_votes["S1_cross_price"] = _vote("LONG", 0.75, "MULTI_VENUE_PRICE_UP", changes=changes)
    elif dirs.count("SHORT") >= 2 and dirs.count("LONG") == 0:
        s_votes["S1_cross_price"] = _vote("SHORT", 0.75, "MULTI_VENUE_PRICE_DOWN", changes=changes)
    elif dirs:
        s_votes["S1_cross_price"] = _vote(reason="CROSS_VENUE_PRICE_CONFLICT", changes=changes)
    else:
        s_votes["S1_cross_price"] = _vote(reason="PRICE_WARMUP_OR_SMALL_MOVE", changes=changes)

    # S2: price x OI. OI must be building, never infer direction from funding.
    pc = _chg(spot, ref.get("spot") if ref else 0)
    oc = _chg(oi, ref.get("oi") if ref else 0)
    if pc is not None and oc is not None and oc >= 0.015 and abs(pc) >= threshold:
        s_votes["S2_price_x_oi"] = _vote("LONG" if pc > 0 else "SHORT", 0.72,
                                             "NEW_POSITION_BUILD", price_pct=pc, oi_pct=oc)
    else:
        s_votes["S2_price_x_oi"] = _vote(reason="OI_NOT_BUILDING_OR_MOVE_SMALL",
                                             price_pct=pc, oi_pct=oc)

    # S3 is more expensive; evaluate only when needed.
    active_light = any(v["vote"] != "ABSTAIN" for v in s_votes.values())
    if force_full or active_light:
        si, st = _spot_flow(state, now)
        fi, ft, fts = _fut_flow(state, now)
        cb_cvd = float(getattr(state, "coinbase_cvd_1m", 0) or 0)
        cb_flow_ts = float(getattr(state, "thoi_gian_coinbase_cuoi", 0) or 0)
        rows = []
        if st > 0 and _flow_side(si) != "ABSTAIN":
            rows.append(("spot", _flow_side(si)))
        if ft > 0 and fts > 0 and now - fts <= 10 and _flow_side(fi) != "ABSTAIN":
            rows.append(("futures", _flow_side(fi)))
        if cb_flow_ts > 0 and now - cb_flow_ts <= 30 and abs(cb_cvd) >= 0.5:
            rows.append(("coinbase", "LONG" if cb_cvd > 0 else "SHORT"))
        sides = [x[1] for x in rows]
        if sides.count("LONG") >= 2 and sides.count("SHORT") == 0:
            s_votes["S3_multi_flow"] = _vote("LONG", 0.78, "MULTI_VENUE_BUY_FLOW", venues=rows)
        elif sides.count("SHORT") >= 2 and sides.count("LONG") == 0:
            s_votes["S3_multi_flow"] = _vote("SHORT", 0.78, "MULTI_VENUE_SELL_FLOW", venues=rows)
        elif rows:
            s_votes["S3_multi_flow"] = _vote(reason="MULTI_VENUE_FLOW_CONFLICT", venues=rows)
        else:
            s_votes["S3_multi_flow"] = _vote(reason="INSUFFICIENT_FLOW_DATA", venues=rows)
        mode = "FULL"
    else:
        s_votes["S3_multi_flow"] = _vote(reason="DEFERRED_LIGHT_SCOUT")
        mode = "LIGHT"

    # A1: funding+basis crowding; advisory only.
    funding = float(getattr(state, "funding_rate", 0) or 0)
    basis = (fut - spot) / spot * 10000.0 if fut > 0 and spot > 0 else 0.0
    if basis >= 8 and funding > 0:
        a1 = _vote("SHORT", 0.55, "LONG_CROWDING", basis_bps=basis, funding=funding)
    elif basis <= -8 and funding < 0:
        a1 = _vote("LONG", 0.55, "SHORT_CROWDING", basis_bps=basis, funding=funding)
    else:
        a1 = _vote(reason="CROWDING_NEUTRAL", basis_bps=basis, funding=funding)

    # A2: spot leads perp; advisory only.
    spr = _chg(spot, ref.get("spot") if ref else 0)
    fur = _chg(fut, ref.get("futures") if ref else 0)
    if spr is not None and fur is not None and abs(spr) >= threshold and abs(spr) >= abs(fur) * 1.2:
        a2 = _vote("LONG" if spr > 0 else "SHORT", 0.55, "SPOT_LEADS_PERP", spot_pct=spr, futures_pct=fur)
    else:
        a2 = _vote(reason="NO_CLEAR_SPOT_LEAD", spot_pct=spr, futures_pct=fur)
    a_votes = {"A1_funding_basis": a1, "A2_spot_lead": a2}

    longs = [v for v in s_votes.values() if v["vote"] == "LONG"]
    shorts = [v for v in s_votes.values() if v["vote"] == "SHORT"]
    if len(longs) >= 2 and not any(v["confidence"] >= 0.75 for v in shorts):
        bias, supporters = "LONG", longs
    elif len(shorts) >= 2 and not any(v["confidence"] >= 0.75 for v in longs):
        bias, supporters = "SHORT", shorts
    else:
        bias, supporters = "ABSTAIN", []
    confidence = sum(v["confidence"] for v in supporters) / len(supporters) if supporters else 0.0
    if supporters:
        for v in a_votes.values():
            if v["vote"] == bias:
                confidence += 0.05 * v["confidence"]
            elif v["vote"] in ("LONG", "SHORT"):
                confidence -= 0.05 * v["confidence"]
    result = {"version": VERSION, "bias": bias, "confidence": round(max(0.0, min(1.0, confidence)), 6),
              "quorum": len(supporters), "mode": mode, "s_votes": s_votes, "a_votes": a_votes, "ts": now}
    return result

def update_state(state, now=None, force_full=False):
    result = evaluate(state, now=now, force_full=force_full)
    state.bias_state = result["bias"]
    state.bias_confidence = result["confidence"]
    state.bias_council = result
    state.bias_updated_at = result["ts"]
    state.bias_version = VERSION
    return result
