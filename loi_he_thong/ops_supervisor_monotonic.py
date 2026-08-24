"""Monotonic scheduler for the hardened ops supervisor."""
from loi_he_thong import ops_supervisor as ops
from pathlib import Path

from loi_he_thong import ops_supervisor_safe as safe
from ops import lightsail_cpu_probe

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _read_boot_id():
    try:
        return _BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_BOOT_ID = _read_boot_id()
_orig_build_snapshot = ops.build_snapshot


def build_snapshot(now=None):
    snapshot = _orig_build_snapshot(now)
    bot = snapshot.get("bot") or {}
    if bot.get("classification") == "STARTING":
        return snapshot
    heartbeat = bot.get("heartbeat") or {}
    heartbeat_boot_id = str(heartbeat.get("watchdog_boot_id") or "")
    if _BOOT_ID and heartbeat_boot_id and heartbeat_boot_id != _BOOT_ID:
        bot["classification"] = "EVENT_LOOP_STALLED"
        bot["heartbeat_boot_mismatch"] = True
        snapshot["status"] = "ERROR"
    return snapshot


ops.build_snapshot = build_snapshot


def _remaining_sleep(start_mono, now_mono, interval):
    elapsed = max(0.0, float(now_mono) - float(start_mono))
    return max(0.2, float(interval) - elapsed)


def run_forever():
    last_bot_restart_mono = 0.0
    last_lightsail_refresh_mono = float("-inf")
    while True:
        started_wall = ops.time.time()
        started_mono = ops.time.monotonic()
        try:
            if started_mono - last_lightsail_refresh_mono >= 300.0:
                try:
                    lightsail_cpu_probe.refresh()
                except Exception as exc:
                    ops.logging.warning("[OPS] Lightsail CPU metric refresh failed: %s", exc)
                last_lightsail_refresh_mono = started_mono
            snapshot = ops.build_snapshot(started_wall)
            bot = snapshot["bot"]
            if (
                bot["classification"] in {
                    "EVENT_LOOP_STALLED",
                    "BIAS_LOOP_STALLED",
                    "ENTRY_LOOP_STALLED",
                    "GUARDIAN_LOOP_STALLED",
                }
                and started_mono - last_bot_restart_mono >= ops.RESTART_COOLDOWN_SECONDS
            ):
                pid = int(snapshot["services"]["bot"].get("pid", 0) or 0)
                ops.logging.critical(
                    "[OPS] Bot critical loop stalled (%s); dump stack then restart pid=%s",
                    bot["classification"],
                    pid,
                )
                acted = bool(ops._restart_stalled_bot(pid))
                snapshot["bot"]["restart_requested"] = acted
                if acted:
                    last_bot_restart_mono = started_mono
                else:
                    snapshot["bot"]["restart_skip_reason"] = "PID_CHANGED_OR_UNAVAILABLE"
            ops._atomic_json(ops.OUTPUT, snapshot)
        except Exception:
            ops.logging.exception("[OPS] Health supervisor iteration failed")

        sleep_for = _remaining_sleep(
            started_mono,
            ops.time.monotonic(),
            ops.INTERVAL_SECONDS,
        )
        ops.time.sleep(sleep_for)


ops.run_forever = run_forever


def main():
    ops.main()


if __name__ == "__main__":
    main()
