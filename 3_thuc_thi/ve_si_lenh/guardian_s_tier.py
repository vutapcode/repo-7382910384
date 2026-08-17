"""Tier-S causal guardian: S1 price acceptance, S2 executed flow, S3 price x OI."""
from collections import deque
import time

VERSION = "GUARDIAN_S_TIER_V1"
PRICE_HORIZONS = (0.25, 1.0, 3.0)
MIN_PRICE_BPS = 0.40
MIN_FLOW_IMB = 0.08
MIN_OI_RISE_PCT = 0.015

def _clamp(x): return max(0.0, min(1.0, float(x)))
def _mid(bid, ask):
    bid, ask = float(bid or 0), float(ask or 0)
    return (bid + ask) / 2.0 if bid > 0 and ask > bid else max(bid, ask)
def _sign(side): return 1.0 if str(side).upper() == "LONG" else -1.0
def _bps(cur, ref):
    cur, ref = float(cur or 0), float(ref or 0)
    return None if cur <= 0 or ref <= 0 else (cur-ref)/ref*10000.0
def _pct(cur, ref):
    cur, ref = float(cur or 0), float(ref or 0)
    return None if cur <= 0 or ref <= 0 else (cur-ref)/ref*100.0
def _vote(status="NEUTRAL", confidence=0.0, reason="", **m):
    return {"status":status,"confidence":round(_clamp(confidence),6),"reason":reason,"metrics":m}

def _last_futures(state, now):
    rows = getattr(state, "danh_sach_khop_lenh_futures", None) or ()
    try:
        r = rows[-1]
        ts = float(r.get("thoi_gian_ms",0) or 0)/1000.0
        px = float(r.get("gia",0) or 0)
        return (px, ts) if px > 0 and ts > 0 and now-ts <= 5 else (0.0, ts)
    except Exception:
        return 0.0, 0.0

def _prices(state, now):
    spot = _mid(getattr(state,"best_bid",0), getattr(state,"best_ask",0))
    cb = float(getattr(state,"coinbase_price",0) or 0)
    cbts = float(getattr(state,"thoi_gian_coinbase_ticker_cuoi",0) or 0)
    if cbts <= 0 or now-cbts > 5: cb = 0.0
    fut, _ = _last_futures(state, now)
    return {"spot":spot,"coinbase":cb,"futures":fut}

def _ensure(state, position):
    ident = (str(getattr(position,"position_cycle_id","") or ""),
             str(getattr(position,"side","") or ""),
             float(getattr(position,"opened_at",0) or 0))
    if getattr(state,"guardian_s_ident",None) != ident:
        state.guardian_s_ident = ident
        state.guardian_s_prices = deque(maxlen=256)
        state.guardian_s_oi = deque(maxlen=128)
        position.guardian_s_candidate_since = 0.0
        position.guardian_s_signature = ()
    return state.guardian_s_prices, state.guardian_s_oi

def _sample(state, position, now):
    ph, oh = _ensure(state, position)
    prices = _prices(state, now)
    if not ph or now-float(ph[-1]["ts"]) >= 0.05:
        ph.append({"ts":now, **prices})
    oi = float(getattr(state,"open_interest",0) or 0)
    if oi > 0 and (not oh or now-float(oh[-1]["ts"]) >= 1.0):
        oh.append({"ts":now,"oi":oi,"spot":prices["spot"]})
    return prices, ph, oh

def _ref(hist, now, lookback):
    target = now-lookback
    out = None
    for r in hist:
        if float(r["ts"]) <= target: out = r
        else: break
    return out

def _price_threshold(state, spot):
    atr = float(getattr(state,"atr_1m",0) or 0)
    dyn = atr/spot*10000.0*0.04 if spot > 0 and atr > 0 else 0
    return max(MIN_PRICE_BPS, min(2.0, dyn or MIN_PRICE_BPS))

def _s1(state, position, now, prices, ph):
    pos = _sign(position.side)
    base = _price_threshold(state, prices.get("spot",0))
    accepted = []
    details = {}
    for h in PRICE_HORIZONS:
        ref = _ref(ph, now, h)
        if not ref: continue
        thr = base*(0.7 if h <= .25 else 1.0 if h <= 1.0 else 1.2)
        adverse = []
        moves = {}
        for venue in ("spot","coinbase","futures"):
            mv = _bps(prices.get(venue), ref.get(venue))
            if mv is None: continue
            signed = mv*pos
            moves[venue] = round(signed,4)
            if signed <= -thr: adverse.append(venue)
        details[str(h)] = {"thr":round(thr,4),"moves":moves,"adverse":adverse}
        if len(adverse) >= 2 and (h >= 1.0 or len(adverse) == 3):
            accepted.append((h,len(adverse)))
    if accepted:
        best = max(accepted, key=lambda x:(x[1],x[0]))
        return _vote("ADVERSE",0.62+0.10*(best[1]-2)+0.06*(len(accepted)-1),
                     "CROSS_VENUE_PRICE_ACCEPTED", horizons=details)
    return _vote("NEUTRAL",0.1,"NO_PRICE_ACCEPTANCE",horizons=details)

def _imb(buy, sell):
    buy, sell = float(buy or 0), float(sell or 0)
    t = buy+sell
    return ((buy-sell)/t if t>0 else 0.0, t)

def _spot_flow(state, now):
    cutoff = now-3
    buy=sell=0.0
    for r in list(getattr(state,"flow_1s_buffer",()) or ()):
        try:
            if float(r.get("ts",0) or 0) >= cutoff:
                buy += float(r.get("buy",0) or 0); sell += float(r.get("sell",0) or 0)
        except Exception: pass
    return _imb(buy,sell)

def _fut_flow(state, now):
    cutoff=(now-3)*1000
    buy=sell=0.0; newest=0.0
    for r in list(getattr(state,"danh_sach_khop_lenh_futures",()) or ()):
        try:
            ts=float(r.get("thoi_gian_ms",0) or 0)
            if ts < cutoff: continue
            q=float(r.get("khoi_luong",0) or 0); newest=max(newest,ts/1000)
            if bool(r.get("ban_chu_dong",False)): sell += q
            else: buy += q
        except Exception: pass
    return _imb(buy,sell) if newest and now-newest <= 5 else (0.0,0.0)

def _cb_flow(state, now):
    ts=float(getattr(state,"coinbase_flow_3s_ts",0) or 0)
    vol=float(getattr(state,"coinbase_volume_3s",0) or 0)
    cvd=float(getattr(state,"coinbase_cvd_3s",0) or 0)
    return (cvd/vol,vol) if ts>0 and now-ts<=5 and vol>0 else (0.0,0.0)

def _s2(state, position, now):
    pos=_sign(position.side); rows={}
    adverse=[]; supportive=[]
    for name,(imb,vol) in {"spot":_spot_flow(state,now),"futures":_fut_flow(state,"now"),"coinbase":_cb_flow(state,"now")}.items():
        if vol=0: continue
        signed=imb*pos; rows[name]=round(signed,4)
        if signed <= -MIN_FLOW_IMB: adverse.append((name,abs(signed)))
        elif signed >= MIN_FLOW_IMB: supportive.append((name,abs(signed)))
    if len(adverse)>=2:
        strength=sum(v for _,v in adverse)/len(adverse)
        return _vote("ADVERSE",0.58+0.28*min(strength,1.0),"MULTI_VENUE_ADVERSE_FLOW",
                     signed_imbalances=rows, venues=[n for n,_ in adverse])
    if len(supportive)>=2:
        return _vote("SUPPORTIVE",0.55,"MULTI_VENUE_SUPPORTIVE_FLOW",signed_imbalances=rows)
    return _vote("NEUTRAL",0.1,"FLOW_NOT_CONSENSUS",signed_imbalances=rows)

def _s3(position, now, prices, oh):
    ref=_ref(oh,now,10.0)
    if not ref: return _vote("NEUTRAL",0.0,"OI_WARMUP")
    pc=_pct(prices.get("spot"),ref.get("spot")); oc=_pct(oh[-1].get("oi"),ref.get("oi")) if oh else None
    if pc is None or oc is None: return _vote("NEUTRAL",0.0,"OI_MISSING")
    adverse_price = pc*_sign(position.side) < -0.01
    if adverse_price and oc >= MIN_OI_RISE_PCT:
        conf=0.58+0.18*min(abs(pc)/0.05,1.0)+0.18*min(oc/0.06,1.0)
        return _vote("ADVERSE",conf,"ADVERSE_PRICE_WITH_NEW_OI_BUILD",price_pct=pc,oi_pct=oc)
    return _vote("NEUTRAL",0.12,"NO_ADVERSE_POSITION_BUILD",price_pct=pc,oi_pct=oc)

def assess(state, position, now=None):
    now=time.time() if now is None else float(now)
    prices,ph,oh=_sample(state,position,now)
    s1=_s1(state,position,now,prices,ph)
    s2=_s2(state,position,now)
    s3=_s3(position,now,prices,oh)
    votes={"S1_price_acceptance":s1,"S2_executed_flow":s2,"S3_price_x_oi":s3}
    adverse=[k for k,v in votes.items() if v["status"]=="ADVERSE"]
    # Flow without price conversion is absorption/noise, never enough to exit.
    if s2["status"]=="ADVERSE" and s1["status"]!="ADVERSE" and s3["status"]!="ADVERSE":
        decision,reason,hold="HOLD","ADVERSE_FLOW_NOT_CONVERTED_TO_PRICE",0.0
    elif len(adverse)>=2:
        conf=sum(votes[k]["confidence"] for k in adverse)/len(adverse)
        hold=max(0.15,min(0.9,0.75-0.55*conf))
        decision,reason="WATCH","TIER_S_CAUSAL_CONVERGENCE"
        sig=tuple(sorted(adverse))
        if getattr(position,"guardian_s_signature",()) != sig:
            position.guardian_s_signature=sig; position.guardian_s_candidate_since=now
        if now-float(getattr(position,"guardian_s_candidate_since",now) or now) >= hold:
            decision="EXIT"; reason="TIER_S_EXIT_CONFIRMED"
    else:
        decision,reason,hold="HOLD","NO_TIER_S_CONVERGENCE",0.0
        position.guardian_s_signature=(); position.guardian_s_candidate_since=0.0
    if decision=="HOLD" and s1["status"]=="SUPPORTIVE":
        reason="CROSS_VENUE_PRICE_STILL_SUPPORTIVE"
    return {"version":VERSION,"decision":decision,"reason":reason,"side":str(position.side).upper(),
            "confidence":round(sum(v["confidence"] for v in votes.values() if v["status"]=="ADVERSE")/max(1,len(adverse)),6),
            "hold_seconds":round(hold,4),"votes":votes,"prices":prices,"ts":now}

def update_state(state, position, now=None):
    result=assess(state,position,now)
    state.guardian_s_result=result
    state.guardian_s_decision=result["decision"]
    state.guardian_s_updated_at=result["ts"]
    return result
