"""Mainnet-shadow SL/TP and profit-lock engine."""
V="RISK_V1"; STOP=0.0055; TP_R=6.0; FEE_BPS=5.0

def sg(side):
    return 1.0 if str(side).upper()=="LONG" else -1.0

def arm(p,e):
    e=float(e); s=sg(p.side); r=e*STOP
    p.r=r; p.hard_sl=e-s*r; p.tp=e+s*TP_R*r
    p.best=e; p.floor_r=None; p.floor=None; p.stage="INITIAL"
    p.fee_r=(e*2*FEE_BPS/10000.0)/r
    return snap(p,e)

def _floor(br,fr):
    if br>=4: return max(2.5,br-1.2),"RUNNER"
    if br>=2.4: return max(1.2,br-1.1),"TRAIL"
    if br>=1.6: return .75,"LOCK_2"
    if br>=1: return max(.25,fr+.05),"LOCK_1"
    return None,"INITIAL"

def assess(p,px):
    px=float(px); e=float(p.entry_price); s=sg(p.side)
    if not getattr(p,"r",0): arm(p,e)
    p.best=max(p.best,px) if s>0 else min(p.best,px)
    br=max(0,s*(p.best-e)/p.r); p.best_r=br
    if (px<=p.hard_sl if s>0 else px>=p.hard_sl): return snap(p,px,"EXIT","HARD_SL")
    if (px>=p.tp if s>0 else px<=p.tp): return snap(p,px,"EXIT","FAILSAFE_TP")
    fr,st=_floor(br,p.fee_r)
    if fr is not None:
        fr=max(p.floor_r,fr) if p.floor_r is not None else fr
        p.floor_r=fr; p.floor=e+s*fr*p.r; p.stage=st
    if p.floor is not None and (px<=p.floor if s>0 else px>=p.floor):
        return snap(p,px,"EXIT","PROFIT_FLOOR")
    return snap(p,px)

def guardian_ok(x):
    v=(x or {}).get("votes") or {}
    def a(k): return (v.get(k) or {}).get("status")=="ADVERSE"
    return a("S1_price_acceptance") and (a("S2_executed_flow") or a("S3_price_x_oi"))

def snap(p,px,d="HOLD",why="PROTECT"):
    return {"version":V,"decision":d,"reason":why,"price":float(px),
            "hard_sl":p.hard_sl,"failsafe_tp":p.tp,
            "profit_floor":p.floor,"floor_r":p.floor_r,
            "stage":p.stage,"best_r":getattr(p,"best_r",0.0)}
