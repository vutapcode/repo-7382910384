"""Attach a monotonic timestamp to the bot heartbeat for watchdog timing."""
import time


def install(wrapper):
    base = wrapper.base
    original = base.app._write_bot_heartbeat

    def write_with_monotonic(payload):
        enriched = dict(payload)
        enriched["watchdog_monotonic_ns"] = int(time.monotonic_ns())
        return original(enriched)

    base.app._write_bot_heartbeat = write_with_monotonic
    return write_with_monotonic
