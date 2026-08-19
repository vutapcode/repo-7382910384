"""Apply bucketed empirical calibration to Tier-S entry edge without touching causal vetoes."""
from loi_he_thong import edge_calibration_v2

VERSION="ENTRY_EDGE_CAL_HOOK_V3_PRESERVE_ORIGINAL_VETO"

def install(edge_module):
    if getattr(edge_module,"_edge_cal_v2_hooked",False):
        return
    original=edge_module.authorize

    def authorize(result,state):
        allowed,report=original(result,state)
        report=dict(report or {})

        # Causal/hard veto authority is immutable. Calibration may only refine
        # an entry that the original edge already authorized.
        if not allowed:
            report["calibration_skipped"]="ORIGINAL_EDGE_VETO"
            report["policy"]="ORIGINAL_EDGE_VETO_PRESERVED"
            return False,report

        edge_class=str(report.get("edge_class") or "UNKNOWN").upper()
        mode=str(report.get("entry_mode") or "NORMAL").upper()
        regime=((report.get("micro_regime") or {}).get("regime") or "NORMAL")
        side=str((result or {}).get("side") or getattr(state,"bias_state","UNKNOWN") or "UNKNOWN").upper()

        cal=edge_calibration_v2.factor(state,mode,regime,side,edge_class)
        old=report.get("empirical_calibration") or {}
        old_factor=float(old.get("factor",1.0) or 1.0)
        expected=float(report.get("expected_excursion_bps_model",0.0) or 0.0)
        expected=(expected/old_factor)*float(cal.get("factor",1.0) or 1.0)
        cost=float(report.get("cost_budget_bps_model",0.0) or 0.0)
        multiple=expected/cost if cost>0 else 999.0
        minimum=float(report.get("min_cost_multiple",0.0) or 0.0)
        cost_ok=edge_class!="LOW_EDGE" and multiple>=minimum

        report["empirical_calibration"]=cal
        report["expected_excursion_bps_model"]=round(expected,4)
        report["cost_multiple_model"]=round(multiple,4)
        report["cost_ok"]=cost_ok
        report["policy"]="ORIGINAL_EDGE_VETO_PRESERVED_BUCKETED_EMPIRICAL_EXPECTANCY_ONLY"
        return bool((result or {}).get("decision")=="GO" and cost_ok),report

    edge_module.authorize=authorize
    edge_module._edge_cal_v2_hooked=True
