"""Monotonic scheduler for the hardened ops supervisor."""
from loi_he_thong import ops_supervisor as ops
from loi_he_thong import ops_supervisor_safe as safe


def _remaining_sleep(start_mono, now_mono, interval):
    elapsed = max(0.0, float(now_mono) - float(start_mono))
    return max(0.2, float(interval) - elapsed)


def run_forever():
    last_bot_restart_mono = 0.0
    while True:
        started_wall = ops.time.time()
        started_mono = ops.time.monotonic()
        try:
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
