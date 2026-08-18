"""Fail closed on new entries while flat shadow persistence is dirty, and retry it safely."""
import time

_MIN_RETRY_SEC = 1.0
_MAX_RETRY_SEC = 5.0

def _retry_delay(consecutive_errors):
    exponent = max(0, min(3, int(consecutive_errors) - 1))
    return min(_MAX_RETRY_SEC, _MIN_RETRY_SEC * (2 ** exponent))

def _wait_result(base, state, now, side):
    current = str(side or getattr(state, "bias_state", "ABSTAIN") or "ABSTAIN").upper()
    return {
        "version": getattr(base.entry_council, "VERSION", "ENTRY"),
        "decision": "WAIT",
        "entry_mode": "NONE",
        "phase": "ARMED",
        "confidence": 0.0,
        "reason": "PERSISTENCE_DIRTY_RETRY",
        "side": current,
        "s_votes": {},
        "ts": float(time.time() if now is None else now),
    }

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

            if bool(getattr(state_obj, "shadow_persistence_dirty", False)):
                return _wait_result(base, state_obj, now, side)

        return original(state_obj, now=now, side=side)

    base.entry_council.evaluate = evaluate_with_flat_persistence_gate
    return evaluate_with_flat_persistence_gate
