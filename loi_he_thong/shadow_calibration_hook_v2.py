"""Record and durably persist causally valid shadow calibration outcomes."""
from loi_he_thong import edge_calibration_v2

VERSION="SHADOW_CAL_HOOK_V3_GAP_TAINT_EXCLUSION"

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
            edge=getattr(state,"mainnet_shadow_entry_edge",None) or {}
            mode=str(edge.get("entry_mode") or "NORMAL").upper()
            regime=((edge.get("micro_regime") or {}).get("regime") or "NORMAL")
            edge_class=str(edge.get("edge_class") or "UNKNOWN").upper()
            side=str(getattr(pos,"side","UNKNOWN") or "UNKNOWN").upper()
            thesis=dict(getattr(pos,"entry_causal_thesis",{}) or {})
            cost_plan=(getattr(pos,"execution_cost_plan",None)
                       or getattr(pos,"shadow_cost_plan",None) or {})
            shadow_execution=getattr(pos,"shadow_execution",None) or {}
            proof_type=str(thesis.get("proof_type") or "UNKNOWN").upper()
            proposer=str(thesis.get("proposer") or "UNKNOWN").upper()
            execution_style=str(
                cost_plan.get("execution_style")
                or shadow_execution.get("style") or "UNKNOWN"
            ).upper()
            raw_cost=cost_plan.get(
                "decision_total_cost_bps",cost_plan.get("total_cost_bps")
            )
            try:
                execution_cost_bps=None if raw_cost is None else float(raw_cost)
            except (TypeError,ValueError):
                execution_cost_bps=None
            if bool(getattr(pos,"calibration_tainted",False)):
                state.edge_cal_v2_excluded_tainted = int(
                    getattr(state,"edge_cal_v2_excluded_tainted",0) or 0
                ) + 1
                state.edge_cal_v2_last_exclusion = {
                    "version": VERSION,
                    "reason": str(
                        getattr(pos,"data_gap_reason",None)
                        or "CALIBRATION_TAINTED"
                    ),
                    "ts": float(now),
                    "side": side,
                    "entry_mode": mode,
                    "regime": regime,
                    "edge_class": edge_class,
                    "position_cycle_id": getattr(pos,"position_cycle_id",None),
                    "causal_episode_id": getattr(pos,"causal_episode_id",None),
                }
                return out
            entry=float(getattr(pos,"entry_price",0.0) or 0.0)
            qty=float(getattr(pos,"initial_qty",getattr(pos,"qty",0.0)) or 0.0)
            notional=entry*qty
            net=float(getattr(state,"mainnet_shadow_last_net_pnl",0.0) or 0.0)
            if notional>0.0:
                edge_calibration_v2.record(
                    state,mode,regime,net/notional*10000.0,side,edge_class,
                    proof_type,proposer,execution_style,
                    execution_cost_bps=execution_cost_bps,
                )
                # The close wrapper persists before this outer calibration hook
                # runs.  Flush once more so a power loss immediately after EXIT
                # cannot lose the newly learned row.
                save = getattr(state, "wstrade_runtime_state_save", None)
                if callable(save):
                    save()
        return out

    base._close_shadow=close_with_calibration
    base._shadow_cal_v2_hooked=True
