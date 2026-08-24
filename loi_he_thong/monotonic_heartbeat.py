"""Attach a monotonic timestamp to the bot heartbeat for watchdog timing."""
import time
from pathlib import Path

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _boot_id():
    try:
        return _BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def install(wrapper):
    base = wrapper.base
    runtime_module = getattr(base.app, "_runtime", None)
    original = getattr(
        runtime_module,
        "_write_bot_heartbeat",
        base.app._write_bot_heartbeat,
    )

    def write_with_monotonic(payload):
        enriched = dict(payload)
        enriched["watchdog_monotonic_ns"] = int(time.monotonic_ns())
        boot_id = _boot_id()
        if boot_id:
            enriched["watchdog_boot_id"] = boot_id
        return original(enriched)

    base.app._write_bot_heartbeat = write_with_monotonic
    if runtime_module is not None:
        runtime_module._write_bot_heartbeat = write_with_monotonic
    return write_with_monotonic
