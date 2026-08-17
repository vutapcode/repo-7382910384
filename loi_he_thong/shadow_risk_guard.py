"""Mainnet-shadow SL/TP and Tier-S adaptive profit-lock engine."""
V="RISK_TIER_S_V2"; STOP=0.0055; TP_R=6.0; FEE_BPS=5.0

def sg(side):
    return 1.0 if str(side).upper()=="LONG" else -1.0

def arm(p,e):
    e=float(e); s=sg(p.side); r=e*STOP
    p.r=r; p.hard_sl=e-s*r; p.tp=e+s*TP_R*r
    p.best=e; p.floor_r=None; p.floor=None; p.stage="INITIAL"; p.tier_mode="PROTECT"
    p.fee_r=(e*2*FEE_BPS/10000.0)/r
    return snap(p,e)

def tier_mode(g):
    v=(g or {}).get("votes") or {}
    st=[(v.get(k) or {}).get("status","NEUTRAL") for k in
        ("S1_price_acceptance","S2_executed_flow","S3_price_x_oi")]
    sup=st.count("SUPPORTIVE"); adv=st.count("ADVERSE")
    if sup==3: mode="MAX_RIDE"
    elif sup>=2 and adv==0: mode="RIDE"
    elif adv>0: mode="TIGHTEN"
    else: mode="PROTECT"
    return mode,sup,adv

def _candidate(br,fr,mode):
    if br<1.0: return None,"INITIAL"
    if br<1.6: return max(.25,fr+.05),"LOCK_1"
    if br<2.4: return .75,"LOCK_2"
    gap={"MAX_RIDE":1.60,"RIDE":1.15,"PROTECT":.75,"TIGHTEN":.45}[mode]
    minimum={"MAX_RIDE":1.00,"RIDE":1.20,"PROTECT":1.35,"TIGHTEN":1.50}[mode]
    stage={"MAX_RIDE":"MAX_RIDE","RIDE":"RIDE","PROTECT":"PROTECT","TIGHTEN":"TIGHTEN"}[mode]
    return max(minimum,br-gap),stage

def assess(p,px,guardian=None):
    px=float(px); e=float(p.entry_price); s=sg(p.side)
    if not getattr(p,"r",0): arm(p,e)
    mode,sup,adv=tier_mode(guardian); p.tier_mode=mode
    p.best=max(p.best,px) if s>0 else min(p.best,px)
    br=max(0,s*(p.best-e)/p.r); p.best_r=br
    if (px<=p.hard_sl if s>0 else px>=p.hard_sl):
        return snap(p,px,"EXIT","HARD_SL",sup,adv)
    # Far TP is only a failsafe. Strong Tier-S continuation converts it into runner mode.
    if (px>=p.tp if s>0 else px<=p.tp) and mode not in ("MAX_RIDE","RIDE"):
        return snap(p,px,"EXIT","FAILSAFE_TP",sup,adv)
    fr,stage=_candidate(br,p.fee_r,mode)
    if fr is not None:
        # Ratchet invariant: a locked profit floor can only improve, never loosen.
        fr=max(p.floor_r,fr) if p.floor_r is not None else fr
        p.floor_r=fr; p.floor=e+s*fr*p.r; p.stage=stage
    if p.floor is not None and (px<=p.floor if s>0 else px>=p.floor):
        return snap(p,px,"EXIT","PROFIT_FLOOR",sup,adv)
    why="TIER_S_"+mode if br>=2.4 else ("PROFIT_LOCK" if p.floor is not None else "INITIAL_RISK")
    return snap(p,px,"HOLD",why,sup,adv)

def guardian_ok(x):
    v=(x or {}).get("votes") or {}
    def a(k): return (v.get(k) or {}).get("status")=="ADVERSE"
    return a("S1_price_acceptance") and (a("S2_executed_flow") or a("S3_price_x_oi"))

def snap(p,px,d="HOLD",why="PROTECT",sup=0,adv=0):
    return {"version":V,"decision":d,"reason":why,"price":float(px),
            "hard_sl":p.hard_sl,"failsafe_tp":p.tp,"profit_floor":p.floor,
            "floor_r":p.floor_r,"stage":p.stage,"best_r":getattr(p,"best_r",0.0),
            "tier_mode":getattr(p,"tier_mode","PROTECT"),
            "supportive_count":sup,"adverse_count":adv}
