"""Record completed shadow outcomes into bucketed empirical calibration."""
from loi_he_thong import edge_calibration_v2

VERSION="SHADOW_CAL_HOOK_V2"

def install(hardened):
    base=hardened.runtime.base
    if getattr(base,"_shadow_cal_v2_hooked",False):
        return
    original=base._close_shadow

    def close_with_calibration(pos,result,now):
        state=base.app.state
        was_active=bool(getattr(pos,"active",False))
        out=original(pos,result,now)
        if was_active and not bool(getattr(pos,"active",False)):
            entry=float(getattr(pos,"entry_price",0.0) or 0.0)
            qty=float(getattr(pos,"initial_qty",getattr(pos,"qty",0.0)) or 0.0)
            notional=entry*qty
            net=float(getattr(state,"mainnet_shadow_last_net_pnl",0.0) or 0.0)
            if notional>0.0:
                edge=getattr(state,"mainnet_shadow_entry_edge",None) or {}
                mode=str(edge.get("entry_mode") or "NORMAL").upper()
                regime=((edge.get("micro_regime") or {}).get("regime") or "NORMAL")
                edge_class=str(edge.get("edge_class") or "UNKNOWN").upper()
                side=str(getattr(pos,"side","UNKNOWN") or "UNKNOWN").upper()
                edge_calibration_v2.record(
                    state,mode,regime,net/notional*10000.0,side,edge_class
                )
        return out

    base._close_shadow=close_with_calibration
    base._shadow_cal_v2_hooked=True
