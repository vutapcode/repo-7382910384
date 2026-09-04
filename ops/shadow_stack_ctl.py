#!/usr/bin/env python3
"""Install, restart and verify the complete WStrade SHADOW stack.

This is an operations owner, not a strategy owner.  It never enables trading,
changes strategy thresholds, contacts authenticated exchange endpoints, or
repairs persisted trading state.  A failed deployment leaves the market
recorder available but stops the bot fail-closed.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATE = Path("/home/ubuntu/.local/state/smc2026/mainnet_shadow/runtime_state.json")
BOT_HEARTBEAT = Path("/home/ubuntu/smc2026_data/health/bot_runtime.json")
RECORDER_HEALTH = Path("/home/ubuntu/smc2026_data/health/status.json")
SYSTEM_HEALTH = Path("/home/ubuntu/smc2026_data/health/system_status.json")

BOT = "wstrade-bot.service"
RECORDER = "wstrade-recorder.service"
HEALTH = "wstrade-health.service"
AUDIT = "wstrade-trade-audit.service"
PUBLISHER_TIMER = "wstrade-research-publisher.timer"
UNITS = (RECORDER, BOT, HEALTH, AUDIT, PUBLISHER_TIMER)

REQUIRED_CONNECTIONS = (
    "public_ws",
    "market_ws",
    "binance_spot_ws",
    "coinbase_spot_ws",
    "rest_macro",
    "retention",
)
FAIL_CLOSED_ENV = {
    "WSTRADE_MODE": "SHADOW",
    "SMC_ENABLE_TRADING": "false",
    "SMC_MAINNET_ARMED": "false",
    "SMC_MAINNET_EXCLUSIVE_ACCOUNT": "false",
}


class StackError(RuntimeError):
    pass


def _run(args: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "no output").strip()
        raise StackError(f"COMMAND_FAILED:{' '.join(args)}:{detail[-1200:]}")
    return completed.stdout.strip()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _environment(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in shlex.split(raw or ""):
        name, separator, value = token.partition("=")
        if separator:
            values[name] = value
    return values


def _service_pid(unit: str) -> int:
    try:
        return int(_run(["systemctl", "show", unit, "-p", "MainPID", "--value"]) or 0)
    except ValueError as exc:
        raise StackError(f"SERVICE_PID_INVALID:{unit}") from exc


def _service_active(unit: str) -> bool:
    completed = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _fresh(payload: dict[str, Any], now: float, maximum_age: float = 15.0) -> bool:
    try:
        updated = float(payload.get("updated_at_ms", 0) or 0) / 1000.0
    except (TypeError, ValueError):
        return False
    return updated > 0.0 and 0.0 <= now - updated <= maximum_age


def assert_flat_state(payload: dict[str, Any]) -> None:
    position = payload.get("position")
    if isinstance(position, dict) and bool(position.get("active", True)):
        raise StackError("SHADOW_POSITION_ACTIVE_RESTART_REFUSED")
    if position not in (None, {}) and not isinstance(position, dict):
        raise StackError("SHADOW_POSITION_STATE_INVALID")


def assert_fail_closed_environment(values: dict[str, str]) -> None:
    wrong = {
        name: values.get(name)
        for name, expected in FAIL_CLOSED_ENV.items()
        if str(values.get(name, "")).lower() != expected.lower()
    }
    if wrong:
        raise StackError("REAL_MONEY_NOT_FAIL_CLOSED:" + json.dumps(wrong, sort_keys=True))


def recorder_ready(payload: dict[str, Any], expected_code: str, now: float) -> tuple[bool, str]:
    if not _fresh(payload, now):
        return False, "RECORDER_HEARTBEAT_STALE"
    if str(payload.get("code_version") or "") != expected_code:
        return False, "RECORDER_CODE_VERSION_OLD"
    connections = payload.get("connections") or {}
    missing = [name for name in REQUIRED_CONNECTIONS if connections.get(name) is not True]
    if missing:
        return False, "RECORDER_CONNECTIONS_NOT_READY:" + ",".join(missing)
    if str(payload.get("current_status") or "") != "OK":
        return False, "RECORDER_STATUS_NOT_OK"
    queue = payload.get("queue") or {}
    depth = payload.get("depth") or {}
    if int(queue.get("dropped", 0) or 0) != 0:
        return False, "RECORDER_DROPPED_EVENTS"
    if int(payload.get("writer_errors", 0) or 0) != 0:
        return False, "RECORDER_WRITER_ERRORS"
    if int(payload.get("decision_tap_parse_errors", 0) or 0) != 0:
        return False, "RECORDER_DECISION_TAP_ERRORS"
    if depth.get("synced") is not True or int(depth.get("gaps", 0) or 0) != 0:
        return False, "RECORDER_DEPTH_NOT_CLEAN"
    return True, "READY"


def bot_ready(payload: dict[str, Any], expected_code: str, pid: int, now: float) -> tuple[bool, str]:
    if not _fresh(payload, now):
        return False, "BOT_HEARTBEAT_STALE"
    if int(payload.get("pid", 0) or 0) != int(pid) or pid <= 0:
        return False, "BOT_PID_MISMATCH"
    if str(payload.get("code_version") or "") != expected_code:
        return False, "BOT_CODE_VERSION_OLD"
    profile = payload.get("strategy_profile") or {}
    if str(profile.get("mode") or "") != "MAINNET_SHADOW":
        return False, "BOT_PROFILE_NOT_SHADOW"
    if payload.get("trading_enabled") is not False:
        return False, "BOT_TRADING_NOT_FALSE"
    if payload.get("wstrade_live_armed") is not False:
        return False, "BOT_LIVE_ARMED"
    if payload.get("system_ready") is not True:
        return False, "BOT_SYSTEM_NOT_READY"
    return True, "READY"


def _wait(label: str, path: Path, predicate, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_reason = "NO_SNAPSHOT"
    while time.monotonic() < deadline:
        payload = _load(path)
        ready, last_reason = predicate(payload, time.time())
        if ready:
            return payload
        time.sleep(1.0)
    raise StackError(f"{label}_TIMEOUT:{last_reason}")


def _guard_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "WSTRADE_MODE": "SHADOW",
        "SMC_ENABLE_TRADING": "false",
        "SMC_MAINNET_TRADING_ENABLED": "false",
        "SMC_MAINNET_ARMED": "false",
        "SMC_MAINNET_EXCLUSIVE_ACCOUNT": "false",
        "SMC_JOURNAL_DIR": str(STATE.parent),
        "SMC_SHADOW_EVENTS_PATH": str(STATE.parent / "events.jsonl"),
    })
    return env


def _preflight() -> None:
    if os.geteuid() != 0:
        raise StackError("ROOT_REQUIRED_FOR_SYSTEMD_DEPLOY")
    if _run(["git", "status", "--porcelain"]):
        raise StackError("DIRTY_WORKTREE_DEPLOY_REFUSED")
    assert_flat_state(_load(STATE))
    env = _guard_environment()
    for script in (
        "repo_integrity_check.py",
        "shadow_state_guard.py",
        "shadow_journal_consistency_guard.py",
        "mainnet_preflight.py",
    ):
        _run([str(ROOT / ".venv/bin/python"), str(ROOT / "ops" / script)], env=env)


def _expected_code() -> str:
    return _run([
        str(ROOT / ".venv/bin/python"), "-c",
        "from recorder.metadata import code_version; print(code_version())",
    ])


def _effective_environment() -> dict[str, str]:
    raw = _run(["systemctl", "show", BOT, "-p", "Environment", "--value"])
    return _environment(raw)


def _install_and_enable() -> None:
    _run([str(ROOT / "ops/install_wstrade_services.sh")])
    _run(["systemctl", "enable", *UNITS])


def deploy(timeout: float = 120.0) -> dict[str, Any]:
    _preflight()
    expected_code = _expected_code()
    _install_and_enable()
    assert_fail_closed_environment(_effective_environment())

    # Stop the decision producer before cycling its recorder and supervisor.
    _run(["systemctl", "stop", HEALTH, BOT])
    try:
        _run(["systemctl", "restart", RECORDER])
        recorder = _wait(
            "RECORDER", RECORDER_HEALTH,
            lambda payload, now: recorder_ready(payload, expected_code, now),
            timeout,
        )
        _run(["systemctl", "start", BOT])
        bot_pid = _service_pid(BOT)
        bot = _wait(
            "BOT", BOT_HEARTBEAT,
            lambda payload, now: bot_ready(payload, expected_code, bot_pid, now),
            timeout,
        )
        _run(["systemctl", "restart", HEALTH, AUDIT])
        _run(["systemctl", "start", PUBLISHER_TIMER])
        system = _wait(
            "SYSTEM", SYSTEM_HEALTH,
            lambda payload, now: (
                _fresh(payload, now)
                and payload.get("status") == "RUNNING",
                str(payload.get("status") or "SYSTEM_NOT_READY"),
            ),
            timeout,
        )
        inactive = [name for name in UNITS if not _service_active(name)]
        if inactive:
            raise StackError("SERVICES_NOT_ACTIVE:" + ",".join(inactive))
        assert_fail_closed_environment(_effective_environment())
        return {
            "result": "SHADOW_STACK_READY",
            "code_version": expected_code,
            "git_head": _run(["git", "rev-parse", "HEAD"]),
            "services": {name: _service_pid(name) for name in UNITS},
            "recorder_status": recorder.get("current_status"),
            "bot_readiness": bot.get("readiness_reason"),
            "system_status": system.get("status"),
            "trading_enabled": bot.get("trading_enabled"),
            "live_armed": bot.get("wstrade_live_armed"),
        }
    except Exception:
        # Keep collecting public evidence, but never leave an unverified bot up.
        subprocess.run(["systemctl", "stop", BOT], check=False)
        subprocess.run(["systemctl", "start", RECORDER, HEALTH], check=False)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    try:
        report = deploy(max(30.0, args.timeout))
    except StackError as exc:
        print(json.dumps({"result": "FAIL_CLOSED", "error": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
