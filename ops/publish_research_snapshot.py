#!/usr/bin/env python3
"""Publish a sanitized rolling SHADOW evidence snapshot to telemetry.

The publisher reads only the durable shadow journal, never raw credentials or
private account payloads. The telemetry branch contains evidence only and
points back to the exact source commit on ``main``.
"""

from collections import Counter, deque
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from loi_he_thong import journal_segments
from recorder.metadata import runtime_commit, source_branch


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
OPPORTUNITY_WAL = Path(os.getenv(
    "WSTRADE_RESEARCH_OPPORTUNITY_WAL",
    "/home/ubuntu/smc2026_data/raw/wal/opportunity_dossier",
))
FEATURE_WAL = Path(os.getenv(
    "WSTRADE_RESEARCH_FEATURE_WAL",
    "/home/ubuntu/smc2026_data/raw/wal/feature_1s",
))
BOT_HEALTH = Path(os.getenv(
    "WSTRADE_BOT_HEALTH_PATH",
    "/home/ubuntu/smc2026_data/health/bot_runtime.json",
))
RECORDER_HEALTH = Path(os.getenv(
    "WSTRADE_RECORDER_HEALTH_PATH",
    "/home/ubuntu/smc2026_data/health/status.json",
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
MAX_DELTA_ROWS = 2000
# Bound first-run RAM/CPU on the 2 GB Lightsail. Subsequent runs are strictly
# incremental from the durable byte offset.
INITIAL_TAIL_BYTES = 8 * 1024 * 1024
VN = timezone(timedelta(hours=7))
IDENTITY_FIELDS = (
    "run_id", "runtime_commit", "code_version", "config_version",
)
FORBIDDEN_EVIDENCE_SUFFIXES = (
    ".py", ".pyc", ".pyo", ".md", ".rst", ".toml", ".ini", ".cfg",
)
SENSITIVE_KEYS = {
    "account", "account_id", "api_key", "api_secret", "authorization",
    "credential", "credentials", "password", "private_key", "secret",
    "session_token", "access_token", "refresh_token",
}
MAX_PUBLIC_DEPTH = 12
MAX_PUBLIC_LIST = 256
MAX_PUBLIC_STRING = 4096


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


def _load_jsonl(path):
    rows = []
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")
    os.replace(tmp, path)


def _iso(ts, zone=timezone.utc):
    try:
        return datetime.fromtimestamp(float(ts), zone).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _identity(source):
    identity = {name: source.get(name) for name in IDENTITY_FIELDS}
    identity.update({
        "runtime_mode": source.get("runtime_mode"),
        "source_branch": source.get("source_branch"),
    })
    identity["identity_status"] = (
        "COMPLETE" if all(identity.get(name) for name in IDENTITY_FIELDS)
        else "LEGACY_MISSING_IDENTITY"
    )
    return identity


def _is_sensitive_key(key):
    normalized = str(key).strip().lower().replace("-", "_")
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_api_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_private_key")
    )


def _public_value(value, depth=0):
    """Bound and redact nested forensic values before they leave the VPS."""
    if depth >= MAX_PUBLIC_DEPTH:
        return "TRUNCATED_MAX_DEPTH"
    if isinstance(value, dict):
        return {
            str(key): _public_value(item, depth + 1)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_public_value(item, depth + 1) for item in value[:MAX_PUBLIC_LIST]]
    if isinstance(value, str):
        return value[:MAX_PUBLIC_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_PUBLIC_STRING]


def _stable_event_id(row):
    existing = row.get("event_id")
    if existing:
        return str(existing)
    identity = {
        key: row.get(key) for key in (
            "event", "ts", "run_id", "cycle_id", "causal_episode_id",
            "timing_attempt_id", "economic_opportunity_id", "side",
        )
    }
    digest = hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()[:24]
    return "legacy:" + digest


def _safe_run_id(value):
    candidate = str(value or "legacy-missing-run-id")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", candidate):
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:24]
    return "invalid-run-id-" + digest


def _forensic_journal_event(row):
    event = str(row.get("event") or "")
    # The journal itself is already an important-event stream. Heartbeats are
    # sampled separately and would otherwise evict causal events from the
    # bounded delta deque.
    return bool(event and event != "DECISION_HEARTBEAT")


def _compact_event(row):
    """Allowlist research fields; unknown/private fields never leave the VPS."""
    event = str(row.get("event") or "")
    base = {
        "event": event,
        "event_id": _stable_event_id(row),
        "event_sequence": row.get("event_sequence"),
        "ts": row.get("ts"),
        "utc": _iso(row.get("ts")),
        "vn": _iso(row.get("ts"), VN),
        "cycle_id": row.get("cycle_id"),
        "causal_episode_id": row.get("causal_episode_id"),
        "side": row.get("side"),
        **_identity(row),
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
            "execution_urgency": thesis.get("execution_urgency"),
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
                "guardian_phase": guardian.get("guardian_phase"),
                "pullback_start_ms": guardian.get("pullback_start_ms"),
                "worst_adverse_bps": guardian.get("worst_adverse_bps"),
                "reclaim_fraction": guardian.get("reclaim_fraction"),
                "recovery_conversion_state": guardian.get("recovery_conversion_state"),
                "opposing_flow_state": guardian.get("opposing_flow_state"),
                "recovery_result": guardian.get("recovery_result"),
                "failed_recovery_reason": guardian.get("failed_recovery_reason"),
                "deterioration_seconds": guardian.get("deterioration_elapsed_seconds"),
                "trend_context": guardian.get("trend_context"),
                "price": _dict(votes.get("S1_price_acceptance")).get("metrics"),
                "flow": _dict(votes.get("S2_executed_flow")).get("metrics"),
                "oi": _dict(votes.get("S3_price_x_oi")).get("metrics"),
            },
        })
    elif event == "DECISION_EVALUATED":
        decision = _dict(row.get("decision_record"))
        inputs = _dict(decision.get("inputs"))
        persistent = _dict(
            row.get("persistent_metaorder_shadow")
            or inputs.get("persistent_metaorder_shadow")
        )
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
    compact = {key: value for key, value in base.items() if value is not None}
    for name in IDENTITY_FIELDS:
        compact.setdefault(name, None)
    return compact


def _decision_dossier_records(row):
    """Split one decision into canonical dossier records and lightweight refs."""
    if str(row.get("event") or "") != "DECISION_EVALUATED":
        return None
    compact = _compact_event(row)
    event_id = compact["event_id"]
    decision_record = _dict(row.get("decision_record"))
    forensic = _dict(decision_record.get("forensics"))
    lineage = _dict(forensic.get("lineage"))
    evidence_slice_id = "evidence:" + event_id
    blockers = list(forensic.get("blockers") or ())
    blocker_ids = [
        "blocker:%s:%s" % (event_id, index)
        for index in range(len(blockers))
    ]
    dossier = {
        **compact,
        "dossier_version": forensic.get("version"),
        "forensic_status": (
            "COMPLETE" if forensic.get("version")
            else "LEGACY_INCOMPLETE"
        ),
        "advisory_only": forensic.get("advisory_only", True),
        "lineage": lineage,
        "evidence_slice_id": evidence_slice_id,
        "question_results": forensic.get("question_results") or [],
        "authority": forensic.get("authority") or {},
        "blocker_refs": blocker_ids,
        "unknowns": forensic.get("unknowns") or [],
        "falsified": forensic.get("falsified") or [],
        "ignored_evidence": forensic.get("ignored_evidence") or [],
        "decision": forensic.get("decision") or {
            "action": row.get("decision"), "reason": row.get("reason"),
        },
        "counterfactual_ref": (
            "counterfactual:" + event_id
            if forensic.get("counterfactual") is not None else None
        ),
    }
    evidence_projection = {
        "market_evidence": forensic.get("market_evidence") or {},
        "data_quality": forensic.get("data_quality") or {},
        "observation": forensic.get("observation") or {},
    }
    evidence_projection = _public_value(evidence_projection)
    projection_sha256 = hashlib.sha256(json.dumps(
        evidence_projection, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    evidence = {
        "evidence_slice_id": evidence_slice_id,
        "decision_event_id": event_id,
        "ts": row.get("ts"),
        **_identity(row),
        "lineage": lineage,
        **evidence_projection,
        "projection_sha256": projection_sha256,
        "raw_archive_ref": None,
        "raw_archive_status": "POINTER_NOT_YET_AVAILABLE",
        "bounded_projection": True,
    }
    blocker_rows = []
    for blocker_id, blocker in zip(blocker_ids, blockers):
        blocker_rows.append({
            "blocker_event_id": blocker_id,
            "decision_event_id": event_id,
            "ts": row.get("ts"),
            **_identity(row),
            "lineage": lineage,
            "blocker": blocker,
        })
    counterfactual = None
    if forensic.get("counterfactual") is not None:
        counterfactual = {
            "counterfactual_id": "counterfactual:" + event_id,
            "decision_event_id": event_id,
            "ts": row.get("ts"),
            **_identity(row),
            "research_only": True,
            "lineage": lineage,
            "counterfactual": forensic.get("counterfactual"),
        }
    return {
        "decision": _public_value(dossier),
        "evidence": _public_value(evidence),
        "blockers": _public_value(blocker_rows),
        "counterfactual": _public_value(counterfactual),
    }


def _transition_record(row):
    transition = row.get("state_transition")
    if not isinstance(transition, dict):
        return None
    base = _compact_event(row)
    decision = _dict(row.get("decision_record"))
    forensic = _dict(decision.get("forensics"))
    return _public_value({
        **base,
        "transition_id": "transition:" + base["event_id"],
        "lineage": forensic.get("lineage") or {
            key: row.get(key) for key in (
                "market_wave_id", "causal_episode_id", "timing_attempt_id",
                "economic_opportunity_id", "cycle_id", "order_id",
                "fill_id", "position_id",
            ) if row.get(key) is not None
        },
        "state_transition": transition,
    })


def _execution_record(row):
    event = str(row.get("event") or "")
    if event not in {
        "ENTRY", "EXIT", "LIVE_ENTRY", "LIVE_EXIT", "LIVE_ORDER_UPDATE",
        "POSITION_STATE", "ENTRY_FILLED_THEN_FLATTENED",
    } and not event.startswith(("ENTRY_", "EXIT_", "SHADOW_MAKER_")):
        return None
    base = _compact_event(row)
    allowed = (
        "order_id", "client_order_id", "trade_id", "fill_id", "position_id",
        "decision_cycle_id", "status", "order_status", "execution_status",
        "price", "entry_price", "exit_price", "avg_price", "limit_price",
        "qty", "qty_btc", "filled_qty", "remaining_qty", "side", "reason",
        "risk_reason", "slippage_bps", "retry_count", "maker_attempt_id",
        "blocking_reason", "reject_stage", "reject_owner", "failed_gates",
        "miss_taxonomy", "ok", "flow_state_at_GO", "flow_state_at_submit",
        "flow_decayed_before_submit", "cash_age_at_submit",
    )
    base.update({key: row.get(key) for key in allowed if row.get(key) is not None})
    return _public_value(base)


def _closed_trade_history(cutoff):
    """Read the sanitized audit mirror through a second strict allowlist."""
    allowed = (
        "schema_version", "cycle_id", "decision_cycle_id", "causal_episode_id",
        "run_id", "runtime_commit", "code_version", "config_version",
        "runtime_mode", "source_branch",
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
        "execution_urgency_status", "execution_urgency_authority",
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
                urgency = _dict(thesis.get("execution_urgency"))
                guardian = _dict(nested_exit.get("guardian"))
                row = {
                    "schema_version": source.get("schema_version"),
                    **_identity(nested_entry or source),
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
                    "execution_urgency_status": urgency.get("status"),
                    "execution_urgency_authority": urgency.get("authority"),
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
                row.update(_identity(source))
            for name in IDENTITY_FIELDS:
                row.setdefault(name, None)
            row["ts"] = ts
            row["vn"] = _iso(ts, VN)
            rows.append(row)
    return rows[-2000:]


def _opportunity_history(cutoff):
    """Publish recorder dossiers through a strict, public-data allowlist."""
    rows = deque(maxlen=2000)
    if not OPPORTUNITY_WAL.exists():
        return []
    for path in sorted(OPPORTUNITY_WAL.glob("*/*.jsonl")):
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    source = json.loads(line)
                    payload = _dict(source.get("payload"))
                    ts = float(source.get("event_time_ms", 0) or 0) / 1000.0
                except (ValueError, TypeError, AttributeError):
                    continue
                if ts < cutoff or not payload:
                    continue
                why = _dict(payload.get("why_no_entry"))
                after = _dict(payload.get("what_happened_after"))
                frozen = _dict(payload.get("frozen_economics"))
                windows = []
                for item in list(after.get("windows") or ()):
                    item = _dict(item)
                    windows.append({key: item.get(key) for key in (
                        "window_seconds", "valid", "outcome_price",
                        "signed_close_bps", "max_favorable_excursion_bps",
                        "max_adverse_excursion_bps",
                        "economic_screen_passed",
                        "hypothetical_hard_sl_hit",
                    ) if item.get(key) is not None})
                rows.append({
                    "ts": ts,
                    "utc": _iso(ts),
                    "vn": _iso(ts, VN),
                    "version": payload.get("version"),
                    "cycle_id": payload.get("cycle_id"),
                    "causal_episode_id": payload.get("causal_episode_id"),
                    "diagnostic_wave_id": payload.get("diagnostic_wave_id"),
                    "persistent_candidate_id": payload.get(
                        "persistent_metaorder_candidate_id"
                    ),
                    "sample_scope": payload.get("sample_scope"),
                    "anchor_role": payload.get("anchor_role"),
                    "side": payload.get("side"),
                    "decision_count": payload.get("decision_count"),
                    "why_no_entry": {
                        key: why.get(key) for key in (
                            "primary_reason", "origin_reason",
                            "terminal_reason", "all_reasons", "failed_gates",
                            "diagnostic_reasons", "miss_taxonomy",
                        ) if why.get(key) is not None
                    },
                    "what_happened_after": {
                        "windows": windows,
                        "max_favorable_excursion_bps": after.get(
                            "max_favorable_excursion_bps"
                        ),
                        "max_adverse_excursion_bps": after.get(
                            "max_adverse_excursion_bps"
                        ),
                        "hypothetical_hard_sl_hit": after.get(
                            "hypothetical_hard_sl_hit"
                        ),
                        "valid": after.get("valid"),
                        "invalid_reason": after.get("invalid_reason"),
                    },
                    "frozen_economics": {
                        key: frozen.get(key) for key in (
                            "execution_style", "cost_budget_bps",
                            "minimum_net_edge_bps", "commission_verified",
                        ) if frozen.get(key) is not None
                    },
                    "economic_miss_eligible": payload.get(
                        "economic_miss_eligible"
                    ),
                    "raw_screen_passed": payload.get("raw_screen_passed"),
                    "classification": payload.get("classification"),
                    "economic_miss_confirmed": payload.get(
                        "economic_miss_confirmed"
                    ),
                    "missing_confirmation": payload.get(
                        "missing_confirmation"
                    ),
                    "guardian_counterfactual_net_bps": payload.get(
                        "guardian_counterfactual_net_bps"
                    ),
                    "strategy_code_version": payload.get(
                        "strategy_code_version"
                    ),
                    "strategy_config_version": payload.get(
                        "strategy_config_version"
                    ),
                    **_identity(source),
                })
    return list(rows)


def _journal_delta(checkpoint):
    stat = JOURNAL.stat()
    same = (
        checkpoint.get("device") == stat.st_dev
        and checkpoint.get("inode") == stat.st_ino
        and 0 <= int(checkpoint.get("offset", 0)) <= stat.st_size
    )
    has_cursor = bool(checkpoint.get("device") and checkpoint.get("inode"))
    sources = journal_segments.cursor_sources(
        JOURNAL,
        checkpoint.get("device", 0), checkpoint.get("inode", 0),
        checkpoint.get("offset", 0),
    )
    if not has_cursor:
        sources = [(JOURNAL, max(0, stat.st_size - INITIAL_TAIL_BYTES))]
    elif same:
        sources = [(JOURNAL, int(checkpoint.get("offset", 0)))]
    # Publisher telemetry is deliberately bounded; the immutable journal
    # segments remain canonical replay evidence. Never materialize an entire
    # multi-GB historical segment inside this low-memory oneshot.
    rows = deque(maxlen=MAX_DELTA_ROWS)
    offset = 0
    for source, start in sources:
        source_size = source.stat().st_size
        bounded_backfill = bool(
            source != JOURNAL
            and int(start) == 0
            and source_size > INITIAL_TAIL_BYTES
        )
        if bounded_backfill:
            start = max(0, source_size - INITIAL_TAIL_BYTES)
        with source.open("rb") as handle:
            handle.seek(int(start))
            # Only an initial bounded tail can start in the middle of a line.
            # A persisted cursor always points to an exact completed-line
            # boundary; consuming one more line there would silently drop the
            # first event appended after every publish cycle.
            if (
                (not has_cursor and source == JOURNAL and start)
                or (bounded_backfill and start)
            ):
                handle.readline()
            while True:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if source == JOURNAL:
                        offset = line_start
                    break
                if source == JOURNAL:
                    offset = handle.tell()
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if _forensic_journal_event(row):
                    rows.append(row)
    stat = JOURNAL.stat()
    next_checkpoint = {
        "device": stat.st_dev, "inode": stat.st_ino, "offset": offset,
        "journal_size": stat.st_size,
    }
    return list(rows), next_checkpoint


def _service_state(name):
    result = _run("systemctl", "is-active", name, check=False)
    return result.stdout.strip() or "unknown"


def _runtime_summary():
    row = _load(RUNTIME_STATE, {})
    position = _dict(row.get("position"))
    ledgers = _dict(row.get("shadow_ledgers"))
    ledger_fields = (
        "trades", "wins", "losses", "breakevens", "realized_pnl",
        "gross_profit", "gross_loss", "stress_25bps_pnl",
    )
    separated_ledgers = {
        name: {
            field: _dict(ledgers.get(name)).get(field)
            for field in ledger_fields
        }
        for name in ("research_probe", "live_like")
    }
    return {
        "trades": row.get("trades"), "wins": row.get("wins"),
        "losses": row.get("losses"), "breakeven": row.get("breakeven"),
        "top_level_demo_scope": "INCLUDES_RESEARCH_PROBE",
        "shadow_ledgers": separated_ledgers,
        "promotion_metric_scope": "LIVE_LIKE_SHADOW_ONLY",
        "balance_usdt": row.get("balance"),
        "position": {
            key: position.get(key) for key in (
                "active", "side", "qty", "entry_price", "opened_at",
                "hard_sl", "best_r", "floor_r", "stage",
            ) if position.get(key) is not None
        } if position else None,
    }


def _latest_wal_record(root):
    paths = sorted(Path(root).glob("*/*.jsonl"))
    for path in reversed(paths):
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                if end <= 0:
                    continue
                cursor = end - 1
                while cursor > 0:
                    handle.seek(cursor)
                    if handle.read(1) == b"\n" and cursor < end - 1:
                        break
                    cursor -= 1
                handle.seek(cursor + 1 if cursor else 0)
                line = handle.readline()
            row = json.loads(line)
            if isinstance(row, dict):
                return row
        except (OSError, ValueError, TypeError):
            continue
    return {}


def _selected(source, names):
    source = _dict(source)
    return {
        name: source.get(name) for name in names
        if source.get(name) is not None
    }


def _market_observations():
    source = _latest_wal_record(FEATURE_WAL)
    if not source:
        return {}
    payload = _dict(source.get("payload"))
    cash = _dict(payload.get("cash_flow"))
    identity = _identity(source)
    common = {
        "ts": float(source.get("event_time_ms", 0) or 0) / 1000.0,
        "event_time_ms": source.get("event_time_ms"),
        **identity,
    }
    binance = {
        **common,
        "futures": {
            **_selected(payload, (
                "first_trade_price", "last_trade_price", "trade_high",
                "trade_low", "buy_qty", "sell_qty", "trade_delta_qty",
                "cvd_btc", "cvd_btc_60s", "liquidation_count",
                "long_liquidation_qty", "short_liquidation_qty",
            )),
            "book": _selected(_dict(payload.get("book")), (
                "best_bid", "best_ask", "mid", "spread_bps",
                "microprice", "microprice_offset_bps", "top_imbalance",
                "bid_depth_5bps", "ask_depth_5bps", "obi_5bps",
                "bid_depth_10bps", "ask_depth_10bps", "obi_10bps",
            )),
            "macro": _selected(_dict(payload.get("macro")), (
                "mark_price", "index_price", "funding_rate",
                "open_interest", "open_interest_change",
            )),
        },
        "spot": _selected(_dict(cash.get("binance_spot")), (
            "trade_count", "buy_qty", "sell_qty", "trade_delta_qty",
            "first_price", "last_price", "high", "low",
        )),
    }
    coinbase = {
        **common,
        "spot": _selected(_dict(cash.get("coinbase_spot")), (
            "trade_count", "buy_qty", "sell_qty", "trade_delta_qty",
            "first_price", "last_price", "high", "low",
        )),
    }
    return {"binance": binance, "coinbase": coinbase}


def _manifest(now):
    bot = _load(BOT_HEALTH, {})
    recorder = _load(RECORDER_HEALTH, {})
    config_id = bot.get("config_version") or bot.get("strategy_config_version")
    manifest = {
        "schema_version": 2,
        "dossier_schema": "FORENSIC_RUN_DOSSIER_V1",
        "run_id": bot.get("run_id"),
        "runtime_commit": bot.get("runtime_commit"),
        "code_version": bot.get("code_version"),
        "config_version": config_id,
        "runtime_mode": bot.get("runtime_mode"),
        "run_execution_mode": bot.get("runtime_execution_mode"),
        "recorded_at": _iso(now),
        "recorded_at_ms": int(now * 1000),
        "source_branch": bot.get("source_branch"),
        "source_repository_branch": "main",
        "publisher_commit": runtime_commit(ROOT),
        "publisher_source_branch": source_branch(ROOT),
        "recorder": {
            key: recorder.get(key) for key in (
                "run_id", "runtime_commit", "code_version", "config_version",
                "runtime_mode", "source_branch",
            )
        },
        "raw_archive": {
            "configured": False,
            "status": "OFFHOST_DISABLED",
            "reconstruction_guarantee": False,
            "reason": "NO_OFFHOST_TARGET_CONFIGURED",
        },
    }
    manifest["identity_status"] = (
        "COMPLETE" if all(manifest.get(name) for name in IDENTITY_FIELDS)
        else "INCOMPLETE_RUNTIME_HEARTBEAT"
    )
    return manifest


def _runtime_evidence(now, manifest):
    bot = _load(BOT_HEALTH, {})
    recorder = _load(RECORDER_HEALTH, {})
    bot_identity = _identity({
        **bot,
        "config_version": bot.get("config_version")
        or bot.get("strategy_config_version"),
    })
    recorder_identity = _identity(recorder)
    heartbeat = {
        "ts": now, **bot_identity,
        **_selected(bot, (
            "updated_at_ms", "pid", "runtime_execution_mode",
            "system_ready", "trading_enabled", "shadow_demo_enabled",
            "live_exchange_mutations_enabled", "readiness_reason",
            "position_status", "decision_revision", "governor_mode",
            "live_entry_cpu_allowed",
        )),
    }
    data_health = {
        "ts": now, **recorder_identity,
        **_selected(recorder, (
            "updated_at_ms", "status", "current_status", "connections",
            "optional_connections", "received", "written", "dropped",
            "writer_errors", "decision_tap_parse_errors", "depth",
            "sequence_gap_total", "last_error_at_ms",
        )),
    }
    source_epochs = {
        "ts": now, **recorder_identity,
        "connections": recorder.get("connections"),
        "optional_connections": recorder.get("optional_connections"),
        "last_event_ms": recorder.get("last_event_ms"),
        "last_available_ms": recorder.get("last_available_ms"),
        "event_age_ms": recorder.get("event_age_ms"),
    }
    return heartbeat, data_health, source_epochs


def _assert_evidence_tree(paths):
    invalid = [
        path for path in paths
        if not path.startswith("telemetry/")
        or path.lower().endswith(FORBIDDEN_EVIDENCE_SUFFIXES)
        or "/__pycache__/" in path
        or path.endswith(".gitignore")
    ]
    if invalid:
        raise RuntimeError("non-evidence files staged: " + ", ".join(invalid[:20]))


def _ensure_clone():
    CLONE.parent.mkdir(parents=True, exist_ok=True)
    if not (CLONE / ".git").exists():
        _run("git", "clone", "--no-checkout", REMOTE, str(CLONE))
    _run("git", "config", "user.name", "WStrade Recorder", cwd=CLONE)
    _run("git", "config", "user.email", "wstrade-recorder@localhost", cwd=CLONE)
    # The branch is one parentless rolling evidence snapshot. Unreachable
    # amended blobs are pruned explicitly after a successful push.
    _run("git", "config", "gc.auto", "0", cwd=CLONE)
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
        row = {**row, **_identity(row)}
        merged[key(row)] = row
    return sorted(merged.values(), key=lambda row: float(row.get("ts", 0))) [-limit:]


def _previous_rows(path, legacy_path=None):
    rows = _load_jsonl(path)
    if rows or legacy_path is None:
        return rows
    legacy = _load(legacy_path, [])
    return legacy if isinstance(legacy, list) else []


def _run_bundle(bundles, run_id):
    return bundles.setdefault(_safe_run_id(run_id), {
        "source_rows": [], "decisions": [], "transitions": [],
        "blockers": [], "evidence": [], "orders": [], "fills": [],
        "positions": [], "guardian": [], "opportunities": [],
        "counterfactuals": [],
    })


def _merge_run_file(path, incoming, key, cutoff, limit=2000):
    rows = _merge_unique(_previous_rows(path), incoming, key, cutoff, limit)
    _write_jsonl(path, rows)


def _historical_run_manifest(run_id, rows, now):
    source = next((row for row in rows if isinstance(row, dict)), {})
    identity = _identity(source)
    return {
        "schema_version": 2,
        "dossier_schema": "FORENSIC_RUN_DOSSIER_V1",
        "run_id": source.get("run_id"),
        "runtime_commit": identity.get("runtime_commit"),
        "code_version": identity.get("code_version"),
        "config_version": identity.get("config_version"),
        "runtime_mode": identity.get("runtime_mode"),
        "source_branch": identity.get("source_branch"),
        "identity_status": identity.get("identity_status"),
        "directory_id": run_id,
        "recorded_at": _iso(now),
        "historical_manifest": True,
        "raw_archive": {
            "configured": False,
            "status": "OFFHOST_DISABLED",
            "reconstruction_guarantee": False,
            "reason": "NO_OFFHOST_TARGET_CONFIGURED",
        },
    }


def _write_run_dossier(run_root, bundle, manifest, heartbeat, data_health,
                       source_epochs, cutoff):
    _write_json(run_root / "manifest.json", manifest)
    _merge_run_file(
        run_root / "decisions" / "events.jsonl", bundle["decisions"],
        lambda row: row.get("event_id"), cutoff,
    )
    _merge_run_file(
        run_root / "decisions" / "state_transitions.jsonl",
        bundle["transitions"], lambda row: row.get("transition_id"), cutoff,
    )
    _merge_run_file(
        run_root / "decisions" / "blockers.jsonl", bundle["blockers"],
        lambda row: row.get("blocker_event_id"), cutoff,
    )
    _merge_run_file(
        run_root / "market" / "evidence_slices.jsonl", bundle["evidence"],
        lambda row: row.get("evidence_slice_id"), cutoff,
    )
    _merge_run_file(
        run_root / "execution" / "orders.jsonl", bundle["orders"],
        lambda row: row.get("event_id"), cutoff,
    )
    _merge_run_file(
        run_root / "execution" / "fills.jsonl", bundle["fills"],
        lambda row: row.get("event_id"), cutoff,
    )
    _merge_run_file(
        run_root / "execution" / "positions.jsonl", bundle["positions"],
        lambda row: row.get("event_id") or (
            row.get("cycle_id"), row.get("ts"), row.get("event")
        ), cutoff,
    )
    _merge_run_file(
        run_root / "execution" / "guardian.jsonl", bundle["guardian"],
        lambda row: row.get("event_id") or (
            row.get("cycle_id"), row.get("ts"), row.get("event")
        ), cutoff,
    )
    _merge_run_file(
        run_root / "research" / "opportunities.jsonl",
        bundle["opportunities"], lambda row: (
            row.get("causal_episode_id"), row.get("cycle_id"), row.get("ts")
        ), cutoff,
    )
    _merge_run_file(
        run_root / "research" / "counterfactuals.jsonl",
        bundle["counterfactuals"], lambda row: row.get("counterfactual_id"),
        cutoff,
    )
    if heartbeat is not None:
        _merge_run_file(
            run_root / "runtime" / "heartbeat.jsonl", [heartbeat],
            lambda row: int(float(row.get("ts", 0)) // 180), cutoff,
        )
    if data_health is not None:
        _merge_run_file(
            run_root / "market" / "data_health.jsonl", [data_health],
            lambda row: int(float(row.get("ts", 0)) // 180), cutoff,
        )
    if source_epochs is not None:
        _merge_run_file(
            run_root / "market" / "source_epochs.jsonl", [source_epochs],
            lambda row: int(float(row.get("ts", 0)) // 180), cutoff,
        )
    _write_json(run_root / "market" / "raw_archive.json", manifest["raw_archive"])


def _publish(no_push=False):
    checkpoint = _load(PUBLISH_STATE, {})
    rows, next_checkpoint = _journal_delta(checkpoint)
    remote_sha, _has_head = _ensure_clone()
    target = CLONE / "telemetry"
    legacy = CLONE / "research_live"
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
        _previous_rows(
            target / "execution" / "trades.jsonl", legacy / "trades.json"
        ), trade_rows,
        lambda row: (row.get("event"), row.get("cycle_id"), row.get("ts")),
        cutoff, 2000,
    )
    closed_trades = _closed_trade_history(cutoff)
    opportunities = _opportunity_history(cutoff)
    candidates = _merge_unique(
        _previous_rows(
            target / "decisions" / "candidates.jsonl",
            legacy / "candidates.json",
        ), candidate_rows,
        lambda row: (
            row.get("causal_episode_id") or row.get("persistent_candidate_id"),
            row.get("reason"), row.get("side"),
        ), cutoff, 2000,
    )

    decisions = [row for row in compact if row.get("event") == "DECISION_EVALUATED"]
    manifest = _manifest(now)
    current_run_id = _safe_run_id(manifest.get("run_id"))
    manifest["current_run_path"] = "telemetry/runs/%s" % current_run_id
    manifest["canonical_decision_file"] = (
        manifest["current_run_path"] + "/decisions/events.jsonl"
    )
    manifest["cross_run_indexes"] = {
        "status": "COMPATIBILITY_ONLY",
        "canonical_records_live_under_runs": True,
    }
    summary = {
        "ts": now, "utc": _iso(now), "vn": _iso(now, VN),
        **_identity(manifest),
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
        _previous_rows(
            target / "decisions" / "timeline.jsonl",
            legacy / "timeline.json",
        ), [summary],
        lambda row: int(float(row.get("ts", 0)) // 180), cutoff, 2000,
    )

    heartbeat, data_health, source_epochs = _runtime_evidence(now, manifest)

    run_bundles = {}
    for source_row in rows:
        bundle = _run_bundle(run_bundles, source_row.get("run_id"))
        bundle["source_rows"].append(source_row)
        dossier = _decision_dossier_records(source_row)
        if dossier is not None:
            bundle["decisions"].append(dossier["decision"])
            bundle["evidence"].append(dossier["evidence"])
            bundle["blockers"].extend(dossier["blockers"])
            if dossier["counterfactual"] is not None:
                bundle["counterfactuals"].append(dossier["counterfactual"])
        transition = _transition_record(source_row)
        if transition is not None:
            bundle["transitions"].append(transition)
        execution = _execution_record(source_row)
        if execution is not None:
            event_name = str(source_row.get("event") or "")
            if (
                "ORDER" in event_name
                or event_name.startswith(("ENTRY_", "EXIT_", "SHADOW_MAKER_"))
            ):
                bundle["orders"].append(execution)
            if event_name in {
                "ENTRY", "EXIT", "LIVE_ENTRY", "LIVE_EXIT",
                "LIVE_ORDER_UPDATE", "ENTRY_FILLED_THEN_FLATTENED",
            }:
                bundle["fills"].append(execution)
            if event_name in {
                "ENTRY", "EXIT", "LIVE_ENTRY", "LIVE_EXIT", "POSITION_STATE",
                "ENTRY_FILLED_THEN_FLATTENED",
            }:
                bundle["positions"].append(execution)
            if event_name in {"EXIT", "LIVE_EXIT", "POSITION_STATE"}:
                bundle["guardian"].append(execution)
    for opportunity in opportunities:
        bundle = _run_bundle(run_bundles, opportunity.get("run_id"))
        bundle["source_rows"].append(opportunity)
        bundle["opportunities"].append(opportunity)
    _run_bundle(run_bundles, manifest.get("run_id"))

    heartbeat_rows = _merge_unique(
        _previous_rows(target / "runtime" / "heartbeat.jsonl"), [heartbeat],
        lambda row: int(float(row.get("ts", 0)) // 180), cutoff, 2000,
    )
    health_rows = _merge_unique(
        _previous_rows(target / "runtime" / "data_health.jsonl"), [data_health],
        lambda row: int(float(row.get("ts", 0)) // 180), cutoff, 2000,
    )
    epoch_rows = _merge_unique(
        _previous_rows(target / "runtime" / "source_epochs.jsonl"),
        [source_epochs], lambda row: int(float(row.get("ts", 0)) // 180),
        cutoff, 2000,
    )
    market = _market_observations()
    market_rows = {}
    for venue, observation in market.items():
        market_rows[venue] = _merge_unique(
            _previous_rows(target / "market" / venue / "timeline.jsonl"),
            [observation], lambda row: int(float(row.get("ts", 0)) // 180),
            cutoff, 2000,
        )
    guardian_rows = [
        row for row in trades
        if row.get("event") == "EXIT" and row.get("guardian")
    ]

    _write_json(target / "manifest.json", manifest)
    for run_id, bundle in run_bundles.items():
        run_manifest = (
            manifest if run_id == current_run_id
            else _historical_run_manifest(
                run_id, bundle["source_rows"], now,
            )
        )
        current = run_id == current_run_id
        _write_run_dossier(
            target / "runs" / run_id, bundle, run_manifest,
            heartbeat if current else None,
            data_health if current else None,
            source_epochs if current else None,
            cutoff,
        )
    _write_jsonl(target / "decisions" / "timeline.jsonl", timeline)
    _write_jsonl(target / "decisions" / "candidates.jsonl", candidates)
    _write_jsonl(target / "decisions" / "opportunities.jsonl", opportunities)
    _write_jsonl(target / "execution" / "trades.jsonl", trades)
    _write_jsonl(target / "execution" / "closed_trades.jsonl", closed_trades)
    _write_jsonl(target / "execution" / "guardian.jsonl", guardian_rows)
    _write_jsonl(target / "runtime" / "heartbeat.jsonl", heartbeat_rows)
    _write_jsonl(target / "runtime" / "data_health.jsonl", health_rows)
    _write_jsonl(target / "runtime" / "source_epochs.jsonl", epoch_rows)
    for venue, venue_rows in market_rows.items():
        _write_jsonl(target / "market" / venue / "timeline.jsonl", venue_rows)
    recorder_identity = _identity(_load(RECORDER_HEALTH, {}))
    source_status = {
        "binance": (True, "STRATEGY_AND_MARKET_TRUTH", "public_ws"),
        "coinbase": (True, "CASH_DIRECTION_TRUTH", "coinbase_spot_ws"),
        "bybit": (True, "RESEARCH_ONLY", "bybit_research_ws"),
        "kraken": (False, "NOT_WIRED", None),
    }
    recorder_health = _load(RECORDER_HEALTH, {})
    connections = {
        **_dict(recorder_health.get("connections")),
        **_dict(recorder_health.get("optional_connections")),
    }
    for venue, (configured, role, connection_name) in source_status.items():
        _write_json(target / "market" / venue / "status.json", {
            "recorded_at": _iso(now), "configured": configured, "role": role,
            "connection": connections.get(connection_name)
            if connection_name else None,
            **recorder_identity,
        })

    if no_push:
        print(json.dumps({"generated": str(target), "rows": len(rows), "push": False}))
        return
    # Rebuild the index from evidence only. This also migrates the original
    # polluted parentless telemetry snapshot without retaining source files.
    _run("git", "rm", "-r", "--cached", "--ignore-unmatch", "--", ".", cwd=CLONE)
    # The dedicated clone may still have an untracked .gitignore from the
    # pre-migration repo snapshot. Evidence JSONL must not inherit repo rules.
    _run("git", "add", "-f", "--", "telemetry", cwd=CLONE)
    tracked = _run("git", "ls-files", cwd=CLONE).stdout.splitlines()
    _assert_evidence_tree(tracked)
    staged = _run("git", "diff", "--cached", "--quiet", cwd=CLONE, check=False)
    if staged.returncode == 0:
        _write_json(PUBLISH_STATE, dict(next_checkpoint, published_at=now))
        print(json.dumps({"changed": False, "rows": len(rows)}))
        return
    message = "telemetry: refresh WStrade shadow research snapshot"
    # commit-tree deliberately omits -p: telemetry is always one parentless
    # snapshot, so polluted or stale history can never remain branch-reachable.
    tree_sha = _run("git", "write-tree", cwd=CLONE).stdout.strip()
    commit_sha = _run(
        "git", "commit-tree", tree_sha, "-m", message, cwd=CLONE,
    ).stdout.strip()
    _run("git", "update-ref", "HEAD", commit_sha, cwd=CLONE)
    if remote_sha:
        lease = "--force-with-lease=refs/heads/%s:%s" % (BRANCH, remote_sha)
        _run("git", "push", lease, "origin", "HEAD:refs/heads/" + BRANCH, cwd=CLONE)
    else:
        _run("git", "push", "-u", "origin", "HEAD:refs/heads/" + BRANCH, cwd=CLONE)
    _write_json(PUBLISH_STATE, dict(next_checkpoint, published_at=now))
    _run("git", "reflog", "expire", "--expire=now", "--all", cwd=CLONE)
    _run("git", "prune", "--expire=now", cwd=CLONE)
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
