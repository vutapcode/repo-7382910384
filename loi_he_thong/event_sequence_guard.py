"""Assign durable sequence numbers to committed shadow ENTRY/EXIT journal events."""

_RELEVANT = {"ENTRY", "EXIT"}


def install(wrapper):
    base = wrapper.base
    state = base.app.state
    original = base._append_event

    def append_with_sequence(event, payload):
        event_name = str(event).upper()
        if event_name not in _RELEVANT:
            return original(event, payload)

        previous = int(getattr(state, "mainnet_shadow_event_seq", 0) or 0)
        current = previous + 1
        enriched = dict(payload)
        enriched["event_seq"] = current
        state.mainnet_shadow_event_seq = current
        try:
            return original(event, enriched)
        except Exception:
            state.mainnet_shadow_event_seq = previous
            raise

    base._append_event = append_with_sequence
    return append_with_sequence
