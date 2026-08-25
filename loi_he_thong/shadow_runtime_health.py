import asyncio,logging,time
ENTRY_FAST_LAG=.20; ENTRY_SLOW_LAG=.45; MACRO_AGE=18.
GUARD_LAGS={.25:.20,1.:.40,3.:.80,10.:2.}
def fresh(ts,now,age):
    try: ts=float(ts or 0)
    except Exception:return False
    return ts>0 and 0<=now-ts<=age
def _ft(base,now):
    try:
        r=(getattr(base.app.state,"danh_sach_khop_lenh_futures",()) or ())[-1]
        ts=float(r.get("thoi_gian_ms",0) or 0)/1000; px=float(r.get("gia",0) or 0)
    except Exception:return 0.,0.
    return (px,ts) if px>0 and fresh(ts,now,5.) else (0.,ts)
def exec_price(base,now=None):
    now=time.time() if now is None else float(now); s=base.app.state
    ts=float(getattr(s,"execution_price_time",0) or 0); b=float(getattr(s,"execution_best_bid",0) or 0); a=float(getattr(s,"execution_best_ask",0) or 0)
    if fresh(ts,now,5.) and b>0 and a>b:return (b+a)/2
    return _ft(base,now)[0]
def health(base,s,now=None):
    now=time.time() if now is None else float(now); _,fts=_ft(base,now)
    bts=float(getattr(s,"execution_price_time",0) or 0); b=float(getattr(s,"execution_best_bid",0) or 0); a=float(getattr(s,"execution_best_ask",0) or 0)
    execution_bbo_ready=bool(fresh(bts,now,5.) and b>0 and a>b)
    futures_observation_ready=bool(fresh(fts,now,5.))
    fp=execution_bo_ready or futures_observation_ready
    sp=fresh(getattr(s,"thoi_gian_tick_cuoi",0),now,3.)
    cp=fresh(getattr(s,"thoi_gian_coinbase_ticker_cuoi",0) or getattr(s,"thoi_gian_coinbase_cuoi",0),now,5.)
    sf=fresh(getattr(s,"thoi_gian_dong_tien_cuoi",0),now,5.)
    ff=fresh(fts,now,5.); cf=fresh(getattr(s,"coinbase_flow_3s_ts",0),now,5.)
    mf=fresh(getattr(s,"thoi_gian_vi_mo_cuoi",0),now,MACRO_AGE)
    blockers=[]
    if bool(getattr(s,"event_loop_stalled",False)):blockers.append("event_loop_stalled")
    if bool(getattr(s,"cpu_runaway",False)):blockers.append("cpu_runaway")
    if bool(getattr(s,"journal_stalled",False)):blockers.append("journal_stalled")
    if bool(getattr(s,"supervisor_fault_latched",False)):blockers.append("supervisor_fault")
    if bool(getattr(s,"shadow_persistence_dirty",False)):blockers.append("persistence_dirty")
    if bool(getattr(s,"shadow_integrity_fault",False)):blockers.append("integrity_fault")
    live_blockers=list(blockers)
    if not bool(getattr(s,"host_cpu_entry_allowed",True)):live_blockers.append("host_cpu_budget")
    observation_feeds_ready=sp and cp and fp and sf and ff
    execution_feeds_ready=sp and cp and execution_bbo_ready and sf and ff
    observation_ready=observation_feeds_ready and not blockers
    feeds_ready=execution_feeds_ready
    ready=feeds_ready and not blockers
    live_ready=ready and not live_blockers
    out={"ts":now,"entry_ready":bool(ready),"observation_ready":bool(observation_ready),"execution_bbo_ready":execution_bbo_ready,"futures_observation_ready":futures_observation_ready,"live_entry_ready":bool(live_ready),"full_tier_s_ready":bool(ready and mf),"live_full_tier_s_ready":bool(live_ready and mf),"spot_price":sp,"coinbase_price":cp,"futures_price":fp,"spot_flow":sf,"coinbase_flow":cf,"futures_flow":ff,"macro_oi_funding":mf,"operational_blockers":bylockers,"live_operational_blockers":live_blockers}
    s.mainnet_shadow_health=out; s.mainnet_shadow_ready=bool(ready); s.mainnet_live_entry_ready=bool(live_ready); s.shadow_readiness_authoritative=True; s.system_ready=bool(ready)
    bad=[k for k,v in out.items() if k not in ("ts","entry_ready","full_tier_s_ready") and v is False]
    if ready:s.last_readiness_reason="SHADOW_READY"
    elif blockers:s.last_readiness_reason="SHADOW_OPERATIONAL_BLOCKED:"+",".join(blockers)
    else:s.last_readiness_reason="SHADOW_FEED_DEGRADED:+",".join(bad)
    return out
def safe_ref(hist,now,sec,lag):
    target=float(now)-float(sec); out=None
    for r in hist:
        try: ts=float(r.get("ts",0) or 0)
        except Exception:continue
        if ts<=target:out=r
        else:break
    if out is None:return None
    ts=float(out.get("ts",0) or 0)
    return out if 0<=target-ts<=lag else None
def install(base,risk,edge):
    orig_eval=base.entry_council.evaluate; orig_guard=base.guardian_s.update_state
    base.entry_council._ref=lambda h,n,a:safe_ref(h,n,a,ENTRY_FAST_LAG if float(a)<=.40 else ENTRY_SLOW_LAG)
    base.guardian_s._ref=lambda h,n,a:safe_ref(h,n,a,GUARD_LAGS[min(GUARD_LAGS,key=lambda x:abs(x-float(a))])
    def reset_entry(s,reason,now):
        for n in ("entry_shadow_price_history","entry_causal_flow_history"):
            h=getattr(s,n,None)
            if h is not None:
                try:h.clear()
                except Exception:setattr(s,n,None)
        s._entry_causal_context_side="ABSTAIN"; s.entry_causal_reset_reason=reason; s.entry_causal_reset_at=now
    def entry_eval(s,now=None,side=None):
        now=time.time() if now is None else float(now)
        if not health(base,s,now)["entry_ready"]:
            reset_entry(s,"SHADOW_FEED_NOT_READY",now)
            return {"version":getattr(base.entry_council,"VERSION","ENTRY"),"decision":"WAIT","entry_mode":"NONE","phase":"ARMED","confidence":0.,"reason":"SHADOW_FEED_NOT_READY","side":str(side or getattr(s,"bias_state","ABSTAIN")).upper(),"s_votes":{},"ts":nug}
        return orig_eval(s,now=now,side=side)
    base.entry_council.evaluate=entry_eval
    def guard_macro(s,pos,now):
        if fresh(getattr(s,"thoi_gian_vi_mo_cuoi",0),now,MACRO_AGE):return orig_guard(s,pos,now=now)
        h=getattr(s,"guardian_s_oi",None)
        if h is not None:
            try:h.clear()
            except Exception:pass
        oi=getattr(s,"open_interest",0)
        try:s.open_interest=0.; r=orig_guard(s,pos,now=now)
        finally:s.open_interest=oi
        v=r.get("votes") or {}; v["S3_price_x_oi"]={"status":"NEUTRAL","confidence":0.,"reason":"STALE_OI","metrics":{}}; r["votes"]=v; r["macro_fresh"]=False
        return r
    def reset_guard(s,pos,reason,now):
        for n in ("guardian_s_prices","guardian_s_oi"):
            h=getattr(s,n,None)
            if h is not None:
                try:h.clear()
                except Exception:pass
        pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.; s.guardian_s_reset_reason=reason; s.guardian_s_reset_at=now
    async def guardian_loop():
        stale=False
        while True:
            try:
                s=base.app.state; pos=getattr(s,"mainnet_shadow_position",None)
                if pos is None or not bool(getattr(pos,"active",False)):
                    base._record_guardian_latency(False); stale=False; await asyncio.sleep(.10); continue
                base._record_guardian_latency(True)
                now=time.time(); px=exec_price(base,now)
                if px<=0:
                    s.guardian_s_decision="HOLD_STALE_FUTURES_EXECUTION_PRICE"; await asyncio.sleep(base.GUARD_POLL); continue
                if not base._spot_fresh(now):
                    if not stale:reset_guard(s,pos,"SPOT_STALE",now); stale=True
                    g={"decision":"HOLD","reason":"STALE_SPOT_CAUSAL_GUARDIAN_DISABLED","votes":{},"supportive_count":0,"adverse_count":0,"ts":now}
                else:
                    if stale:reset_guard(s,pos,"SPOT_RECONECTED",now); stale=False
                    g=guard_macro(s,pos,now)
                rr=risk.assess(pos,px,g,market_state=s,now=now); s.mainnet_shadow_risk=rr
                base._record_position_state(pos,g,rr,px,now)
                if rr.get("decision")=="EXIT":await base._close_position(pos,{"decision":"EXIT","reason":rr["reason"],"risk":rr,"guardian":g},now)
                elif g.get("decision")=="EXIT" and risk.guardian_ok(g):await base._close_position(pos,g,now)
                elif g.get("decision")=="EXIT":s.guardian_s_decision="WATCH_CAUSAL_GATE"
            except asyncio.CancelledError:raise
            except Exception:
                logging.exception("[MAINNET-SHADOW] hardened guardian failure"); await asyncio.sleep(.25)
            await asyncio.sleep(base.GUARD_POLL)
    base._latest_futures_price=lambda now=None:exec_price(base,now)
    base._guardian_loop=guardian_loop
    return lambda:health(base,base.app.state,time.time())
