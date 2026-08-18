"""Small wrapper that suppresses stale critical-loop state only during PID startup grace."""
import time

from loi_he_thong import ops_supervisor as ops

_orig_build_snapshot = ops.build_snapshot


def build_snapshot(now=None):
    now = time.time() if now is None else float(now)
    snapshot = _orig_build_snapshot(now)

    try:
        service = snapshot["services"]["bot"]
        heartbeat = snapshot["bot"]["heartbeat"]
        pid = int(service.get("pid", 0) or 0)
        heartbeat_pid = int(heartbeat.get("pid", 0) or 0)
        first_seen = ops._BOT_PID_FIRST_SEEN.get(pid)
        in_grace = bool(
            pid > 0
            and heartbeat_pid != pid
            and first_seen is not None
            and now - float(first_seen) < ops.BOT_STARTUP_GRACE_SECONDS
        )
    except (KeyError, TypeError, ValueError):
        in_grace = False

    if in_grace:
        snapshot["bot"]["classification"] = "STARTING"
        snapshot["bot"]["restart_requested"] = False

    return snapshot


ops.build_snapshot = build_snapshot


def main():
    ops.main()


if __name__ == "__main__":
    main()
