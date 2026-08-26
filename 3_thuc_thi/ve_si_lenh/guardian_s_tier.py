"""Minimal Tier-S causal guardian. No BOS/CHoCH/EMA/POC/OBI/wall votes."""
from collections import deque
import time

VERSION="GUARDIAN_S_TIER_V6_SCOUT_CONFIRM_RUNNER_SHIELD"
MIN_PRICE_BPS=1.50
MAX_PRICE_BPS=3.00
MIN_FLOW_IMB=0.20
MIN_OI_RISE_PCT=0.0085
SCOUT_PRICE_BPS=0.40
SCOUT_FLOW_IMB=0.08
STRONG_FLOW_IMB=0.55
KILL_PRICE_BPS=2.50
TREND_KILL_PRICE_BPS=KILL_PRICE_BPS*2.0
FAST_KILL_HOLD_SECONDS=0.25
MIN_DETERIORATION_SECONDS=0.55
MAX_DETERIORATION_SECONDS=1.05
RUNNER_DETERIORATION_SECONDS=1.80
RUNNER_MIN_BEST_R=1.0
COINBASE_STRICT_AGE_SECONDS=2.5

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
        pos.guardian_s_scout_since=0.0
    return state.guardian_s_prices,state.guardian_s_oi

def _sample(state,pos,now):
    ph,oh=_ensure(state,pos); p=_prices(state,now)
    if not ph or now-float(ph[-1]["ts"])>=0.05: ph.append({"ts":now,**p})
    oi=float(getattr(state,"open_interest",0) or 0)
    if oi>0 and (not oh or now-float(oh[-1]["ts"])>=1.0):
        oh.append({"ts":now,"oi":oi,"spot":p["spot"]})
    return p,ph,oh

def _ref(hist,now,sec):
    target=now-sec
    for r in reversed(hist):
        if float(r["ts"])<=target:return r
    return None

def _threshold(state,spot,now=None):
    now=time.time() if now is None else float(now)
    atr=float(getattr(state,"atr_1m",0) or 0)
    atr_ts=float(getattr(state,"atr_1m_updated_at",0) or 0)
    atr_age=now-atr_ts if atr_ts>0 else float("inf")
    if atr_age<0 or atr_age>120.0:
        atr=0.0
    range_proxy=atr/spot*10000 if spot>0 and atr>0 else 0
    dyn=range_proxy*0.25 if range_proxy>0 else MIN_PRICE_BPS
    return max(MIN_PRICE_BPS,min(MAX_PRICE_BPS,dyn))

def _s1(state,pos,now,p,ph):
    sign=_sign(pos.side); base=_threshold(state,p["spot"],now)
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
        cash_adverse=bool(set(adv) & {"spot","coinbase"})
        cash_supportive=bool(set(sup) & {"spot","coinbase"})
        if len(adv)>=2 and cash_adverse and (h>=1.0 or len(adv)==3):
            adverse_hits.append((h,len(adv)))
        if len(sup)>=2 and cash_supportive and (h>=1.0 or len(sup)==3):
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
    rows=getattr(state,"flow_1s_buffer",()) or ()
    buy=sell=0.0; cutoff=now-3; previous_ts=None
    for r in reversed(rows):
        try:
            ts=float(r.get("ts",0) or 0)
            if previous_ts is not None and ts>previous_ts:
                # Keep Guardian fail-neutral on malformed ordering. It must not
                # manufacture an adverse/supportive vote from an ambiguous tail.
                state.guardian_s_spot_flow_ordering="DISORDERED_NEUTRAL"
                return (0.0,0.0)
            previous_ts=ts
            if ts<cutoff: break
            buy+=float(r.get("buy",0) or 0); sell+=float(r.get("sell",0) or 0)
        except Exception:
            state.guardian_s_spot_flow_ordering="INVALID_NEUTRAL"
            return (0.0,0.0)
    state.guardian_s_spot_flow_ordering="MONOTONIC"
    return _imb(buy,sell)

def _fut_flow(state,now):
    buy=sell=0.0; newest=0.0; cutoff=(now-3)*1000
    rows=getattr(state,"danh_sach_khop_lenh_futures",()) or ()
    previous_ts=None
    for r in reversed(rows):
        try:
            ts=float(r.get("thoi_gian_ms",0) or 0)
            if previous_ts is not None and ts>previous_ts:
                state.guardian_s_futures_flow_ordering="DISORDERED_NEUTRAL"
                return (0.0,0.0)
            previous_ts=ts
            if ts<cutoff: break
            q=float(r.get("khoi_luong",0) or 0); newest=max(newest,ts/1000)
            if bool(r.get("ban_chu_dong",False)): sell+=q
            else: buy+=q
        except Exception:
            state.guardian_s_futures_flow_ordering="INVALID_NEUTRAL"
            return (0.0,0.0)
    state.guardian_s_futures_flow_ordering="MONOTONIC"
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

def _external_guard(state,now,s1,s2,s3):
    cbts=float(getattr(state,"thoi_gian_coinbase_ticker_cuoi",0) or 0)
    cb_age=(now-cbts) if cbts>0 and now>=cbts else None
    cb_strict_fresh=bool(cb_age is not None and cb_age<=COINBASE_STRICT_AGE_SECONDS)
    price_adverse=any(
        "coinbase" in (horizon.get("adverse") or ())
        for horizon in ((s1.get("metrics") or {}).get("horizons") or {}).values()
    )
    flow_adverse="coinbase" in ((s2.get("metrics") or {}).get("venues") or ())
    oi_adverse=s3.get("status")=="ADVERSE"
    external_adverse=bool(price_adverse or flow_adverse)
    # Missing/degraded Coinbase must not disable the position safety path.
    # We only suppress a normal causal EXIT when fresh external cash data is
    # actively available and fails to corroborate a Binance-only sweep.
    blocks_binance_only_exit=bool(cb_strict_fresh and not external_adverse and not oi_adverse)
    return {
        "coinbase_strict_fresh":cb_strict_fresh,
        "coinbase_age_s":round(cb_age,4) if cb_age is not None else None,
        "coinbase_price_adverse":price_adverse,
        "coinbase_flow_adverse":flow_adverse,
        "oi_adverse":oi_adverse,
        "external_adverse":external_adverse,
        "blocks_binance_only_exit":blocks_binance_only_exit,
    }

def _adverse_profile(s1,s2):
    """Reuse the old sensitive Guardian as a scout, never as exit authority."""
    price_rows=[]
    kill_price=False
    trend_kill_price=False
    for horizon,payload in ((s1.get("metrics") or {}).get("horizons") or {}).items():
        moves=(payload or {}).get("moves") or {}
        adverse=[name for name,value in moves.items() if float(value or 0.0)<=-SCOUT_PRICE_BPS]
        if len(adverse)>=2 and set(adverse)&{"spot","coinbase"}:
            price_rows.append({"horizon":horizon,"venues":adverse})
        try: fast_horizon=float(horizon)<=1.0
        except (TypeError,ValueError): fast_horizon=False
        if fast_horizon and set(adverse)=={"spot","coinbase","futures"}:
            kill_price=kill_price or any(
                float(moves.get(name,0.0) or 0.0)<=-KILL_PRICE_BPS
                for name in adverse
            )
            # A verified trend must not be cancelled by the same small
            # arbitrage echo appearing on three venues.  Fast override remains
            # available for a materially larger cash displacement; hard risk
            # and feed safety are separate authorities and are unchanged.
            trend_kill_price=trend_kill_price or any(
                float(moves.get(name,0.0) or 0.0)<=-TREND_KILL_PRICE_BPS
                for name in ("spot","coinbase")
            )

    signed=(s2.get("metrics") or {}).get("signed_imbalances") or {}
    flow_adverse=[name for name,value in signed.items() if float(value or 0.0)<=-SCOUT_FLOW_IMB]
    strong_flow=[name for name,value in signed.items() if float(value or 0.0)<=-STRONG_FLOW_IMB]
    flow_ready=bool(len(flow_adverse)>=2 and set(flow_adverse)&{"spot","coinbase"})
    scout=bool(price_rows and flow_ready)
    kill_fast=bool(
        scout and kill_price and len(strong_flow)>=2
        and set(strong_flow)&{"spot","coinbase"}
    )
    return {
        "active":scout,"price_rows":price_rows,
        "flow_adverse":flow_adverse,"strong_flow_adverse":strong_flow,
        "kill_fast":kill_fast,"trend_kill_price":trend_kill_price,
        "policy":"SENSITIVE_SCOUT_STRONG_CAUSAL_CONFIRM_ONLY",
    }

def _entry_thesis_break(state,pos,now,s1,s2,s3):
    thesis=dict(getattr(pos,"entry_causal_thesis",{}) or {})
    primary=str(thesis.get("primary_cash_anchor") or "").lower()
    anchors=set(thesis.get("cash_anchors") or ())&{"spot","coinbase"}
    if primary in {"spot","coinbase"}: anchors.add(primary)

    price_adverse=set()
    for row in ((s1.get("metrics") or {}).get("horizons") or {}).values():
        price_adverse.update((row or {}).get("adverse") or ())
    flow_adverse=set((s2.get("metrics") or {}).get("venues") or ())
    cbts=float(getattr(state,"thoi_gian_coinbase_ticker_cuoi",0) or 0)
    available={"spot"}
    if cbts>0 and 0<=now-cbts<=5.0: available.add("coinbase")
    active_anchors=anchors&available

    # Old/restored positions without thesis metadata retain the proven generic
    # causal safety path. A missing external anchor must never disable exits.
    if not anchors or not active_anchors:
        broken=True; reason="GENERIC_CAUSAL_FALLBACK"
    else:
        selected={primary} if primary in active_anchors else active_anchors
        anchor_price=bool(selected&price_adverse)
        cause=bool(selected&flow_adverse or "futures" in flow_adverse or s3.get("status")=="ADVERSE")
        broken=bool(anchor_price and cause)
        reason="ENTRY_CASH_THESIS_BROKEN" if broken else "ENTRY_CASH_THESIS_HOLDS"
    return {
        "broken":broken,"reason":reason,"primary_cash_anchor":primary or None,
        "cash_anchors":sorted(anchors),"available_anchors":sorted(active_anchors),
        "price_adverse":sorted(price_adverse),"flow_adverse":sorted(flow_adverse),
    }

def _trend_context_shield(state,pos):
    """Let a verified frozen trend survive normal noise, never hard danger."""
    thesis=dict(getattr(pos,"entry_causal_thesis",{}) or {})
    frozen=dict(thesis.get("bias_thesis") or {})
    current=dict(getattr(state,"bias_council",{}) or {})
    memory=dict(current.get("direction_memory") or {})
    side=str(getattr(pos,"side","") or "").upper()
    frozen_side=str(frozen.get("context_side") or "ABSTAIN").upper()
    frozen_phase=str(frozen.get("phase") or "").upper()
    current_side=str(memory.get("context_side") or "ABSTAIN").upper()
    current_phase=str(memory.get("phase") or "").upper()
    current_bias=str(current.get("bias") or "ABSTAIN").upper()
    eligible_phases={
        "ESTABLISHED_TREND","PULLBACK_AGAINST_CONTEXT",
        "CONTEXT_WITHOUT_CONFIRMATION",
    }
    active=bool(
        side in ("LONG","SHORT") and frozen_side==side
        and frozen_phase in eligible_phases and current_side==side
        and current_phase!="REVERSAL_CANDIDATE"
        and current_bias in (side,"ABSTAIN")
    )
    # A transient neutral/ABSTAIN state is not an independently confirmed
    # reversal.  Preserve the immutable entry thesis through that uncertainty;
    # an opposing context or REVERSAL_CANDIDATE still disables the shield.
    if (
        not active and side in ("LONG","SHORT") and frozen_side==side
        and frozen_phase in eligible_phases and current_side=="ABSTAIN"
        and current_phase!="REVERSAL_CANDIDATE"
        and current_bias in (side,"ABSTAIN")
    ):
        active=True
    return {
        "active":active,"side":side,"frozen_context_side":frozen_side,
        "frozen_phase":frozen_phase,"current_context_side":current_side,
        "current_phase":current_phase,"current_bias":current_bias,
        "policy":"SOFT_CAUSAL_HOLD_ONLY_HARD_RISK_UNCHANGED",
    }

def assess(state,pos,now=None):
    now=time.time() if now is None else float(now)
    p,ph,oh=_sample(state,pos,now)
    votes={"S1_price_acceptance":_s1(state,pos,now,p,ph),
           "S2_executed_flow":_s2(state,pos,now),
           "S3_price_x_oi":_s3(pos,now,p,oh)}
    adverse=[k for k,v in votes.items() if v["status"]=="ADVERSE"]
    supportive=[k for k,v in votes.items() if v["status"]=="SUPPORTIVE"]
    s1,s2,s3=votes["S1_price_acceptance"],votes["S2_executed_flow"],votes["S3_price_x_oi"]

    raw_causal_exit=(s1["status"]=="ADVERSE" and
                     (s2["status"]=="ADVERSE" or s3["status"]=="ADVERSE"))
    external_guard=_external_guard(state,now,s1,s2,s3)
    profile=_adverse_profile(s1,s2)
    thesis=_entry_thesis_break(state,pos,now,s1,s2,s3)
    if profile["active"]:
        if float(getattr(pos,"guardian_s_scout_since",0.0) or 0.0)<=0.0:
            pos.guardian_s_scout_since=now
    else:
        pos.guardian_s_scout_since=0.0
    causal_exit=bool(
        raw_causal_exit and thesis["broken"]
        and not external_guard["blocks_binance_only_exit"]
    )
    runner_active=bool(
        float(getattr(pos,"best_r",0.0) or 0.0)>=RUNNER_MIN_BEST_R
        and getattr(pos,"floor_r",None) is not None
    )
    trend_context=_trend_context_shield(state,pos)
    trend_fast_override=bool(
        profile.get("trend_kill_price") or s3.get("status")=="ADVERSE"
    )
    kill_fast=bool(
        causal_exit and profile["kill_fast"]
        and (not trend_context["active"] or trend_fast_override)
    )
    runner_shield=bool(causal_exit and runner_active and not kill_fast)
    trend_shield=bool(causal_exit and trend_context["active"] and not kill_fast)
    exit_profile="HOLD"
    if causal_exit:
        confirmed=[k for k in adverse if k=="S1_price_acceptance" or k in
                   ("S2_executed_flow","S3_price_x_oi")]
        conf=sum(votes[k]["confidence"] for k in confirmed)/len(confirmed)
        base_hold=max(MIN_DETERIORATION_SECONDS,min(MAX_DETERIORATION_SECONDS,1.20-0.65*conf))
        hold=(FAST_KILL_HOLD_SECONDS if kill_fast else
              max(RUNNER_DETERIORATION_SECONDS,base_hold)
              if (runner_shield or trend_shield) else base_hold)
        exit_profile=("KILL_FAST" if kill_fast else "RUNNER_SHIELD" if runner_shield
                      else "TREND_SHIELD" if trend_shield else "CAUSAL_CONFIRM")
        sig=tuple(sorted(confirmed))+(exit_profile,)
        if getattr(pos,"guardian_s_signature",())!=sig:
            scout_since=float(getattr(pos,"guardian_s_scout_since",0.0) or 0.0)
            pos.guardian_s_signature=sig
            pos.guardian_s_candidate_since=scout_since if scout_since>0.0 else now
        if now-float(getattr(pos,"guardian_s_candidate_since",now) or now)>=hold:
            decision,reason="EXIT","TIER_S_PRICE_PLUS_CAUSE_EXIT"
        else:
            decision,reason="DETERIORATING","TIER_S_PRICE_PLUS_CAUSE_CONVERGENCE"
    elif raw_causal_exit and external_guard["blocks_binance_only_exit"]:
        decision,reason,hold="DETERIORATING","BINANCE_ONLY_ADVERSE_AWAITING_EXTERNAL_OR_OI",0.0
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif raw_causal_exit and not thesis["broken"]:
        decision,reason,hold="DETERIORATING","ENTRY_THESIS_NOT_BROKEN",0.0
        exit_profile="THESIS_HOLDS"
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif s1["status"]=="ADVERSE":
        decision,reason,hold="DETERIORATING","PRICE_ADVERSE_AWAITING_FLOW_OR_OI",0.0
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif profile["active"]:
        decision,reason,hold="DETERIORATING","EARLY_ADVERSE_SCOUT_AWAITING_CONFIRM",0.0
        exit_profile="SCOUT"
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif s2["status"]=="ADVERSE" and s3["status"]=="ADVERSE":
        decision,reason,hold="HOLD","FLOW_AND_OI_NOT_CONVERTED_TO_PRICE",0.0
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
            "exchange_independence":external_guard,
            "entry_thesis":thesis,"adverse_profile":profile,
            "exit_profile":exit_profile,"runner_shield_active":runner_shield,
            "trend_shield_active":trend_shield,"trend_context":trend_context,
            "kill_fast":kill_fast,"scout_since":float(getattr(pos,"guardian_s_scout_since",0.0) or 0.0) or None,
            "deterioration_since":float(getattr(pos,"guardian_s_candidate_since",0.0) or 0.0) or None,
            "deterioration_elapsed_seconds":round(max(0.0,now-float(getattr(pos,"guardian_s_candidate_since",now) or now)),4) if causal_exit else 0.0,
            "prices":p,"ts":now}

def update_state(state,pos,now=None):
    r=assess(state,pos,now)
    state.guardian_s_result=r; state.guardian_s_decision=r["decision"]; state.guardian_s_updated_at=r["ts"]
    return r
