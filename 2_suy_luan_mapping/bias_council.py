"""Tier-S direction estimator only. LONG/SHORT/ABSTAIN; never entry timing."""
import time

VERSION="BIAS_COUNCIL_V7_DIRECTION_MEMORY"
LOOKBACK=60.; TRIGGER=15.; CONTEXT=180.; FAST=4.; OI_CONTEXT=300.; HMAX=1536
SPOT_AGE=3.; CB_AGE=5.; FUT_AGE=5.; OI_AGE=12.
# OI is causal context, not an entry vote.  The 60-second build floor is kept
# above the local 15-second polling noise; the 5-minute value only checks that
# a short-lived build is not sitting inside a material unwind.
MIN_MOVE=.015; MIN_OI=.02; MIN_OI_CONTEXT=.05; MIN_FLOW=.20
H_ABS=.85; H_FLIP=.55; H_CONTEXT_FLIP=2.0; H_FAST=.78
W={"S1_cross_price":1.05,"S2_price_x_oi":1.10,"S3_multi_flow":1.0}

def C(x): return max(0.,min(1.,float(x)))
def mid(b,a):
 b,a=float(b or 0),float(a or 0); return (b+a)/2 if b>0 and a>b else max(b,a)
def chg(c,o):
 c,o=float(c or 0),float(o or 0); return None if c<=0 or o<=0 else (c-o)/o*100
def vote(side="ABSTAIN",conf=0.,reason="",**m):
 return {"vote":side,"confidence":round(C(conf),6),"reason":reason,"metrics":m}
def side(x,t):
 return "ABSTAIN" if x is None or abs(x)<t else ("LONG" if x>0 else "SHORT")
def fresh(ts,now,age):
 ts=float(ts or 0); return ts>0 and 0<=now-ts<=age
def ref(hist,target,lag):
 r=None
 for x in hist:
  if float(x.get("ts",0) or 0)<=target:r=x
  else:break
 return r if r and 0<=target-float(r.get("ts",0) or 0)<=lag else None

def bucket_ref(buckets,target,lag):
 """Bounded direct lookup; never scan the long-lived bias history."""
 start=int(target)
 for offset in range(int(lag)+2):
  row=buckets.get(start-offset)
  if row is None:continue
  age=target-float(row.get("ts",0) or 0)
  if 0<=age<=lag:return row
 return None

def bias_buckets(s,now,legacy):
 buckets=getattr(s,"bias_price_buckets",None)
 if buckets is None:
  buckets={}
  # One-time migration for an already warm shadow process/test fixture.  The
  # hot path below is second-keyed and constant-time.
  for row in list(legacy or ()):
   try:buckets[int(float(row.get("ts",0) or 0))]=row
   except (AttributeError,TypeError,ValueError):pass
  s.bias_price_buckets=buckets
 last=float(getattr(s,"_bias_bucket_last_sample_at",0) or 0)
 if last>0 and now-last>SPOT_AGE:
  buckets.clear();s._bias_bucket_oldest_second=int(now)
 second=int(now);oldest=int(getattr(s,"_bias_bucket_oldest_second",second) or second)
 cutoff=second-330
 while oldest<cutoff:
  buckets.pop(oldest,None);oldest+=1
 s._bias_bucket_oldest_second=oldest
 s._bias_bucket_last_sample_at=now
 return buckets
def thr(s,spot):
 a=float(getattr(s,"atr_1m",0) or 0)
 return max(MIN_MOVE,a/spot*100*.15 if spot>0 and a>0 else 0)

def fut_price(s,now):
 if not bool(getattr(s,"_api_is_testnet",False)):
  ts=float(getattr(s,"execution_price_time",0) or 0)
  if fresh(ts,now,FUT_AGE):
   p=mid(getattr(s,"execution_best_bid",0),getattr(s,"execution_best_ask",0))
   if p>0:return p,"FUTURES_MAINNET_BBO"
 try:
  r=(getattr(s,"danh_sach_khop_lenh_futures",()) or ())[-1]
  ts=float(r.get("thoi_gian_ms",0) or 0)/1000; p=float(r.get("gia",0) or 0)
  if p>0 and fresh(ts,now,FUT_AGE):return p,"FUTURES_MAINNET_TRADE"
 except (IndexError,AttributeError,TypeError,ValueError):pass
 return 0.,"MISSING"

def price_vote(cur,old,t,fast=False):
 if old is None:return vote(reason="WARMUP_FAST" if fast else "WARMUP")
 tt=t*(1.25 if fast else 1.0); rows=[]
 for n in ("spot","coinbase","futures"):
  x=chg(cur.get(n),old.get(n)); d=side(x,tt)
  if d!="ABSTAIN": rows.append((n,d,x,C((abs(x)/tt-1)/2)))
 L=[r for r in rows if r[1]=="LONG"]; S=[r for r in rows if r[1]=="SHORT"]
 maj=L if len(L)>=2 else S if len(S)>=2 else []; opp=S if maj is L else L
 if not maj:return vote(reason="PRICE_CONFLICT" if L and S else "PRICE_INCOMPLETE")
 if fast and len(maj)!=3:return vote(reason="FAST_NEEDS_3_VENUES")
 ms=sum(r[3] for r in maj)/len(maj); os=max((r[3] for r in opp),default=0.)
 if opp and os>=.45:return vote(reason="MATERIAL_PRICE_CONFLICT")
 conf=(.60+.22*ms) if fast else (.55+.12*max(0,len(maj)-2)+.25*ms-.10*(.5+os if opp else 0))
 return vote(maj[0][1],conf,"FAST_3VENUE_PRICE" if fast else "MULTI_VENUE_PRICE",
             agreeing=len(maj),strength=round(ms,6),opposition=round(os,6))

def cash_price_vote(cur,old,t):
 """Independent cash direction; derivatives never define slow memory."""
 if old is None:return vote(reason="WARMUP_CASH_CONTEXT")
 rows=[]
 for n in ("spot","coinbase"):
  x=chg(cur.get(n),old.get(n));d=side(x,t)
  if d!="ABSTAIN":rows.append((n,d,x,C((abs(x)/t-1)/2)))
 if len(rows)!=2:return vote(reason="CASH_CONTEXT_INCOMPLETE",venues=rows)
 if rows[0][1]!=rows[1][1]:return vote(reason="CASH_CONTEXT_CONFLICT",venues=rows)
 st=sum(r[3] for r in rows)/2.
 return vote(rows[0][1],.58+.30*st,"CASH_PRICE_ALIGNED",venues=rows,strength=st)

def direction_memory(cur,context,slow,trigger,t):
 """Classify trend, pullback and reversal without turning timing into Bias."""
 c180=cash_price_vote(cur,context,t)
 c60=cash_price_vote(cur,slow,t)
 c15=cash_price_vote(cur,trigger,t)
 a,b,c=c180["vote"],c60["vote"],c15["vote"]
 if a in ("LONG","SHORT") and b==a and c==a:
  phase="ESTABLISHED_TREND";candidate="ABSTAIN"
 elif a in ("LONG","SHORT") and b==a and c in ("LONG","SHORT") and c!=a:
  phase="PULLBACK_AGAINST_CONTEXT";candidate=c
 elif a in ("LONG","SHORT") and b==c and b in ("LONG","SHORT") and b!=a:
  phase="REVERSAL_CANDIDATE";candidate=b
 elif a in ("LONG","SHORT"):
  phase="CONTEXT_WITHOUT_CONFIRMATION";candidate="ABSTAIN"
 else:
  phase="WARMUP_OR_NEUTRAL";candidate=b if b==c and b in ("LONG","SHORT") else "ABSTAIN"
 return {"version":"CASH_DIRECTION_MEMORY_V1","context_side":a,
         "candidate_side":candidate,"phase":phase,
         "cash_180s":c180,"cash_60s":c60,"cash_15s":c15,
         "policy":"180S_CONTEXT_60S_TREND_15S_TRIGGER_FUTURES_NO_AUTHORITY"}

def s1(cur,slow,trigger,fast_ref,t):
 """Bias needs a 60s base confirmed by 15s; 4s is telemetry only."""
 base=price_vote(cur,slow,t)
 confirm=price_vote(cur,trigger,t)
 fast=price_vote(cur,fast_ref,t,True)
 metrics={
  "base_60s":base,"confirm_15s":confirm,"fast_4s":fast,
  "fast_policy":"FLIP_TELEMETRY_ONLY_NO_BIAS_ACQUISITION",
 }
 if base["vote"] in ("LONG","SHORT") and confirm["vote"]==base["vote"]:
  conf=min(base["confidence"],confirm["confidence"])
  return vote(base["vote"],conf,"MULTI_HORIZON_PRICE_CONFIRMED",**metrics)
 if base["vote"] in ("LONG","SHORT") and confirm["vote"] in ("LONG","SHORT"):
  return vote(reason="PRICE_HORIZON_CONFLICT",**metrics)
 return vote(reason="PRICE_MULTI_HORIZON_INCOMPLETE",**metrics)

def s2(cur,old,t,oi_fresh,context=None):
 if not oi_fresh:return vote(reason="STALE_OI",regime="OI_UNAVAILABLE")
 if old is None:return vote(reason="WARMUP_OI",regime="WARMUP")
 p,o=chg(cur.get("spot"),old.get("spot")),chg(cur.get("oi"),old.get("oi"))
 if p is None or o is None:return vote(reason="MISSING_PRICE_OR_OI",regime="MISSING")
 context_oi=chg(cur.get("oi"),context.get("oi")) if context else None
 context_status=("BUILD" if context_oi is not None and context_oi>=MIN_OI_CONTEXT else
                 "UNWIND" if context_oi is not None and context_oi<=-MIN_OI_CONTEXT else
                 "NEUTRAL" if context_oi is not None else "WARMUP")
 d=side(p,t)
 if o>=MIN_OI and d in ("LONG","SHORT"):
  if context_status=="UNWIND":
   return vote(reason="OI_CONTEXT_CONFLICT",regime="OI_CONTEXT_CONFLICT",
               price_pct=p,oi_pct=o,oi_5m_pct=context_oi,oi_context=context_status)
  ps=C((abs(p)/t-1)/2); os=C((o/MIN_OI-1)/3); reg="NEW_LONG_BUILD" if d=="LONG" else "NEW_SHORT_BUILD"
  return vote(d,.56+.20*ps+.20*os,reg,regime=reg,price_pct=p,oi_pct=o,
              oi_5m_pct=context_oi,oi_context=context_status)
 if o<=-MIN_OI and d=="LONG":return vote(reason="SHORT_COVERING",regime="SHORT_COVERING",price_pct=p,oi_pct=o,oi_5m_pct=context_oi,oi_context=context_status)
 if o<=-MIN_OI and d=="SHORT":return vote(reason="LONG_LIQUIDATION_CLOSING",regime="LONG_LIQUIDATION_CLOSING",price_pct=p,oi_pct=o,oi_5m_pct=context_oi,oi_context=context_status)
 return vote(reason="NO_NEW_POSITION_BUILD",regime="NEUTRAL",price_pct=p,oi_pct=o,oi_5m_pct=context_oi,oi_context=context_status)

def flow_imb(s,now,fut=False):
 B=S=0.; newest=0.
 if fut:
  # The raw Futures ring is intentionally only 20s for Entry/CPU. Bias uses
  # exact 1s aggregates retained for its declared 60s horizon.
  buckets=getattr(s,"futures_flow_1s_buffer",None)
  if buckets is not None:
   cut=now-LOOKBACK
   for r in list(buckets or ()):
    try:
     ts=float(r.get("ts",0) or 0)
     if ts<cut:continue
     B+=float(r.get("buy",0) or 0);S+=float(r.get("sell",0) or 0);newest=max(newest,ts)
    except (AttributeError,TypeError,ValueError):pass
   if not fresh(newest,now,FUT_AGE):return 0.,0.
   T=B+S;return ((B-S)/T if T>0 else 0.),T
  cut=(now-LOOKBACK)*1000
  for r in list(getattr(s,"danh_sach_khop_lenh_futures",()) or ()):
   try:
    ts=float(r.get("thoi_gian_ms",0) or 0)
    if ts<cut:continue
    q=float(r.get("khoi_luong",0) or 0);newest=max(newest,ts/1000)
    if bool(r.get("ban_chu_dong",False)):S+=q
    else:B+=q
   except (AttributeError,TypeError,ValueError):pass
  if not fresh(newest,now,FUT_AGE):return 0.,0.
 else:
  cut=now-LOOKBACK
  for r in list(getattr(s,"flow_1s_buffer",()) or ()):
   try:
    if float(r.get("ts",0) or 0)>=cut:
     B+=float(r.get("buy",0) or 0);S+=float(r.get("sell",0) or 0)
   except (AttributeError,TypeError,ValueError):pass
 T=B+S;return ((B-S)/T if T>0 else 0.),T

def s3(s,now):
 rows=[]
 for n,(i,v) in {"spot":flow_imb(s,now),"futures":flow_imb(s,now,True)}.items():
  if v>0 and abs(i)>=MIN_FLOW:rows.append((n,"LONG" if i>0 else "SHORT",C(abs(i)/.35)))
 cb=float(getattr(s,"coinbase_cvd_1m",0) or 0); cv=float(getattr(s,"coinbase_volume_1m",0) or 0); ct=float(getattr(s,"thoi_gian_coinbase_cuoi",0) or 0)
 ci=cb/cv if cv>0 else 0.
 if fresh(ct,now,CB_AGE) and cv>0 and abs(ci)>=MIN_FLOW:rows.append(("coinbase","LONG" if ci>0 else "SHORT",C(abs(ci)/.35)))
 L=[r for r in rows if r[1]=="LONG"];S=[r for r in rows if r[1]=="SHORT"]
 if L and S:return vote(reason="MULTI_VENUE_FLOW_CONFLICT",venues=rows)
 a=L if len(L)>=2 else S if len(S)>=2 else []
 if not a:return vote(reason="INSUFFICIENT_FLOW_CONSENSUS",venues=rows)
 st=sum(r[2] for r in a)/len(a);return vote(a[0][1],.52+.12*max(0,len(a)-2)+.30*st,"MULTI_VENUE_FLOW",venues=rows,strength=st)

def story(sv):
 p,o,f=(sv[k]["vote"] for k in ("S1_cross_price","S2_price_x_oi","S3_multi_flow"))
 reg=(sv["S2_price_x_oi"].get("metrics") or {}).get("regime","NEUTRAL")
 if p==o==f=="LONG":return "NEW_LONG_BUILD_CONFIRMED","LONG",.08,False
 if p==o==f=="SHORT":return "NEW_SHORT_BUILD_CONFIRMED","SHORT",.08,False
 if reg=="SHORT_COVERING":
  return ("SHORT_COVERING_WITH_BUY_FLOW","LONG",-.10,False) if p==f=="LONG" else ("SHORT_COVERING_UNCONFIRMED","ABSTAIN",-.12,True)
 if reg=="LONG_LIQUIDATION_CLOSING":
  return ("LONG_LIQUIDATION_WITH_SELL_FLOW","SHORT",-.10,False) if p==f=="SHORT" else ("LONG_LIQUIDATION_UNCONFIRMED","ABSTAIN",-.12,True)
 if p==o=="LONG" and f=="SHORT":return "SELL_FLOW_ABSORBED_BY_LONG_BUILD","LONG",.04,False
 if p==o=="SHORT" and f=="LONG":return "BUY_FLOW_ABSORBED_BY_SHORT_BUILD","SHORT",.04,False
 if p in ("LONG","SHORT") and f==p:return "PRICE_FLOW_DIRECTION_OI_NEUTRAL",p,.02,False
 if f in ("LONG","SHORT") and p!=f:return "FLOW_NOT_CONVERTED_TO_PRICE","ABSTAIN",-.08,True
 return "MIXED_OR_INCOMPLETE","ABSTAIN",0.,False

def combine(sv,st):
 L=[(k,v) for k,v in sv.items() if v["vote"]=="LONG"];S=[(k,v) for k,v in sv.items() if v["vote"]=="SHORT"]
 def sc(d):return sum(W[k]*v["confidence"] for k,v in sv.items() if v["vote"]==d)
 ls,ss=sc("LONG"),sc("SHORT"); name,sd,bonus,veto=st
 if veto:return "ABSTAIN",0.,0,"CAUSAL_STORY_VETO",ls,ss
 if len(L)>=2: side_,sup,opp="LONG",L,S
 elif len(S)>=2: side_,sup,opp="SHORT",S,L
 else:return "ABSTAIN",0.,max(len(L),len(S)),"INSUFFICIENT_S_QUORUM",ls,ss
 if sd not in ("ABSTAIN",side_):return "ABSTAIN",0.,len(sup),"STORY_DIRECTION_CONFLICT",ls,ss
 absorb=name in ("SELL_FLOW_ABSORBED_BY_LONG_BUILD","BUY_FLOW_ABSORBED_BY_SHORT_BUILD")
 if opp and not absorb and max(v["confidence"] for _,v in opp)>=.75:return "ABSTAIN",0.,len(sup),"STRONG_S_OPPOSITION",ls,ss
 base=sum(W[k]*v["confidence"] for k,v in sup)/sum(W[k] for k,_ in sup)+(.06 if len(sup)==3 else 0)-sum(.10*v["confidence"] for _,v in opp)+bonus
 return side_,C(base),len(sup),name,ls,ss

def evaluate(s,now=None,force_full=False):
 now=time.time() if now is None else float(now)
 sf=fresh(getattr(s,"thoi_gian_tick_cuoi",0),now,SPOT_AGE)
 spot=mid(getattr(s,"best_bid",0),getattr(s,"best_ask",0)) if sf else 0.
 ct=float(getattr(s,"thoi_gian_coinbase_ticker_cuoi",0) or 0) or float(getattr(s,"thoi_gian_coinbase_cuoi",0) or 0)
 cb=float(getattr(s,"coinbase_price",0) or 0) if fresh(ct,now,CB_AGE) else 0.
 fut,src=fut_price(s,now)
 mt=float(getattr(s,"thoi_gian_vi_mo_cuoi",0) or 0); mf=fresh(mt,now,OI_AGE)
 oi=float(getattr(s,"open_interest",0) or 0) if mf else 0.
 h=getattr(s,"bias_price_history",None)
 buckets=bias_buckets(s,now,h)
 slow=bucket_ref(buckets,now-LOOKBACK,3.)
 trigger=bucket_ref(buckets,now-TRIGGER,3.)
 context=bucket_ref(buckets,now-CONTEXT,5.)
 fast_ref=bucket_ref(buckets,now-FAST,1.25)
 oi_context=bucket_ref(buckets,now-OI_CONTEXT,20.)
 cur={"ts":now,"spot":spot,"coinbase":cb,"futures":fut,"oi":oi};buckets[int(now)]=cur;t=thr(s,spot)
 sv={"S1_cross_price":(s1(cur,slow,trigger,fast_ref,t) if sf else vote(reason="STALE_SPOT")),"S2_price_x_oi":s2(cur,slow,t,mf,oi_context),"S3_multi_flow":s3(s,now)}
 st=story(sv); bias,conf,q,reason,ls,ss=combine(sv,st);total=ls+ss
 memory=direction_memory(cur,context,slow,trigger,t)
 return {"version":VERSION,"bias":bias,"confidence":round(conf,6),"quorum":q,"reason":reason,"mode":"FULL",
 "story":{"name":st[0],"direction":st[1],"confidence_adjustment":st[2],"veto":st[3]},"s_votes":sv,
 "a_votes":{"A1_funding_basis":vote(reason="CONTEXT_ONLY"),"A2_spot_lead":vote(reason="CONTEXT_ONLY")},
 "direction_scores":{"long":round(ls,6),"short":round(ss,6),"margin":round(C(abs(ls-ss)/total if total>0 else 0),6)},
 "direction_memory":memory,
 "freshness":{"spot":sf,"coinbase":fresh(ct,now,CB_AGE),"futures":fut>0,"oi_macro":mf},
 "contract":"DIRECTION_ONLY_NO_ENTRY_TIMING","futures_price_source":src,"ts":now}

def _hyst(s,r):
 now=float(r["ts"]);new=r["bias"];nc=float(r["confidence"]);old=str(getattr(s,"bias_state","ABSTAIN") or "ABSTAIN").upper();oc=float(getattr(s,"bias_confidence",0) or 0)
 cand=str(getattr(s,"_bias_flip_candidate","") or "");since=float(getattr(s,"_bias_flip_since",0) or 0);last=float(getattr(s,"_bias_last_supported_at",0) or 0)
 memory=r.get("direction_memory") or {};context=str(memory.get("context_side","ABSTAIN"));phase=str(memory.get("phase",""))
 if old not in ("LONG","SHORT"):
  # Do not acquire the opposite side in a mere pullback of established cash
  # direction. Reversal candidates must first survive the transition latch.
  if new in ("LONG","SHORT") and context in ("LONG","SHORT") and new!=context:
   if phase!="REVERSAL_CANDIDATE":
    s._bias_flip_candidate="";s._bias_flip_since=0.
    return "ABSTAIN",0.,"CONTEXT_OPPOSES_ACQUISITION"
   if cand!=new or since<=0:
    s._bias_flip_candidate=new;s._bias_flip_since=now
    return "ABSTAIN",0.,"CONTEXT_REVERSAL_ACQUIRE_CANDIDATE"
   if now-since<H_CONTEXT_FLIP or nc<.62:
    return "ABSTAIN",0.,"CONTEXT_REVERSAL_ACQUIRE_PENDING"
   s._bias_flip_candidate="";s._bias_flip_since=0.;s._bias_last_supported_at=now
   return new,nc,"CONTEXT_REVERSAL_ACQUIRED"
  s._bias_flip_candidate="";s._bias_flip_since=0.
  if new in ("LONG","SHORT"):s._bias_last_supported_at=now
  return new,nc,"ACQUIRE"
 if new==old:
  s._bias_flip_candidate="";s._bias_flip_since=0.;s._bias_last_supported_at=now;return old,nc,"STABLE"
 if new=="ABSTAIN":
  s._bias_flip_candidate="";s._bias_flip_since=0.
  return (old,oc*.82,"HOLD_THROUGH_ABSTAIN") if last>0 and now-last<=H_ABS else ("ABSTAIN",0.,"RELEASE_TO_ABSTAIN")
 confirmed=(r.get("story") or {}).get("name") in ("NEW_LONG_BUILD_CONFIRMED","NEW_SHORT_BUILD_CONFIRMED")
 if context==old and new!=old:
  if phase!="REVERSAL_CANDIDATE":
   s._bias_flip_candidate="";s._bias_flip_since=0.
   return old,oc*.88,"HOLD_CONTEXT_PULLBACK"
  if cand!=new or since<=0:
   s._bias_flip_candidate=new;s._bias_flip_since=now
   return old,oc*.82,"CONTEXT_REVERSAL_CANDIDATE"
  # Two seconds of 60s+15s cash agreement is short enough for an event-driven
  # reversal, but long enough that one sweep cannot flip the background Bias.
  if now-since<H_CONTEXT_FLIP or nc<.62:
   return old,oc*.82,"CONTEXT_REVERSAL_PENDING"
  s._bias_flip_candidate="";s._bias_flip_since=0.;s._bias_last_supported_at=now
  return new,nc,"CONTEXT_REVERSAL_CONFIRMED"
 if int(r.get("quorum",0) or 0)>=3 and nc>=H_FAST and confirmed:
  s._bias_flip_candidate="";s._bias_flip_since=0.;s._bias_last_supported_at=now;return new,nc,"FAST_CONFIRMED_FLIP"
 if cand!=new or since<=0:s._bias_flip_candidate=new;s._bias_flip_since=now;return old,oc*.72,"PENDING_FLIP"
 if now-since>=H_FLIP and nc>=.58:
  s._bias_flip_candidate="";s._bias_flip_since=0.;s._bias_last_supported_at=now;return new,nc,"CONFIRMED_FLIP"
 return old,oc*.72,"PENDING_FLIP"

def update_state(s,now=None,force_full=False):
 raw=evaluate(s,now,force_full);sd,cf,hy=_hyst(s,raw);out=dict(raw)
 out.update(raw_bias=raw["bias"],raw_confidence=raw["confidence"],bias=sd,confidence=round(C(cf),6),hysteresis=hy)
 if sd in ("LONG","SHORT") and raw["bias"]==sd:s._bias_last_supported_at=float(out["ts"])
 s.bias_state=sd;s.bias_confidence=out["confidence"];s.bias_council=out;s.bias_updated_at=out["ts"];s.bias_version=VERSION;s.macro_bias="NEUTRAL"
 return out
