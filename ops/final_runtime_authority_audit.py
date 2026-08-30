#!/usr/bin/env python3
"""Read-only audit of the canonical WStrade runtime and its evidence claims.

This tool deliberately does not import the trading launcher, write state, restart
services, or contact authenticated exchange endpoints.  It checks the running
processes and bounded telemetry so a module cannot be declared active merely
because a similarly named file exists in the repository.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path("/home/ubuntu/WStrade")
HEALTH = Path("/home/ubuntu/smc2026_data/health/status.json")
BOT_HEARTBEAT = Path("/home/ubuntu/smc2026_data/health/bot_runtime.json")
RUNTIME_STATE = Path(
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow/runtime_state.json"
)
JOURNAL = Path(
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl"
)
SERVICES = (
    "wstrade-bot.service",
    "wstrade-recorder.service",
    "wstrade-health.service",
    "wstrade-trade-audit.service",
)
MAX_JOURNAL_BYTES = 1_000_000_000
# Keep the audit cheap enough to run beside SHADOW collection.  A larger
# forensic scan must be an explicit offline action with the bot stopped.
TAIL_BYTES = 8 * 1024 * 1024


def _run(*args: str) -> str:
    return subprocess.run(
        args, check=False, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout.strip()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _finding(code: str, severity: str, message: str, **evidence: Any) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": evidence,
    }


def classify_service(
    name: str, active: str, pid: int, cwd: str, expected_root: Path,
) -> list[dict]:
    findings: list[dict] = []
    if active != "active" or pid <= 0:
        findings.append(_finding(
            "SERVICE_NOT_ACTIVE", "FAIL", f"{name} is not active",
            service=name, active=active, pid=pid,
        ))
        return findings
    if not cwd or Path(cwd).resolve() != expected_root.resolve():
        findings.append(_finding(
            "SERVICE_CODE_ROOT_MISMATCH", "WARN",
            f"{name} runs outside the canonical checkout",
            service=name, pid=pid, cwd=cwd,
            expected=str(expected_root.resolve()),
        ))
    return findings


def safety_findings(environment: str) -> list[dict]:
    tokens = {}
    for token in environment.split():
        if "=" in token:
            key, value = token.split("=", 1)
            tokens[key] = value
    expected = {
        "WSTRADE_MODE": "SHADOW",
        "SMC_ENABLE_TRADING": "false",
        "SMC_MAINNET_ARMED": "false",
        "SMC_MAINNET_EXCLUSIVE_ACCOUNT": "false",
    }
    bad = {key: tokens.get(key) for key, value in expected.items()
           if tokens.get(key) != value}
    if bad:
        return [_finding(
            "REAL_MONEY_NOT_FAIL_CLOSED", "FAIL",
            "effective bot environment does not prove collect-only shadow mode",
            observed=bad, expected=expected,
        )]
    return []


def journal_findings(size: int, active_writer_has_rotation: bool) -> list[dict]:
    if size <= MAX_JOURNAL_BYTES:
        return []
    if not active_writer_has_rotation:
        return [_finding(
            "ACTIVE_JOURNAL_UNBOUNDED", "FAIL",
            "active decision journal exceeds the bound and its writer has no rotation",
            bytes=size, threshold_bytes=MAX_JOURNAL_BYTES,
        )]
    return [_finding(
        "ACTIVE_JOURNAL_OVERSIZED", "WARN",
        "active decision journal exceeds the operational bound",
        bytes=size, threshold_bytes=MAX_JOURNAL_BYTES,
    )]


def _tail_bytes(path: Path, limit: int) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            data = handle.read(limit)
    except OSError:
        return b""
    if size > limit:
        first_newline = data.find(b"\n")
        return data[first_newline + 1:] if first_newline >= 0 else b""
    return data


def inspect_follow_age(rows: list[dict]) -> dict[str, Any]:
    observed = absolute = latency = 0
    examples: list[float] = []
    for row in rows:
        stack = [row]
        while stack:
            value = stack.pop()
            for key, item in value.items():
                if isinstance(item, dict):
                    stack.append(item)
                elif key in {"futures_follow_age", "futures_response_ms"}:
                    try:
                        number = float(item)
                    except (TypeError, ValueError):
                        continue
                    observed += 1
                    if number > 10_000_000_000:
                        absolute += 1
                        if len(examples) < 3:
                            examples.append(number)
                    elif 0 <= number <= 60_000:
                        latency += 1
    return {
        "observed": observed,
        "absolute_timestamp_values": absolute,
        "latency_values": latency,
        "examples": examples,
    }


def _json_rows_from_tail(path: Path) -> list[dict]:
    rows = []
    for line in _tail_bytes(path, TAIL_BYTES).splitlines():
        if b"futures_follow_age" not in line and b"futures_response_ms" not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _service_snapshot(name: str, root: Path) -> tuple[dict, list[dict]]:
    active = _run("systemctl", "is-active", name)
    raw_pid = _run("systemctl", "show", "-p", "MainPID", "--value", name)
    try:
        pid = int(raw_pid or 0)
    except ValueError:
        pid = 0
    try:
        cwd = os.path.realpath(f"/proc/{pid}/cwd") if pid > 0 else ""
    except OSError:
        cwd = ""
    cmdline = ""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except (OSError, UnicodeDecodeError):
        pass
    snapshot = {
        "active": active, "pid": pid, "cwd": cwd, "cmdline": cmdline.strip(),
    }
    return snapshot, classify_service(name, active, pid, cwd, root)


def audit(root: Path, journal: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    findings: list[dict] = []
    services = {}
    for name in SERVICES:
        services[name], new = _service_snapshot(name, root)
        findings.extend(new)

    environment = _run(
        "systemctl", "show", "wstrade-bot.service", "-p", "Environment",
        "--value",
    )
    findings.extend(safety_findings(environment))

    health = _load_json(HEALTH)
    heartbeat = _load_json(BOT_HEARTBEAT)
    state = _load_json(RUNTIME_STATE)
    health_age = (
        now - float(health.get("updated_at_ms", 0) or 0) / 1000.0
        if health else None
    )
    if not health or health.get("current_status") != "OK" or (
        health_age is not None and health_age > 30.0
    ):
        findings.append(_finding(
            "RECORDER_HEALTH_NOT_CURRENT", "FAIL",
            "recorder health is missing, stale, or non-OK",
            status=health.get("current_status"), age_seconds=health_age,
        ))
    queue = health.get("queue") or {}
    depth = health.get("depth") or {}
    if int(queue.get("dropped", 0) or 0) or int(depth.get("gaps", 0) or 0):
        findings.append(_finding(
            "RECORDER_DATA_LOSS", "FAIL",
            "recorder reports dropped events or depth sequence gaps",
            dropped=queue.get("dropped"), depth_gaps=depth.get("gaps"),
        ))

    size = journal.stat().st_size if journal.exists() else 0
    writer = root / "mainnet_tier_s_lean_launcher.py"
    durable = root / "loi_he_thong" / "durable_shadow_journal.py"
    writer_text = writer.read_text(encoding="utf-8") if writer.exists() else ""
    durable_text = durable.read_text(encoding="utf-8") if durable.exists() else ""
    active_writer_has_rotation = bool(
        "durable_shadow_journal.install" in writer_text
        and "journal_segments.prepare_append" in durable_text
    )
    findings.extend(journal_findings(size, active_writer_has_rotation))

    follow = inspect_follow_age(_json_rows_from_tail(journal))
    if follow["absolute_timestamp_values"]:
        findings.append(_finding(
            "FUTURES_FOLLOW_LATENCY_UNIT_CORRUPT", "WARN",
            "decision telemetry treats an absolute receive timestamp as latency",
            **follow,
        ))

    mode = str(heartbeat.get("strategy_profile", {}).get("mode") or "")
    if mode != "MAINNET_SHADOW" or bool(heartbeat.get("trading_enabled")):
        findings.append(_finding(
            "HEARTBEAT_NOT_SHADOW", "FAIL",
            "runtime heartbeat does not prove shadow-only collection",
            mode=mode, trading_enabled=heartbeat.get("trading_enabled"),
        ))

    severity_order = {"FAIL": 3, "WARN": 2, "INFO": 1}
    worst = max((severity_order.get(x["severity"], 0) for x in findings), default=0)
    return {
        "schema_version": "WSTRADE_FINAL_RUNTIME_AUTHORITY_AUDIT_V1",
        "generated_at": now,
        "read_only": True,
        "root": str(root),
        "git_head": _run("git", "-C", str(root), "rev-parse", "HEAD"),
        "git_dirty": bool(_run("git", "-C", str(root), "status", "--porcelain")),
        "services": services,
        "runtime": {
            "heartbeat_mode": mode,
            "trading_enabled": heartbeat.get("trading_enabled"),
            "system_ready": heartbeat.get("system_ready"),
            "governor_mode": heartbeat.get("governor_mode"),
            "recorder_status": health.get("current_status"),
            "recorder_health_age_seconds": health_age,
            "journal_bytes": size,
            "shadow_trades": state.get("trades"),
            "shadow_wins": state.get("wins"),
            "shadow_losses": state.get("losses"),
        },
        "futures_follow_semantics": follow,
        "status": "FAIL" if worst >= 3 else "WARN" if worst == 2 else "PASS",
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--journal", type=Path, default=JOURNAL)
    args = parser.parse_args()
    report = audit(args.root.resolve(), args.journal.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
