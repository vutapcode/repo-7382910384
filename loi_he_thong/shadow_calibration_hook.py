"""Install shadow-close outcome capture for bounded empirical calibration."""
from loi_he_thong import empirical_edge_calibrator as calibrator

VERSION = "SHADOW_CALIBRATION_HOOK_V1"

def install(hardened):
    runtime = hardened.runtime
    base = runtime.base
    if getattr(base, "_tier_s_calibration_hooked", False):
        return
    original = base._close_shadow

    def close_with_calibration(pos, result, now):
        state = base.app.state
        was_active = bool(getattr(pos, "active", False))
        out = original(pos, result, now)
        if was_active and not bool(getattr(pos, "active", False)):
            entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
            qty = float(getattr(pos, "initial_qty", getattr(pos, "qty", 0.0)) or 0.0)
            net = float(getattr(state, "mainnet_shadow_last_net_pnl", 0.0) or 0.0)
            notional = entry * qty
            if notional > 0.0:
                last_entry = getattr(state, "mainnet_shadow_last_entry", None) or {}
                mode = last_entry.get("entry_mode") if isinstance(last_entry, dict) else "NORMAL"
                report = getattr(state, "tier_s_entry_regime", None) or {}
                regime = report.get("regime") if isinstance(report, dict) else "NORMAL"
                calibrator.record(state, mode, regime, net / notional * 10000.0)
        return out

    base._close_shadow = close_with_calibration
    base._tier_s_calibration_hooked = True
