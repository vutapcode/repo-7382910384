"""Keep Risk decisions available even when persistence I/O fails."""
import logging
import time


def install(wrapper):
    original = wrapper._orig_assess

    def assess_safe(pos, px, guardian, market_state=None, now=None):
        out = original(pos, px, guardian, market_state=market_state, now=now)
        wall = time.time() if now is None else float(now)
        mono = time.monotonic()
        last_mono = float(getattr(wrapper, "_last_persist_mono", 0.0) or 0.0)
        if mono - last_mono < 1.0:
            return out

        state = wrapper.base.app.state
        try:
            wrapper.runtime_state.save(wrapper.base)
        except Exception as exc:
            state.shadow_persistence_dirty = True
            state.shadow_persistence_last_error = f"{type(exc).__name__}:{exc}"[:300]
            state.shadow_persistence_last_error_at = wall
            state.shadow_persistence_error_count = int(
                getattr(state, "shadow_persistence_error_count", 0) or 0
            ) + 1
            logging.exception("[MAINNET-SHADOW] risk checkpoint failed; decision preserved")
        else:
            wrapper._last_persist_mono = mono
            state.shadow_persistence_dirty = False
            state.shadow_persistence_last_ok_at = wall
        return out

    wrapper.risk.assess = assess_safe
    return assess_safe
