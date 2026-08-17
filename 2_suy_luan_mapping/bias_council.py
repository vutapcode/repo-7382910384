"""Tier-S direction estimator only: LONG/SHORT/ABSTAIN + confidence. Never entry timing."""
from collections import deque
import time

VERSION="BIAS_COUNCIL_V3_DIRECTION_ONLY"
LOOKBACK=15.0; FAST_LOOKBACK=4.0; HISTORY_MAXLEN=192
CB_MAX_AGE=30.0; FUT_MAX_AGE=10.0
MIN_MOVE_PCT=.015; MIN_OI_RISE_PCT=.015; MIN_FLOW_IMB=.05
STRONG_OPPOSITION=.75; HYST_ABSTAIN_SEC=.85; HYST_FLIP_SEC=.55; HYST_FAST_FLIP_CONF=.76
W={"S1_cross_price":1.05,"S2_price_x_oi":1.10,"S3_multi_flow":1.0}

def _cl(x): return max(0.,min(1.,float(x)))
def _mid(b,a):
    b,a=float(b or 0),float(a or 0); return (b+a)/2 if b>0 and a>b else max(b,a)
def _chg(c,o):
    c,o=float(c or 0),float(o or 0); return None if c<=0 or o<=0 else (c-o)/o*100
def _v(side="ABSTAIN",conf=0.,reason="",**m):
    return {"vote":side,"confidence":round(_cl(conf),6),"reason":reason,"metrics":m}
def _side(x,t): return "ABSTAIN" if x is None or abs(x)<t else ("LONG" if x>0 else "SHORT")
def _ex(x,t): return 0. if x is None or t<=0 else _cl((abs(x)/t-1)/2)
def _ref(h,t):
    r=None
    for x in h:
        if float(x.get("ts",0) or 0)<=t:r=x
        else:break
    return r

def _fut(state,now):
    if not bool(getattr(state,"_api_is_testnet",False)):
        ts=float(getattr(state,"execution_price_time",0) or 0)
        if ts>0 and now-ts<=FUT_MAX_AGE:
            p=_mid(getattr(state,"execution_best_bid",0),getattr(state,"execution_best_ask",0))
            if p>0:return p,"FUTURES_MAINNET_BBO"
    try:
        r=(getattr(state,"danh_sach_khop_lenh_futures",()) or ())[-1]
        ts=float(r.get("thoi_gian_ms",0) or 0)/1000
        p=float(r.get("gia",0) or 0)
        return (p,"FUTURES_MAINNET_TRADE") if p>0 and ts>0 and now-ts<=FUT_MAX_AGE else (0.,"MISSING")
    except (IndexError,AttributeError,TypeError,ValueError): return 0.,"MISSING"

def _thr(state,spot):
    a=float(getattr(state,"atr_1m",0) or 0)
    return max(MIN_MOVE_PCT,a/spot*100*.15 if spot>0 and a>0 else 0)

def _price(cur,ref,t,fast=False):
    if ref is None:return _v(reason="WARMUP_FAST_PRICE_HISTORY" if fast else "WARMUP_PRICE_HISTORY")
    t*=1.25 if fast else 1.; rows=[]; changes={}
    for venue in ("spot","coinbase","futures"):
        c=_chg(cur.get(venue),ref.get(venue)); changes[venue]=c; d=_side(c,t)
        if d!="ABSTAIN":rows.append((venue,d,c))
    L=[x for x in rows if x[1]=="LONG"]; S=[x for x in rows if x[1]=="SHORT"]
    if L and S:return _v(reason="CROSS_VENUE_PRICE_CONFLICT",changes=changes)
    a=L if len(L)>=2 and not S else S if len(S)>=2 and not L else []
    if not a:return _v(reason="PRICE_MOVE_INCOMPLETE",changes=changes)
    if fast and len(a)!=3:return _v(reason="FAST_PRICE_NEEDS_3_VENUES",changes=changes,agreeing=len(a))
    st=sum(_ex(x[2],t) for x in a)/len(a); side=a[0][1]
    conf=(.58+.10*st) if fast else (.55+.12*max(0,len(a)-2)+.25*st)
    return _v(side,conf,"FAST_3VENUE_PRICE_DIRECTION" if fast else "MULTI_VENUE_PRICE",
              changes=changes,agreeing=len(a),strength=round(st,6),horizon_sec=FAST_LOOKBACK if fast else LOOKBACK)

def _s1(cur,slow,fast,t):
    a=_price(cur,slow,t); f=_price(cur,fast,t,True)
    if a["vote"]!="ABSTAIN":
        m=dict(a["metrics"]);m.update(fast_vote=f["vote"],fast_confidence=f["confidence"])
        return _v(a["vote"],a["confidence"],a["reason"],**m)
    if f["vote"]!="ABSTAIN":
        m=dict(f["metrics"]);m.update(slow_reason=a["reason"],provisional=True)
        return _v(f["vote"],min(.72,f["confidence"]),"FAST_DIRECTIONAL_PRICE_FALLBACK",**m)
    return _v(reason=a["reason"],fast_vote=f["vote"],fast_reason=f["reason"])

def _s2(cur,ref,t):
    if ref is None:return _v(reason="WARMUP_OI_HISTORY")
    pc,oc=_chg(cur.get("spot"),ref.get("spot")),_chg(cur.get("oi"),ref.get("oi"))
    if pc is None or oc is None:return _v(reason="MISSING_PRICE_OR_OI",price_pct=pc,oi_pct=oc)
    if oc<MIN_OI_RISE_PCT:return _v(reason="OI_NOT_BUILDING",price_pct=pc,oi_pct=oc)
    side=_side(pc,t)
    if side=="ABSTAIN":return _v(reason="PRICE_MOVE_TOO_SMALL",price_pct=pc,oi_pct=oc)
    ps,os=_ex(pc,t),_cl((oc/MIN_OI_RISE_PCT-1)/3)
    return _v(side,.55+.20*ps+.20*os,"NEW_POSITION_BUILD",price_pct=pc,oi_pct=oc,
              price_strength=round(ps,6),oi_strength=round(os,6))

def _flow(state,now,fut=False):
    B=S=0.; newest=0.
    if fut:
        cut=(now-15)*1000
        for r in list(getattr(state,"danh_sach_khop_lenh_futures",()) or ()):
            try:
                ts=float(r.get("thoi_gian_ms",0) or 0)
                if ts<cut:continue
                q=float(r.get("khoi_luong",0) or 0);newest=max(newest,ts/1000)
                if bool(r.get("ban_chu_dong",False)):S+=q
                else:B+=q
            except (AttributeError,TypeError,ValueError):pass
        if newest<=0 or now-newest>FUT_MAX_AGE:return 0.,0.
    else:
        cut=now-15
        for r in list(getattr(state,"flow_1s_buffer",()) or ()):
            try:
                if float(r.get("ts",0) or 0)>=cut:B+=float(r.get("buy",0) or 0);S+=float(r.get("sell",0) or 0)
            except (AttributeError,TypeError,ValueError):pass
    T=B+S;return ((B-S)/T if T>0 else 0.),T

def _s3(state,now):
    si,st=_flow(state,now);fi,ft=_flow(state,now,True)
    cb=float(getattr(state,"coinbase_cvd_1m",0) or 0);ct=float(getattr(state,"thoi_gian_coinbase_cuoi",0) or 0)
    rows=[]
    for n,i,t in (("spot",si,st),("futures",fi,ft)):
        if t>0 and abs(i)>=MIN_FLOW_IMB:rows.append((n,"LONG" if i>0 else "SHORT",_cl(abs(i)/.35)))
    if ct>0 and now-ct<=CB_MAX_AGE and abs(cb)>=.5:rows.append(("coinbase","LONG" if cb>0 else "SHORT",_cl(abs(cb)/3)))
    L=[x for x in rows if x[1]=="LONG"];S=[x for x in rows if x[1]=="SHORT"]
    if L and S:return _v(reason="MULTI_VENUE_FLOW_CONFLICT",venues=rows)
    a=L if len(L)>=2 and not S else S if len(S)>=2 and not L else []
    if not a:return _v(reason="INSUFFICIENT_FLOW_CONSENSUS",venues=rows)
    stg=sum(x[2] for x in a)/len(a)
    return _v(a[0][1],.52+.12*max(0,len(a)-2)+.30*stg,"MULTI_VENUE_FLOW",
              venues=rows,agreeing=len(a),strength=round(stg,6),horizon_sec=15.)

def _a1(state,spot,fut):
    f=float(getattr(state,"funding_rate",0) or 0)
    if spot<=0 or fut<=0:return _v(reason="MISSING_BASIS_PRICE")
    b=(fut-spot)/spot*10000
    if b>=8 and f>0:return _v("SHORT",_cl(.45+min(abs(b),40)/100),"LONG_CROWDING",basis_bps=b,funding=f)
    if b<=-8 and f<0:return _v("LONG",_cl(.45+min(abs(b),40)/100),"SHORT_CROWDING",basis_bps=b,funding=f)
    return _v(reason="CROWDING_NEUTRAL",basis_bps=b,funding=f)

def _a2(cur,ref,t):
    if ref is None:return _v(reason="WARMUP_LEAD_HISTORY")
    s,f=_chg(cur.get("spot"),ref.get("spot")),_chg(cur.get("futures"),ref.get("futures"))
    if s is None or f is None or abs(s)<t:return _v(reason="NO_CLEAR_SPOT_LEAD")
    same=(s>0 and f>=0) or (s<0 and f<=0)
    if not same or abs(s)<max(abs(f)*1.2,t):return _v(reason="PERP_NOT_LAGGING_SPOT")
    rat=abs(s)/max(abs(f),t*.25)
    return _v("LONG" if s>0 else "SHORT",_cl(.45+.1*min(max(rat-1,0),2)),"SPOT_LEADS_PERP",
              spot_pct=s,futures_pct=f,lead_ratio=rat)

def _cons(sv,av):
    L=[(k,v) for k,v in sv.items() if v["vote"]=="LONG"];S=[(k,v) for k,v in sv.items() if v["vote"]=="SHORT"]
    def score(side):
        z=sum(W[k]*v["confidence"] for k,v in sv.items() if v["vote"]==side)
        return z+sum(.12*v["confidence"] for v in av.values() if v["vote"]==side)
    ls,ss=score("LONG"),score("SHORT")
    if len(L)>=2 and not any(v["confidence"]>=STRONG_OPPOSITION for _,v in S): side,sup,opp="LONG",L,S
    elif len(S)>=2 and not any(v["confidence"]>=STRONG_OPPOSITION for _,v in L): side,sup,opp="SHORT",S,L
    else:return "ABSTAIN",0.,max(len(L),len(S)),"S_CONFLICT" if L and S else "INSUFFICIENT_S_QUORUM",ls,ss
    conf=sum(W[k]*v["confidence"] for k,v in sup)/sum(W[k] for k,_ in sup)
    conf+=.06 if len(sup)==3 else 0;conf-=sum(.12*v["confidence"] for _,v in opp)
    for v in av.values():
        conf+=( .035 if v["vote"]==side else -.035 if v["vote"] in ("LONG","SHORT") else 0)*v["confidence"]
    conf+=min(.045,abs(ls-ss)*.018)
    return side,_cl(conf),len(sup),"S_QUORUM",ls,ss

def evaluate(state,now=None,force_full=False):
    now=time.time() if now is None else float(now)
    spot=_mid(getattr(state,"best_bid",0),getattr(state,"best_ask",0))
    cb=float(getattr(state,"coinbase_price",0) or 0);ct=float(getattr(state,"thoi_gian_coinbase_ticker_cuoi",0) or 0)
    if ct<=0 or now-ct>CB_MAX_AGE:cb=0.
    fut,src=_fut(state,now);oi=float(getattr(state,"open_interest",0) or 0)
    h=getattr(state,"bias_price_history",None)
    if h is None or getattr(h,"maxlen",None)!=HISTORY_MAXLEN:
        h=deque(list(h or ())[-HISTORY_MAXLEN:],maxlen=HISTORY_MAXLEN);state.bias_price_history=h
    slow,fast=_ref(h,now-LOOKBACK),_ref(h,now-FAST_LOOKBACK)
    cur={"ts":now,"spot":spot,"coinbase":cb,"futures":fut,"oi":oi};h.append(cur);t=_thr(state,spot)
    sv={"S1_cross_price":_s1(cur,slow,fast,t),"S2_price_x_oi":_s2(cur,slow,t),"S3_multi_flow":_s3(state,now)}
    av={"A1_funding_basis":_a1(state,spot,fut),"A2_spot_lead":_a2(cur,slow,t)}
    bias,conf,q,reason,ls,ss=_cons(sv,av);tot=ls+ss
    return {"version":VERSION,"bias":bias,"confidence":round(conf,6),"quorum":q,"reason":reason,"mode":"FULL",
            "s_votes":sv,"a_votes":av,"direction_scores":{"long":round(ls,6),"short":round(ss,6),
            "margin":round(_cl(abs(ls-ss)/tot if tot>0 else 0),6)},"contract":"DIRECTION_ONLY_NO_ENTRY_TIMING",
            "futures_price_source":src,"ts":now}

def _hyst(state,r):
    now=float(r["ts"]);new=r["bias"];nc=float(r["confidence"])
    old=str(getattr(state,"bias_state","ABSTAIN") or "ABSTAIN").upper();oc=float(getattr(state,"bias_confidence",0) or 0)
    cand=str(getattr(state,"_bias_flip_candidate","") or "");since=float(getattr(state,"_bias_flip_since",0) or 0)
    last=float(getattr(state,"_bias_last_supported_at",0) or 0)
    if old not in ("LONG","SHORT"):
        state._bias_flip_candidate="";state._bias_flip_since=0.
        if new in ("LONG","SHORT"):state._bias_last_supported_at=now
        return new,nc,"ACQUIRE"
    if new==old:
        state._bias_flip_candidate="";state._bias_flip_since=0.;state._bias_last_supported_at=now
        return old,nc,"STABLE"
    if new=="ABSTAIN":
        state._bias_flip_candidate="";state._bias_flip_since=0.
        return (old,oc*.82,"HOLD_THROUGH_ABSTAIN") if last>0 and now-last<=HYST_ABSTAIN_SEC else ("ABSTAIN",0.,"RELEASE_TO_ABSTAIN")
    if int(r.get("quorum",0) or 0)>=3 and nc>=HYST_FAST_FLIP_CONF:
        state._bias_flip_candidate="";state._bias_flip_since=0.;return new,nc,"FAST_CONFIRMED_FLIP"
    if cand!=new or since<=0:
        state._bias_flip_candidate=new;state._bias_flip_since=now;return old,oc*.72,"PENDING_FLIP"
    if now-since>=HYST_FLIP_SEC and nc>=.58:
        state._bias_flip_candidate="";state._bias_flip_since=0.;return new,nc,"CONFIRMED_FLIP"
    return old,oc*.72,"PENDING_FLIP"

def update_state(state,now=None,force_full=False):
    raw=evaluate(state,now,force_full);side,conf,h=_hyst(state,raw);out=dict(raw)
    out.update(raw_bias=raw["bias"],raw_confidence=raw["confidence"],bias=side,confidence=round(_cl(conf),6),hysteresis=h)
    if side in ("LONG","SHORT") and raw["bias"]==side:state._bias_last_supported_at=float(out["ts"])
    state.bias_state=side;state.bias_confidence=out["confidence"];state.bias_council=out
    state.bias_updated_at=out["ts"];state.bias_version=VERSION;state.macro_bias="NEUTRAL"
    return out
