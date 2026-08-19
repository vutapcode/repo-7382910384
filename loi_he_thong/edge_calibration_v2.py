"""Hierarchical RAM-only empirical expectancy calibrator."""
VERSION="EDGE_CAL_V3_HIERARCHICAL"
MAX_ROWS=768

def _u(x,d): return str(x or d).upper()

def _rows(s):
    r=getattr(s,"_edge_cal_v2_rows",None)
    if r is None:
        r=[]; s._edge_cal_v2_rows=r
    if len(r)>MAX_ROWS: del r[:-MAX_ROWS]
    return r

def record(s,mode,regime,net_bps,side=None,edge_class=None):
    r=(_u(side,"UNKNOWN"),_u(mode,"NORMAL"),_u(regime,"NORMAL"),_u(edge_class,"UNKNOWN"),float(net_bps))
    _rows(s).append(r); s.edge_cal_v2_last=r
    return r

def _vals(rows,key,level):
    side,mode,regime,edge=key
    if level=="EXACT": return [r[4] for r in rows if r[:4]==key]
    if level=="SIDE_REGIME_EDGE": return [r[4] for r in rows if r[0]==side and r[2]==regime and r[3]==edge]
    if level=="REGIME_EDGE": return [r[4] for r in rows if r[2]==regime and r[3]==edge]
    if level=="EDGE": return [r[4] for r in rows if r[3]==edge]
    return [r[4] for r in rows]

def _estimate(v,bound):
    v=sorted(v[-256:]); k=max(1,int(len(v)*.1))
    core=v[k:-k] if len(v)>2*k else v
    mean=sum(core)/len(core); wr=sum(x>0 for x in v)/len(v)
    f=1+max(-bound,min(bound,mean/250))
    if wr<.44: f=min(f,.97)
    elif wr>.61: f=max(f,1.02)
    return max(1-bound,min(1+bound,f)),mean,wr

def factor(s,mode,regime,side=None,edge_class=None):
    key=(_u(side,"UNKNOWN"),_u(mode,"NORMAL"),_u(regime,"NORMAL"),_u(edge_class,"UNKNOWN"))
    rows=list(_rows(s))
    levels=(("EXACT",24,.08),("SIDE_REGIME_EDGE",32,.06),("REGIME_EDGE",40,.05),("EDGE",56,.04),("GLOBAL",80,.03))
    for level,n,bound in levels:
        v=_vals(rows,key,level)
        if len(v)>=n:
            f,mean,wr=_estimate(v,bound)
            out={"version":VERSION,"samples":len(v),"factor":round(f,4),"mean_net_bps":round(mean,4),"win_rate":round(wr,4),"status":"ACTIVE","level":level,"max_adjust":bound,"bucket":"|".join(key),"total_samples":len(rows)}
            s.edge_cal_v2=out
            return out
    out={"version":VERSION,"samples":len(_vals(rows,key,"EXACT")),"factor":1.0,"status":"INSUFFICIENT_DATA","level":"NONE","bucket":"|".join(key),"total_samples":len(rows)}
    s.edge_cal_v2=out
    return out
