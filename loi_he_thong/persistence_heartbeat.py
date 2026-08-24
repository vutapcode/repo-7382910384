"""Expose shadow persistence health in the canonical bot heartbeat."""
import time

_FLAT_CHECKPOINT_SECONDS = 15.0


def install(wrapper):
    base = wrapper.base
    state = base.app.state
    runtime_module = getattr(base.app, "_runtime", None)
    original_write = getattr(
        runtime_module,
        "_write_bot_heartbeat",
        base.app._write_bot_heartbeat,
    )

    def write_with_persistence(payload):
        mono = time.monotonic()
        last_checkpoint = float(
            getattr(state, "shadow_flat_checkpoint_mono", 0.0) or 0.0
        )
        if (
            not bool(getattr(state, "shadow_persistence_dirty", False))
            and mono - last_checkpoint >= _FLAT_CHECKPOINT_SECONDS
        ):
            callback = getattr(state, "wstrade_runtime_state_save", None)
            if callable(callback):
                try:
                    callback()
                except Exception as exc:
                    state.shadow_persistence_dirty = True
                    state.shadow_persistence_last_error = (
                        f"{type(exc).__name__}:{exc}"[:300]
                    )
                    state.shadow_persistence_last_error_at = time.time()
                    state.shadow_persistence_error_count = int(
                        getattr(state, "shadow_persistence_error_count", 0) or 0
                    ) + 1
                else:
                    state.shadow_flat_checkpoint_mono = mono
                    state.shadow_persistence_last_ok_at = time.time()
        state.journal_loop_heartbeat_mono = mono
        # Shadow persistence is event-driven. While no checkpoint is dirty,
        # there is no pending disk mutation to flush, so the durable state is
        # current even when the market is idle. Stop advancing this marker as
        # soon as a write fails; the out-of-band watchdog will then fail closed.
        if not bool(getattr(state, "shadow_persistence_dirty", False)):
            state.journal_last_persist_mono = mono
            state.journal_age_seconds = 0.0
            state.journal_stalled = False
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
    if runtime_module is not None:
        runtime_module._write_bot_heartbeat = write_with_persistence
    return write_with_persistence
