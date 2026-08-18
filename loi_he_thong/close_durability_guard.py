"""Rollback in-memory CLOSE mutations if the durable checkpoint fails."""
import logging
import time

_STATE_FIELDS = (
    "mainnet_shadow_balance_usdt",
    "mainnet_shadow_realized_pnl",
    "mainnet_shadow_trades",
    "mainnet_shadow_wins",
    "mainnet_shadow_losses",
    "mainnet_shadow_breakevens",
    "mainnet_shadow_last_net_pnl",
    "mainnet_shadow_last_closed_at",
    "mainnet_shadow_last_extra_cost",
    "mainnet_shadow_last_model_cost_bps",
)
_MISSING = object()


def install(wrapper):
    original = wrapper.base._close_shadow

    def close_safe(pos, result, now):
        state = wrapper.base.app.state
        was_active = bool(getattr(pos, "active", False))
        pos_snapshot = dict(vars(pos))
        state_snapshot = {
            name: getattr(state, name, _MISSING)
            for name in _STATE_FIELDS
        }

        try:
            return original(pos, result, now)
        except Exception as exc:
            mutated_closed = was_active and not bool(getattr(pos, "active", False))
            if not mutated_closed:
                raise

            pos.__dict__.clear()
            pos.__dict__.update(pos_snapshot)
            for name, value in state_snapshot.items():
                if value is _MISSING:
                    try:
                        delattr(state, name)
                    except AttributeError:
                        pass
                else:
                    setattr(state, name, value)

            t = float(now) if now is not None else time.time()
            state.shadow_persistence_dirty = True
            state.shadow_close_persistence_failed = True
            state.shadow_close_persistence_last_error = f"{type(exc).__name__}:{exc}"[:300]
            state.shadow_close_persistence_last_error_at = t
            state.shadow_close_persistence_error_count = int(
                getattr(state, "shadow_close_persistence_error_count", 0) or 0
            ) + 1
            logging.exception(
                "[MAINNET-SHADOW] CLOSE checkpoint failed; in-memory position rolled back active"
            )
            return None

    wrapper.base._close_shadow = close_safe
    return close_safe
