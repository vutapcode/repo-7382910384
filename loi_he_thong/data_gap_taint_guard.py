"""Persistently taint active shadow positions that cross a Futures execution-data gap."""
import time

PERSIST_FIELDS = (
    "calibration_tainted",
    "data_gap_seen",
    "data_gap_started_at",
    "data_gap_recovered_at",
    "data_gap_reason",
)

_RETRY_MIN_SEC = 1.0
_RETRY_MAX_SEC = 5.0


def install(wrapper):
    base = wrapper.base
    state_mod = wrapper.runtime_state
    health = wrapper.health
    state = base.app.state

    fields = tuple(getattr(state_mod, "PERSIST_FIELDS", ()))
    state_mod.PERSIST_FIELDS = fields + tuple(name for name in PERSIST_FIELDS if name not in fields)

    original = health.exec_price

    def persist_gap_state(now):
        try:
            state_mod.save(base)
        except Exception as exc:
            errors = int(getattr(state, "shadow_data_gap_persist_errors", 0) or 0) + 1
            delay = min(_RETRY_MAX_SEC, _RETRY_MIN_SEC * (2 ** min(3, errors - 1)))
            state.shadow_data_gap_persist_errors = errors
            state.shadow_data_gap_retry_after_mono = time.monotonic() + delay
            state.shadow_persistence_dirty = True
            state.shadow_persistence_last_error = f"{type(exc).__name__}:{exc}"[:300]
            state.shadow_persistence_last_error_at = now
            return False
        state.shadow_data_gap_persist_errors = 0
        state.shadow_data_gap_retry_after_mono = 0.0
        state.shadow_persistence_dirty = False
        state.shadow_persistence_last_ok_at = now
        return True

    def maybe_retry_gap_persist(now):
        if not bool(getattr(state, "shadow_data_gap_persist_pending", False)):
            return
        retry_after = float(getattr(state, "shadow_data_gap_retry_after_mono", 0.0) or 0.0)
        if time.monotonic() < retry_after:
            return
        if persist_gap_state(now):
            state.shadow_data_gap_persist_pending = False

    def exec_price_with_gap_taint(base_obj, now=None):
        now = time.time() if now is None else float(now)
        px = original(base_obj, now)
        pos = getattr(state, "mainnet_shadow_position", None)
        active = pos is not None and bool(getattr(pos, "active", False))
        if not active:
            state.shadow_data_gap_active = False
            state.shadow_data_gap_persist_pending = False
            return px

        if px > 0.0:
            if bool(getattr(state, "shadow_data_gap_active", False)):
                state.shadow_data_gap_active = False
                if bool(getattr(pos, "data_gap_seen", False)):
                    pos.data_gap_recovered_at = now
                    state.shadow_data_gap_persist_pending = True
            maybe_retry_gap_persist(now)
            return px

        state.shadow_data_gap_active = True
        state.shadow_data_gap_last_at = now
        if not bool(getattr(pos, "calibration_tainted", False)):
            pos.calibration_tainted = True
            pos.data_gap_seen = True
            pos.data_gap_started_at = now
            pos.data_gap_recovered_at = 0.0
            pos.data_gap_reason = "FUTURES_EXECUTION_FEED_STALE"
            state.shadow_data_gap_reason = pos.data_gap_reason
            state.shadow_data_gap_count = int(getattr(state, "shadow_data_gap_count", 0) or 0) + 1
            state.shadow_data_gap_persist_pending = True

        maybe_retry_gap_persist(now)
        return px

    health.exec_price = exec_price_with_gap_taint
    return exec_price_with_gap_taint
