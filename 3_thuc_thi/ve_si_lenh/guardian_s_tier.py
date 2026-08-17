"""Minimal Tier-S causal guardian. No BOS/CHoCH/EMA/POC/OBI/wall votes."""
from collections import deque
import time

VERSION="GUARDIAN_S_TIER_V2"
MIN_PRICE_BPS=0.40
MIN_FLOW_IMB=0.08
MIN_OI_RISE_PCT=0.015

def _clamp(x): return max(0.0,min(1.0,float(x)))
def _mid(b,a):
    b,a=float(b or 0),float(a or 0)
    return (b+a)/2 if b>0 and a>b else max(b,a)
def _sign(side): return 1.0 if str(side).upper()=="LONG" else -1.0
def _bps(c,r):
    c,r=float(c or 0),float(r or 0)
    return None if c<=0 or r<=0 else (c-r)/r*10000
def _pct(c,r):
    c,r=float(c or 0),float(r or 0)
    return None if c<=0 or r<=0 else (c-r)/r*100
def _vote(status="NEUTRAL",conf=0.0,reason="",**m):
    return {"status":status,"confidence":round(_clamp(conf),6),"reason":reason,"metrics":m}

def _last_fut(state,now):
    rows=getattr(state,"danh_sach_khop_lenh_futures",None) or ()
    try:
        r=rows[-1]; ts=float(r.get("thoi_gian_ms",0) or 0)/1000
        px=float(r.get("gia",0) or 0)
        return px if px>0 and ts>0 and now-ts<=5 else 0.0
    except Exception: return 0.0

def _prices(state,now):
    cb=float(getattr(state,"coinbase_price",0) or 0)
    cbts=float(getattr(state,"thoi_gian_coinbase_ticker_cuoi",0) or 0)
    if cbts<=0 or now-cbts>5: cb=0.0
    return {"spot":_mid(getattr(state,"best_bid",0),getattr(state,"best_ask",0)),
            "coinbase":cb,"futures":_last_fut(state,now)}

def _ensure(state,pos):
    ident=(str(getattr(pos,"position_cycle_id","") or ""),str(getattr(pos,"side","") or ""),
           float(getattr(pos,"opened_at",0) or 0))
    if getattr(state,"guardian_s_ident",None)!=ident:
        state.guardian_s_ident=ident
        state.guardian_s_prices=deque(maxlen=256)
        state.guardian_s_oi=deque(maxlen=128)
        pos.guardian_s_candidate_since=0.0
        pos.guardian_s_signature=()
    return state.guardian_s_prices,state.guardian_s_oi

def _sample(state,pos,now):
    ph,oh=_ensure(state,pos); p=_prices(state,now)
    if not ph or now-float(ph[-1]["ts"])>=0.05: ph.append({"ts":now,**p})
    oi=float(getattr(state,"open_interest",0) or 0)
    if oi>0 and (not oh or now-float(oh[-1]["ts"])>=1.0):
        oh.append({"ts":now,"oi":oi,"spot":p["spot"]})
    return p,ph,oh

def _ref(hist,now,sec):
    out=None; target=now-sec
    for r in hist:
        if float(r["ts"])<=target: out=r
        else: break
    return out

def _threshold(state,spot):
    atr=float(getattr(state,"atr_1m",0) or 0)
    dyn=atr/spot*10000*0.04 if spot>0 and atr>0 else 0
    return max(MIN_PRICE_BPS,min(2.0,dyn or MIN_PRICE_BPS))

def _s1(state,pos,now,p,ph):
    sign=_sign(pos.side); base=_threshold(state,p["spot"])
    adverse_hits=[]; support_hits=[]; detail={}
    for h in (0.25,1.0,3.0):
        ref=_ref(ph,now,h)
        if not ref: continue
        thr=base*(0.7 if h<=.25 else 1.0 if h<=1 else 1.2)
        adv=[]; sup=[]; moves={}
        for v in ("spot","coinbase","futures"):
            m=_bps(p.get(v),ref.get(v))
            if m is None: continue
            signed=m*sign; moves[v]=round(signed,4)
            if signed<=-thr: adv.append(v)
            elif signed>=thr: sup.append(v)
        detail[str(h)]={"threshold_bps":round(thr,4),"moves":moves,
                        "adverse":adv,"supportive":sup}
        if len(adv)>=2 and (h>=1.0 or len(adv)==3):
            adverse_hits.append((h,len(adv)))
        if len(sup)>=2 and (h>=1.0 or len(sup)==3):
            support_hits.append((h,len(sup)))
    if adverse_hits and support_hits:
        return _vote("NEUTRAL",0.20,"PRICE_ACCEPTANCE_CONFLICT",horizons=detail)
    if adverse_hits:
        h,n=max(adverse_hits,key=lambda x:(x[1],x[0]))
        return _vote("ADVERSE",0.62+0.10*(n-2)+0.06*(len(adverse_hits)-1),
                     "CROSS_VENUE_PRICE_ADVERSE",horizons=detail)
    if support_hits:
        h,n=max(support_hits,key=lambda x:(x[1],x[0]))
        return _vote("SUPPORTIVE",0.60+0.10*(n-2)+0.06*(len(support_hits)-1),
                     "CROSS_VENUE_PRICE_SUPPORTIVE",horizons=detail)
    return _vote("NEUTRAL",0.10,"NO_PRICE_ACCEPTANCE",horizons=detail)

def _imb(buy,sell):
    buy,sell=float(buy or 0),float(sell or 0); total=buy+sell
    return ((buy-sell)/total if total>0 else 0.0,total)

def _spot_flow(state,now):
    buy=sell=0.0; cutoff=now-3
    for r in list(getattr(state,"flow_1s_buffer",()) or ()):
        try:
            if float(r.get("ts",0) or 0)>=cutoff:
                buy+=float(r.get("buy",0) or 0); sell+=float(r.get("sell",0) or 0)
        except Exception: pass
    return _imb(buy,sell)

def _fut_flow(state,now):
    buy=sell=0.0; newest=0.0; cutoff=(now-3)*1000
    for r in list(getattr(state,"danh_sach_khop_lenh_futures",()) or ()):
        try:
            ts=float(r.get("thoi_gian_ms",0) or 0)
            if ts<cutoff: continue
            q=float(r.get("khoi_luong",0) or 0); newest=max(newest,ts/1000)
            if bool(r.get("ban_chu_dong",False)): sell+=q
            else: buy+=q
        except Exception: pass
    return _imb(buy,sell) if newest and now-newest<=5 else (0.0,0.0)

def _cb_flow(state,now):
    ts=float(getattr(state,"coinbase_flow_3s_ts",0) or 0)
    vol=float(getattr(state,"coinbase_volume_3s",0) or 0)
    cvd=float(getattr(state,"coinbase_cvd_3s",0) or 0)
    return (cvd/vol,vol) if ts>0 and now-ts<=5 and vol>0 else (0.0,0.0)

def _s2(state,pos,now):
    sign=_sign(pos.side); rows={}; adv=[]; support=[]
    feeds={"spot":_spot_flow(state,now),"futures":_fut_flow(state,now),"coinbase":_cb_flow(state,now)}
    for name,(imb,vol) in feeds.items():
        if vol<=0: continue
        s=imb*sign; rows[name]=round(s,4)
        if s<=-MIN_FLOW_IMB: adv.append((name,abs(s)))
        elif s>=MIN_FLOW_IMB: support.append((name,abs(s)))
    if len(adv)>=2:
        strength=sum(v for _,v in adv)/len(adv)
        return _vote("ADVERSE",0.58+0.28*min(strength,1),"MULTI_VENUE_ADVERSE_FLOW",
                     signed_imbalances=rows,venues=[n for n,_ in adv])
    if len(support)>=2:
        return _vote("SUPPORTIVE",0.55,"MULTI_VENUE_SUPPORTIVE_FLOW",signed_imbalances=rows)
    return _vote("NEUTRAL",0.10,"FLOW_NOT_CONSENSUS",signed_imbalances=rows)

def _s3(pos,now,p,oh):
    ref=_ref(oh,now,10.0)
    if not ref: return _vote("NEUTRAL",0.0,"OI_WARMUP")
    pc=_pct(p["spot"],ref["spot"]); oc=_pct(oh[-1]["oi"],ref["oi"])
    if pc is None or oc is None: return _vote("NEUTRAL",0.0,"OI_MISSING")
    signed=pc*_sign(pos.side)
    if signed<-0.01 and oc>=MIN_OI_RISE_PCT:
        conf=0.58+0.18*min(abs(pc)/0.05,1)+0.18*min(oc/0.06,1)
        return _vote("ADVERSE",conf,"ADVERSE_PRICE_WITH_NEW_OI_BUILD",price_pct=pc,oi_pct=oc)
    if signed>0.01 and oc>=MIN_OI_RISE_PCT:
        conf=0.56+0.18*min(abs(pc)/0.05,1)+0.18*min(oc/0.06,1)
        return _vote("SUPPORTIVE",conf,"SUPPORTIVE_PRICE_WITH_NEW_OI_BUILD",price_pct=pc,oi_pct=oc)
    return _vote("NEUTRAL",0.12,"NO_DIRECTIONAL_NEW_POSITION_BUILD",price_pct=pc,oi_pct=oc)

def assess(state,pos,now=None):
    now=time.time() if now is None else float(now)
    p,ph,oh=_sample(state,pos,now)
    votes={"S1_price_acceptance":_s1(state,pos,now,p,ph),
           "S2_executed_flow":_s2(state,pos,now),
           "S3_price_x_oi":_s3(pos,now,p,oh)}
    adverse=[k for k,v in votes.items() if v["status"]=="ADVERSE"]
    supportive=[k for k,v in votes.items() if v["status"]=="SUPPORTIVE"]
    s1,s2,s3=votes["S1_price_acceptance"],votes["S2_executed_flow"],votes["S3_price_x_oi"]

    causal_exit=(s1["status"]=="ADVERSE" and
                 (s2["status"]=="ADVERSE" or s3["status"]=="ADVERSE"))
    if causal_exit:
        confirmed=[k for k in adverse if k=="S1_price_acceptance" or k in
                   ("S2_executed_flow","S3_price_x_oi")]
        conf=sum(votes[k]["confidence"] for k in confirmed)/len(confirmed)
        hold=max(0.15,min(0.90,0.75-0.55*conf))
        sig=tuple(sorted(confirmed))
        if getattr(pos,"guardian_s_signature",())!=sig:
            pos.guardian_s_signature=sig; pos.guardian_s_candidate_since=now
        if now-float(getattr(pos,"guardian_s_candidate_since",now) or now)>=hold:
            decision,reason="EXIT","TIER_S_PRICE_PLUS_CAUSE_EXIT"
        else:
            decision,reason="WATCH","TIER_S_PRICE_PLUS_CAUSE_CONVERGENCE"
    elif s1["status"]=="ADVERSE":
        decision,reason,hold="WATCH","PRICE_ADVERSE_AWAITING_FLOW_OR_OI",0.0
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif s2["status"]=="ADVERSE" and s3["status"]=="ADVERSE":
        decision,reason,hold="WATCH","FLOW_AND_OI_NOT_CONVERTED_TO_PRICE",0.0
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif s2["status"]=="ADVERSE" or s3["status"]=="ADVERSE":
        decision,reason,hold="HOLD","ADVERSE_CAUSE_NOT_CONVERTED_TO_PRICE",0.0
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    else:
        decision,reason,hold="HOLD","NO_TIER_S_ADVERSE_CONVERGENCE",0.0
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0

    if decision=="HOLD" and len(supportive)==3:
        reason="THREE_S_SUPPORTIVE"
    elif decision=="HOLD" and len(supportive)>=2:
        reason="MULTI_S_SUPPORTIVE"
    elif decision=="HOLD" and s1["status"]=="SUPPORTIVE":
        reason="PRICE_STILL_SUPPORTIVE"

    conf=sum(votes[k]["confidence"] for k in adverse)/max(1,len(adverse))
    return {"version":VERSION,"decision":decision,"reason":reason,"side":str(pos.side).upper(),
            "confidence":round(conf,6),"hold_seconds":round(hold,4),"votes":votes,
            "supportive_count":len(supportive),"adverse_count":len(adverse),
            "prices":p,"ts":now}

def update_state(state,pos,now=None):
    r=assess(state,pos,now)
    state.guardian_s_result=r; state.guardian_s_decision=r["decision"]; state.guardian_s_updated_at=r["ts"]
    return r
