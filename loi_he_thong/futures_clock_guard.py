"""Reset the hardened Futures-flow epoch when local receive time moves backwards."""


def install(wrapper):
    flow = wrapper.futures_flow
    original_trim = flow._trim

    def trim_clock_safe(state, now_ms):
        ring = flow._ensure_ring(state)
        rollback = False
        if len(ring) >= 2:
            try:
                previous = float(ring[-2].get("thoi_gian_ms", 0.0) or 0.0)
                current = float(ring[-1].get("thoi_gian_ms", 0.0) or 0.0)
                rollback = previous > 0.0 and current > 0.0 and current < previous
            except (AttributeError, TypeError, ValueError):
                rollback = False

        if rollback:
            newest = ring[-1]
            ring.clear()
            ring.append(newest)
            state.futures_flow_ring_saturated = False
            state.futures_flow_ring_coverage_sec = 0.0
            state.futures_flow_ring_size = 1
            state.futures_flow_epoch = int(getattr(state, "futures_flow_epoch", 0) or 0) + 1
            state.futures_flow_epoch_clock_reset = True
            state.futures_flow_epoch_clock_reset_at_ms = float(now_ms)

        return original_trim(state, now_ms)

    flow._trim = trim_clock_safe
    return trim_clock_safe
