"""Small bucketed empirical expectancy calibrator."""
VERSION="EDGE_CAL_V2"
MIN_N=24

def _u(x,d): return str(x or d).upper()

def _rows(s):
    r=getattr(s,"_edge_cal_v2_rows",None)
    if r is None:
        r=[]
        s._edge_cal_v2_rows=r
    if len(r)>768:
        del r[:-768]
    return r

def record(s,mode,regime,net_bps,side=None,edge_class=None):
    row=(_u(side,"UNKNOWN"),_u(mode,"NORMAL"),_u(regime,"NORMAL"),_u(edge_class,"UNKNOWN"),float(net_bps))
    _rows(s).append(row)
    s.edge_cal_v2_last=row
    return row

def factor(s,mode,regime,side=None,edge_class=None):
    key=(_u(side,"UNKNOWN"),_u(mode,"NORMAL"),_u(regime,"NORMAL"),_u(edge_class,"UNKNOWN"))
    vals=[r[4] for r in _rows(s) if r[:4]==key][-256:]
    if len(vals)<MIN_N:
        out={"version":VERSION,"samples":len(vals),"factor":1.0,"status":"INSUFFICIENT_DATA","bucket":"|".join(key)}
        s.edge_cal_v2=out
        return out
    vals=sorted(vals)
    k=max(1,int(len(vals)*0.1))
    core=vals[k:-k] if len(vals)>2*k else vals
    mean=sum(core)/len(core)
    wr=sum(v>0 for v in vals)/len(vals)
    f=1.0+max(-0.08,min(0.08,mean/250.0))
    if wr<0.44: f=min(f,0.95)
    elif wr>0.61: f=max(f,1.03)
    f=max(0.92,min(1.08,f))
    out={"version":VERSION,"samples":len(vals),"factor":round(f,4),"mean_net_bps":round(mean,4),"win_rate":round(wr,4),"status":"ACTIVE","bucket":"|".join(key)}
    s.edge_cal_v2=out
    return out
