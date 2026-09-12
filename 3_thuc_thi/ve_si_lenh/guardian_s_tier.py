"""Minimal Tier-S causal guardian. No BOS/CHoCH/EMA/POC/OBI/wall votes."""
from collections import deque
import time

from loi_he_thong import authority_contracts, liquidation_context, market_thesis

VERSION="GUARDIAN_S_TIER_V15_CANONICAL_MARKET_TRUTH"
MIN_PRICE_BPS=1.50
MAX_PRICE_BPS=3.00
MIN_FLOW_IMB=0.20
MIN_OI_RISE_PCT=0.0085
SCOUT_PRICE_BPS=0.40
SCOUT_FLOW_IMB=0.08
STRONG_FLOW_IMB=0.55
FLOW_MATERIALITY_ALPHA=0.125
FLOW_MATERIALITY_WARMUP=8
FLOW_MATERIALITY_MIN_Z=-2.0
KILL_PRICE_BPS=2.50
TREND_KILL_PRICE_BPS=KILL_PRICE_BPS*2.0
FAST_KILL_HOLD_SECONDS=0.25
MIN_DETERIORATION_SECONDS=0.55
MAX_DETERIORATION_SECONDS=1.05
RUNNER_DETERIORATION_SECONDS=1.80
TREND_DETERIORATION_SECONDS=3.00
RUNNER_MIN_BEST_R=1.0
COINBASE_STRICT_AGE_SECONDS=2.5
RECOVERY_PHASES={
    "HEALTHY","FIRST_PULLBACK","RECOVERY_TEST","RECOVERED","FAILED_RECOVERY",
    "BREAK_PENDING","DIRECT_THESIS_BREAK",
}

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

def _reset_recovery_path(pos):
    pos.guardian_s_phase="HEALTHY"
    pos.guardian_s_pullback_started_at=0.0
    pos.guardian_s_pullback_start_price=0.0
    pos.guardian_s_worst_adverse_price=0.0
    pos.guardian_s_worst_adverse_bps=0.0
    pos.guardian_s_reclaim_peak_fraction=0.0
    pos.guardian_s_reclaim_hold_since=0.0
    pos.guardian_s_recovery_result="NONE"
    pos.guardian_s_failed_recovery_reason=None
    pos.guardian_s_pullback_flow_state="UNKNOWN"
    pos.guardian_s_pullback_opposing_flow_state="UNKNOWN"

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
        pos.guardian_s_adverse_started_at=0.0
        _reset_recovery_path(pos)
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

def _flow_materiality(state,feeds):
    """Classify venue-local executed notional against an O(1) EWMA baseline."""
    baselines=dict(getattr(state,"guardian_flow_materiality_baselines",{}) or {})
    result={}
    for venue,(_,raw_volume) in feeds.items():
        volume=max(0.0,float(raw_volume or 0.0))
        prior=dict(baselines.get(venue) or {})
        count=int(prior.get("count",0) or 0)
        mean=max(0.0,float(prior.get("mean",0.0) or 0.0))
        variance=max(0.0,float(prior.get("variance",0.0) or 0.0))
        if volume<=0.0:
            status="MISSING"; material=False; z_score=None
        elif count<FLOW_MATERIALITY_WARMUP or mean<=0.0:
            status="WARMUP_UNVERIFIED"; material=True; z_score=None
        else:
            # The variance floor prevents tiny normal changes after a flat
            # sample run from looking exceptional. Units never cross venues.
            scale=max(variance**0.5,mean*0.25,1e-12)
            z_score=(volume-mean)/scale
            material=bool(z_score>=FLOW_MATERIALITY_MIN_Z)
            status="MATERIAL" if material else "COLLAPSED"
        result[venue]={
            "status":status,"material":material,"sample_count":count,
            "volume":round(volume,8),"baseline_mean":round(mean,8),
            "z_score":round(z_score,6) if z_score is not None else None,
            "venue_local_units":True,
        }
        if volume>0.0:
            if count<=0 or mean<=0.0:
                new_mean=volume; new_variance=0.0
            else:
                delta=volume-mean
                new_mean=mean+FLOW_MATERIALITY_ALPHA*delta
                new_variance=(
                    (1.0-FLOW_MATERIALITY_ALPHA)
                    *(variance+FLOW_MATERIALITY_ALPHA*delta*delta)
                )
            baselines[venue]={
                "count":min(count+1,1_000_000),
                "mean":new_mean,"variance":new_variance,
            }
    state.guardian_flow_materiality_baselines=baselines
    return result

def _s2(state,pos,now):
    sign=_sign(pos.side); rows={}; adv=[]; support=[]
    feeds={"spot":_spot_flow(state,now),"futures":_fut_flow(state,now),"coinbase":_cb_flow(state,now)}
    materiality=_flow_materiality(state,feeds)
    for name,(imb,vol) in feeds.items():
        if vol<=0: continue
        s=imb*sign; rows[name]=round(s,4)
        if not materiality[name]["material"]:continue
        if s<=-MIN_FLOW_IMB: adv.append((name,abs(s)))
        elif s>=MIN_FLOW_IMB: support.append((name,abs(s)))
    if len(adv)>=2:
        strength=sum(v for _,v in adv)/len(adv)
        return _vote("ADVERSE",0.58+0.28*min(strength,1),"MULTI_VENUE_ADVERSE_FLOW",
                     signed_imbalances=rows,venues=[n for n,_ in adv],
                     flow_materiality=materiality)
    if len(support)>=2:
        return _vote("SUPPORTIVE",0.55,"MULTI_VENUE_SUPPORTIVE_FLOW",
                     signed_imbalances=rows,
                     venues=[n for n,_ in support],
                     flow_materiality=materiality)
    reason=("FLOW_NOT_MATERIAL" if any(
        row["status"]=="COLLAPSED" for row in materiality.values()
    ) else "FLOW_NOT_CONSENSUS")
    return _vote("NEUTRAL",0.10,reason,signed_imbalances=rows,
                 flow_materiality=materiality)

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


def _time_to_edge(pos, now, prices, s1, s2, s3, thesis):
    """Turn a cohort timeout into suspicion, never an autonomous exit."""
    entry=float(getattr(pos,"entry_price",0.0) or 0.0)
    current=float(prices.get("futures",0.0) or 0.0)
    gross=(
        _sign(getattr(pos,"side",""))*float(_bps(current,entry) or 0.0)
        if entry>0.0 and current>0.0 else None
    )
    plan=(getattr(pos,"execution_cost_plan",None)
          or getattr(pos,"shadow_cost_plan",None) or {})
    recovery_cost=float(plan.get("total_cost_bps",0.0) or 0.0)
    opened=float(getattr(pos,"opened_at",now) or now)
    elapsed=max(0.0,now-opened)
    if (
        gross is not None and gross>recovery_cost
        and getattr(pos,"edge_first_positive_net_at",None) is None
    ):
        pos.edge_first_positive_net_at=now
        pos.edge_time_to_positive_net_seconds=elapsed
    entry_thesis=dict(getattr(pos,"entry_causal_thesis",{}) or {})
    contract=dict(entry_thesis.get("time_to_edge") or {})
    p80=contract.get("p80_seconds")
    try: p80=float(p80) if p80 is not None else None
    except (TypeError,ValueError): p80=None
    authority=bool(contract.get("authority") and p80 is not None and p80>0.0)
    late=bool(
        authority and elapsed>p80 and gross is not None
        and gross<=recovery_cost
    )
    causal_confirmation=bool(
        s1.get("status")=="ADVERSE"
        or s2.get("status")=="ADVERSE"
        or s3.get("status")=="ADVERSE"
        or bool((thesis or {}).get("broken"))
    )
    return {
        "version":"TIME_TO_EDGE_V1",
        "status":"EDGE_LATE" if late else (
            "POSITIVE_NET_REACHED"
            if getattr(pos,"edge_first_positive_net_at",None) is not None
            else "BOOTSTRAP_UNVERIFIED" if not authority else "ON_TIME"
        ),
        "authority":authority,"elapsed_seconds":round(elapsed,4),
        "p80_seconds":p80,"gross_pnl_bps":(
            round(gross,6) if gross is not None else None
        ),
        "positive_net_hurdle_bps":round(recovery_cost,6),
        "causal_deterioration_confirmed":causal_confirmation,
        "can_exit_alone":False,
        "policy":"DETERIORATION_EVIDENCE_NEVER_HARD_TIMEOUT",
    }

def _entry_thesis_break(state,pos,now,s1,s2,s3):
    """Retired local-break counterfactual; never canonical Market Truth."""
    thesis=dict(getattr(pos,"entry_causal_thesis",{}) or {})
    market_contract=dict(thesis.get("market_thesis") or {})
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
        # Market Truth is owned by executed cash conversion. Derivatives and
        # OI explain urgency/mechanism, but cannot manufacture a broken cash
        # thesis when the selected cash anchor has no adverse executed flow.
        cause=bool(selected&flow_adverse)
        broken=bool(anchor_price and cause)
        reason=(
            "LEGACY_LOCAL_CASH_BREAK_SIGNAL"
            if broken else "LEGACY_LOCAL_CASH_HOLD_SIGNAL"
        )
    if reason=="GENERIC_CAUSAL_FALLBACK":
        thesis_status="UNKNOWN"
    else:
        thesis_status=(
            "NONCANONICAL_BREAK_SIGNAL"
            if broken else "NONCANONICAL_HOLD_SIGNAL"
        )
    observed_falsifiers=[]
    if broken:
        observed_falsifiers.append("PRIMARY_CASH_STOPS_OR_REVERSES_CONVERSION")
        if (
            {"spot","coinbase"}.issubset(price_adverse)
            and {"spot","coinbase"}.issubset(flow_adverse)
        ):
            observed_falsifiers.append("OPPOSITE_DUAL_CASH_CONTROL")
        if s3.get("status")=="ADVERSE":
            observed_falsifiers.append("FRESH_OPPOSITE_POSITION_BUILD")
    return {
        "broken":broken,"reason":reason,"primary_cash_anchor":primary or None,
        "cash_anchors":sorted(anchors),"available_anchors":sorted(active_anchors),
        "price_adverse":sorted(price_adverse),"flow_adverse":sorted(flow_adverse),
        "thesis_status":thesis_status,
        "market_thesis_version":market_contract.get("version"),
        "mechanism":market_contract.get("mechanism"),
        "observed_falsifiers":observed_falsifiers,
        "pnl_fields_used_for_thesis":False,
        "owner":"GUARDIAN_LEGACY_DIAGNOSTIC",
        "authority":False,
        "can_terminalize":False,
    }


def _frozen_market_thesis(pos):
    """Read the exact sealed Market Truth approved at Entry, if available."""
    entry = dict(getattr(pos, "entry_causal_thesis", {}) or {})
    handoff = dict(entry.get("entry_thesis_handoff") or {})
    expected_side = str(getattr(pos, "side", "") or "").upper()
    expected_episode = str(
        getattr(pos, "causal_episode_id", "")
        or entry.get("causal_episode_id") or ""
    )
    if authority_contracts.verify_entry_handoff(
        handoff,
        expected_side=expected_side,
        expected_episode_id=expected_episode,
    ):
        return dict(handoff.get("market_thesis") or {})
    direct = dict(entry.get("market_thesis") or {})
    if (
        authority_contracts.verify(direct)
        and direct.get("layer") == "MARKET_TRUTH"
        and str(direct.get("side") or "").upper() == expected_side
        and str(direct.get("causal_episode_id") or "") == expected_episode
    ):
        return direct
    return {}


def _fresh(ts, now, limit):
    try:
        age = float(now) - float(ts or 0.0)
    except (TypeError, ValueError):
        return False
    return float(ts or 0.0) > 0.0 and 0.0 <= age <= float(limit)


def _canonical_thesis_observation(state, pos, now, s1, s2, s3):
    """Adapt canonical runtime measurements without issuing a truth vote."""
    truth = _frozen_market_thesis(pos)
    episode_id = str(
        (truth or {}).get("causal_episode_id")
        or getattr(pos, "causal_episode_id", "") or ""
    )
    spot_fresh = bool(
        _fresh(getattr(state, "thoi_gian_tick_cuoi", 0.0), now, 3.0)
        and _fresh(getattr(state, "thoi_gian_dong_tien_cuoi", 0.0), now, 5.0)
        and str(getattr(
            state, "guardian_s_spot_flow_ordering", "MONOTONIC",
        )) == "MONOTONIC"
    )
    coinbase_fresh = bool(
        _fresh(
            getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0), now, 5.0,
        )
        and _fresh(getattr(state, "coinbase_flow_3s_ts", 0.0), now, 5.0)
    )
    futures_fresh = bool(
        _last_fut(state, now) > 0.0
        and str(getattr(
            state, "guardian_s_futures_flow_ordering", "MONOTONIC",
        )) == "MONOTONIC"
    )
    oi_fresh = _fresh(
        getattr(state, "thoi_gian_vi_mo_cuoi", 0.0), now, 18.0,
    )

    frozen_sources = dict(((truth or {}).get("source_health") or {}).get(
        "sources", {}
    ) or {})
    current_epochs = {
        "binance_spot": getattr(state, "spot_flow_epoch", None),
        "coinbase_spot": getattr(state, "coinbase_flow_epoch", None),
        "futures": getattr(state, "futures_flow_epoch", None),
    }
    epoch_changed = False
    for name, row in frozen_sources.items():
        expected = (row or {}).get("epoch")
        current = current_epochs.get(str(name))
        if expected is None or current is None:
            continue
        try:
            epoch_changed = epoch_changed or int(expected) != int(current)
        except (TypeError, ValueError):
            epoch_changed = True

    oi_metrics = dict(s3.get("metrics") or {})
    cash_wave_snapshot = dict(
        getattr(state, "cross_cash_causal_wave_shadow", {}) or {}
    )
    active_cash_wave = dict(cash_wave_snapshot.get("active_wave") or {})
    position_wave_id = str(
        getattr(pos, "market_wave_id", "")
        or getattr(pos, "causal_episode_id", "")
        or episode_id
        or ""
    )
    wave_tombstones = dict(
        getattr(state, "market_truth_wave_tombstones", {}) or {}
    )
    position_falsifier = wave_tombstones.get(position_wave_id)
    acquisition_handoff = dict(
        getattr(state, "bias_acquisition_handoff", {}) or {}
    )
    return truth, {
        "version": "GUARDIAN_CANONICAL_OBSERVATION_V1",
        "causal_episode_id": episode_id or None,
        "position_side": str(getattr(pos, "side", "") or "").upper(),
        "source_health": {
            "spot": "FRESH" if spot_fresh else "UNKNOWN",
            "coinbase": "FRESH" if coinbase_fresh else "UNKNOWN",
            "futures": "FRESH" if futures_fresh else "UNKNOWN",
        },
        "price_horizons": dict((s1.get("metrics") or {}).get("horizons") or {}),
        "flow_signed_imbalances": dict(
            (s2.get("metrics") or {}).get("signed_imbalances") or {}
        ),
        "oi": {
            "status": s3.get("status") if oi_fresh else "STALE_UNKNOWN",
            "fresh": bool(oi_fresh),
            "change_pct": oi_metrics.get("oi_pct") if oi_fresh else None,
        },
        "observed_at_ms": int(float(now) * 1000.0),
        "cash_control_wave": {
            "version": cash_wave_snapshot.get("version"),
            "causal_wave_id": active_cash_wave.get("causal_wave_id"),
            "side": active_cash_wave.get("side"),
            "state": active_cash_wave.get("state"),
            "observed_at_ms": cash_wave_snapshot.get("observed_at_ms"),
            "cash_roots": dict(active_cash_wave.get("cash_roots") or {}),
        },
        "position_wave": {
            "causal_wave_id": position_wave_id or None,
            "status": "FALSIFIED" if position_falsifier else "UNKNOWN",
            "falsifier": position_falsifier,
        },
        "control_ownership": {
            "current_bias_side": str(
                getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN"
            ).upper(),
            "acquisition_handoff": acquisition_handoff,
        },
        "gap_or_epoch_invalid": bool(
            getattr(state, "shadow_data_gap_active", False)
            or getattr(pos, "data_gap_seen", False)
            or epoch_changed
        ),
        "material_flow_imbalance": MIN_FLOW_IMB,
    }


def _canonical_thesis_action(observation):
    """Map the sole canonical truth contract into position policy."""
    status = str((observation or {}).get("status") or "UNKNOWN").upper()
    if status in {"CONTROL_TRANSFER", "FALSIFY"}:
        decision = "EXIT"
        reason = (
            "CANONICAL_MARKET_CONTROL_TRANSFER"
            if status == "CONTROL_TRANSFER"
            else "CANONICAL_MARKET_THESIS_FALSIFIED"
        )
    elif status == "DIVERGENCE":
        decision = "DETERIORATING"
        reason = "CANONICAL_MARKET_THESIS_DIVERGING"
    elif status == "SUPPORT":
        decision = "HOLD"
        reason = "CANONICAL_MARKET_THESIS_SUPPORTED"
    else:
        decision = "HOLD"
        reason = "CANONICAL_MARKET_THESIS_UNKNOWN_SAFETY_SEPARATE"
    return {
        "version": "GUARDIAN_CANONICAL_THESIS_ACTION_V1",
        "owner": "GUARDIAN_POSITION_POLICY",
        "authority": True,
        "decision": decision,
        "reason": reason,
        "thesis_status": status,
        "market_truth_owner": "MARKET_THESIS",
        "canonical_market_truth_exit_authorized": bool(
            decision == "EXIT"
            and (observation or {}).get("old_thesis_falsified") is True
        ),
        "safety_bypass_separate": True,
        "weighted_ensemble": False,
    }


def _shared_thesis_shadow_action(observation):
    """Compatibility view for old reports; it has no runtime authority."""
    action = _canonical_thesis_action(observation)
    return {
        **action,
        "version": "GUARDIAN_SHARED_THESIS_SHADOW_V1",
        "authority": False,
        "promoted_to_runtime_authority": True,
    }

def _advance_adverse_wave_ledger(pos,now,shared,recovery):
    """Observe one bounded adverse process without changing Guardian action."""
    episode_id=str(
        getattr(pos,"causal_episode_id","")
        or (getattr(pos,"entry_causal_thesis",{}) or {}).get(
            "causal_episode_id", ""
        )
        or getattr(pos,"position_cycle_id","") or ""
    )
    prior=dict(getattr(pos,"guardian_adverse_wave_ledger",{}) or {})
    if str(prior.get("causal_episode_id") or "")!=episode_id:
        prior={}
    prior_state=str(prior.get("state") or "HEALTHY")
    status=str((shared or {}).get("status") or "UNKNOWN").upper()
    reason=str((shared or {}).get("reason") or "UNKNOWN").upper()
    observation_hash=str((shared or {}).get("observation_hash") or "")
    previous_hash=str(prior.get("last_observation_hash") or "")
    distinct_evidence=bool(observation_hash and observation_hash!=previous_hash)
    recovery_phase=str((recovery or {}).get("guardian_phase") or "HEALTHY")

    if "DISCONTINUITY" in reason:
        state="INVALIDATED_SOURCE_BREAK"
    elif recovery_phase=="RECOVERED" or status=="SUPPORT":
        state="RECOVERED"
    elif recovery_phase=="RECOVERY_TEST":
        state="RECOVERY_TEST"
    elif recovery_phase=="FAILED_RECOVERY":
        state="RECOVERY_FAILED"
    elif status in {"CONTROL_TRANSFER","FALSIFY"}:
        state=(
            "TRANSFER_CONFIRMED"
            if prior_state=="TRANSFER_CANDIDATE" and distinct_evidence
            else "TRANSFER_CANDIDATE"
        )
    elif status=="DIVERGENCE" or recovery_phase in {
        "FIRST_PULLBACK","BREAK_PENDING",
    }:
        state="CHALLENGED"
    elif status=="UNKNOWN":
        # Unknown evidence neither repairs nor falsifies an existing process.
        state=prior_state
    else:
        state="HEALTHY"

    onset_ms=prior.get("onset_ms")
    if state in {"CHALLENGED","TRANSFER_CANDIDATE"} and not onset_ms:
        onset_ms=round(float(now)*1000.0,3)
    if state in {"HEALTHY","RECOVERED"}:
        onset_ms=None
    row={
        "version":"GUARDIAN_ADVERSE_WAVE_LEDGER_V1",
        "authority":False,
        "owner":"GUARDIAN_POSITION_OBSERVER",
        "causal_episode_id":episode_id or None,
        "state":state,
        "previous_state":prior_state,
        "onset_ms":onset_ms,
        "last_evidence_ms":round(float(now)*1000.0,3),
        "last_observation_hash":observation_hash or None,
        "distinct_evidence":distinct_evidence,
        "market_truth_status":status,
        "recovery_phase":recovery_phase,
        "unknown_falsifies":False,
        "time_alone_transitions":False,
        "action_policy":"SHADOW_UNTIL_SAME_WAL_GUARDIAN_PROOF",
    }
    pos.guardian_adverse_wave_ledger=row
    return row

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
    promoted_phases={"ESTABLISHED_TREND","PULLBACK_AGAINST_CONTEXT"}
    soft_promoted=bool(
        side in ("LONG","SHORT") and frozen_side=="ABSTAIN"
        and current_side==side and current_phase in promoted_phases
        and current_bias in (side,"ABSTAIN")
    )
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
    if soft_promoted:
        active=True
    return {
        "active":active,"side":side,"frozen_context_side":frozen_side,
        "frozen_phase":frozen_phase,"current_context_side":current_side,
        "current_phase":current_phase,"current_bias":current_bias,
        "soft_promoted_after_entry":soft_promoted,
        "promotion_source":(
            "CURRENT_CONFIRMED_CONTEXT" if soft_promoted else None
        ),
        "frozen_thesis_rewritten":False,
        "policy":"SOFT_CAUSAL_HOLD_ONLY_HARD_RISK_UNCHANGED",
    }

def _classify_adverse_event(state,pos,now,s1,s2,s3,profile,thesis,recovery_window=None):
    """Classify legacy deterioration for telemetry, never Market Truth."""
    primary=str(thesis.get("primary_cash_anchor") or "").lower()
    horizons=(s1.get("metrics") or {}).get("horizons") or {}
    primary_horizons=[]; dual_cash_horizons=[]
    for horizon,row in horizons.items():
        adverse=set((row or {}).get("adverse") or ())
        try: seconds=float(horizon)
        except (TypeError,ValueError): continue
        if primary in adverse: primary_horizons.append(seconds)
        if {"spot","coinbase"}.issubset(adverse):
            dual_cash_horizons.append(seconds)
    dual_cash=bool(dual_cash_horizons)
    generic_fallback=bool(
        not primary and str(thesis.get("reason") or "")=="GENERIC_CAUSAL_FALLBACK"
    )
    primary_broken=bool(
        (primary and primary_horizons) or (generic_fallback and dual_cash)
    )
    persistent_cash_acceptance=bool(
        primary_broken and any(horizon>=3.0 for horizon in dual_cash_horizons)
    )

    flow_venues=set((s2.get("metrics") or {}).get("venues") or ())
    signed_flows=(s2.get("metrics") or {}).get("signed_imbalances") or {}
    adverse_cash_flow=bool(flow_venues&{"spot","coinbase"})
    primary_flow=bool(primary and primary in flow_venues)
    original_cash_flow=bool(any(
        float(signed_flows.get(name,0.0) or 0.0)>=MIN_FLOW_IMB
        for name in ("spot","coinbase")
    ))
    futures_flow_supportive=bool(
        float(signed_flows.get("futures",0.0) or 0.0)>=MIN_FLOW_IMB
    )
    futures_price_supportive=False
    for row in horizons.values():
        moves=(row or {}).get("moves") or {}
        if float(moves.get("futures",0.0) or 0.0)>=SCOUT_PRICE_BPS:
            futures_price_supportive=True
            break
    futures_supportive=bool(futures_flow_supportive or futures_price_supportive)

    oi_metrics=s3.get("metrics") or {}
    oi_pct=oi_metrics.get("oi_pct")
    try: oi_pct=float(oi_pct)
    except (TypeError,ValueError): oi_pct=None
    if s3.get("status")=="ADVERSE":
        oi_state="OPPOSITE_POSITION_BUILD"
    elif oi_pct is not None and oi_pct<=-MIN_OI_RISE_PCT:
        oi_state="UNWIND"
    elif oi_pct is None:
        oi_state="UNKNOWN"
    else:
        oi_state="NEUTRAL"

    adverse_side="SHORT" if str(pos.side).upper()=="LONG" else "LONG"
    liquidation=liquidation_context.snapshot(state,adverse_side,now)
    liquidation_phase=str(liquidation.get("phase") or "UNKNOWN").upper()
    liquidation_flush=bool(
        oi_state=="UNWIND" and (
            liquidation.get("burst") or dual_cash or profile.get("kill_fast")
        )
    )

    price_stalled=bool(
        adverse_cash_flow and (
            not primary_broken
            or (primary_horizons and max(primary_horizons)<1.0)
        )
    )
    # Full executed depletion/refill lives in the recorder and remains
    # authority=false. Do not pretend its cross-process output is available in
    # Guardian; price non-conversion is the only hot-path absorption evidence.
    liquidity={
        "status":"PRICE_NON_CONVERSION" if price_stalled else "UNAVAILABLE",
        "depth_authority":False,
        "reason":"RECORDER_DEPTH_RESEARCH_NOT_GUARDIAN_AUTHORITY",
    }

    price_reclaim=bool(s1.get("status")=="SUPPORTIVE")
    recovery_window=dict(recovery_window or {})
    # Recovery authority belongs to the ordered path state machine below.
    # Contemporaneous evidence must never become recovery merely because a
    # timer happened to fall inside an arbitrary window.
    recovery_confirmed=False
    cross_evidence_conflict=bool(
        dual_cash and futures_supportive
        and oi_state!="OPPOSITE_POSITION_BUILD"
    )

    confirmed=bool(
        primary_broken and dual_cash and adverse_cash_flow and not price_stalled
        and (
            oi_state=="OPPOSITE_POSITION_BUILD"
            or (persistent_cash_acceptance and oi_state!="UNWIND")
        )
    )
    if recovery_confirmed:
        classification="THESIS_RECOVERY_CONFIRMED"
        reason="ADVERSE_BURST_LOST_EFFICIENCY_OR_ORIGINAL_FLOW_RECLAIMED"
    elif confirmed:
        classification="THESIS_BREAK_CONFIRMED"
        reason="PRIMARY_CASH_PLUS_INDEPENDENT_ACCEPTANCE_AND_NEW_CAUSE"
    elif cross_evidence_conflict:
        classification="CONFLICTED_CAUSAL_EVIDENCE"
        reason="CASH_ADVERSE_BUT_FUTURES_SUPPORTS_POSITION_WITHOUT_OPPOSITE_BUILD"
    elif liquidation_flush:
        classification="TRANSIENT_LIQUIDATION_FLUSH"
        reason="OI_UNWIND_WITH_FORCED_OR_CROSS_CASH_FLUSH"
    elif price_stalled or (
        liquidation_phase=="DECELERATING" and not persistent_cash_acceptance
    ):
        # Without live executed-depletion + replenishment evidence Guardian
        # can prove non-conversion, not the hidden mechanism "absorption".
        classification="FLOW_NON_CONVERSION_PULLBACK"
        reason="ADVERSE_FLOW_NOT_CONVERTING_IN_PRIMARY_CASH"
    else:
        classification="UNCERTAIN"
        reason="ADVERSE_EVENT_LACKS_INDEPENDENT_CAUSAL_CONFIRMATION"
    return {
        "classification":classification,"reason":reason,
        "primary_cash":{
            "anchor":primary or None,"broken":primary_broken,
            "generic_fallback":generic_fallback,
            "adverse_horizons_seconds":sorted(primary_horizons),
        },
        "oi":{"state":oi_state,"change_pct":oi_pct},
        "force_order":liquidation,
        "cash_acceptance":{
            "dual_cash_adverse":dual_cash,
            "dual_cash_horizons_seconds":sorted(dual_cash_horizons),
            "persistent_3s":persistent_cash_acceptance,
            "adverse_flow_venues":sorted(flow_venues),
            "primary_flow_adverse":primary_flow,
        },
        "cross_evidence":{
            "conflicted":cross_evidence_conflict,
            "futures_price_supportive":futures_price_supportive,
            "futures_flow_supportive":futures_flow_supportive,
            "opposite_oi_build":oi_state=="OPPOSITE_POSITION_BUILD",
            "no_refill_proxy":not price_stalled,
            "market_truth_authority":False,
        },
        "recovery":{
            **recovery_window,
            "confirmed":recovery_confirmed,
            "price_reclaim":price_reclaim,
            "original_cash_flow_returned":original_cash_flow,
            "adverse_flow_price_nonconversion":price_stalled,
            "refill_authority":False,
            "refill_proxy":"PRICE_NON_CONVERSION_ONLY",
        },
        "liquidity_response":liquidity,
        "kill_fast_eligible":classification=="THESIS_BREAK_CONFIRMED",
        "market_truth_owner":"MARKET_THESIS",
        "derivatives_can_create_or_rescue_direction":False,
        "policy":"NONCANONICAL_DIAGNOSTIC_NO_EXIT_AUTHORITY",
    }

def _recovery_anchor_price(prices,thesis):
    primary=str((thesis or {}).get("primary_cash_anchor") or "").lower()
    if primary in {"spot","coinbase"}:
        value=float((prices or {}).get(primary,0.0) or 0.0)
        if value>0.0:return primary,value
    for name in ("spot","coinbase"):
        value=float((prices or {}).get(name,0.0) or 0.0)
        if value>0.0:return name,value
    return None,0.0

def _pullback_reference_price(current,anchor,s1,side):
    """Reconstruct the pre-adverse cash reference from the shortest horizon."""
    if current<=0.0 or not anchor:return current
    rows=[]
    for horizon,row in ((s1.get("metrics") or {}).get("horizons") or {}).items():
        try:
            seconds=float(horizon)
            signed=float(((row or {}).get("moves") or {}).get(anchor))
        except (TypeError,ValueError):continue
        if signed<0.0:rows.append((seconds,signed))
    if not rows:return current
    signed=min(rows,key=lambda item:item[0])[1]
    raw=signed*_sign(side)
    denominator=1.0+raw/10000.0
    return current/denominator if denominator>0.0 else current

def _recovery_flow_state(s2):
    signed=(s2.get("metrics") or {}).get("signed_imbalances") or {}
    adverse=[]; favorable=[]
    for name in ("spot","coinbase"):
        try:value=float(signed.get(name,0.0) or 0.0)
        except (TypeError,ValueError):continue
        if value<=-MIN_FLOW_IMB:adverse.append(name)
        elif value>=MIN_FLOW_IMB:favorable.append(name)
    opposing=("PERSISTENT" if len(adverse)>=2 else
              "PRESENT" if adverse else "DECAYED")
    favorable_state=("MULTI_CASH_RETURN" if len(favorable)>=2 else
                     "PRIMARY_CASH_RETURN" if favorable else "ABSENT")
    return opposing,favorable_state,adverse,favorable

def _advance_recovery_path(pos,now,prices,s1,s2,s3,profile,thesis,adverse_event):
    """Remember pullback -> recovery -> failure without making time an exit rule."""
    phase=str(getattr(pos,"guardian_s_phase","HEALTHY") or "HEALTHY").upper()
    if phase not in RECOVERY_PHASES:phase="HEALTHY"
    # Restored V14 positions may carry the retired name. A break observation
    # is an exit candidate, not a permanent Market Truth tombstone.
    if phase=="DIRECT_THESIS_BREAK":phase="BREAK_PENDING"
    previous_phase=phase
    price_adverse=bool(s1.get("status")=="ADVERSE" or profile.get("active"))
    price_supportive=bool(s1.get("status")=="SUPPORTIVE")
    opposing,favorable,adverse_cash,favorable_cash=_recovery_flow_state(s2)
    conversion=("CONVERTING" if price_supportive and favorable_cash else
                "FLOW_NOT_CONVERTING" if favorable_cash else
                "PRICE_ONLY_RECLAIM" if price_supportive else "ABSENT")
    anchor,current=_recovery_anchor_price(prices,thesis)

    if phase=="RECOVERED":
        _reset_recovery_path(pos)
        phase="HEALTHY"

    if phase=="HEALTHY" and price_adverse:
        phase="FIRST_PULLBACK"
        pos.guardian_s_pullback_started_at=now
        pos.guardian_s_pullback_start_price=_pullback_reference_price(
            current,anchor,s1,getattr(pos,"side","")
        )
        pos.guardian_s_worst_adverse_price=current
        pos.guardian_s_worst_adverse_bps=0.0
        pos.guardian_s_reclaim_peak_fraction=0.0
        pos.guardian_s_reclaim_hold_since=0.0
        pos.guardian_s_recovery_result="PULLBACK_OPEN"
        pos.guardian_s_failed_recovery_reason=None
        pos.guardian_s_pullback_flow_state=conversion
        pos.guardian_s_pullback_opposing_flow_state=opposing
        if float(getattr(pos,"guardian_s_candidate_since",0.0) or 0.0)<=0.0:
            pos.guardian_s_candidate_since=now

    start=float(getattr(pos,"guardian_s_pullback_start_price",0.0) or 0.0)
    previous_worst=float(getattr(pos,"guardian_s_worst_adverse_bps",0.0) or 0.0)
    signed_move=(
        float(_bps(current,start) or 0.0)*_sign(getattr(pos,"side",""))
        if current>0.0 and start>0.0 else 0.0
    )
    current_adverse=max(0.0,-signed_move)
    new_extreme=bool(current_adverse>previous_worst+1e-9)
    if new_extreme:
        pos.guardian_s_worst_adverse_bps=current_adverse
        pos.guardian_s_worst_adverse_price=current
    worst=max(previous_worst,current_adverse)
    reclaim_fraction=(
        _clamp((worst-current_adverse)/worst) if worst>0.0 else 0.0
    )
    peak=max(
        float(getattr(pos,"guardian_s_reclaim_peak_fraction",0.0) or 0.0),
        reclaim_fraction,
    )
    pos.guardian_s_reclaim_peak_fraction=peak

    # An attempted reclaim is still a recovery test when opposing cash flow is
    # present.  Requiring that flow to disappear before opening RECOVERY_TEST
    # erased the most informative path: price tries to recover, cannot convert,
    # then loses the reclaimed area. Opposing-flow decay belongs to SUCCESS,
    # not to recognizing that a test occurred.
    recovery_attempt=bool(
        favorable_cash and (price_supportive or reclaim_fraction>0.0)
    )
    if phase in {
        "FIRST_PULLBACK","FAILED_RECOVERY","BREAK_PENDING",
    } and recovery_attempt:
        phase="RECOVERY_TEST"
        pos.guardian_s_reclaim_hold_since=now
        pos.guardian_s_recovery_result="IN_PROGRESS"

    failed_reason=None
    hold_since=float(getattr(pos,"guardian_s_reclaim_hold_since",0.0) or 0.0)
    held_across_observation=bool(hold_since>0.0 and now>hold_since)
    if phase=="RECOVERY_TEST":
        recovery_success=bool(
            held_across_observation and price_supportive and favorable_cash
            and opposing!="PERSISTENT" and conversion=="CONVERTING"
            and s3.get("status")!="ADVERSE"
        )
        lost_reclaim=bool(
            peak>0.0 and reclaim_fraction<peak and price_adverse
            and opposing=="PERSISTENT"
        )
        failed_conversion=bool(
            favorable_cash and conversion=="FLOW_NOT_CONVERTING"
            and opposing=="PERSISTENT"
        )
        if recovery_success:
            phase="RECOVERED"
            pos.guardian_s_recovery_result="SUCCESS"
            pos.guardian_s_failed_recovery_reason=None
            pos.guardian_s_signature=()
            pos.guardian_s_candidate_since=0.0
        elif (
            adverse_event.get("classification")=="THESIS_BREAK_CONFIRMED"
            or lost_reclaim or failed_conversion or (
            new_extreme and price_adverse and opposing=="PERSISTENT"
            )
        ):
            phase="FAILED_RECOVERY"
            failed_reason=("BROAD_CASH_BREAK_DURING_RECOVERY_TEST"
                           if adverse_event.get("classification")=="THESIS_BREAK_CONFIRMED" else
                           "RECLAIM_LOST_WITH_PERSISTENT_OPPOSING_FLOW"
                           if lost_reclaim else
                           "FAVORABLE_FLOW_FAILED_TO_CONVERT"
                           if failed_conversion else
                           "NEW_ADVERSE_EXTREME_AFTER_RECOVERY_ATTEMPT")
            pos.guardian_s_recovery_result="FAILED"
            pos.guardian_s_failed_recovery_reason=failed_reason

    dual_cash=bool(
        (adverse_event.get("cash_acceptance") or {}).get("dual_cash_adverse")
    )
    opposite_build=bool(
        (adverse_event.get("oi") or {}).get("state")=="OPPOSITE_POSITION_BUILD"
    )
    direct_break=bool(
        phase=="FIRST_PULLBACK"
        and adverse_event.get("classification")=="THESIS_BREAK_CONFIRMED"
        and not recovery_attempt
    )
    if direct_break:
        phase="BREAK_PENDING"
        pos.guardian_s_recovery_result="BREAK_PENDING"
        pos.guardian_s_failed_recovery_reason=(
            "BROAD_CASH_ACCEPTANCE_WITHOUT_RECOVERY"
        )
    # Revalidate an unfilled break on every observation. A later flush,
    # conflict or unknown classification cannot inherit exit authority from
    # the prior scheduler turn.
    break_revalidated=bool(
        adverse_event.get("classification")=="THESIS_BREAK_CONFIRMED"
    )
    if phase=="BREAK_PENDING" and not break_revalidated:
        phase="FIRST_PULLBACK"
        pos.guardian_s_recovery_result="BREAK_REVALIDATION_DOWNGRADED"
        pos.guardian_s_failed_recovery_reason=None
    second_adverse_kill=bool(
        previous_phase=="FAILED_RECOVERY" and phase=="FAILED_RECOVERY"
        and price_adverse and dual_cash and opposing=="PERSISTENT"
        and (new_extreme or opposite_build or bool((thesis or {}).get("broken")))
    )
    pos.guardian_s_phase=phase
    started=float(getattr(pos,"guardian_s_pullback_started_at",0.0) or 0.0)
    return {
        "guardian_phase":phase,
        "previous_phase":previous_phase,
        "pullback_start_ms":round(started*1000.0,3) if started>0.0 else None,
        "pullback_anchor":anchor,
        "pullback_start_price":start or None,
        "worst_adverse_price":float(getattr(pos,"guardian_s_worst_adverse_price",0.0) or 0.0) or None,
        "worst_adverse_bps":round(worst,6),
        "reclaim_fraction":round(reclaim_fraction,6),
        "reclaim_hold_seconds":round(max(0.0,now-hold_since),4) if hold_since>0.0 else 0.0,
        "recovery_conversion_state":conversion,
        "opposing_flow_state":opposing,
        "favorable_flow_state":favorable,
        "recovery_result":str(getattr(pos,"guardian_s_recovery_result","NONE") or "NONE"),
        "failed_recovery_reason":failed_reason or getattr(pos,"guardian_s_failed_recovery_reason",None),
        "new_adverse_extreme":new_extreme,
        "second_adverse_kill_eligible":second_adverse_kill,
        "direct_thesis_break":direct_break or phase=="BREAK_PENDING",
        "break_revalidated":break_revalidated,
        "time_only_authority":False,
        "single_venue_kill_authority":False,
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
    frozen_truth,canonical_thesis_event=_canonical_thesis_observation(
        state,pos,now,s1,s2,s3
    )
    shared_thesis_observation=market_thesis.observe(
        frozen_truth,canonical_thesis_event
    )
    canonical_thesis_action=_canonical_thesis_action(
        shared_thesis_observation
    )
    if profile["active"]:
        if float(getattr(pos,"guardian_s_scout_since",0.0) or 0.0)<=0.0:
            pos.guardian_s_scout_since=now
    else:
        pos.guardian_s_scout_since=0.0
    causal_candidate=bool(
        raw_causal_exit and thesis["broken"]
        and not external_guard["blocks_binance_only_exit"]
    )
    runner_active=bool(
        float(getattr(pos,"best_r",0.0) or 0.0)>=RUNNER_MIN_BEST_R
        and getattr(pos,"floor_r",None) is not None
    )
    trend_context=_trend_context_shield(state,pos)
    adverse_event=_classify_adverse_event(
        state,pos,now,s1,s2,s3,profile,thesis
    )
    recovery_path=_advance_recovery_path(
        pos,now,p,s1,s2,s3,profile,thesis,adverse_event
    )
    adverse_wave_ledger=_advance_adverse_wave_ledger(
        pos,now,shared_thesis_observation,recovery_path
    )
    # A local cash break that survives into a later scheduler observation is
    # revalidated by current price + executed-flow evidence, not by elapsed
    # time. The clock only proves this is a distinct observation. Flush and
    # cross-evidence conflict classifications can never inherit this path.
    candidate_since=float(
        getattr(pos,"guardian_s_candidate_since",0.0) or 0.0
    )
    local_break_revalidated=bool(
        causal_candidate
        and recovery_path["guardian_phase"]=="FIRST_PULLBACK"
        and candidate_since>0.0 and now>candidate_since
        and s1["status"]=="ADVERSE" and s2["status"]=="ADVERSE"
        and adverse_event["classification"] not in {
            "TRANSIENT_LIQUIDATION_FLUSH",
            "CONFLICTED_CAUSAL_EVIDENCE",
        }
    )
    if local_break_revalidated:
        adverse_event["local_break_revalidated"]=True
        recovery_path["guardian_phase"]="BREAK_PENDING"
        recovery_path["direct_thesis_break"]=True
        recovery_path["break_revalidated"]=True
        recovery_path["recovery_result"]="BREAK_REVALIDATED"
        recovery_path["failed_recovery_reason"]=(
            "CURRENT_CASH_BREAK_REVALIDATED"
        )
        pos.guardian_s_phase="BREAK_PENDING"
        pos.guardian_s_recovery_result="BREAK_REVALIDATED"
        pos.guardian_s_failed_recovery_reason=(
            "CURRENT_CASH_BREAK_REVALIDATED"
        )
    path_break_authorized=bool(
        adverse_event["classification"]=="THESIS_BREAK_CONFIRMED"
        or recovery_path["guardian_phase"]=="FAILED_RECOVERY"
        or recovery_path.get("break_revalidated")
    )
    # Retire the old generic price+cause exit that could bypass recovery
    # semantics. A first pullback remains deterioration until either broad
    # independent cash confirms a direct break or an actual recovery test
    # fails. Hard Risk remains outside this classifier and is unchanged.
    causal_exit=bool(causal_candidate and path_break_authorized)
    preserve_deterioration=bool(
        recovery_path["guardian_phase"] in {
            "FIRST_PULLBACK","RECOVERY_TEST","FAILED_RECOVERY",
            "BREAK_PENDING",
        }
    )
    def clear_deterioration_if_path_complete():
        if not preserve_deterioration:
            pos.guardian_s_signature=()
            pos.guardian_s_candidate_since=0.0
    adverse_event["recovery_path"]=recovery_path
    if recovery_path["guardian_phase"]=="RECOVERED":
        adverse_event["classification"]="THESIS_RECOVERY_CONFIRMED"
        adverse_event["reason"]="RECLAIM_HELD_WITH_FAVORABLE_CASH_CONVERSION"
    time_to_edge=_time_to_edge(pos,now,p,s1,s2,s3,thesis)
    trend_fast_override=bool(adverse_event["kill_fast_eligible"])
    kill_fast=bool(causal_exit and (
        (profile["kill_fast"] and trend_fast_override)
        or recovery_path["second_adverse_kill_eligible"]
    ))
    classifier_shield=bool(
        causal_exit and profile["kill_fast"] and not kill_fast
    )
    recovery_shield=bool(
        recovery_path["guardian_phase"] in {"RECOVERY_TEST","RECOVERED"}
        and adverse_event["classification"]!="THESIS_BREAK_CONFIRMED"
    )
    runner_shield=bool(causal_candidate and runner_active and not kill_fast)
    trend_shield=bool(
        causal_candidate and (trend_context["active"] or classifier_shield)
        and not kill_fast
    )
    if recovery_shield:
        causal_exit=False
    exit_profile="HOLD"
    if causal_exit:
        confirmed=[k for k in adverse if k=="S1_price_acceptance" or k in
                   ("S2_executed_flow","S3_price_x_oi")]
        conf=sum(votes[k]["confidence"] for k in confirmed)/len(confirmed)
        base_hold=max(MIN_DETERIORATION_SECONDS,min(MAX_DETERIORATION_SECONDS,1.20-0.65*conf))
        hold=(FAST_KILL_HOLD_SECONDS if kill_fast else
              max(TREND_DETERIORATION_SECONDS,base_hold)
              if trend_shield else
              max(RUNNER_DETERIORATION_SECONDS,base_hold)
              if runner_shield else base_hold)
        exit_profile=("KILL_FAST" if kill_fast else
                      "RUNNER_SHIELD" if runner_shield else
                      "TREND_SHIELD" if trend_shield else
                      "BREAK_PENDING" if recovery_path["guardian_phase"]=="BREAK_PENDING" else
                      "FAILED_RECOVERY" if recovery_path["guardian_phase"]=="FAILED_RECOVERY" else
                      "CAUSAL_CONFIRM")
        sig=tuple(sorted(confirmed))+(exit_profile,)
        if getattr(pos,"guardian_s_signature",())!=sig:
            scout_since=float(getattr(pos,"guardian_s_scout_since",0.0) or 0.0)
            pos.guardian_s_signature=sig
            if float(getattr(pos,"guardian_s_candidate_since",0.0) or 0.0)<=0.0:
                pos.guardian_s_candidate_since=scout_since if scout_since>0.0 else now
        if now-float(getattr(pos,"guardian_s_candidate_since",now) or now)>=hold:
            decision,reason="EXIT","TIER_S_PRICE_PLUS_CAUSE_EXIT"
        else:
            decision="DETERIORATING"
            reason=(
                "BREAK_REVALIDATED_AWAITING_EXIT_CONFIRMATION"
                if exit_profile=="BREAK_PENDING" else
                "TIER_S_PRICE_PLUS_CAUSE_CONVERGENCE"
            )
    elif recovery_shield:
        decision,reason,hold=(
            "HOLD",
            "THESIS_RECOVERY_SHIELD" if recovery_path["guardian_phase"]=="RECOVERED"
            else "RECOVERY_TEST_IN_PROGRESS",
            0.0,
        )
        exit_profile=("THESIS_RECOVERY" if recovery_path["guardian_phase"]=="RECOVERED"
                      else "RECOVERY_TEST")
        if recovery_path["guardian_phase"]=="RECOVERED":
            pos.guardian_s_signature=(); pos.guardian_s_candidate_since=0.0
    elif raw_causal_exit and external_guard["blocks_binance_only_exit"]:
        decision,reason,hold="DETERIORATING","BINANCE_ONLY_ADVERSE_AWAITING_EXTERNAL_OR_OI",0.0
        clear_deterioration_if_path_complete()
    elif raw_causal_exit and not thesis["broken"]:
        decision,reason,hold="DETERIORATING","ENTRY_THESIS_NOT_BROKEN",0.0
        exit_profile="THESIS_HOLDS"
        clear_deterioration_if_path_complete()
    elif causal_candidate and not path_break_authorized:
        decision,reason,hold=(
            "DETERIORATING","THESIS_BREAK_AWAITING_PATH_CONFIRMATION",0.0
        )
        exit_profile="FIRST_PULLBACK_PATH_PENDING"
    elif s1["status"]=="ADVERSE":
        decision,reason,hold="DETERIORATING","PRICE_ADVERSE_AWAITING_FLOW_OR_OI",0.0
        clear_deterioration_if_path_complete()
    elif profile["active"]:
        decision,reason,hold="DETERIORATING","EARLY_ADVERSE_SCOUT_AWAITING_CONFIRM",0.0
        exit_profile="SCOUT"
        clear_deterioration_if_path_complete()
    elif s2["status"]=="ADVERSE" and s3["status"]=="ADVERSE":
        decision,reason,hold="HOLD","FLOW_AND_OI_NOT_CONVERTED_TO_PRICE",0.0
        clear_deterioration_if_path_complete()
    elif s2["status"]=="ADVERSE" or s3["status"]=="ADVERSE":
        decision,reason,hold="HOLD","ADVERSE_CAUSE_NOT_CONVERTED_TO_PRICE",0.0
        clear_deterioration_if_path_complete()
    else:
        decision,reason,hold="HOLD","NO_TIER_S_ADVERSE_CONVERGENCE",0.0
        clear_deterioration_if_path_complete()

    if decision=="HOLD" and not recovery_shield and len(supportive)==3:
        reason="THREE_S_SUPPORTIVE"
    elif decision=="HOLD" and not recovery_shield and len(supportive)>=2:
        reason="MULTI_S_SUPPORTIVE"
    elif decision=="HOLD" and not recovery_shield and s1["status"]=="SUPPORTIVE":
        reason="PRICE_STILL_SUPPORTIVE"

    if decision=="HOLD" and time_to_edge["status"]=="EDGE_LATE":
        decision="DETERIORATING"
        reason=(
            "EDGE_LATE_WITH_CAUSAL_DETERIORATION"
            if time_to_edge["causal_deterioration_confirmed"]
            else "EDGE_LATE_AWAITING_CAUSAL_BREAK"
        )
        exit_profile="TIME_TO_EDGE_SUSPICION"

    conf=sum(votes[k]["confidence"] for k in adverse)/max(1,len(adverse))
    legacy_guardian_diagnostic={
        "version":"GUARDIAN_LEGACY_DIAGNOSTIC_V1",
        "owner":"GUARDIAN_LEGACY_DIAGNOSTIC",
        "authority":False,
        "can_terminalize":False,
        "decision":decision,
        "reason":reason,
        "exit_profile":exit_profile,
        "hold_seconds":round(hold,4),
        "local_break_revalidated":local_break_revalidated,
        "path_break_authorized":path_break_authorized,
    }
    # Runtime action is now derived exclusively from canonical Market Truth.
    # Legacy S1/S2/S3 and recovery machinery remain visible as deterioration
    # diagnostics, but can no longer manufacture thesis death or close a
    # position. Hard Risk/profit-floor authority runs outside this classifier.
    decision=canonical_thesis_action["decision"]
    reason=canonical_thesis_action["reason"]
    if (
        decision=="HOLD"
        and shared_thesis_observation["status"]=="UNKNOWN"
        and legacy_guardian_diagnostic["decision"] in {
            "DETERIORATING","EXIT",
        }
    ):
        # Invalid/missing old contracts fail closed for terminal authority,
        # while the Guardian may still expose current deterioration to Risk
        # and telemetry. This compatibility lane can never return EXIT.
        decision="DETERIORATING"
        reason="NONCANONICAL_DETERIORATION_OBSERVED_NO_EXIT_AUTHORITY"
    hold=0.0
    exit_profile=(
        "CANONICAL_MARKET_TRUTH_TERMINAL"
        if decision=="EXIT" else
        "CANONICAL_MARKET_TRUTH_DIVERGENCE"
        if decision=="DETERIORATING" else
        "CANONICAL_MARKET_TRUTH_HOLD"
    )
    shared_thesis_shadow={
        **_shared_thesis_shadow_action(shared_thesis_observation),
        "legacy_guardian_decision":legacy_guardian_diagnostic["decision"],
        "decision_match":(
            canonical_thesis_action["decision"]
            == legacy_guardian_diagnostic["decision"]
        ),
        "cutover_eligible":True,
        "cutover_blocker":None,
    }
    return {"version":VERSION,"decision":decision,"reason":reason,"side":str(pos.side).upper(),
            "thesis_status":shared_thesis_observation["status"],
            "legacy_thesis_status":thesis.get("thesis_status","UNKNOWN"),
            "shared_thesis_status":shared_thesis_observation["status"],
            "shared_thesis_observation":shared_thesis_observation,
            "canonical_thesis_event":canonical_thesis_event,
            "canonical_thesis_action":canonical_thesis_action,
            "shared_thesis_shadow":shared_thesis_shadow,
            "legacy_guardian_diagnostic":legacy_guardian_diagnostic,
            "guardian_action":decision,
            "thesis_and_capital_policy_separate":True,
            "confidence":round(conf,6),"hold_seconds":round(hold,4),"votes":votes,
            "supportive_count":len(supportive),"adverse_count":len(adverse),
            "exchange_independence":external_guard,
            "entry_thesis":thesis,"adverse_profile":profile,
            "adverse_event":adverse_event,
            "adverse_wave_ledger":adverse_wave_ledger,
            "exit_profile":exit_profile,"runner_shield_active":runner_shield,
            "trend_shield_active":trend_shield,"trend_context":trend_context,
            "recovery_shield_active":recovery_shield,
            "guardian_phase":recovery_path["guardian_phase"],
            "pullback_start_ms":recovery_path["pullback_start_ms"],
            "worst_adverse_bps":recovery_path["worst_adverse_bps"],
            "reclaim_fraction":recovery_path["reclaim_fraction"],
            "recovery_conversion_state":recovery_path["recovery_conversion_state"],
            "opposing_flow_state":recovery_path["opposing_flow_state"],
            "recovery_result":recovery_path["recovery_result"],
            "failed_recovery_reason":recovery_path["failed_recovery_reason"],
            "time_to_edge":time_to_edge,
            "kill_fast":kill_fast,"scout_since":float(getattr(pos,"guardian_s_scout_since",0.0) or 0.0) or None,
            "deterioration_since":float(getattr(pos,"guardian_s_candidate_since",0.0) or 0.0) or None,
            "deterioration_elapsed_seconds":round(max(0.0,now-float(getattr(pos,"guardian_s_candidate_since",now) or now)),4) if decision=="DETERIORATING" else 0.0,
            "prices":p,"ts":now}

def update_state(state,pos,now=None):
    r=assess(state,pos,now)
    state.guardian_s_result=r; state.guardian_s_decision=r["decision"]; state.guardian_s_updated_at=r["ts"]
    return r
