"""Expose shadow persistence health in the canonical bot heartbeat."""
import time


def install(wrapper):
    base = wrapper.base
    state = base.app.state
    original_write = base.app._write_bot_heartbeat

    def write_with_persistence(payload):
        enriched = dict(payload)
        enriched["persistence"] = {
            "dirty": bool(getattr(state, "shadow_persistence_dirty", False)),
            "last_error": getattr(state, "shadow_persistence_last_error", None),
            "last_error_at": float(
                getattr(state, "shadow_persistence_last_error_at", 0.0) or 0.0
            ) or None,
            "error_count": int(
                getattr(state, "shadow_persistence_error_count", 0) or 0
            ),
            "retry_after_sec": float(
                getattr(state, "shadow_persistence_retry_after_sec", 0.0) or 0.0
            ),
            "last_ok_at": float(
                getattr(state, "shadow_persistence_last_ok_at", 0.0) or 0.0
            ) or None,
        }
        return original_write(enriched)

    base.app._write_bot_heartbeat = write_with_persistence
    return write_with_persistence
