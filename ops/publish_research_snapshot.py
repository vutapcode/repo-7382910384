#!/usr/bin/env python3
"""Publish a sanitized rolling SHADOW research snapshot to a telemetry branch.

The publisher reads only the durable shadow journal, never raw credentials or
private account payloads.  The dedicated branch is amended in place so a
three-minute cadence does not create hundreds of commits on ``main``.
"""

from collections import Counter
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


JOURNAL = Path(os.getenv(
    "WSTRADE_RESEARCH_JOURNAL",
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl",
))
RUNTIME_STATE = Path(os.getenv(
    "WSTRADE_RESEARCH_RUNTIME_STATE",
    "/home/ubuntu/.local/state/smc2026/mainnet_shadow/runtime_state.json",
))
TRADE_AUDIT = Path(os.getenv(
    "WSTRADE_RESEARCH_TRADE_AUDIT",
    "/home/ubuntu/wstrade_trade_log/trades.jsonl",
))
PUBLISH_STATE = Path(os.getenv(
    "WSTRADE_RESEARCH_PUBLISH_STATE",
    "/home/ubuntu/.local/state/wstrade/research_publisher_state.json",
))
CLONE = Path(os.getenv(
    "WSTRADE_RESEARCH_CLONE",
    "/home/ubuntu/.local/share/wstrade-telemetry",
))
REMOTE = os.getenv(
    "WSTRADE_RESEARCH_REMOTE",
    "git@github.com:vutapcode/repo-7382910384.git",
)
BRANCH = os.getenv("WSTRADE_RESEARCH_BRANCH", "telemetry")
RETENTION_SECONDS = 84 * 3600
# Bound first-run RAM/CPU on the 2 GB Lightsail. Subsequent runs are strictly
# incremental from the durable byte offset.
INITIAL_TAIL_BYTES = 8 * 1024 * 1024
VN = timezone(timedelta(hours=7))


def _run(*args, cwd=None, check=True):
    return subprocess.run(
        args, cwd=cwd, check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _iso(ts, zone=timezone.utc):
    try:
        return datetime.fromtimestamp(float(ts), zone).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _compact_event(row):
    """Allowlist research fields; unknown/private fields never leave the VPS."""
    event = str(row.get("event") or "")
    base = {
        "event": event,
        "ts": row.get("ts"),
        "utc": _iso(row.get("ts")),
        "vn": _iso(row.get("ts"), VN),
        "cycle_id": row.get("cycle_id"),
        "causal_episode_id": row.get("causal_episode_id"),
        "side": row.get("side"),
    }
    if event == "ENTRY":
        thesis = _dict(row.get("entry_causal_thesis"))
        base.update({
            "price": row.get("price"), "qty_btc": row.get("qty_btc"),
            "entry_mode": row.get("entry_mode"), "phase": row.get("phase"),
            "confidence": row.get("confidence"), "edge_class": row.get("edge_class"),
            "hard_sl": row.get("hard_sl"),
            "proof_type": thesis.get("proof_type"),
            "proposer": thesis.get("proposer"),
            "impulse_phase": thesis.get("impulse_phase"),
            "bias_thesis": thesis.get("bias_thesis"),
            "oi_intent": thesis.get("oi_intent"),
        })
    elif event == "EXIT":
        guardian = _dict(row.get("guardian_state") or row.get("guardian"))
        votes = _dict(guardian.get("votes"))
        base.update({
            "entry_price": row.get("entry_price"),
            "exit_price": row.get("exit_price"),
            "gross_pnl_bps": row.get("gross_pnl_bps"),
            "net_pnl_bps": row.get("net_pnl_bps"),
            "net_pnl_usdt": row.get("net_pnl_usdt"),
            "fees_usdt": row.get("fees_usdt"),
            "holding_time_seconds": row.get("holding_time_seconds"),
            "risk_reason": row.get("risk_reason"),
            "best_r": row.get("best_r"), "floor_r": row.get("floor_r"),
            "guardian": {
                "reason": guardian.get("reason"),
                "exit_profile": guardian.get("exit_profile"),
                "kill_fast": guardian.get("kill_fast"),
                "trend_shield_active": guardian.get("trend_shield_active"),
                "deterioration_seconds": guardian.get("deterioration_elapsed_seconds"),
                "trend_context": guardian.get("trend_context"),
                "price": _dict(votes.get("S1_price_acceptance")).get("metrics"),
                "flow": _dict(votes.get("S2_executed_flow")).get("metrics"),
                "oi": _dict(votes.get("S3_price_x_oi")).get("metrics"),
            },
        })
    elif event == "DECISION_EVALUATED":
        persistent = _dict(row.get("persistent_metaorder_shadow"))
        base.update({
            "decision": row.get("decision"), "reason": row.get("reason"),
            "entry_mode": row.get("entry_mode"), "phase": row.get("phase"),
            "miss_taxonomy": row.get("miss_taxonomy"),
            "failed_gates": row.get("failed_gates"),
            "blocking_stage": row.get("blocking_stage"),
            "consumed_fraction": row.get("impulse_consumed_fraction"),
            "ignition_proposer": row.get("ignition_proposer"),
            "ignition_proof_type": row.get("ignition_proof_type"),
            "oi_intent": row.get("oi_intent"),
            "persistent_status": persistent.get("status"),
            "persistent_candidate_side": persistent.get("candidate_side"),
            "persistent_candidate_id": persistent.get("candidate_id"),
        })
    return {key: value for key, value in base.items() if value is not None}


def _closed_trade_history(cutoff):
    """Read the sanitized audit mirror through a second strict allowlist."""
    allowed = (
        "schema_version", "cycle_id", "decision_cycle_id", "causal_episode_id",
        "side", "entry_ts", "entry_time_utc", "entry_price", "exit_ts",
        "exit_time_utc", "exit_price", "qty_btc", "entry_mode", "phase",
        "proof_type", "proposer", "primary_cash_anchor", "bias_side",
        "bias_phase", "oi_intent", "oi_causal_class", "impulse_phase",
        "edge_class", "execution_style", "commission_verified",
        "total_cost_bps", "minimum_net_edge_bps", "gross_pnl_bps",
        "net_pnl_bps", "net_pnl_usdt", "net_pnl_r", "holding_time_seconds",
        "exit_reason", "guardian_exit_profile", "guardian_trend_shield_active",
        "guardian_version", "virtual_only", "historical_current_authority",
        "economic_contract_version", "flow_efficiency_state",
        "oi_verification_status", "consumed_band",
        "time_to_positive_net_seconds", "flow_efficiency",
    )
    rows = []
    try:
        handle = TRADE_AUDIT.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            try:
                source = json.loads(line)
                nested_entry = _dict(source.get("entry"))
                nested_exit = _dict(source.get("exit"))
                ts = float(
                    source.get("exit_ts", 0) or nested_exit.get("ts", 0) or 0
                )
            except (ValueError, TypeError, AttributeError):
                continue
            if ts < cutoff:
                continue
            if nested_entry and nested_exit:
                thesis = _dict(nested_entry.get("causal_thesis"))
                bias = _dict(thesis.get("bias_thesis"))
                oi = _dict(thesis.get("oi_intent"))
                oi_verification = _dict(thesis.get("oi_verification_state"))
                economics = _dict(thesis.get("economic_feature_snapshot"))
                guardian = _dict(nested_exit.get("guardian"))
                row = {
                    "schema_version": source.get("schema_version"),
                    "cycle_id": source.get("trade_id"),
                    "decision_cycle_id": nested_entry.get("decision_cycle_id"),
                    "causal_episode_id": nested_entry.get("causal_episode_id"),
                    "side": nested_entry.get("side"),
                    "entry_ts": nested_entry.get("ts"),
                    "entry_time_utc": nested_entry.get("utc"),
                    "entry_price": nested_entry.get("entry_price"),
                    "exit_ts": nested_exit.get("ts"),
                    "exit_time_utc": nested_exit.get("utc"),
                    "exit_price": nested_exit.get("exit_price"),
                    "qty_btc": nested_entry.get("actual_qty_btc"),
                    "entry_mode": nested_entry.get("mode"),
                    "phase": nested_entry.get("phase"),
                    "proof_type": thesis.get("proof_type"),
                    "proposer": thesis.get("proposer"),
                    "primary_cash_anchor": thesis.get("primary_cash_anchor"),
                    "bias_side": bias.get("direction"),
                    "bias_phase": bias.get("phase"),
                    "oi_intent": oi.get("intent"),
                    "oi_causal_class": oi.get("causal_class"),
                    "edge_class": nested_entry.get("edge_class"),
                    "execution_style": _dict(nested_entry.get("execution")).get("style"),
                    "gross_pnl_bps": nested_exit.get("gross_pnl_bps"),
                    "net_pnl_bps": nested_exit.get("net_pnl_bps"),
                    "net_pnl_usdt": nested_exit.get("net_pnl_usdt"),
                    "net_pnl_r": nested_exit.get("net_pnl_r"),
                    "holding_time_seconds": nested_exit.get("holding_seconds"),
                    "time_to_positive_net_seconds": nested_exit.get(
                        "time_to_positive_net_seconds"
                    ),
                    "economic_contract_version": thesis.get(
                        "economic_contract_version"
                    ),
                    "flow_efficiency_state": economics.get(
                        "flow_efficiency_state"
                    ),
                    "oi_verification_status": oi_verification.get("status"),
                    "consumed_band": economics.get("consumed_band"),
                    "flow_efficiency": thesis.get("flow_efficiency"),
                    "exit_reason": nested_exit.get("reason"),
                    "guardian_exit_profile": guardian.get("exit_profile"),
                    "guardian_trend_shield_active": guardian.get("trend_shield_active"),
                    "virtual_only": source.get("virtual_only"),
                }
                row = {key: value for key, value in row.items() if value is not None}
            else:
                row = {
                    key: source.get(key) for key in allowed
                    if source.get(key) is not None
                }
            row["ts"] = ts
            row["vn"] = _iso(ts, VN)
            rows.append(row)
    return rows[-2000:]


def _journal_delta(checkpoint):
    stat = JOURNAL.stat()
    same = (
        checkpoint.get("device") == stat.st_dev
        and checkpoint.get("inode") == stat.st_ino
        and 0 <= int(checkpoint.get("offset", 0)) <= stat.st_size
    )
    start = int(checkpoint.get("offset", 0)) if same else max(
        0, stat.st_size - INITIAL_TAIL_BYTES,
    )
    rows = []
    offset = start
    with JOURNAL.open("rb") as handle:
        handle.seek(start)
        if start:
            handle.readline()
            offset = handle.tell()
        while True:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                offset = line_start
                break
            offset = handle.tell()
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if row.get("event") in {"DECISION_EVALUATED", "ENTRY", "EXIT"}:
                rows.append(row)
    next_checkpoint = {
        "device": stat.st_dev, "inode": stat.st_ino, "offset": offset,
        "journal_size": stat.st_size,
    }
    return rows, next_checkpoint


def _service_state(name):
    result = _run("systemctl", "is-active", name, check=False)
    return result.stdout.strip() or "unknown"


def _runtime_summary():
    row = _load(RUNTIME_STATE, {})
    position = _dict(row.get("position"))
    return {
        "trades": row.get("trades"), "wins": row.get("wins"),
        "losses": row.get("losses"), "breakeven": row.get("breakeven"),
        "balance_usdt": row.get("balance"),
        "position": {
            key: position.get(key) for key in (
                "active", "side", "qty", "entry_price", "opened_at",
                "hard_sl", "best_r", "floor_r", "stage",
            ) if position.get(key) is not None
        } if position else None,
    }


def _ensure_clone():
    CLONE.parent.mkdir(parents=True, exist_ok=True)
    if not (CLONE / ".git").exists():
        _run("git", "clone", "--no-checkout", REMOTE, str(CLONE))
    _run("git", "config", "user.name", "WStrade Recorder", cwd=CLONE)
    _run("git", "config", "user.email", "wstrade-recorder@localhost", cwd=CLONE)
    remote = _run(
        "git", "ls-remote", "--heads", "origin", BRANCH,
        cwd=CLONE, check=False,
    ).stdout.strip()
    remote_sha = remote.split()[0] if remote else None
    if remote_sha:
        _run("git", "fetch", "origin", BRANCH, cwd=CLONE)
        _run("git", "checkout", "-B", BRANCH, "FETCH_HEAD", cwd=CLONE)
    else:
        current = _run(
            "git", "symbolic-ref", "--short", "HEAD", cwd=CLONE, check=False,
        ).stdout.strip()
        if current != BRANCH:
            _run("git", "checkout", "--orphan", BRANCH, cwd=CLONE)
    has_head = _run(
        "git", "rev-parse", "--verify", "HEAD", cwd=CLONE, check=False,
    ).returncode == 0
    return remote_sha, has_head


def _merge_unique(existing, incoming, key, cutoff, limit):
    merged = {}
    for row in list(existing or ()) + list(incoming or ()):
        try:
            if float(row.get("ts", 0) or 0) < cutoff:
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        merged[key(row)] = row
    return sorted(merged.values(), key=lambda row: float(row.get("ts", 0))) [-limit:]


def _publish(no_push=False):
    checkpoint = _load(PUBLISH_STATE, {})
    rows, next_checkpoint = _journal_delta(checkpoint)
    remote_sha, has_head = _ensure_clone()
    target = CLONE / "research_live"
    now = time.time()
    cutoff = now - RETENTION_SECONDS

    compact = [_compact_event(row) for row in rows]
    trade_rows = [row for row in compact if row.get("event") in {"ENTRY", "EXIT"}]
    candidate_rows = [
        row for row in compact
        if row.get("event") == "DECISION_EVALUATED"
        and row.get("side") in {"LONG", "SHORT"}
        and (row.get("causal_episode_id") or row.get("persistent_candidate_id"))
    ]
    trades = _merge_unique(
        _load(target / "trades.json", []), trade_rows,
        lambda row: (row.get("event"), row.get("cycle_id"), row.get("ts")),
        cutoff, 2000,
    )
    closed_trades = _closed_trade_history(cutoff)
    candidates = _merge_unique(
        _load(target / "candidates.json", []), candidate_rows,
        lambda row: (
            row.get("causal_episode_id") or row.get("persistent_candidate_id"),
            row.get("reason"), row.get("side"),
        ), cutoff, 2000,
    )

    decisions = [row for row in compact if row.get("event") == "DECISION_EVALUATED"]
    summary = {
        "ts": now, "utc": _iso(now), "vn": _iso(now, VN),
        "journal_events_read": len(rows),
        "event_counts": dict(Counter(row.get("event") for row in compact)),
        "decision_counts": dict(Counter(row.get("decision") for row in decisions)),
        "side_counts": dict(Counter(row.get("side") for row in decisions)),
        "reason_counts": dict(Counter(row.get("reason") for row in decisions).most_common(25)),
        "miss_taxonomy_counts": dict(Counter(
            row.get("miss_taxonomy") for row in decisions if row.get("miss_taxonomy")
        ).most_common(25)),
        "services": {
            name: _service_state(name) for name in (
                "wstrade-bot", "wstrade-recorder", "wstrade-health",
            )
        },
        "runtime": _runtime_summary(),
    }
    timeline = _merge_unique(
        _load(target / "timeline.json", []), [summary],
        lambda row: int(float(row.get("ts", 0)) // 180), cutoff, 2000,
    )
    latest = dict(summary, source={
        "mode": "SHADOW_ONLY", "real_exchange_mutations": False,
        "journal": str(JOURNAL), "retention_hours": 84,
        "branch": BRANCH,
    }, recent_trades=trades[-20:], recent_closed_trades=closed_trades[-20:],
       recent_candidates=candidates[-40:])

    _write_json(target / "latest.json", latest)
    _write_json(target / "timeline.json", timeline)
    _write_json(target / "trades.json", trades)
    _write_json(target / "closed_trades.json", closed_trades)
    _write_json(target / "candidates.json", candidates)
    (target / "README.md").write_text(
        "# WStrade live research telemetry\n\n"
        "Sanitized SHADOW-only evidence, refreshed about every three minutes. "
        "No API credentials, private account payloads, or raw WAL are published.\n\n"
        "Start with `latest.json`, then inspect `closed_trades.json`, "
        "`trades.json`, `candidates.json`, and `timeline.json`. "
        "Times are provided in UTC and UTC+7.\n",
        encoding="utf-8",
    )

    if no_push:
        print(json.dumps({"generated": str(target), "rows": len(rows), "push": False}))
        return
    _run("git", "add", "--", "research_live", cwd=CLONE)
    staged = _run("git", "diff", "--cached", "--quiet", cwd=CLONE, check=False)
    if staged.returncode == 0:
        _write_json(PUBLISH_STATE, dict(next_checkpoint, published_at=now))
        print(json.dumps({"changed": False, "rows": len(rows)}))
        return
    message = "telemetry: refresh WStrade shadow research snapshot"
    if has_head:
        _run("git", "commit", "--amend", "-m", message, cwd=CLONE)
    else:
        _run("git", "commit", "-m", message, cwd=CLONE)
    if remote_sha:
        lease = "--force-with-lease=refs/heads/%s:%s" % (BRANCH, remote_sha)
        _run("git", "push", lease, "origin", "HEAD:refs/heads/" + BRANCH, cwd=CLONE)
    else:
        _run("git", "push", "-u", "origin", "HEAD:refs/heads/" + BRANCH, cwd=CLONE)
    _write_json(PUBLISH_STATE, dict(next_checkpoint, published_at=now))
    print(json.dumps({"changed": True, "rows": len(rows), "branch": BRANCH}))


def main():
    no_push = "--no-push" in sys.argv[1:]
    PUBLISH_STATE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = PUBLISH_STATE.with_suffix(".lock")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("publisher already running", file=sys.stderr)
            return 0
        _publish(no_push=no_push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
