"""Tier-S direction only: LONG/SHORT/ABSTAIN. No entry timing authority."""
from collections import deque
import time
VERSION="BIAS_COUNCIL_V4_CAUSAL_STORY"
LOOKBACK=15.; FAST=4.; HMAX=192
SPOT_AGE=3.; CB_AGE=5.; FUT_AGE=5.; OI_AGE=12.
MIN_MOVE=.015; MIN_OI=.015; MIN_FLOW=.05
H_ABS=.85; H_FLIP=.55; H_FAST=.78
W={"S1_cross_price":1.05,"S2_price_x_oi":1.10,"S3_multi_flow":1.0}

def _c(x): return max(0.,min(1.,float(x)))
def _mid(b,a):
 b,a=float(b or 0),float(a or 0); return (b+a)/2 if b>0 and a>b else max(b,a)
def _chg(c,o):
 c,o=float(c or 0),float(o or 0); return None if c<=0 or o<=0 else (c-o)/o*100
def _v(side="ABSTAIN",conf=0.,reason="",**m): return {"vote":side,"confidence":round(_c(conf),6),"reason":reason,"metrics":m}
def _side(x,t): return "ABSTAIN" if x is None or abs(x)<t else ("LONG" if x>0 else "SHORT")
def _ex(x,t): return 0. if x is None else _c((abs(x)/t-1)/2)
def _fresh(ts,now,age):
 ts=float(ts or 0); return ts>0 and 0<=now-ts<=age
def _ref(h,target,lag):
 r=None
 for x in h:
  if float(x.get("ts",0) or 0)<=target:r=x
  else:break
 return r if r is not None and 0<=target-float(r.get("ts",0) or 0)<=lag else None

def _fut(s,now):
 if not bool(getattr(s,"_api_is_testnet",False)):
  ts=float(getattr(s,"execution_price_time",0) or 0)
  if _fresh(ts,now,FUT_AGE):
   p=_mid(getattr(s,"execution_best_bid",0),getattr(s,"execution_best_ask",0))
   if p>0:return p,"FUTURES_MAINNET_BBO"
 try:
  r=(getattr(s,"danh_sach_khop_lenh_futures",()) or ())[-1]; ts=float(r.get("thoi_gian_ms",0) or 0)/1000; p=float(r.get("gia",0) or 0)
  if p>0 and _fresh(ts,now,FUT_AGE):return p,"FUTURES_MAINNET_TRADE"
 except (IndexError,AttributeError,TypeError,ValueError):pass
 return 0.,"MISSING"

def _thr(s,spot):
 a=float(getattr(s,"atr_1m",0) or 0); return max(MIN_MOVE,a/spot*100*.15 if spot>0 and a>0 else 0)

def _pv(cur,ref,t,fast=False):
 if ref is None:return _v(reason="WARMUP_FAST_PRICE_HISTORY" if fast else "WARMUP_PRICE_HISTORY")
 t*=1.25 if fast else 1.; rows=[]; ch={}
 for n in ("spot","coinbase","futures"):
  x=_chg(cur.get(n),ref.get(n)); ch[n]=x; d=_side(x,t)
  if d!="ABSTAIN":rows.append((n,d,x,_ex(x,t)))
 L=[r for r in rows if r[1]=="LONG"]; S=[r for r in rows if r[1]=="SHORT"]; maj=L if len(L)>=2 else S if len(S)>=2 else []; opp=S if maj is L else L if maj is S else []
 if not maj:return _v(reason="CROSS_VENUE_PRICE_CONFLICT" if L and S else "PRICE_MOVE_INCOMPLETE",changes=ch)
 if fast and len(maj)!=3:return _v(reason="FAST_PRICE_NEEDS_3_VENUES",changes=ch,agreeing=len(maj))
 ms=sum(r[3] for r in maj)/len(maj); os=max((r[3] for r in opp),default=0.)
 if opp and os>=.45:return _v(reason="CROSS_VENUE_PRICE_MATERIAL_CONFLICT",changes=ch,majority_strength=ms,opposing_strength=os)
 side=maj[0][1]; conf=(.60+.22*ms) if fast else (.55+.12*max(0,len(maj)-2)+.25*ms-.10*(.5+os if opp else 0))
 return _v(side,conf,"FAST_3VENUE_PRICE_DIRECTION" if fast else ("MULTI_VENUE_PRICE_WEAK_OPPOSITION" if opp else "MULTI_VENUE_PRICE"),changes=ch,agreeing=len(maj),strength=round(ms,6),opposing_strength=round(os,6))

def _s1(cur,slow,fast,t):
 a=_pv(cur,slow,t); f=_pv(cur,fast,t,True)
 if a["vote"]!="ABSTAIN":
  m=dict(a["metrics"]);m.update(fast_vote=f["vote"],fast_confidence=f["confidence"]);return _v(a["vote"],a["confidence"],a["reason"],**m)
 if f["vote"]!="ABSTAIN":
  m=dict(f["metrics"]);m["provisional"]=True;return _v(f["vote"],min(.74,f["confidence"]),"FAST_DIRECTIONAL_PRICE_FALLBACK",**m)
 return _v(reason=a["reason"],fast_reason=f["reason"])

def _s2(cur,ref,t,fresh):
 if not fresh:return _v(reason="STALE_OI",regime="OI_UNAVAILABLE")
 if ref is None:return _v(reason="WARMUP_OI_HISTORY",regime="WARMUP")
 p,o=_chg(cur.get("spot"),ref.get("spot")),_chg(cur.get("oi"),ref.get("oi"))
 if p is None or o is None:return _v(reason="MISSING_PRICE_OR_OI",regime="MISSING",price_pct=p,oi_pct=o)
 d=_side(p,t)
 if o>=MIN_OI and d in ("LONG","SHORT"):
  ps,os=_ex(p,t),_c((o/MIN_OI-1)/3); reg="NEW_LONG_BUILD" if d=="LONG" else "NEW_SHORT_BUILD"
  return _v(d,.56+.20*ps+.20*os,reg,regime=reg,price_pct=p,oi_pct=o,price_strength=ps,oi_strength=os)
 if o<=-MIN_OI and d=="LONG":return _v(reason="SHORT_COVERING",regime="SHORT_COVERING",price_pct=p,oi_pct=o,directional_hint="LONG")
 if o<=-MIN_OI and d=="SHORT":return _v(reason="LONG_LIQUIDATION_CLOSING",regime="LONG_LIQUIDATION_CLOSING",price_pct=p,oi_pct=o,directional_hint="SHORT")
 return _v(reason="NO_NEW_POSITION_BUILD",regime="NEUTRAL",price_pct=p,oi_pct=o)

def _flow(s,now,fut=False):
 B=S=new=0.
 if fut:
  cut=(now-LOOKBACK)*1000
  for r in list(getattr(s,"danh_sach_khop_lenh_futures",()) or ()):
   try:
    ts=float(r.get("thoi_gian_ms",0) or 0)
    if ts<cut:continue
    q=float(r.get("khoi_luong",0) or 0);new=max(new,ts/1000)
    if bool(r.get("ban_chu_dong",False)):S+=q
    else:B+=q
   except (AttributeError,TypeError,ValueError):pass
  if not _fresh(new,now,FUT_AGE):return 0.,0.
 else:
  cut=now-LOOKBACK
  for r in list(getattr(s,"flow_1s_buffer",()) or ()):
   try:
    if float(r.get("ts",0) or 0)>=cut:B+=float(r.get("buy",0) or 0);S+=float(r.get("sell",0) or 0)
   except (AttributeError,TypeError,ValueError):pass
 T=B+S;return ((B-S)/T if T>0 else 0.),T

def _s3(s,now):
 si,sv=_flow(s,now);fi,fv=_flow(s,now,True);cb=float(getattr(s,"coinbase_cvd_1m",0) or 0);ct=float(getattr(s,"thoi_gian_coinbase_cuoi",0) or 0);rows=[]
 for n,i,v in (("spot",si,sv),("futures",fi,fv)):
  if v>0 and abs(i)>=MIN_FLOW:rows.append((n,"LONG" if i>0 else "SHORT",_c(abs(i)/.35)))
 if _fresh(ct,now,CB_AGE) and abs(cb)>=.5:rows.append(("coinbase","LONG" if cb>0 else "SHORT",_c(abs(cb)/3)))
 L=[r for r in rows if r[1]=="LONG"];S=[r for r in rows if r[1]=="SHORT"]
 if L and S:return _v(reason="MULTI_VENUE_FLOW_CONFLICT",venues=rows)
 a=L if len(L)>=2 else S if len(S)>=2 else []
 if not a:return _v(reason="INSUFFICIENT_FLOW_CONSENSUS",venues=rows)
 st=sum(r[2] for r in a)/len(a);return _v(a[0][1],.52+.12*max(0,len(a)-2)+.30*st,"MULTI_VENUE_FLOW",venues=rows,agreeing=len(a),strength=st)

def _story(sv):
 p,o,f=(sv[k]["vote"] for k in ("S1_cross_price","S2_price_x_oi","S3_multi_flow"));reg=(sv["S2_price_x_oi"].get("metrics") or {}).get("regime","NEUTRAL")
 if p==o==f=="LONG":return ("NEW_LONG_BUILD_CONFIRMED","LONG",.08,False)
 if p==o==f=="SHORT":return ("NEW_SHORT_BUILD_CONFIRMED","SHORT",.08,False)
 if reg=="SHORT_COVERING":return ("SHORT_COVERING_WITH_BUY_FLOW","LONG",-.10,False) if p==f=="LONG" else ("SHORT_COVERING_UNCONFIRMED","ABSTAIN",-.12,True)
 if reg=="LONG_LIQUIDATION_CLOSING":return ("LONG_LIQUIDATION_WITH_SELL_FLOW","SHORT",-.10,False) if p==f=="SHORT" else ("LONG_LIQUIDATION_UNCONFIRMED","ABSTAIN",-.12,True)
 if p==o=="LONG" and f=="SHORT":return ("SELL_FLOW_ABSORBED_BY_LONG_BUILD","LONG",.04,False)
 if p==o=="SHORT" and f=="LONG":return ("BUY_FLOW_ABSORBED_BY_SHORT_BUILD","SHORT",.04,False)
 if p in ("LONG","SHORT") and f==p and o=="ABSTAIN":return ("PRICE_FLOW_DIRECTION_OI_NEUTRAL",p,.02,False)
 if p in ("LONG","SHORT") and o==p and f=="ABSTAIN":return ("PRICE_OI_DIRECTION_FLOW_NEUTRAL",p,0.,False)
 if f in ("LONG","SHORT") and p!=f:return ("FLOW_NOT_CONVERTED_TO_PRICE","ABSTAIN",-.08,True)
 return ("MIXED_OR_INCOMPLETE","ABSTAIN",0.,False)

def _a1(s,spot,fut,fresh):
 if not fresh or spot<=0 or fut<=0:return _v(reason="STALE_OR_MISSING_FUNDING_BASIS")
 f=float(getattr(s,"funding_rate",0) or 0);b=(fut-spot)/spot*10000
 if b>=8 and f>0:return _v("SHORT",_c(.45+min(abs(b),40)/100),"LONG_CROWDING",basis_bps=b)
 if b<=-8 and f<0:return _v("LONG",_c(.45+min(abs(b),40)/100),"SHORT_CROWDING",basis_bps=b)
 return _v(reason="CROWDING_NEUTRAL",basis_bps=b)

def _a2(cur,ref,t):
 if ref is None:return _v(reason="WARMUP_LEAD_HISWFY")
