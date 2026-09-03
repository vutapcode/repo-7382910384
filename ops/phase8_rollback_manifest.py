#!/usr/bin/env python3
"""Phase-8 operator rollback package validator. It never executes rollback."""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
VERSION="PHASE8_ROLLBACK_MANIFEST_V1"
STATES=("CUTOVER_SHADOW","HEALTH_CHECK","ACCEPT","ENTRY_SEALED","POSITION_RECONCILED","SERVICES_STOPPED","OPERATOR_ROLLBACK","REPLAY/STATE_CHECK","SHADOW_RESTART","MANUAL_ACCEPTANCE")
REQUIRED=("known_good_commit","candidate_commit","service_unit_versions","schema_compatibility","state_journal_compatibility","current_position_requirement","exchange_reconciliation_requirement","stop_order_verification","operator_checklist","evidence_archive_locations","rollback_reason_codes","state_machine")
FORBIDDEN=re.compile(r"\b(git\s+(reset|checkout)|systemctl\s+restart|restart\b|kill\b|pkill\b)\b",re.I)
def _stable(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def validate(m):
    m=dict(m or {}); blockers=[]
    for k in REQUIRED:
        if m.get(k) in (None,"",[],{}): blockers.append("MISSING_FIELD:"+k)
    if m.get("exchange_reconciliation_requirement")!="REQUIRED_BEFORE_ROLLBACK": blockers.append("ROLLBACK_RECONCILIATION_REQUIRED")
    if m.get("current_position_requirement") not in ("FLAT","RECONCILED_EXPOSURE_ONLY"): blockers.append("ROLLBACK_EXPOSURE_STATE_INVALID")
    if m.get("stop_order_verification")!="REQUIRED": blockers.append("ROLLBACK_STOP_ORDER_VERIFICATION_REQUIRED")
    if tuple(m.get("state_machine") or ())!=STATES: blockers.append("ROLLBACK_STATE_MACHINE_INVALID")
    if m.get("allow_concurrent_old_new_brain") is not False: blockers.append("ROLLBACK_CONCURRENT_BRAINS_FORBIDDEN")
    for item in m.get("operator_checklist") or ():
        if FORBIDDEN.search(str(item)): blockers.append("ROLLBACK_MANIFEST_CONTAINS_MUTATING_COMMAND")
    out={"version":VERSION,"valid":not blockers,"blockers":sorted(set(blockers)),"automatic_rollback":False}
    out["manifest_hash"]=_stable({**m,"validation":out}); return out
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("manifest",type=Path); a=p.parse_args(argv); out=validate(json.loads(a.manifest.read_text())); print(json.dumps(out,indent=2,sort_keys=True)); return 0 if out["valid"] else 2
if __name__=="__main__": raise SystemExit(main())
