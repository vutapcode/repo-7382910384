#!/usr/bin/env python3
"""Phase-8 ordered cutover manifest validator; read-only preparation."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
VERSION="PHASE8_RETIREMENT_MANIFEST_V1"
ORDER=("C8.1_SHARED_THESIS_GUARDIAN","C8.2_CANONICAL_ACTION_POLICY","C8.3_CONTRADICTION_ONLY_EXECUTION","C8.4_APPROVED_PSEUDO_RULE","C8.5_FINAL_ACTIVE_GRAPH_CLEANUP")
REQUIRED=("baseline_commit","candidate_commit","exact_changed_concern","files_imports_config_flags","wal_identity","candidate_population_hash","schema_version","inference_version","economic_version","frozen_cost_hash","guardian_version","hard_risk_version","fill_model_version","empirical_metrics","acceptance_thresholds","rollback_commit","rollback_config","rollback_schema","pre_deploy_flat_state_requirement","post_deploy_runtime_checks","manual_approval_identity","manual_approval_hash")
def _stable(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def validate(m):
    m=dict(m or {}); blockers=[]; concern=m.get("exact_changed_concern")
    if concern not in ORDER: blockers.append("UNKNOWN_CUTOVER_CONCERN")
    changed=list(m.get("changed_authority_concerns") or ())
    if len(changed)!=1 or (concern and changed!=[concern]): blockers.append("CUTOVER_CHANGES_MULTIPLE_AUTHORITY_CONCERNS")
    for k in REQUIRED:
        if m.get(k) in (None,"",[],{}): blockers.append("MISSING_FIELD:"+k)
    if concern=="C8.4_APPROVED_PSEUDO_RULE":
        rules=list(m.get("pseudo_rules") or ())
        if len(rules)!=1: blockers.append("PHASE5_RULE_CUTOVER_MUST_BE_ONE_RULE")
        if m.get("phase5_empirical_promotion_manifest_status")!="PROMOTE": blockers.append("PHASE5_RULE_EMPIRICAL_PROMOTION_MISSING")
    if m.get("pre_deploy_flat_state_requirement")!="FLAT": blockers.append("CUTOVER_REQUIRES_FLAT_STATE")
    if not m.get("manual_approval_hash"): blockers.append("MANUAL_APPROVAL_MISSING")
    out={"version":VERSION,"valid":not blockers,"order_index":ORDER.index(concern)+1 if concern in ORDER else None,"concern":concern,"blockers":sorted(set(blockers)),"authority_change_executed":False}
    out["manifest_hash"]=_stable({**m,"validation":out}); return out
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("manifest",type=Path); a=p.parse_args(argv); out=validate(json.loads(a.manifest.read_text())); print(json.dumps(out,indent=2,sort_keys=True)); return 0 if out["valid"] else 2
if __name__=="__main__": raise SystemExit(main())
