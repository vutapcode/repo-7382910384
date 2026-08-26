"""Reset Spot rolling evidence when local receive wall-clock moves backwards."""
def install(wrapper, hardening):
    state = wrapper.base.app.state
    original = hardening._spot_flow

    def _clear_window(name):
        value = getattr(state, name, None)
        if hasattr(value, "clear"):
            value.clear()

    def spot_flow_clock_safe(state_obj, now):
        rows = getattr(state_obj, "flow_1s_buffer", None)
        if rows:
            try:
                newest = float(rows[-1].get("ts", 0.0) or 0.0)
            except (AttributeError, TypeError, ValueError, IndexError):
                newest = 0.0
            if newest > float(now):
                for name in ("flow_1s_buffer", "trade_flow_timeline", "cvd_30m_buffer"):
                    value = getattr(state_obj, name, None)
                    if hasattr(value, "clear"):
                        value.clear()
                state_obj.cvd_buy_30m = 0.0
                state_obj.cvd_sell_30m = 0.0
                state_obj.last_3s_window_ts = 0.0
                state_obj.current_vol_3s = 0.0
                state_obj.current_cvd_buy_3s = 0.0
                state_obj.current_cvd_sell_3s = 0.0
                state_obj.spot_flow_epoch = int(getattr(state_obj, "spot_flow_epoch", 0) or 0) + 1
                state_obj.spot_flow_epoch_clock_reset = True
                state_obj.spot_flow_epoch_clock_reset_at = float(now)
                # Preserve the wrapped flow function's return contract. The
                # current Bias background flow returns imbalance, volume and
                # coverage; older runtime variants returned only two values.
                return original(state_obj, now)
        return original(state_obj, now)

    hardening._spot_flow = spot_flow_clock_safe
    return spot_flow_clock_safe
