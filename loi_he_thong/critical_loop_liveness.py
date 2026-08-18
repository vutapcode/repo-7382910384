"""Expose liveness of the canonical Bias, Entry, and active-position Risk loops."""
import time


def _wrap(state, obj, name, prefix):
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        try:
            out = original(*args, **kwargs)
        except Exception as exc:
            now = time.time()
            setattr(state, f"{prefix}_loop_last_error_at", now)
            setattr(state, f"{prefix}_loop_last_error", f"{type(exc).__name__}:{exc}"[:300])
            setattr(
                state,
                f"{prefix}_loop_error_count",
                int(getattr(state, f"{prefix}_loop_error_count", 0) or 0) + 1,
            )
            setattr(
                state,
                f"{prefix}_loop_consecutive_errors",
                int(getattr(state, f"{prefix}_loop_consecutive_errors", 0) or 0) + 1,
            )
            raise
        now = time.time()
        setattr(state, f"{prefix}_loop_last_ok", now)
        setattr(state, f"{prefix}_loop_consecutive_errors", 0)
        return out

    setattr(obj, name, wrapped)
    return wrapped


def install(wrapper):
    base = wrapper.base
    state = base.app.state
    state.critical_liveness_installed_at = time.time()

    _wrap(state, base.bias_council, "update_state", "bias")
    _wrap(state, base.entry_council, "evaluate", "entry")
    _wrap(state, wrapper.risk, "assess", "guardian")

    original_write = base.app._write_bot_heartbeat

    def write_with_liveness(payload):
        now = time.time()
        enriched = dict(payload)
        loops = {}
        for prefix in ("bias", "entry", "guardian"):
            last_ok = float(getattr(state, f"{prefix}_loop_last_ok", 0.0) or 0.0)
            loops[prefix] = {
                "last_ok": last_ok or None,
                "age_sec": None if last_ok <= 0.0 else max(0.0, now - last_ok),
                "last_error_at": float(
                    getattr(state, f"{prefix}_loop_last_error_at", 0.0) or 0.0
                ) or None,
                "last_error": getattr(state, f"{prefix}_loop_last_error", None),
                "error_count": int(
                    getattr(state, f"{prefix}_loop_error_count", 0) or 0
                ),
                "consecutive_errors": int(
                    getattr(state, f"{prefix}_loop_consecutive_errors", 0) or 0
                ),
            }
        pos = getattr(state, "mainnet_shadow_position", None)
        enriched["critical_liveness_installed_at"] = float(
            getattr(state, "critical_liveness_installed_at", 0.0) or 0.0
        )
        enriched["critical_loops"] = loops
        enriched["shadow_position_active"] = bool(
            pos is not None and getattr(pos, "active", False)
        )
        return original_write(enriched)

    base.app._write_bot_heartbeat = write_with_liveness
    return write_with_liveness
