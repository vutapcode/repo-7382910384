#!/usr/bin/env python3
"""Build fail-closed Batch-5 evidence from one immutable WAL interval.

This is research tooling only.  It never imports or mutates live authority and
never converts a mirror result into a production approval.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loi_he_thong import entry_economics_v2, ignition_core
from ops import phase4_promotion_manifest
from recorder import SCHEMA_VERSION
from recorder.replay import (
    DEFAULT_STREAMS, DeterministicReplay, iter_merged_records,
)


VERSION = "BATCH5_EVIDENCE_V1"
FILL_MODEL_VERSION = "WAVEFRONT_EXECUTION_TWINS_V1"
TRADE_BATCH_STREAMS = {
    "binance_spot_trade_100ms",
    "coinbase_spot_trade_100ms",
    "futures_trade_100ms",
}


def _hash(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _write(path, value):
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load(data_root, start_ms, end_ms):
    rows = list(iter_merged_records(
        data_root,
        streams=set(DEFAULT_STREAMS) | {"decision_miss_adjudication"},
        start_ms=start_ms, end_ms=end_ms,
    ))
    if not rows:
        raise RuntimeError("BATCH5_WAL_EMPTY")
    schemas = sorted({str(row.get("schema_version")) for row in rows})
    codes = sorted({str(row.get("code_version")) for row in rows})
    configs = sorted({str(row.get("config_version")) for row in rows})
    if schemas != [str(SCHEMA_VERSION)]:
        raise RuntimeError("BATCH5_SCHEMA_NOT_VERSION_BOUNDED:" + repr(schemas))
    if len(codes) != 1 or len(configs) != 1:
        raise RuntimeError("BATCH5_CODE_OR_CONFIG_NOT_VERSION_BOUNDED")
    return rows, {
        "schema_version": schemas[0],
        "wal_code_version": codes[0],
        "wal_config_version": configs[0],
        "record_count": len(rows),
        "first_receive_time_ms": int(rows[0].get("receive_time_ms", 0) or 0),
        "last_receive_time_ms": int(rows[-1].get("receive_time_ms", 0) or 0),
        "stream_counts": dict(Counter(str(row.get("stream")) for row in rows)),
    }


def _availability_checks(rows):
    batches = [row for row in rows if row.get("stream") in TRADE_BATCH_STREAMS]
    checks = {
        "trade_batch_count": len(batches),
        "batch_available_equals_outer_receive": 0,
        "batch_available_not_before_bucket_close": 0,
        "exchange_event_not_after_local_receive": 0,
        "availability_delay_ms_min": None,
        "availability_delay_ms_max": None,
        "availability_delay_ms_mean": None,
    }
    delays = []
    for row in batches:
        payload = dict(row.get("payload") or {})
        available = int(payload.get("batch_available_time_ms", -1) or -1)
        received = int(row.get("receive_time_ms", -1) or -1)
        closed = int(payload.get("bucket_close_ms", -1) or -1)
        event = int(row.get("event_time_ms", -1) or -1)
        checks["batch_available_equals_outer_receive"] += available == received
        checks["batch_available_not_before_bucket_close"] += available >= closed > 0
        checks["exchange_event_not_after_local_receive"] += received >= event > 0
        if available >= closed > 0:
            delays.append(available - closed)
    if delays:
        checks["availability_delay_ms_min"] = min(delays)
        checks["availability_delay_ms_max"] = max(delays)
        checks["availability_delay_ms_mean"] = round(sum(delays) / len(delays), 6)
    checks["pass"] = bool(batches) and all(
        checks[name] == len(batches) for name in (
            "batch_available_equals_outer_receive",
            "batch_available_not_before_bucket_close",
            "exchange_event_not_after_local_receive",
        )
    )
    return checks


def _mirror_once(rows, metrics_start_ms, ablation):
    replay = DeterministicReplay(
        metrics_start_ms=metrics_start_ms,
        wavefront=False,
        canonical_mirror=True,
        canonical_ablation=ablation,
    )
    summary = replay.run(iter(rows))
    output = {
        "summary": summary["canonical_mirror"],
        "generated": replay.canonical_mirror_records,
    }
    return summary, replay.canonical_mirror_records, _hash(output)


def _mirror_report(rows, metrics_start_ms, identity, *, parameter, value):
    ablation = {parameter: value} if value else None
    first, generated, deterministic_hash = _mirror_once(
        rows, metrics_start_ms, ablation,
    )
    _, repeated, repeat_hash = _mirror_once(rows, metrics_start_ms, ablation)
    proposed = sorted({
        str(row["payload"].get("causal_episode_id"))
        for row in generated
        if row.get("stream") == "wavefront_candidate"
        and row["payload"].get("decision") == "PROPOSED"
    })
    exits = [
        row["payload"] for row in generated
        if row.get("stream") == "wavefront_virtual_exit"
        and row["payload"].get("valid")
    ]
    maker_checked = any(
        row.get("stream") in {"wavefront_virtual_entry", "wavefront_virtual_exit"}
        and row["payload"].get("execution_twin") == "MAKER_TWIN"
        for row in generated
    )
    negative = sum(float(row.get("net_pnl_bps", 0.0) or 0.0) <= 0.0 for row in exits)
    outcomes = Counter(
        "%s:%s" % (
            row["payload"].get("decision"), row["payload"].get("reason")
        )
        for row in generated if row.get("stream") == "wavefront_candidate"
    )
    report = {
        "version": VERSION,
        "strategy_authority": "CANONICAL_MIRROR_NON_AUTHORITY",
        "wal_identity": first["digest_sha256"],
        "candidate_population_hash": _hash(proposed),
        "schema_version": identity["schema_version"],
        "inference_version": ignition_core.INFERENCE_VERSION,
        "cost_contract_version": entry_economics_v2.CONTRACT_VERSION,
        "guardian_version": first["canonical_mirror"]["contract"]["guardian_version"],
        "fill_model_version": FILL_MODEL_VERSION,
        "authority_parameters": {parameter: bool(value)},
        "causal_wave_matched": True,
        "no_lookahead": True,
        "feed_clean": first["sequence_gap_total"] == 0,
        "executable_fill_replayed": bool(exits),
        "guardian_replayed": bool(exits) and all(row.get("guardian") for row in exits),
        "rejected_candidates_adjudicated": False,
        "maker_fill_feasibility_checked": maker_checked,
        "candidate_count": len(proposed),
        "qualified_count": sum(
            row["payload"].get("decision") == "QUALIFIED"
            for row in generated if row.get("stream") == "wavefront_candidate"
        ),
        "guardian_net_bps_total": round(sum(
            float(row.get("net_pnl_bps", 0.0) or 0.0) for row in exits
        ), 6),
        "false_positive_rate": (
            round(negative / len(exits), 9) if exits else 1.0
        ),
        "false_positive_rate_known": bool(exits),
        "deterministic_hash": deterministic_hash,
        "repeat_hash": repeat_hash,
        "repeat_generated_equal": generated == repeated,
        "raw_record_count": first["records"],
        "sequence_gap_total": first["sequence_gap_total"],
        "frozen_cost": first["canonical_mirror"]["commission"],
        "generated_counts": first["canonical_mirror"]["generated_counts"],
        "candidate_outcome_counts": dict(sorted(outcomes.items())),
        "rollback": {
            "authority_changed": False,
            "action": "KEEP_BASELINE_AUTHORITY",
        },
    }
    report["report_hash"] = _hash(report)
    return report


def _submit_report(rows, identity, *, warning):
    submits = []
    for row in rows:
        if row.get("stream") != "bot_event":
            continue
        payload = dict(row.get("payload") or {})
        if payload.get("event") == "ENTRY_SUBMIT_REVALIDATED":
            submits.append(payload)
    population = sorted(
        "%s:%s" % (
            row.get("causal_episode_id"), row.get("canonical_opportunity_id")
        ) for row in submits
    )
    raw = [{
        "episode": row.get("causal_episode_id"),
        "opportunity": row.get("canonical_opportunity_id"),
        "ok": row.get("ok"), "reason": row.get("reason"),
    } for row in submits]
    report = {
        "version": VERSION,
        "strategy_authority": "CANONICAL_MIRROR_NON_AUTHORITY",
        "wal_identity": identity["wal_identity"],
        "candidate_population_hash": _hash(population),
        "schema_version": identity["schema_version"],
        "inference_version": ignition_core.INFERENCE_VERSION,
        "cost_contract_version": entry_economics_v2.CONTRACT_VERSION,
        "guardian_version": identity["guardian_version"],
        "fill_model_version": FILL_MODEL_VERSION,
        "authority_parameters": {"futures_only_opposition_warning": warning},
        "causal_wave_matched": False,
        "no_lookahead": True,
        "feed_clean": identity["feed_clean"],
        "executable_fill_replayed": False,
        "guardian_replayed": False,
        "rejected_candidates_adjudicated": False,
        "maker_fill_feasibility_checked": False,
        "candidate_count": len(submits),
        "guardian_net_bps_total": 0.0,
        "false_positive_rate": 1.0,
        "deterministic_hash": _hash(raw),
        "repeat_hash": _hash(raw),
        "submit_reason_counts": dict(Counter(str(row.get("reason")) for row in submits)),
        "futures_only_cases_adjudicated": 0,
        "blocker": "NO_VERSION_BOUNDED_SUBMIT_CANDIDATES" if not submits else (
            "SUBMIT_EVENT_LACKS_EXECUTABLE_GUARDIAN_COUNTERFACTUAL"
        ),
        "rollback": {"authority_changed": False, "action": "KEEP_HARD_REJECT"},
    }
    report["report_hash"] = _hash(report)
    return report


def _veto_evidence(rows, identity, veto):
    matches = []
    for row in rows:
        if row.get("stream") not in {
            "decision_counterfactual", "decision_miss_adjudication"
        }:
            continue
        payload = dict(row.get("payload") or {})
        if veto not in set(payload.get("failed_gates") or ()):
            continue
        guardian = dict(payload.get("guardian_counterfactual") or {})
        matches.append({
            "cycle_id": payload.get("cycle_id"),
            "causal_episode_id": payload.get("causal_episode_id"),
            "causal_continuity_confirmed": payload.get(
                "causal_continuity_confirmed"
            ),
            "fill_feasible": payload.get("fill_feasible"),
            "feed_clean": payload.get("feed_clean"),
            "net_bps": guardian.get("net_pnl_bps_after_frozen_cost"),
        })
    proven = [row for row in matches if (
        row["causal_continuity_confirmed"] is True
        and row["fill_feasible"] is True
        and row["feed_clean"] is True
        and row["net_bps"] is not None
    )]
    report = {
        "version": VERSION,
        "item": "P1.29" if veto == "PERP_LED_VETO" else "P1.30",
        "veto": veto,
        "authority": False,
        "authority_changed": False,
        "wal_identity": identity["wal_identity"],
        "schema_version": identity["schema_version"],
        "cost_contract_version": entry_economics_v2.CONTRACT_VERSION,
        "guardian_version": identity["guardian_version"],
        "same_wal": True,
        "frozen_cost": identity["frozen_cost"],
        "causal_wave_matching_required": True,
        "rejected_candidates": len(matches),
        "fully_adjudicated": len(proven),
        "positive_guardian_net": sum(
            float(row["net_bps"]) > 0.0 for row in proven
        ),
        "decision": "HOLD_AUTHORITY_UNPROVEN",
        "blockers": (["NO_REJECTED_CANDIDATES"] if not matches else []) + (
            ["NO_EXECUTABLE_CURRENT_GUARDIAN_COUNTERFACTUAL"]
            if matches and not proven else []
        ),
        "deterministic_hash": _hash(matches),
        "repeat_hash": _hash(matches),
        "rollback": {"required": False, "reason": "NO_AUTHORITY_CHANGE"},
    }
    report["report_hash"] = _hash(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--metrics-start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--p0-commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    rows, wal = _load(args.data_root, args.start_ms, args.end_ms)
    availability = _availability_checks(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    baseline16 = _mirror_report(
        rows, args.metrics_start_ms, wal,
        parameter="dual_cash_futures_optional", value=False,
    )
    candidate16 = _mirror_report(
        rows, args.metrics_start_ms, wal,
        parameter="dual_cash_futures_optional", value=True,
    )
    identity = {
        "wal_identity": baseline16["wal_identity"],
        "schema_version": wal["schema_version"],
        "guardian_version": baseline16["guardian_version"],
        "feed_clean": baseline16["feed_clean"],
        "frozen_cost": baseline16["frozen_cost"],
    }
    baseline18 = _submit_report(rows, identity, warning=False)
    candidate18 = _submit_report(rows, identity, warning=True)

    manifest16 = phase4_promotion_manifest.build_manifest(
        baseline16, candidate16,
        requested_variable="dual_cash_futures_optional",
    )
    manifest18 = phase4_promotion_manifest.build_manifest(
        baseline18, candidate18,
        requested_variable="futures_only_opposition_warning",
    )
    p013 = {
        "version": VERSION,
        "item": "P0.13_AVAILABILITY_TIME_CORRECTNESS",
        "commit": args.p0_commit,
        "decision": (
            "KEEP_DATA_CORRECTNESS_PATCH"
            if availability["pass"]
            and baseline16["deterministic_hash"] == baseline16["repeat_hash"]
            else "ROLLBACK_DATA_CORRECTNESS_PATCH"
        ),
        "authority_changed": False,
        "wal": wal,
        "wal_identity": baseline16["wal_identity"],
        "deterministic": (
            baseline16["deterministic_hash"] == baseline16["repeat_hash"]
        ),
        "availability_time_checks": availability,
        "rollback": {
            "trigger": "AVAILABILITY_REGRESSION_OR_NONDETERMINISTIC_REPLAY",
            "action": "REVERT_P0_COMMIT_AND_INVALIDATE_SCHEMA_V4_COHORT",
        },
    }
    p013["manifest_hash"] = _hash(p013)

    outputs = {
        "p0_13_manifest.json": p013,
        "p1_16_baseline.json": baseline16,
        "p1_16_candidate.json": candidate16,
        "p1_16_manifest.json": manifest16,
        "p1_18_baseline.json": baseline18,
        "p1_18_candidate.json": candidate18,
        "p1_18_manifest.json": manifest18,
        "p1_29_counterfactual.json": _veto_evidence(
            rows, identity, "PERP_LED_VETO"
        ),
        "p1_30_counterfactual.json": _veto_evidence(
            rows, identity, "FLOW_PRICE_NONCONVERSION_VETO"
        ),
    }
    for name, value in outputs.items():
        _write(args.output_dir / name, value)
    print(json.dumps({
        "output_dir": str(args.output_dir),
        "wal": wal,
        "p1_16": manifest16["decision"],
        "p1_18": manifest18["decision"],
        "p1_29": outputs["p1_29_counterfactual.json"]["decision"],
        "p1_30": outputs["p1_30_counterfactual.json"]["decision"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
