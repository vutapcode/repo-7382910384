#!/usr/bin/env python3
"""Fail-closed evidence report for the Phase-4 Guardian migration.

The active Guardian and shared-thesis shadow are recorded in the same
``POSITION_STATE`` row, so neither can receive a different market universe.
This tool verifies the sealed Entry truth and deterministically recomputes the
shared observation. It never changes runtime authority.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

import orjson

from loi_he_thong import authority_contracts, market_thesis
from recorder.replay import iter_merged_records, parse_time


VERSION = "PHASE4_GUARDIAN_SHARED_THESIS_REPORT_V1"
MIN_COMPLETED_POSITIONS = 30


def _stable(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _payload(record):
    value = dict(record.get("payload") or record or {})
    return dict(value.get("payload") or value)


def build_report(records):
    rows = sorted(
        (dict(row or {}) for row in records),
        key=lambda row: (
            int(row.get("available_time_ms", 0) or 0),
            int(row.get("event_time_ms", 0) or 0),
            str(row.get("event_id") or ""),
        ),
    )
    wal_hasher = hashlib.sha256()
    code_versions = set()
    config_versions = set()
    status_counts = Counter()
    decision_pairs = Counter()
    positions = defaultdict(lambda: {
        "samples": 0, "first_shared_exit_ms": None,
        "first_legacy_exit_ms": None, "causal_clean": True,
    })
    entries = {}
    exits = {}
    shared_rows = invalid_truth = nondeterministic = 0
    observation_mismatch = causal_mismatch = 0

    for row in rows:
        wal_hasher.update(orjson.dumps(row, option=orjson.OPT_SORT_KEYS))
        if row.get("code_version"):
            code_versions.add(str(row["code_version"]))
        if row.get("config_version"):
            config_versions.add(str(row["config_version"]))
        payload = _payload(row)
        event = str(payload.get("event") or "")
        cycle = str(payload.get("cycle_id") or "")
        if event == "ENTRY" and cycle:
            entries[cycle] = payload
            continue
        if event == "EXIT" and cycle:
            exits[cycle] = payload
            continue
        if event != "POSITION_STATE" or not cycle:
            continue
        guardian = dict(payload.get("guardian_state") or {})
        shadow = dict(guardian.get("shared_thesis_shadow") or {})
        recorded = dict(guardian.get("shared_thesis_observation") or {})
        canonical = dict(guardian.get("canonical_thesis_event") or {})
        if shadow.get("version") != "GUARDIAN_SHARED_THESIS_SHADOW_V1":
            continue
        shared_rows += 1
        state = positions[cycle]
        state["samples"] += 1
        bundle_view = authority_contracts.read_journal_bundle(payload)
        bundle = dict(bundle_view.get("bundle") or {})
        truth = dict((bundle.get("contracts") or {}).get("MARKET_TRUTH") or {})
        if not bundle_view.get("authority_eligible") or not truth:
            invalid_truth += 1
            continue
        replay_a = market_thesis.observe(truth, canonical)
        replay_b = market_thesis.observe(truth, canonical)
        if replay_a != replay_b:
            nondeterministic += 1
        if any(
            replay_a.get(name) != recorded.get(name)
            for name in (
                "status", "reason", "observation_hash",
                "observed_falsifiers", "old_thesis_falsified",
            )
        ):
            observation_mismatch += 1
        episode_ids = {
            str(value) for value in (
                payload.get("causal_episode_id"),
                canonical.get("causal_episode_id"),
                truth.get("causal_episode_id"),
                recorded.get("causal_episode_id"),
            ) if value not in (None, "")
        }
        if len(episode_ids) != 1:
            causal_mismatch += 1
            state["causal_clean"] = False
        status = str(recorded.get("status") or "UNKNOWN")
        legacy_decision = str(guardian.get("decision") or "HOLD")
        shared_decision = str(shadow.get("decision") or "HOLD")
        status_counts[status] += 1
        decision_pairs[(legacy_decision, shared_decision)] += 1
        at_ms = int(row.get("available_time_ms", 0) or 0)
        if shared_decision == "EXIT" and state["first_shared_exit_ms"] is None:
            state["first_shared_exit_ms"] = at_ms
        if legacy_decision == "EXIT" and state["first_legacy_exit_ms"] is None:
            state["first_legacy_exit_ms"] = at_ms

    completed = sorted(set(entries) & set(exits))
    frozen_cost_complete = all(bool(
        (entries[cycle].get("frozen_cost_contract") or {})
        or (entries[cycle].get("execution_cost_plan") or {})
        or (entries[cycle].get("fee_model") or {})
    ) for cycle in completed)
    safety_exits = 0
    causal_exits = 0
    hard_stops = 0
    for cycle in completed:
        payload = exits[cycle]
        reason = str(
            payload.get("risk_reason")
            or (payload.get("risk_state") or {}).get("reason")
            or (payload.get("guardian_state") or {}).get("reason") or "UNKNOWN"
        ).upper()
        if reason in {
            "HARD_SL", "PROFIT_FLOOR", "CRITICAL_FEED_STALE", "DATA_GAP",
            "RECONCILIATION", "UNKNOWN_EXECUTION_STATE",
        }:
            safety_exits += 1
            hard_stops += int(reason == "HARD_SL")
        else:
            causal_exits += 1

    earlier = later = same = 0
    for cycle in completed:
        state = positions.get(cycle) or {}
        shared_at = state.get("first_shared_exit_ms")
        legacy_at = state.get("first_legacy_exit_ms")
        if shared_at is None or legacy_at is None:
            continue
        if shared_at < legacy_at:
            earlier += 1
        elif shared_at > legacy_at:
            later += 1
        else:
            same += 1

    blockers = []
    if shared_rows <= 0:
        blockers.append("NO_SHARED_THESIS_SHADOW_ROWS")
    if len(completed) < MIN_COMPLETED_POSITIONS:
        blockers.append("COMPLETED_POSITION_SAMPLE_INSUFFICIENT")
    if invalid_truth:
        blockers.append("INVALID_OR_LEGACY_ENTRY_TRUTH")
    if nondeterministic or observation_mismatch:
        blockers.append("NON_DETERMINISTIC_SHARED_THESIS_TRACE")
    if causal_mismatch:
        blockers.append("CAUSAL_EPISODE_MISMATCH")
    if len(code_versions) != 1 or len(config_versions) != 1:
        blockers.append("WAL_NOT_VERSION_BOUNDED")
    if completed and not frozen_cost_complete:
        blockers.append("FROZEN_COST_INCOMPLETE")
    # Live parallel telemetry proves semantic and timing differences, but it
    # cannot invent an executable counterfactual close price. A later canonical
    # replay must supply net/capture/hard-stop deltas before authority cutover.
    blockers.append("EXECUTABLE_COUNTERFACTUAL_GUARDIAN_REPLAY_REQUIRED")

    report = {
        "version": VERSION,
        "authority": False,
        "cutover_decision": "KEEP_LEGACY_GUARDIAN",
        "wal_identity": wal_hasher.hexdigest(),
        "code_versions": sorted(code_versions),
        "config_versions": sorted(config_versions),
        "shared_thesis_rows": shared_rows,
        "completed_positions": len(completed),
        "status_counts": dict(sorted(status_counts.items())),
        "decision_pairs": {
            "%s->%s" % pair: count
            for pair, count in sorted(decision_pairs.items())
        },
        "determinism": {
            "recompute_failures": nondeterministic,
            "recorded_observation_mismatches": observation_mismatch,
        },
        "causal_episode_mismatches": causal_mismatch,
        "invalid_entry_truth_rows": invalid_truth,
        "exit_comparison": {
            "shared_earlier": earlier,
            "shared_later": later,
            "same_tick": same,
            "legacy_causal_exits": causal_exits,
            "safety_exits_excluded_from_thesis_labels": safety_exits,
            "legacy_hard_stop_rate": (
                round(hard_stops / len(completed), 8) if completed else None
            ),
            "shared_capture_ratio": None,
            "shared_guardian_net_bps": None,
        },
        "frozen_cost_complete": frozen_cost_complete,
        "blockers": sorted(set(blockers)),
        "rollback_manifest": {
            "current_authority": "GUARDIAN_S_TIER_LEGACY_ACTION_V13",
            "candidate": "GUARDIAN_SHARED_THESIS_SHADOW_V1",
            "candidate_authority": False,
            "rollback_action": "KEEP_CURRENT_GUARDIAN_ACTION_PATH",
        },
        "policy": "NO_CUTOVER_WITHOUT_SAME_WAL_EXECUTABLE_GUARDIAN_OUTCOMES",
    }
    report["report_hash"] = hashlib.sha256(
        _stable(report).encode("utf-8")
    ).hexdigest()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path(
        "/home/ubuntu/smc2026_data"
    ))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rows = iter_merged_records(
        args.data_root, streams={"bot_event"},
        start_ms=parse_time(args.start), end_ms=parse_time(args.end),
    )
    report = build_report(rows)
    body = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(body, encoding="utf-8")
    else:
        print(body, end="")
    return 0 if not report["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
