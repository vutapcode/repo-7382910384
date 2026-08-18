"""Causal RAM-only regime adapter for Tier-S entry."""
from collections import deque
import time
from loi_he_thong import flow_lead_engine

VERSION = "MICRO_REGIME_V2_FLOW_LEAD"

def _f(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0

def _mid(s):
    b, a = _f(getattr(s, "best_bid", 0)), _f(getattr(s, "best_ask", 0))
    return (b + a) / 2.0 if b > 0 and a > b else max(b, a)

def classify(state, side=None):
    now, px = time.time(), _mid(state)
    hist = getattr(state, "_micro_regime_hist", None)
    if hist is None:
        hist = deque(maxlen=64)
        state._micro_regime_hist = hist
    oi = _f(getattr(state, "open_interest", 0))
    vol = max(
        _f(getattr(state, "current_vol_3s", 0)),
        _f(getattr(state, "current_cvd_buy_3s", 0)) + _f(getattr(state, "current_cvd_sell_3s", 0)),
    )
    if not hist or now - hist[-1][0] >= 0.25:
        hist.append((now, px, oi, vol))
    ref = hist[0] if hist else (now, px, oi, vol)
    for row in hist:
        if row[0] <= now - 8.0:
            ref = row
        else:
            break
    move_bps = ((px - ref[1]) / ref[1] * 10000.0) if px > 0 and ref[1] > 0 else 0.0
    oi_pct = ((oi - ref[2]) / ref[2] * 100.0) if oi > 0 and ref[2] > 0 else 0.0
    p90 = _f(getattr(state, "vol_pct90", 0))
    vr = vol / p90 if p90 > 0 else 1.0
    atr = _f(getattr(state, "atr_1m", 0))
    atr_bps = atr / px * 10000.0 if atr > 0 and px > 0 else 0.0

    regime, pf, cf, ef = "NORMAL", 1.0, 1.0, 1.0
    if oi_pct <= -0.20 and abs(move_bps) >= max(2.5, atr_bps * 0.10) and vr >= 0.9:
        regime, pf, cf, ef = "LIQUIDATION", 1.15, 1.12, 0.90
    elif abs(move_bps) >= max(2.0, atr_bps * 0.08) and vr >= 0.9:
        regime, pf, cf, ef = "EXPANSION", 0.90, 0.94, 1.08
    elif vr <= 0.55 and abs(move_bps) <= max(1.0, atr_bps * 0.04):
        regime, pf, cf, ef = "QUIET", 0.92, 1.04, 0.96
    elif len(hist) >= 8:
        signs = []
        rows = list(hist)[-10:]
        for left, right in zip(rows, rows[1:]):
            d = right[1] - left[1]
            if abs(d) > max(px * 0.000015, 1e-9):
                signs.append(1 if d > 0 else -1)
        flips = sum(a != b for a, b in zip(signs, signs[1:]))
        if flips >= 3 and abs(move_bps) <= max(1.5, atr_bps * 0.05):
            regime, pf, cf, ef = "CHOP", 1.18, 1.10, 0.90

    direction = str(side or getattr(state, "bias_state", "") or "").upper()
    lead = flow_lead_engine.analyze(state, direction)
    if lead.get("status") == "OK":
        persistence = _f(lead.get("persistence"))
        oppose = _f(lead.get("oppose_ratio"))
        lead_name = lead.get("lead")
        accel = _f(lead.get("lead_accel_bps"))
        if regime not in {"LIQUIDATION", "CHOP"} and lead_name == "CASH_LED" and persistence >= 0.58 and oppose <= 0.20:
            regime = "EXPANSION" if regime == "NORMAL" else regime
            pf = min(pf, 0.88)
            cf = min(cf, 0.96)
            ef = max(ef, 1.06)
        if lead_name == "PERP_LED" and (_f(lead.get("lead_gap_bps")) >= 1.25 or accel >= 0.45):
            pf = max(pf, 1.12)
            cf = max(cf, 1.08)
            ef = min(ef, 0.94)
        if oppose >= 0.34:
            pf = max(pf, 1.15)
            cf = max(cf, 1.08)
            ef = min(ef, 0.92)

    out = {
        "version": VERSION,
        "regime": regime,
        "price_factor": round(pf, 4),
        "cost_factor": round(cf, 4),
        "expectancy_factor": round(ef, 4),
        "move_bps": round(move_bps, 4),
        "oi_pct": round(oi_pct, 5),
        "vol_ratio": round(vr, 4),
        "flow_lead": lead,
        "policy": "ADAPT_THRESHOLDS_ONLY_NO_SIGNAL_AUTHORITY",
    }
    state.tier_s_micro_regime = out
    return out
