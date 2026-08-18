"""Expose liveness of the canonical Bias, Entry, and Guardian loops."""
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
        setattr(state, f"{prefix}_loop_last_ok", time.time())
        setattr(state, f"{prefix}_loop_last_ok_mono", time.monotonic())
        setattr(state, f"{prefix}_loop_consecutive_errors", 0)
        return out

    setattr(obj, name, wrapped)
    return wrapped


def install(wrapper):
    base = wrapper.base
    state = base.app.state
    state.critical_liveness_installed_at = time.time()
    state.critical_liveness_installed_mono = time.monotonic()

    _wrap(state, base.bias_council, "update_state", "bias")
    _wrap(state, base.entry_council, "evaluate", "entry")

    # Guardian liveness describes loop progress, not whether risk.assess() happened.
    # A stale Futures execution feed intentionally bypasses risk.assess() while
    # Guardian remains alive and polling execution price.
    original_exec_price = wrapper.health.exec_price

    def exec_price_with_liveness(*args, **kwargs):
        try:
            out = original_exec_price(*args, **kwargs)
        except Exception as exc:
            now = time.time()
            state.guardian_loop_last_error_at = now
            state.guardian_loop_last_error = f"{type(exc).__name__}:{exc}"[:300]
            state.guardian_loop_error_count = int(
                getattr(state, "guardian_loop_error_count", 0) or 0
            ) + 1
            state.guardian_loop_consecutive_errors = int(
                getattr(state, "guardian_loop_consecutive_errors", 0) or 0
            ) + 1
            raise
        state.guardian_loop_last_ok = time.time()
        state.guardian_loop_last_ok_mono = time.monotonic()
        state.guardian_loop_consecutive_errors = 0
        return out

    wrapper.health.exec_price = exec_price_with_liveness

    original_write = base.app._write_bot_heartbeat

    def write_with_liveness(payload):
        now = time.time()
        mono = time.monotonic()
        enriched = dict(payload)
        loops = {}
        for prefix in ("bias", "entry", "guardian"):
            last_ok = float(getattr(state, f"{prefix}_loop_last_ok", 0.0) or 0.0)
            last_ok_mono = float(getattr(state, f"{prefix}_loop_last_ok_mono", 0.0) or 0.0)
            loops[prefix] = {
                "last_ok": last_ok or None,
                "age_sec": None if last_ok_mono <= 0.0 else max(0.0, mono - last_ok_mono),
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
        installed_mono = float(
            getattr(state, "critical_liveness_installed_mono", 0.0) or 0.0
        )
        enriched["critical_liveness_installed_age_sec"] = (
            None if installed_mono <= 0.0 else max(0.0, mono - installed_mono)
        )
        enriched["critical_loops"] = loops
        enriched["shadow_position_active"] = bool(
            pos is not None and getattr(pos, "active", False)
        )
        return original_write(enriched)

    base.app._write_bot_heartbeat = write_with_liveness
    return write_with_liveness
