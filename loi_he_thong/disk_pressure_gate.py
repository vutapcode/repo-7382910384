"""Gate new shadow entries when the journal filesystem is under disk pressure."""
import os
import time
from pathlib import Path

from loi_he_thong import mainnet_safety

_HEALTHY_CHECK_INTERVAL_SEC = 30.0
_PRESSURE_RECHECK_INTERVAL_SEC = 5.0
from loi_he_thong.storage_health import measure as measure_storage


def _journal_root():
    return Path(
        os.environ.get("SMC_JOURNAL_DIR")
        or (Path.home() / ".local" / "state" / "smc2026" / "mainnet_shadow")
    )


def _measure(path):
    status = measure_storage(path)
    return status["free_bytes"], status["free_ratio"]


def install(wrapper):
    base = wrapper.base
    state = base.app.state
    original = getattr(base, "_entry_evaluate", base.entry_council.evaluate)

    state.shadow_disk_check_after_mono = 0.0
    state.shadow_disk_pressure = False

    def evaluate_with_disk_gate(state_obj, now=None, side=None):
        mono = time.monotonic()
        check_after = float(getattr(state_obj, "shadow_disk_check_after_mono", 0.0) or 0.0)
        if mono >= check_after:
            try:
                storage = measure_storage(_journal_root())
                free_bytes, free_ratio = (
                    storage["free_bytes"], storage["free_ratio"]
                )
                pressure = bool(storage["pressure"])
                state_obj.shadow_storage_health = storage
                state_obj.shadow_disk_free_bytes = free_bytes
                state_obj.shadow_disk_free_ratio = free_ratio
                state_obj.shadow_disk_pressure = pressure
                state_obj.shadow_disk_check_error = None
            except OSError as exc:
                pressure = True
                state_obj.shadow_disk_pressure = True
                state_obj.shadow_disk_check_error = f"{type(exc).__name__}:{exc}"[:300]

            # Healthy filesystems are cheap to sample infrequently. Once pressure is
            # detected, re-check quickly so a cleanup/recovery can resume valid
            # entries without waiting a full healthy interval.
            interval = _PRESSURE_RECHECK_INTERVAL_SEC if pressure else _HEALTHY_CHECK_INTERVAL_SEC
            state_obj.shadow_disk_check_after_mono = mono + interval

        pressure = bool(getattr(state_obj, "shadow_disk_pressure", False))
        safety = mainnet_safety.set_entry_operational_blocker(
            state_obj, "DISK_PRESSURE", pressure,
            {
                "free_bytes": getattr(
                    state_obj, "shadow_disk_free_bytes", None,
                ),
                "free_ratio": getattr(
                    state_obj, "shadow_disk_free_ratio", None,
                ),
                "measurement_error": getattr(
                    state_obj, "shadow_disk_check_error", None,
                ),
            },
        )
        result = dict(original(state_obj, now=now, side=side) or {})
        result["operational_safety"] = safety
        return result

    base._entry_evaluate = evaluate_with_disk_gate
    return evaluate_with_disk_gate
