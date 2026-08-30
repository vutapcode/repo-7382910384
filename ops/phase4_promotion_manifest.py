#!/usr/bin/env python3
"""Build a fail-closed Phase-4 one-variable ablation manifest.

This tool does not replay the strategy.  It validates two canonical replay
reports and refuses promotion unless both prove the same evidence universe and
contain executable fill, frozen-cost and Guardian-net results.  Feeding the
retired Whale/Wavefront mirror therefore cannot accidentally authorize live
Entry behavior.
"""

import argparse
import hashlib
import json
from pathlib import Path


VERSION = "PHASE4_PROMOTION_MANIFEST_V1"
REQUIRED_IDENTITY = (
    "wal_identity", "candidate_population_hash", "schema_version",
    "inference_version", "cost_contract_version", "guardian_version",
    "fill_model_version",
)
REQUIRED_EVIDENCE = (
    "causal_wave_matched", "no_lookahead", "feed_clean",
    "executable_fill_replayed", "guardian_replayed",
    "rejected_candidates_adjudicated", "maker_fill_feasibility_checked",
)


def _stable_hash(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _changed_variables(baseline, candidate):
    left = dict(baseline.get("authority_parameters") or {})
    right = dict(candidate.get("authority_parameters") or {})
    keys = sorted(set(left) | set(right))
    return [name for name in keys if left.get(name) != right.get(name)]


def build_manifest(baseline, candidate, *, requested_variable=None):
    baseline = dict(baseline or {})
    candidate = dict(candidate or {})
    blockers = []

    if str(baseline.get("strategy_authority") or "") != "IGNITION_CORE_V1":
        blockers.append("BASELINE_NOT_CANONICAL_LIVE_AUTHORITY")
    if str(candidate.get("strategy_authority") or "") != "IGNITION_CORE_V1":
        blockers.append("CANDIDATE_NOT_CANONICAL_LIVE_AUTHORITY")

    identity = {}
    for field in REQUIRED_IDENTITY:
        left, right = baseline.get(field), candidate.get(field)
        identity[field] = {"baseline": left, "candidate": right, "same": left == right}
        if left in (None, "") or right in (None, ""):
            blockers.append("MISSING_IDENTITY:%s" % field)
        elif left != right:
            blockers.append("IDENTITY_MISMATCH:%s" % field)

    changed = _changed_variables(baseline, candidate)
    if len(changed) != 1:
        blockers.append("ABLATION_MUST_CHANGE_EXACTLY_ONE_AUTHORITY_VARIABLE")
    if requested_variable and changed != [requested_variable]:
        blockers.append("REQUESTED_VARIABLE_MISMATCH")

    evidence = {}
    for field in REQUIRED_EVIDENCE:
        left = bool(baseline.get(field))
        right = bool(candidate.get(field))
        evidence[field] = {"baseline": left, "candidate": right}
        if not left or not right:
            blockers.append("MISSING_CANONICAL_EVIDENCE:%s" % field)

    baseline_count = int(baseline.get("candidate_count", 0) or 0)
    candidate_count = int(candidate.get("candidate_count", 0) or 0)
    if baseline_count <= 0 or candidate_count != baseline_count:
        blockers.append("CANDIDATE_POPULATION_CHANGED")

    baseline_net = _f(baseline.get("guardian_net_bps_total"))
    candidate_net = _f(candidate.get("guardian_net_bps_total"))
    baseline_fp = _f(baseline.get("false_positive_rate"), 1.0)
    candidate_fp = _f(candidate.get("false_positive_rate"), 1.0)
    deterministic = bool(
        baseline.get("deterministic_hash")
        and candidate.get("deterministic_hash")
        and baseline.get("repeat_hash") == baseline.get("deterministic_hash")
        and candidate.get("repeat_hash") == candidate.get("deterministic_hash")
    )
    if not deterministic:
        blockers.append("NON_DETERMINISTIC_REPLAY")
    if candidate_net <= baseline_net:
        blockers.append("NO_GUARDIAN_NET_IMPROVEMENT")
    if candidate_fp > baseline_fp:
        blockers.append("FALSE_POSITIVE_RATE_WORSE")

    manifest = {
        "version": VERSION,
        "decision": "PROMOTE" if not blockers else "REJECT_UNPROVEN",
        "authority": False,
        "requested_variable": requested_variable,
        "changed_variables": changed,
        "identity": identity,
        "canonical_evidence": evidence,
        "candidate_count": baseline_count,
        "guardian_net_bps_delta": round(candidate_net - baseline_net, 6),
        "false_positive_rate_delta": round(candidate_fp - baseline_fp, 9),
        "deterministic": deterministic,
        "blockers": sorted(set(blockers)),
        "policy": "NO_PROMOTION_WITHOUT_CANONICAL_LIVE_AUTHORITY_REPLAY",
    }
    manifest["manifest_hash"] = _stable_hash(manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--variable")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    manifest = build_manifest(
        baseline, candidate, requested_variable=args.variable,
    )
    body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(body, encoding="utf-8")
    else:
        print(body, end="")
    return 0 if manifest["decision"] == "PROMOTE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
