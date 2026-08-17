"""Mainnet-shadow hard SL + Tier-S whale-riding profit protection.

No fixed TP and no partial exits. The full 0.001 BTC shadow position is held while
Tier-S support persists. Profit protection ratchets one-way. Exit is whole-position
only: hard SL, locked-profit floor, causal Guardian exit, or adaptive whale exhaustion.

Whale exhaustion is regime-aware:
- ATR/R widens retrace and confirmation in noisy regimes;
- adverse price speed shortens confirmation when reversal accelerates;
- neutral S1+S2 requires more evidence than adverse S1/S2;
- S3 may accelerate confirmation but can never trigger exhaustion by itself.
"""
import time

VERSION = "RISK_TIER_S_WHALE_RIDE_V4"
STOP = 0.0055
FEE_BPS = 5.0

EXHAUSTION_MIN_R = 1.6
ATR_R_FALLBACK = 0.60
SAMPLE_WINDOW_SEC = 1.25

_KEYS = ("S1_price_acceptance", "S2_executed_flow", "S3_price_x_oi")


def _clamp(lo, hi, value):
    return max(lo, min(hi, float(value)))


def sg(side):
    side = str(side or "").upper()
    if side == "LONG":
        return 1.0
    if side == "SHORT":
        return -1.0
    raise ValueError("side must be LONG or SHORT")


def arm(p, entry):
    e = float(entry)
    s = sg(p.side)
    r = e * STOP
    p.r = r
    p.hard_sl = e - s * r
    p.best = e
    p.best_r = 0.0
    p.floor_r = None
    p.floor = None
    p.stage = "INITIAL"
    p.tier_mode = "PROTECT"
    p.fee_r = (e * 2.0 * FEE_BPS / 10000.0) / r
    p.whale_seen = False
    p.whale_exhaustion_since = 0.0
    p.whale_exhaustion_pressure = 0.0
    p.risk_px_samples = []
    p.exhaustion_meta = {}
    return snap(p, e)


def _states(g):
    votes = (g or {}).get("votes") or {}
    return [(votes.get(k) or {}).get("status", "NEUTRAL") for k in _KEYS]


def tier_mode(g):
    st = _states(g)
    sup, adv = st.count("SUPPORTIVE"), st.count("ADVERSE")
    if sup == 3:
        mode = "MAX_RIDE"
    elif sup >= 2 and adv == 0:
        mode = "RIDE"
    elif adv > 0:
        mode = "TIGHTEN"
    else:
        mode = "PROTECT"
    return mode, sup, adv, st


def _candidate(best_r, fee_r, mode):
    if best_r < 1.0:
        return None, "INITIAL"
    if best_r < 1.6:
        return max(0.25, fee_r + 0.05), "LOCK_1"
    if best_r < 2.4:
        return 0.75, "LOCK_2"
    gap = {"MAX_RIDE": 1.60, "RIDE": 1.15, "PROTECT": 0.75, "TIGHTEN": 0.45}[mode]
    minimum = {"MAX_RIDE": 1.00, "RIDE": 1.20, "PROTECT": 1.35, "TIGHTEN": 1.50}[mode]
    return max(minimum, best_r - gap), mode


def _record_price(p, px, now):
    samples = list(getattr(p, "risk_px_samples", []) or [])
    if not samples or now - float(samples[-1][0]) >= 0.04:
        samples.append((float(now), float(px)))
    cutoff = now - SAMPLE_WINDOW_SEC
    samples = [row for row in samples if float(row[0]) >= cutoff][-40:]
    p.risk_px_samples = samples


def _signed_r(p, px):
    return sg(p.side) * (float(px) - float(p.entry_price)) / float(p.r)


def _adverse_speed_rps(p, px, now):
    samples = list(getattr(p, "risk_px_samples", []) or [])
    if len(samples) < 2:
        return 0.0
    current_r = _signed_r(p, px)
    target = now - 0.45
    ref = min(samples, key=lambda row: abs(float(row[0]) - target))
    dt = now - float(ref[0])
    if dt < 0.12:
        ref = samples[0]
        dt = now - float(ref[0])
    if dt <= 0:
        return 0.0
    old_r = _signed_r(p, ref[1])
    return max(0.0, (old_r - current_r) / dt)


def _atr_r(p, market_state):
    atr = float(getattr(market_state, "atr_1m", 0.0) or 0.0) if market_state is not None else 0.0
    if atr <= 0 or float(p.r) <= 0:
        return ATR_R_FALLBACK
    return _clamp(0.20, 2.50, atr / float(p.r))


def _exhaustion_thresholds(p, px, states, market_state, now):
    atr_r = _atr_r(p, market_state)
    speed = _clamp(0.0, 3.0, _adverse_speed_rps(p, px, now))
    core_adverse = int(states[0] == "ADVERSE") + int(states[1] == "ADVERSE")
    s3_adverse = states[2] == "ADVERSE"

    # Normal-vol fallback is near the former 0.35R, but neutral pauses require
    # materially deeper retrace. Real adverse evidence contracts the gate.
    retrace_r = _clamp(0.26, 0.72, 0.24 + 0.18 * atr_r)
    hold_sec = _clamp(0.20, 0.82, 0.50 + 0.10 * atr_r)

    if core_adverse == 0:
        retrace_r *= 1.25
        hold_sec *= 1.25
    elif core_adverse == 2:
        retrace_r *= 0.82
        hold_sec *= 0.72

    if s3_adverse:
        retrace_r *= 0.92
        hold_sec *= 0.88

    # Price speed may accelerate confirmation only when S1/S2 already contains
    # adverse evidence. A fast neutral retrace alone is not allowed to force exit.
    effective_speed = speed if core_adverse > 0 else 0.0
    hold_sec /= 1.0 + 0.55 * effective_speed

    retrace_r = _clamp(0.24, 0.82, retrace_r)
    hold_sec = _clamp(0.16, 0.90, hold_sec)
    pressure = float(core_adverse) + (0.5 if s3_adverse else 0.0)
    return {
        "retrace_gate_r": round(retrace_r, 4),
        "hold_sec": round(hold_sec, 4),
        "atr_r": round(atr_r, 4),
        "adverse_speed_rps": round(speed, 4),
        "effective_speed_rps": round(effective_speed, 4),
        "core_adverse": core_adverse,
        "s3_adverse": s3_adverse,
        "pressure": pressure,
    }


def _reset_exhaustion(p):
    p.whale_exhaustion_since = 0.0
    p.whale_exhaustion_pressure = 0.0


def _whale_exhausted(p, px, best_r, sup, states, market_state, now):
    # Whale support must have existed for real before "whale left" can be inferred.
    if sup >= 2:
        p.whale_seen = True
        _reset_exhaustion(p)
        p.exhaustion_meta = {"reason": "WHALE_SUPPORT_PRESENT"}
        return False

    if not getattr(p, "whale_seen", False) or best_r < EXHAUSTION_MIN_R:
        _reset_exhaustion(p)
        p.exhaustion_meta = {"reason": "NOT_ELIGIBLE"}
        return False

    # S1 price acceptance OR S2 executed flow still supporting => whale not gone.
    if states[0] == "SUPPORTIVE" or states[1] == "SUPPORTIVE":
        _reset_exhaustion(p)
        p.exhaustion_meta = {"reason": "CORE_SUPPORT_STILL_PRESENT"}
        return False

    meta = _exhaustion_thresholds(p, px, states, market_state, now)
    current_r = _signed_r(p, px)
    retrace_r = best_r - current_r
    meta["current_r"] = round(current_r, 4)
    meta["retrace_r"] = round(retrace_r, 4)

    if retrace_r < meta["retrace_gate_r"]:
        _reset_exhaustion(p)
        meta["reason"] = "RETRACE_TOO_SMALL"
        p.exhaustion_meta = meta
        return False

    old_pressure = float(getattr(p, "whale_exhaustion_pressure", 0.0) or 0.0)
    new_pressure = float(meta["pressure"])
    since = float(getattr(p, "whale_exhaustion_since", 0.0) or 0.0)

    # If evidence weakens (e.g. ADVERSE -> NEUTRAL), restart confirmation.
    # If it strengthens, preserve elapsed confirmation instead of delaying exit.
    if since <= 0.0 or new_pressure < old_pressure:
        p.whale_exhaustion_since = now
        p.whale_exhaustion_pressure = new_pressure
        meta["reason"] = "CONFIRMING"
        meta["elapsed_sec"] = 0.0
        p.exhaustion_meta = meta
        return False

    p.whale_exhaustion_pressure = max(old_pressure, new_pressure)
    elapsed = now - since
    meta["elapsed_sec"] = round(max(0.0, elapsed), 4)
    meta["reason"] = "CONFIRMED" if elapsed >= meta["hold_sec"] else "CONFIRMING"
    p.exhaustion_meta = meta
    return elapsed >= meta["hold_sec"]


def assess(p, px, guardian=None, market_state=None, now=None):
    now = time.time() if now is None else float(now)
    px = float(px)
    e = float(p.entry_price)
    s = sg(p.side)
    if not getattr(p, "r", 0):
        arm(p, e)

    _record_price(p, px, now)
    mode, sup, adv, states = tier_mode(guardian)
    p.tier_mode = mode
    p.best = max(p.best, px) if s > 0 else min(p.best, px)
    best_r = max(0.0, s * (p.best - e) / p.r)
    p.best_r = best_r

    if (px <= p.hard_sl if s > 0 else px >= p.hard_sl):
        return snap(p, px, "EXIT", "HARD_SL", sup, adv)

    floor_r, stage = _candidate(best_r, p.fee_r, mode)
    if floor_r is not None:
        # One-way ratchet: mode changes can never give locked profit back.
        floor_r = max(p.floor_r, floor_r) if p.floor_r is not None else floor_r
        p.floor_r = floor_r
        p.floor = e + s * floor_r * p.r
        p.stage = stage

    if _whale_exhausted(p, px, best_r, sup, states, market_state, now):
        return snap(p, px, "EXIT", "WHALE_EXHAUSTION", sup, adv)

    if p.floor is not None and (px <= p.floor if s > 0 else px >= p.floor):
        return snap(p, px, "EXIT", "PROFIT_FLOOR", sup, adv)

    why = "TIER_S_" + mode if best_r >= 1.0 else "INITIAL_RISK"
    return snap(p, px, "HOLD", why, sup, adv)


def guardian_ok(x):
    v = (x or {}).get("votes") or {}

    def adverse(k):
        return (v.get(k) or {}).get("status") == "ADVERSE"

    return adverse("S1_price_acceptance") and (
        adverse("S2_executed_flow") or adverse("S3_price_x_oi")
    )


def snap(p, px, decision="HOLD", why="PROTECT", sup=0, adv=0):
    return {
        "version": VERSION,
        "decision": decision,
        "reason": why,
        "price": float(px),
        "hard_sl": p.hard_sl,
        "profit_floor": p.floor,
        "floor_r": p.floor_r,
        "stage": p.stage,
        "best_r": getattr(p, "best_r", 0.0),
        "tier_mode": getattr(p, "tier_mode", "PROTECT"),
        "supportive_count": sup,
        "adverse_count": adv,
        "whale_seen": bool(getattr(p, "whale_seen", False)),
        "whale_exhaustion_since": float(getattr(p, "whale_exhaustion_since", 0.0) or 0.0),
        "exhaustion": dict(getattr(p, "exhaustion_meta", {}) or {}),
    }
