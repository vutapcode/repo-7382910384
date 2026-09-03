"""Phase-6 empirical execution report.

Descriptive only. It never selects TAKER/MAKER or changes runtime authority.
"""
from __future__ import annotations

from collections import Counter, defaultdict

VERSION = "PHASE6_EXECUTION_EMPIRICAL_REPORT_V1"
AUTHORITY = False


def _mean(values):
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def build(twin_records, *, evidence_complete=False):
    records = [dict(r or {}) for r in twin_records]
    matched = []
    censored = 0
    for record in records:
        identity = dict(record.get("identity") or {})
        for branch in record.get("branches") or ():
            row = dict(branch or {})
            if str(row.get("status") or "").startswith("CENSORED"):
                censored += 1
                continue
            outcome = dict(row.get("outcome") or {})
            if not outcome or outcome.get("status") != "CLOSED":
                if outcome:
                    censored += 1
                continue
            matched.append({
                **identity,
                "branch": row.get("branch"),
                "net_bps": outcome.get("net_bps"),
                "hard_stop": bool(outcome.get("hard_stop")),
                "capture_ratio": outcome.get("capture_ratio"),
                "time_to_support": outcome.get("time_to_support"),
                "time_to_failure": outcome.get("time_to_failure"),
                "exit_reason": outcome.get("exit_reason"),
                "economic_miss": bool(outcome.get("economic_miss", False)),
                "false_entry": bool(outcome.get("false_entry", False)),
            })

    by_branch = defaultdict(list)
    for row in matched:
        by_branch[row["branch"]].append(row)

    metrics = {}
    all_branches = sorted({
        branch.get("branch")
        for record in records
        for branch in (record.get("branches") or ())
        if branch.get("branch")
    })
    for branch in all_branches:
        rows = by_branch.get(branch, [])
        attempted = sum(
            1
            for record in records
            for candidate in (record.get("branches") or ())
            if candidate.get("branch") == branch
        )
        filled = sum(
            1
            for record in records
            for candidate in (record.get("branches") or ())
            if candidate.get("branch") == branch
            and candidate.get("status") == "FILLED"
        )
        metrics[branch] = {
            "sample_count": len(rows),
            "fill_rate": (filled / attempted) if attempted else None,
            "guardian_net_bps": _mean([r["net_bps"] for r in rows]),
            "economic_miss": sum(r["economic_miss"] for r in rows),
            "false_entry": sum(r["false_entry"] for r in rows),
            "capture_ratio": _mean([r["capture_ratio"] for r in rows]),
            "hard_stop_rate": (
                sum(r["hard_stop"] for r in rows) / len(rows) if rows else None
            ),
            "time_to_support": _mean([r["time_to_support"] for r in rows]),
            "time_to_failure": _mean([r["time_to_failure"] for r in rows]),
            "exit_reasons": dict(Counter(r["exit_reason"] for r in rows)),
        }

    status = (
        "EXECUTION_URGENCY_OBSERVED_NOT_AUTHORIZED"
        if matched and evidence_complete
        else "EXECUTION_URGENCY_UNVERIFIED"
    )
    return {
        "version": VERSION,
        "authority": AUTHORITY,
        "status": status,
        "matched_outcome_count": len(matched),
        "censored_count": censored,
        "metrics_by_execution_style": metrics,
        "selection": None,
        "forecast": None,
        "policy": "EMPIRICAL_DESCRIPTIVE_ONLY_NO_MFE_ALPHA_NO_RUNTIME_SELECTION",
    }
