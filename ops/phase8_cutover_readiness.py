#!/usr/bin/env python3
"""Phase-8 read-only cutover readiness validator.

Consumes content-addressed artifacts. It never mutates repository/runtime state.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

VERSION="PHASE8_CUTOVER_READINESS_V1"
DECISIONS=("READY_FOR_MANUAL_SHADOW_CUTOVER","NOT_READY")
REQUIRED_ARTIFACTS=(
 "temporal_replay","shared_thesis","guardian_acceptance","phase5_rules",
 "phase6_economics","phase7_durability","trading_evidence","runtime_sre",
 "manual_approval",
)

def _hash_bytes(data): return hashlib.sha256(data).hexdigest()
def _stable(v): return _hash_bytes(json.dumps(v,sort_keys=True,separators=(",",":")).encode())
def _f(v,d=0.0):
    try:return float(v)
    except (TypeError,ValueError):return d

def load_ref(ref, root=None):
    if not isinstance(ref,dict): raise ValueError("ARTIFACT_REFERENCE_INVALID")
    for k in ("path","sha256","producer","version"):
        if not ref.get(k): raise ValueError(f"ARTIFACT_REFERENCE_MISSING:{k}")
    p=Path(ref["path"])
    if root is not None and not p.is_absolute(): p=Path(root)/p
    raw=p.read_bytes(); actual=_hash_bytes(raw)
    if actual!=str(ref["sha256"]): raise ValueError("ARTIFACT_HASH_MISMATCH")
    try: payload=json.loads(raw)
    except Exception as e: raise ValueError("ARTIFACT_JSON_INVALID") from e
    if str(payload.get("version") or payload.get("schema_version") or "")!=str(ref["version"]):
        raise ValueError("ARTIFACT_VERSION_MISMATCH")
    return payload, {"path":str(ref["path"]),"sha256":actual,"producer":ref["producer"],"version":ref["version"]}

def _same(a,b,fields,blockers,prefix):
    for k in fields:
        if not a.get(k) or not b.get(k): blockers.append(f"{prefix}_MISSING_{k.upper()}")
        elif a.get(k)!=b.get(k): blockers.append(f"{prefix}_MISMATCH_{k.upper()}")

def _gate_temporal(a,b):
    if a.get("synthetic_only"): b.append("SYNTHETIC_ONLY_EVIDENCE")
    if not a.get("wal_identity"): b.append("TEMPORAL_WAL_IDENTITY_MISSING")
    if not a.get("deterministic_hash") or a.get("deterministic_hash")!=a.get("repeat_hash"): b.append("NON_DETERMINISTIC_REPLAY")
    if a.get("availability_time_no_lookahead")!="PASS": b.append("AVAILABILITY_TIME_NO_LOOKAHEAD_UNPROVEN")
    if a.get("epoch_gap_sequence_integrity")!="PASS": b.append("EPOCH_GAP_SEQUENCE_INTEGRITY_UNPROVEN")
    vers=a.get("versions") or {}; vals=[vers.get(k) for k in ("schema","inference","config","code")]
    if any(v in (None,"","UNKNOWN") or str(v).startswith("MIXED:") for v in vals): b.append("TEMPORAL_VERSION_BOUNDARY_INVALID")

def _gate_thesis(a,b):
    if a.get("synthetic_only"): b.append("PHASE4_SYNTHETIC_ONLY")
    if a.get("sealed_entry_handoff")!="PASS": b.append("PHASE4_SEALED_ENTRY_HANDOFF_UNPROVEN")
    if a.get("guardian_same_thesis_hash")!="PASS": b.append("PHASE4_GUARDIAN_SHARED_THESIS_UNPROVEN")
    n=int(a.get("completed_positions",0) or 0); need=int(a.get("acceptance_min_completed_positions",1) or 1)
    if n<need: b.append("PHASE4_SHARED_THESIS_SAMPLE_INSUFFICIENT")
    if not a.get("deterministic_trace_hash") or a.get("deterministic_trace_hash")!=a.get("repeat_trace_hash"): b.append("PHASE4_SHARED_THESIS_NONDETERMINISTIC")
    semantics=a.get("state_semantics") or {}
    if semantics.get("unknown_equals_falsified") or semantics.get("system_unsafe_equals_falsified"): b.append("PHASE4_THESIS_STATE_SEMANTICS_MIXED")

def _gate_guardian(a,b):
    base=a.get("baseline") or {}; cand=a.get("candidate") or {}
    _same(base,cand,("wal_identity","fill_model_version","frozen_cost_hash","hard_risk_version"),b,"GUARDIAN")
    for k in ("premature_exit_rate","late_exit_hard_stop_rate"):
        if _f(cand.get(k),999)>_f(base.get(k),-999): b.append("GUARDIAN_ACCEPTANCE_WORSE_"+k.upper())
    if _f(cand.get("capture_ratio"),-999)<_f(base.get("capture_ratio"),999): b.append("GUARDIAN_CAPTURE_RATIO_DEGRADED")

def _gate_p5(a,b):
    rules=a.get("rules") or []
    if not rules: b.append("PHASE5_RULE_ABLATION_INCOMPLETE"); return
    seen=set()
    for r in rules:
        rid=r.get("rule_id")
        if not rid or rid in seen: b.append("PHASE5_RULE_ID_INVALID"); continue
        seen.add(rid); changed=list(r.get("changed_variables") or [])
        if len(changed)!=1 or changed[0]!=rid: b.append("PHASE5_BATCH_REMOVAL_REJECTED")
        state=str(r.get("evidence_state") or "UNKNOWN")
        if state not in ("UNKNOWN","PRIOR_ONLY","SHADOW_MEASURED_REVIEW_REQUIRED"): b.append("PHASE5_EVIDENCE_STATE_INVALID")
        if r.get("retire_proposed") and state!="SHADOW_MEASURED_REVIEW_REQUIRED": b.append("PHASE5_RETIRE_WITHOUT_EVIDENCE")
        if r.get("retire_proposed") and int(r.get("hidden_consumer_count",1) or 0)!=0: b.append("PHASE5_HIDDEN_CONSUMER_REMAINS")
    expected=set(a.get("expected_rule_ids") or ())
    if expected and seen!=expected: b.append("PHASE5_RULE_ABLATION_INCOMPLETE")

def _gate_p6(a,b):
    if a.get("action_execution_owner_conflict") is not False: b.append("PHASE6_ACTION_EXECUTION_OWNER_CONFLICT")
    n=int(a.get("matched_twin_samples",0) or 0)
    if n<=0: b.append("PHASE6_TWIN_SAMPLE_INSUFFICIENT")
    if a.get("executable_fills")!="PASS": b.append("PHASE6_EXECUTABLE_FILL_UNPROVEN")
    if a.get("cost_application_count_per_outcome")!=1: b.append("PHASE6_COST_APPLICATION_INVALID")
    if a.get("all_outcomes_and_censoring")!="PASS": b.append("PHASE6_LIFETIME_CENSORING_INCOMPLETE")
    if n<=0 and a.get("execution_urgency_status")!="EXECUTION_URGENCY_UNVERIFIED": b.append("PHASE6_ZERO_SAMPLE_FALSE_VERIFICATION")

def _gate_p7(a,b):
    if a.get("offhost_backend_status")!="APPROVED_PRODUCTION_OFFHOST_BACKEND": b.append("PHASE7_PRODUCTION_OFFHOST_BACKEND_UNAPPROVED")
    if a.get("real_restore_status")!="RESTORE_VERIFIED": b.append("PHASE7_REAL_OFFHOST_RESTORE_UNPROVEN")
    if not a.get("sealed_canonical_replay_hash") or a.get("sealed_canonical_replay_hash")!=a.get("restored_canonical_replay_hash"): b.append("PHASE7_RESTORED_REPLAY_HASH_MISMATCH")
    if a.get("ha_deployed"):
        if a.get("external_fencing_proof")!="PASS": b.append("PHASE7_EXTERNAL_FENCING_UNPROVEN")
    elif a.get("standby_execution_authority") is not False or int(a.get("execution_authority_count",0) or 0)>1: b.append("PHASE7_UNCONFIGURED_HA_HAS_AUTHORITY")
    if a.get("secondary_transport_status")=="AVAILABLE" and a.get("secondary_transport_authenticated_proof")!="PASS": b.append("PHASE7_SECONDARY_TRANSPORT_FALSE_AVAILABILITY")

def _gate_trading(a,b):
    if _f(a.get("profit_factor"))<1.25: b.append("TRADING_PF_BELOW_1_25")
    if _f(a.get("net_expectancy"))<=0: b.append("TRADING_NET_EXPECTANCY_NOT_POSITIVE")
    if _f(a.get("lcb"))<0: b.append("TRADING_LCB_NEGATIVE")
    if _f(a.get("stress_total_cost_25bps"))<0: b.append("TRADING_STRESS_25BPS_NEGATIVE")
    if _f(a.get("candidate_economic_miss"),999)>_f(a.get("baseline_economic_miss"),-999): b.append("TRADING_ECONOMIC_MISS_WORSE")
    if _f(a.get("candidate_false_entry"),999)>_f(a.get("baseline_false_entry"),-999): b.append("TRADING_FALSE_ENTRY_WORSE")
    for k in ("distinct_sessions","distinct_days","distinct_regimes"):
        if int(a.get(k,0) or 0)<int((a.get("minimums") or {}).get(k,1) or 1): b.append("TRADING_COHORT_INSUFFICIENT_"+k.upper())
    if a.get("mixed_version_totals") is not False: b.append("TRADING_MIXED_VERSION_TOTALS_REJECTED")

def _gate_runtime(a,b):
    if a.get("mode")!="SHADOW": b.append("RUNTIME_NOT_SHADOW")
    if a.get("mainnet_armed") is not False: b.append("RUNTIME_MAINNET_NOT_DISARMED")
    if a.get("position_state")!="FLAT": b.append("RUNTIME_POSITION_NOT_FLAT")
    if a.get("cpu_15m_coverage")!="COMPLETE" or a.get("cpu_1h_coverage")!="COMPLETE": b.append("CPU_WINDOW_COVERAGE_INCOMPLETE")
    if _f(a.get("cpu_15m_pct"),999)>=30 or _f(a.get("cpu_1h_pct"),999)>=30: b.append("CPU_WINDOW_LIMIT_EXCEEDED")
    if a.get("latency_slo")!="PASS": b.append("RUNTIME_LATENCY_SLO_FAILED")
    if a.get("data_integrity_slo")!="PASS": b.append("RUNTIME_DATA_INTEGRITY_SLO_FAILED")
    if a.get("recorder_status")!="CURRENT": b.append("RECORDER_NOT_CURRENT")
    if int(a.get("recorder_loss_count",1) or 0)>0 or int(a.get("gap_violation_count",1) or 0)>0: b.append("RECORDER_LOSS_OR_GAP_VIOLATION")
    if a.get("authenticated_protection_claimed") and a.get("authenticated_protection_reconciliation")!="PASS": b.append("AUTHENTICATED_PROTECTION_PATH_UNVERIFIED")

def _gate_approval(a,b,candidate_commit=None):
    if a.get("approval_kind")!="MANUAL_SHADOW_CUTOVER": b.append("MANUAL_APPROVAL_MISSING")
    if not a.get("approver_identity") or not a.get("approval_hash"): b.append("MANUAL_APPROVAL_MISSING")
    if str(a.get("producer") or "").lower().find("auto")>=0: b.append("AUTO_PROMOTION_CANNOT_APPROVE")
    if candidate_commit and a.get("candidate_commit")!=candidate_commit: b.append("MANUAL_APPROVAL_CANDIDATE_MISMATCH")

def evaluate(package, *, root=None):
    package=dict(package or {}); refs=package.get("artifacts") or {}; blockers=[]; loaded={}; evidence={}
    for name in REQUIRED_ARTIFACTS:
        if name not in refs: blockers.append("MISSING_ARTIFACT:"+name); continue
        try: loaded[name], evidence[name]=load_ref(refs[name],root=root)
        except (OSError,ValueError) as e: blockers.append(f"INVALID_ARTIFACT:{name}:{e}")
    if "temporal_replay" in loaded:_gate_temporal(loaded["temporal_replay"],blockers)
    if "shared_thesis" in loaded:_gate_thesis(loaded["shared_thesis"],blockers)
    if "guardian_acceptance" in loaded:_gate_guardian(loaded["guardian_acceptance"],blockers)
    if "phase5_rules" in loaded:_gate_p5(loaded["phase5_rules"],blockers)
    if "phase6_economics" in loaded:_gate_p6(loaded["phase6_economics"],blockers)
    if "phase7_durability" in loaded:_gate_p7(loaded["phase7_durability"],blockers)
    if "trading_evidence" in loaded:_gate_trading(loaded["trading_evidence"],blockers)
    if "runtime_sre" in loaded:_gate_runtime(loaded["runtime_sre"],blockers)
    if "manual_approval" in loaded:_gate_approval(loaded["manual_approval"],blockers,package.get("candidate_commit"))
    decision="READY_FOR_MANUAL_SHADOW_CUTOVER" if not blockers else "NOT_READY"
    out={"version":VERSION,"decision":decision,"mainnet_ready":False,"candidate_commit":package.get("candidate_commit"),"artifact_evidence":evidence,"blockers":sorted(set(blockers))}
    out["manifest_hash"]=_stable(out); return out

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("package",type=Path); p.add_argument("--root",type=Path); a=p.parse_args(argv)
    out=evaluate(json.loads(a.package.read_text()),root=a.root); print(json.dumps(out,indent=2,sort_keys=True)); return 0 if out["decision"]==DECISIONS[0] else 2
if __name__=="__main__": raise SystemExit(main())
