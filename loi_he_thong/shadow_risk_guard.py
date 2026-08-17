"""Mainnet-shadow hard SL + Tier-S whale-riding profit protection.

No fixed TP and no partial exits. The whole 0.001 BTC shadow position is held while
Tier-S support persists, profit is ratcheted one-way, and the whole position exits
only on hard SL, locked-profit floor, causal Guardian exit, or persistent whale
exhaustion after a profitable run.
"""
import time

VERSION = "RISK_TIER_S_WHALE_RIDE_V3"
STOP = 0.0055
FEE_BPS = 5.0
EXHAUSTION_MIN_R = 1.6
EXHAUSTION_RETRACE_R = 0.35
EXHAUSTION_HOLD_SEC = 0.45

_KEYS = ("S1_price_acceptance", "S2_executed_flow", "S3_price_x_oi")

def sg(side):
    return 1.0 if str(side).upper() == "LONG" else -1.0

def arm(p, entry):
    e = float(entry); s = sg(p.side); r = e * STOP
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

def _whale_exhausted(p, px, best_r, mode, sup, adv, states, now):
    # Only relevant after whale support actually existed and the trade has paid.
    if sup >= 2:
        p.whale_seen = True
        p.whale_exhaustion_since = 0.0
        return False
    if not getattr(p, "whale_seen", False) or best_r < EXHAUSTION_MIN_R:
        p.whale_exhaustion_since = 0.0
        return False

    # "Whale full": price acceptance and executed flow both stop supporting;
    # S3 is allowed to lag/turn neutral. Require price to give back some of bestR.
    s1_support = states[0] == "SUPPORTIVE"
    s2_support = states[1] == "SUPPORTIVE"
    if s1_support or s2_support:
        p.whale_exhaustion_since = 0.0
        return False

    e = float(p.entry_price); s = sg(p.side)
    current_r = s * (float(px) - e) / p.r
    retrace_r = best_r - current_r
    if retrace_r < EXHAUSTION_RETRACE_R:
        p.whale_exhaustion_since = 0.0
        return False

    since = float(getattr(p, "whale_exhaustion_since", 0.0) or 0.0)
    if since <= 0.0:
        p.whale_exhaustion_since = now
        return False
    return now - since >= EXHAUSTION_HOLD_SEC

def assess(p, px, guardian=None, now=None):
    now = time.time() if now is None else float(now)
    px = float(px); e = float(p.entry_price); s = sg(p.side)
    if not getattr(p, "r", 0):
        arm(p, e)

    mode, sup, adv, states = tier_mode(guardian)
    p.tier_mode = mode
    p.best = max(p.best, px) if s > 0 else min(p.best, px)
    best_r = max(0.0, s * (p.best - e) / p.r)
    p.best_r = best_r

    if (px <= p.hard_sl if s > 0 else px >= p.hard_sl):
        return snap(p, px, "EXIT", "HARD_SL", sup, adv)

    floor_r, stage = _candidate(best_r, p.fee_r, mode)
    if floor_r is not None:
        # One-way ratchet: once locked, profit can never be given back by a mode change.
        floor_r = max(p.floor_r, floor_r) if p.floor_r is not None else floor_r
        p.floor_r = floor_r
        p.floor = e + s * floor_r * p.r
        p.stage = stage

    if _whale_exhausted(p, px, best_r, mode, sup, adv, states, now):
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
        "fixed_tp": None,
        "partial_tp": False,
        "profit_floor": p.floor,
        "floor_r": p.floor_r,
        "stage": p.stage,
        "best_r": getattr(p, "best_r", 0.0),
        "tier_mode": getattr(p, "tier_mode", "PROTECT"),
        "supportive_count": sup,
        "adverse_count": adv,
        "whale_seen": bool(getattr(p, "whale_seen", False)),
        "whale_exhaustion_since": float(getattr(p, "whale_exhaustion_since ", 0.0) or 0.0),
    }
