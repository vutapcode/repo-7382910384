"""Small wrapper that suppresses stale critical-loop state only during PID startup grace."""
import time

from loi_he_thong import ops_supervisor as ops

_orig_build_snapshot = ops.build_snapshot
_PID_FIRST_SEEN_MONO = {}


def _pid_startup_grace(pid, heartbeat_pid):
    pid = int(pid or 0)
    heartbeat_pid = int(heartbeat_pid or 0)

    for old_pid in tuple(_PID_FIRST_SEEN_MONO):
        if old_pid != pid:
            _PID_FIRST_SEEN_MONO.pop(old_pid, None)

    if pid <= 0 or heartbeat_pid == pid:
        _PID_FIRST_SEEN_MONO.pop(pid, None)
        return False

    mono = time.monotonic()
    first_seen = _PID_FIRST_SEEN_MONO.setdefault(pid, mono)
    return mono - first_seen < ops.BOT_STARTUP_GRACE_SECONDS


def build_snapshot(now=None):
    now = time.time() if now is None else float(now)
    snapshot = _orig_build_snapshot(now)

    try:
        service = snapshot["services"]["bot"]
        heartbeat = snapshot["bot"]["heartbeat"]
        in_grace = _pid_startup_grace(
            service.get("pid", 0),
            heartbeat.get("pid", 0),
        )
    except (KeyError, TypeError, ValueError):
        in_grace = False

    if in_grace:
        snapshot["bot"]["classification"] = "STARTING"
        snapshot["bot"]["restart_requested"] = False
    else:
        persistence = heartbeat.get("persistence") or {}
        if (
            bool(persistence.get("dirty", False))
            and snapshot["bot"].get("classification") in {"IDLE_MARKET", "SAFETY_BLOCK"}
        ):
            snapshot["bot"]["classification"] = "PERSISTENCE_DEGRADED"
            snapshot["status"] = "ERROR"

    return snapshot


ops.build_snapshot = build_snapshot


def _bot_pid_still_current(pid):
    try:
        service = ops._service_state(ops.SERVICES["bot"])
        return int(service.get("pid", 0) or 0) == int(pid) and service.get("active_state") == "active"
    except Exception:
        return False


def _restart_stalled_bot_safe(pid):
    pid = int(pid or 0)
    if pid <= 0 or not _bot_pid_still_current(pid):
        return False
    try:
        ops.os.kill(pid, ops.signal.SIGUSR1)
    except ProcessLookupError:
        return False

    ops.time.sleep(0.5)
    if not _bot_pid_still_current(pid):
        return False

    try:
        ops.os.kill(pid, ops.signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


ops._restart_stalled_bot = _restart_stalled_bot_safe


def _run_forever_action_aware():
    last_bot_restart_mono = 0.0
    while True:
        started = ops.time.time()
        started_mono = ops.time.monotonic()
        try:
            snapshot = ops.build_snapshot(started)
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
                    bot["classification"], pid,
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
        elapsed = ops.time.time() - started
        ops.time.sleep(max(0.2, ops.INTERVAL_SECONDS - elapsed))


ops.run_forever = _run_forever_action_aware


def main():
    ops.main()


if __name__ == "__main__":
    main()
