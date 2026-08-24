"""Rollback partial shadow OPEN mutations if risk-arm or durable checkpoint fails."""
import logging
import os
import time

_STATE_FIELDS = (
    "mainnet_shadow_position",
    "mainnet_shadow_position_status",
    "mainnet_shadow_last_entry",
    "mainnet_shadow_last_feasibility",
    "mainnet_shadow_last_skip",
    "mainnet_shadow_risk",
    "mainnet_shadow_entry_edge",
    "mainnet_shadow_event_seq",
)
_MISSING = object()


def _journal_size(base):
    path = getattr(base, "EVENTS_PATH", None)
    if path is None:
        return None, None
    try:
        return path, path.stat().st_size if path.exists() else 0
    except OSError:
        return path, None


def _rollback_journal(state, path, size):
    if path is None or size is None:
        return
    try:
        with open(path, "r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        state.shadow_open_journal_rollback_failed = True
        state.shadow_open_journal_rollback_last_error = f"{type(exc).__name__}:{exc}"[:300]
        logging.exception("[MAINNET-SHADOW] failed to roll back partial OPEN journal")


def install(wrapper):
    base = wrapper.base
    original = base._open_shadow

    def open_safe(side, result, now):
        state = base.app.state
        state_snapshot = {
            name: getattr(state, name, _MISSING)
            for name in _STATE_FIELDS
        }
        journal_path, journal_size = _journal_size(base)

        try:
            return original(side, result, now)
        except Exception as exc:
            for name, value in state_snapshot.items():
                if value is _MISSING:
                    try:
                        delattr(state, name)
                    except AttributeError:
                        pass
                else:
                    setattr(state, name, value)

            _rollback_journal(state, journal_path, journal_size)
            state.shadow_open_persistence_failed = True
            state.shadow_persistence_dirty = True
            state.shadow_open_persistence_last_error = f"{type(exc).__name__}:{exc}"[:300]
            state.shadow_open_persistence_last_error_at = (
                float(now) if now is not None else time.time()
            )
            state.shadow_open_persistence_error_count = int(
                getattr(state, "shadow_open_persistence_error_count", 0) or 0
            ) + 1
            logging.exception(
                "[MAINNET-SHADOW] OPEN arm/checkpoint failed; state/journal rolled back"
            )
            raise

    base._open_shadow = open_safe
    return open_safe
