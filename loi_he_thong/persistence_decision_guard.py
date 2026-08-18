"""Keep Risk decisions available even when persistence I/O fails."""
import logging
import time

_MIN_RETRY_SEC = 1.0
_MAX_RETRY_SEC = 5.0


def _retry_delay(consecutive_errors):
    exponent = max(0, min(3, int(consecutive_errors) - 1))
    return min(_MAX_RETRY_SEC, _MIN_RETRY_SEC * (2 ** exponent))


def install(wrapper):
    original = wrapper._orig_assess

    def assess_safe(pos, px, guardian, market_state=None, now=None):
        out = original(pos, px, guardian, market_state=market_state, now=now)
        wall = time.time() if now is None else float(now)
        mono = time.monotonic()
        last_mono = float(getattr(wrapper, "_last_persist_mono", 0.0) or 0.0)
        retry_after = float(getattr(wrapper, "_persist_retry_after_mono", 0.0) or 0.0)

        if mono < retry_after:
            return out
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
            state.shadow_persistence_consecutive_errors = int(
                getattr(state, "shadow_persistence_consecutive_errors", 0) or 0
            ) + 1
            delay = _retry_delay(state.shadow_persistence_consecutive_errors)
            wrapper._persist_retry_after_mono = mono + delay
            state.shadow_persistence_retry_after_sec = delay
            logging.exception("[MAINNET-SHADOW] risk checkpoint failed; decision preserved")
        else:
            wrapper._last_persist_mono = mono
            wrapper._persist_retry_after_mono = 0.0
            state.shadow_persistence_dirty = False
            state.shadow_persistence_consecutive_errors = 0
            state.shadow_persistence_last_ok_at = wall
            state.shadow_persistence_retry_after_sec = 0.0
        return out

    wrapper.risk.assess = assess_safe
    return assess_safe
