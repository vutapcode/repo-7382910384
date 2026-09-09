#!/usr/bin/env python3
"""Deterministic, fail-closed report for Guardian action twins."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys


ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from loi_he_thong import guardian_action_twins


VERSION="GUARDIAN_ACTION_TWIN_REPORT_V1"
MIN_EXECUTABLE_SAMPLES=30


def _hash(value):
    body=json.dumps(
        value,sort_keys=True,separators=(",",":"),ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _valid_sample(raw):
    row=dict(raw or {})
    supplied=row.pop("deterministic_hash",None)
    return bool(
        supplied and supplied==_hash(row)
        and row.get("version")==guardian_action_twins.VERSION
        and row.get("authority") is False
        and row.get("runtime_policy_selected") is False
        and row.get("same_wal") is True
        and row.get("same_causal_wave") is True
        and row.get("same_guardian_version") is True
        and row.get("same_frozen_cost") is True
    )


def build_report(samples):
    samples=[dict(row or {}) for row in samples]
    samples.sort(key=lambda row:(
        str(((row.get("branches") or [{}])[0].get("identity") or {}).get(
            "wal_identity", ""
        )),
        str(((row.get("branches") or [{}])[0].get("identity") or {}).get(
            "causal_wave_id", ""
        )),
    ))
    blockers=[]; seen=set(); outcomes=defaultdict(list); invalid=0
    for sample in samples:
        if not _valid_sample(sample):
            invalid+=1;continue
        branches=list(sample.get("branches") or ())
        if {row.get("branch") for row in branches}!=set(
            guardian_action_twins.BRANCHES
        ):
            invalid+=1;continue
        identity=dict(branches[0].get("identity") or {})
        key=(identity.get("wal_identity"),identity.get("causal_wave_id"))
        if not all(dict(row.get("identity") or {})==identity for row in branches):
            invalid+=1;continue
        if key in seen:
            invalid+=1;continue
        seen.add(key)
        for row in branches:
            if row.get("status")=="EXECUTABLE_COUNTERFACTUAL":
                outcomes[str(row["branch"])].append(
                    float(row["net_pnl_bps_after_frozen_cost"])
                )
    if invalid:blockers.append("INVALID_OR_DUPLICATE_TWIN_SAMPLE")
    if len(seen)<MIN_EXECUTABLE_SAMPLES:
        blockers.append("GUARDIAN_ACTION_TWIN_SAMPLE_INSUFFICIENT")
    if any(len(outcomes[name])!=len(seen) for name in guardian_action_twins.BRANCHES):
        blockers.append("UNRESOLVED_EXECUTABLE_COUNTERFACTUAL")
    summaries={}
    for name in guardian_action_twins.BRANCHES:
        values=outcomes[name]
        summaries[name]={
            "executable_samples":len(values),
            "mean_net_bps":round(sum(values)/len(values),8) if values else None,
        }
    report={
        "version":VERSION,"authority":False,
        "decision":"KEEP_CURRENT_GUARDIAN",
        "manual_cutover_required":True,
        "runtime_policy_selected":False,
        "sample_count":len(seen),"invalid_sample_count":invalid,
        "branches":summaries,"blockers":sorted(set(blockers)),
        "same_wal_required":True,"frozen_cost_once_required":True,
        "current_guardian_required":True,"no_lookahead_required":True,
    }
    report["deterministic_hash"]=_hash({
        "report":report,"samples":[row.get("deterministic_hash") for row in samples],
    })
    return report


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("samples",type=Path)
    parser.add_argument("--verify-determinism",action="store_true")
    args=parser.parse_args(argv)
    payload=json.loads(args.samples.read_text())
    samples=payload.get("samples",payload) if isinstance(payload,dict) else payload
    first=build_report(samples)
    if args.verify_determinism:
        second=build_report(samples)
        first["repeat_hash"]=second["deterministic_hash"]
        first["deterministic_replay"]=(
            first["deterministic_hash"]==second["deterministic_hash"]
        )
    print(json.dumps(first,indent=2,sort_keys=True))
    return 0 if not first["blockers"] else 2


if __name__=="__main__":raise SystemExit(main())
