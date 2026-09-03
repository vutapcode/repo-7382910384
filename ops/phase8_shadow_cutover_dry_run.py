#!/usr/bin/env python3
"""Read-only Phase-8 manual SHADOW cutover dry-run."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
VERSION="PHASE8_SHADOW_CUTOVER_DRY_RUN_V1"
def _stable(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def _f(v,d=999):
    try:return float(v)
    except:return d
def evaluate(package):
    p=dict(package or {}); b=[]; rt=p.get("runtime_precheck") or {}; ready=p.get("readiness") or {}; graph=p.get("authority_graph") or {}; cut=p.get("cutover_manifest") or {}; rollback=p.get("rollback_manifest") or {}; approval=p.get("manual_approval") or {}
    if not p.get("correct_commit"): b.append("DRYRUN_WRONG_COMMIT")
    if p.get("clean_worktree") is not True: b.append("DRYRUN_DIRTY_WORKTREE")
    if rt.get("mode")!="SHADOW": b.append("DRYRUN_NOT_SHADOW")
    if rt.get("mainnet_armed") is not False: b.append("DRYRUN_MAINNET_ARMED")
    if rt.get("position_state")!="FLAT": b.append("DRYRUN_POSITION_NOT_FLAT")
    if rt.get("recorder_status")!="CURRENT": b.append("DRYRUN_RECORDER_UNHEALTHY")
    if rt.get("cpu_15m_coverage")!="COMPLETE" or rt.get("cpu_1h_coverage")!="COMPLETE": b.append("CPU_WINDOW_COVERAGE_INCOMPLETE")
    if _f(rt.get("cpu_15m_pct"))>=30 or _f(rt.get("cpu_1h_pct"))>=30: b.append("CPU_WINDOW_LIMIT_EXCEEDED")
    if p.get("artifact_hashes_verified") is not True: b.append("DRYRUN_ARTIFACT_HASH_UNVERIFIED")
    if ready.get("decision")!="READY_FOR_MANUAL_SHADOW_CUTOVER": b.append("READINESS_NOT_READY")
    if approval.get("approval_kind")!="MANUAL_SHADOW_CUTOVER" or not approval.get("approval_hash"): b.append("MANUAL_APPROVAL_MISSING")
    if graph.get("status")!="PASS" or len(graph.get("truth_owners") or ())!=1: b.append("CANDIDATE_AUTHORITY_GRAPH_INVALID")
    if graph.get("duplicate_question_owners"): b.append("CANDIDATE_AUTHORITY_CONFLICT")
    if cut.get("valid") is not True: b.append("CUTOVER_MANIFEST_INVALID")
    if not cut.get("retired_active_edges") and cut.get("concern")=="C8.5_FINAL_ACTIVE_GRAPH_CLEANUP": b.append("OLD_IMPORT_RETIREMENT_UNPROVEN")
    if rollback.get("valid") is not True: b.append("ROLLBACK_MANIFEST_INVALID")
    expected=p.get("postcheck_expectations") or {}
    for k in ("profile_version","services","telemetry","sealed_thesis_guardian_trace","zero_live_exchange_mutation","soak_duration","rollback_triggers"):
        if expected.get(k) in (None,"",[],{}): b.append("POSTCHECK_EXPECTATION_MISSING:"+k)
    out={"version":VERSION,"status":"READY_FOR_MANUAL_SHADOW_CUTOVER_DRY_RUN" if not b else "NOT_READY","mainnet_ready":False,"mutations_performed":False,"blockers":sorted(set(b)),"simulated_deploy_only":True}
    out["dry_run_hash"]=_stable(out); return out
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("package",type=Path); a=p.parse_args(argv); out=evaluate(json.loads(a.package.read_text())); print(json.dumps(out,indent=2,sort_keys=True)); return 0 if out["status"]!="NOT_READY" else 2
if __name__=="__main__": raise SystemExit(main())
