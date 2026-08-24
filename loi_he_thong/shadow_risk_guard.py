"""Mainnet-shadow hard SL and one-way profit protection.

No fixed TP and no partial exits. The full 0.001 BTC shadow position is held while
Tier-S support persists. Profit protection ratchets one-way. Exit is whole-position
only: hard SL, locked-profit floor, or the separate causal Guardian. This module
must not infer Whale Intent or independently convert loss of support into an exit.
"""

VERSION = "RISK_TIER_S_CAUSAL_BOUNDARY_V6_SUPPORTIVE_RUNNER"
STOP = 0.0055
FEE_BPS = 5.0

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
    return snap(p, e)


def _states(g):
    votes = (g or {}).get("votes") or {}
    return [(votes.get(k) or {}).get("status", "NEUTRAL") for k in _KEYS]


def tier_mode(g):
    st = _states(g)
    sup, adv = st.count("SUPPORTIVE"), st.count("ADVERSE")
    decision = str((g or {}).get("decision") or "HOLD").upper()
    runner_shield = bool((g or {}).get("runner_shield_active"))
    kill_fast = bool((g or {}).get("kill_fast"))
    if sup == 3:
        mode = "MAX_RIDE"
    elif sup >= 2 and adv == 0:
        mode = "RIDE"
    elif runner_shield and not kill_fast:
        # The one-way floor remains armed, but a normal runner pullback does
        # not tighten itself into an exit before Guardian confirmation.
        mode = "RIDE"
    elif decision in ("DETERIORATING", "EXIT") and adv >= 2:
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
    # Port the useful idea from the old runner: supportive evidence widens the
    # trail, while the one-way floor guarantees already locked profit is never
    # handed back when support later disappears.
    gap = {"MAX_RIDE": 2.00, "RIDE": 1.40, "PROTECT": 0.80, "TIGHTEN": 0.40}[mode]
    minimum = {"MAX_RIDE": 1.00, "RIDE": 1.20, "PROTECT": 1.35, "TIGHTEN": 1.50}[mode]
    return max(minimum, best_r - gap), mode


def assess(p, px, guardian=None, market_state=None, now=None):
    px = float(px)
    e = float(p.entry_price)
    s = sg(p.side)
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
        # One-way ratchet: mode changes can never give locked profit back.
        floor_r = max(p.floor_r, floor_r) if p.floor_r is not None else floor_r
        p.floor_r = floor_r
        p.floor = e + s * floor_r * p.r
        p.stage = stage

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
    }
