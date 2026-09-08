"""Fail closed on new entries while flat shadow persistence is dirty, and retry it safely."""
import time

from loi_he_thong import mainnet_safety

_MIN_RETRY_SEC = 1.0
_MAX_RETRY_SEC = 5.0

def _retry_delay(consecutive_errors):
    exponent = max(0, min(3, int(consecutive_errors) - 1))
    return min(_MAX_RETRY_SEC, _MIN_RETRY_SEC * (2 ** exponent))

def install(wrapper):
    base = wrapper.base
    state = base.app.state
    original = base.entry_council.evaluate

    def evaluate_with_flat_persistence_gate(state_obj, now=None, side=None):
        pos = getattr(state_obj, "mainnet_shadow_position", None)
        active = bool(pos is not None and getattr(pos, "active", False))
        dirty = bool(getattr(state_obj, "shadow_persistence_dirty", False))

        if dirty and not active:
            mono = time.monotonic()
            retry_after = float(
                getattr(state_obj, "shadow_flat_persistence_retry_after_mono", 0.0) or 0.0
            )
            if mono >= retry_after:
                wall = time.time() if now is None else float(now)
                try:
                    wrapper.runtime_state.save(base)
                except Exception as exc:
                    state_obj.shadow_persistence_dirty = True
                    state_obj.shadow_persistence_last_error = (
                        f"{type(exc).__name__}:{exc}"[:300]
                    )
                    state_obj.shadow_persistence_last_error_at = wall
                    state_obj.shadow_persistence_error_count = int(
                        getattr(state_obj, "shadow_persistence_error_count", 0) or 0
                    ) + 1
                    consecutive = int(
                        getattr(
                            state_obj,
                            "shadow_persistence_consecutive_errors",
                            0,
                        )
                        or 0
                    ) + 1
                    state_obj.shadow_persistence_consecutive_errors = consecutive
                    delay = _retry_delay(consecutive)
                    state_obj.shadow_flat_persistence_retry_after_mono = mono + delay
                    state_obj.shadow_persistence_retry_after_sec = delay
                else:
                    state_obj.shadow_persistence_dirty = False
                    state_obj.shadow_persistence_consecutive_errors = 0
                    state_obj.shadow_flat_persistence_retry_after_mono = 0.0
                    state_obj.shadow_persistence_retry_after_sec = 0.0
                    state_obj.shadow_persistence_last_ok_at = wall

        dirty = bool(getattr(state_obj, "shadow_persistence_dirty", False))
        safety = mainnet_safety.set_entry_operational_blocker(
            state_obj, "PERSISTENCE_DIRTY_RETRY", dirty,
            {
                "last_error": getattr(
                    state_obj, "shadow_persistence_last_error", None,
                ),
                "retry_after_sec": float(getattr(
                    state_obj, "shadow_persistence_retry_after_sec", 0.0,
                ) or 0.0),
            },
        )
        result = dict(original(state_obj, now=now, side=side) or {})
        result["operational_safety"] = safety
        return result

    base.entry_council.evaluate = evaluate_with_flat_persistence_gate
    return evaluate_with_flat_persistence_gate
