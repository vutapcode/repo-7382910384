"""Keep shadow CLOSE telemetry consistent with durability rollback state."""


_MISSING = object()
_FIELDS = (
    "mainnet_shadow_position_status",
    "mainnet_shadow_last_exit",
)


def install(wrapper):
    base = wrapper.base
    original = base._close_shadow

    def close_with_telemetry_rollback(pos, result, now):
        state = base.app.state
        was_active = bool(getattr(pos, "active", False))
        before = {name: getattr(state, name, _MISSING) for name in _FIELDS}
        out = original(pos, result, now)

        if was_active and bool(getattr(pos, "active", False)):
            for name, value in before.items():
                if value is _MISSING:
                    try:
                        delattr(state, name)
                    except AttributeError:
                        pass
                else:
                    setattr(state, name, value)
        return out

    base._close_shadow = close_with_telemetry_rollback
    return close_with_telemetry_rollback
