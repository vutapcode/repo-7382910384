#!/usr/bin/env python3
"""Static, read-only active-authority graph from the canonical launcher."""
from __future__ import annotations
import argparse, ast, hashlib, json, re
from pathlib import Path

VERSION="PHASE8_AUTHORITY_GRAPH_V1"
ENTRYPOINT="mainnet_tier_s_lean_launcher.py"
KNOWN_ACTIVE_FILES=(
 "mainnet_tier_s_lean_launcher.py","mainnet_tier_s_shadow_launcher.py","mainnet_tier_s_shadow_hardened_launcher.py",
 "loi_he_thong/market_thesis.py","loi_he_thong/execution_causal_revalidation.py","loi_he_thong/mainnet_safety.py",
 "loi_he_thong/auto_promotion.py","3_thuc_thi/ve_si_lenh/guardian_s_tier.py","3_thuc_thi/wstrade_live_execution.py",
)
SHADOW_ONLY=(
 "loi_he_thong/phase5_pseudo_authority_shadow.py","loi_he_thong/entry_action_policy.py","loi_he_thong/execution_contradiction_shadow.py",
 "loi_he_thong/phase6_execution_twins.py","loi_he_thong/phase6_execution_report.py","recorder/offhost_durability.py",
 "loi_he_thong/execution_fencing_contract.py","loi_he_thong/execution_transport_contract.py",
)
LEGACY_PATTERNS=("entry_s_tier","whale","volume_profile","vp_entry","smc_legacy")
def _stable(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def _imports(text):
    out=[]
    try: tree=ast.parse(text)
    except SyntaxError:return out
    for n in ast.walk(tree):
        if isinstance(n,ast.Import): out += [a.name for a in n.names]
        elif isinstance(n,ast.ImportFrom):
            m=n.module or ""; out += [m+"."+a.name for a in n.names]
    return sorted(set(out))

def analyze_sources(sources, runtime_profile=None):
    sources={str(k):str(v) for k,v in (sources or {}).items()}; blockers=[]; active_edges=[]
    lean=sources.get(ENTRYPOINT,""); shadow=sources.get("mainnet_tier_s_shadow_launcher.py","")
    execution=sources.get("loi_he_thong/execution_causal_revalidation.py",""); guardian=sources.get("3_thuc_thi/ve_si_lenh/guardian_s_tier.py","")
    safety=sources.get("loi_he_thong/mainnet_safety.py",""); promotion=sources.get("loi_he_thong/auto_promotion.py",""); live=sources.get("3_thuc_thi/wstrade_live_execution.py","")
    if not lean: blockers.append("ACTIVE_ENTRYPOINT_MISSING")
    if "mainnet_tier_s_shadow_launcher" in lean: active_edges.append([ENTRYPOINT,"mainnet_tier_s_shadow_launcher.py","import"])
    if "mainnet_tier_s_shadow_hardened_launcher" in lean: active_edges.append([ENTRYPOINT,"mainnet_tier_s_shadow_hardened_launcher.py","import"])
    truth=[]
    if "market_thesis" in shadow and "loi_he_thong/market_thesis.py" in sources: truth.append("loi_he_thong/market_thesis.py")
    truth += [x for x in re.findall(r"PHASE8_TRUTH_OWNER:([A-Za-z0-9_./-]+)","\n".join(sources.values())) if x not in truth]
    if len(truth)!=1: blockers.append("MARKET_TRUTH_OWNER_COUNT_INVALID")
    action=[]
    if "def _action_contract" in shadow or "TIER_S_ENTRY_ACTION" in shadow: action.append("mainnet_tier_s_shadow_launcher.py")
    if "loi_he_thong/entry_action_policy" in lean or "from loi_he_thong import entry_action_policy" in lean: action.append("loi_he_thong/entry_action_policy.py")
    execution_owners=[]
    if "execution_causal_revalidation" in shadow: execution_owners.append("loi_he_thong/execution_causal_revalidation.py")
    if "wstrade_live_execution.py" in shadow: execution_owners.append("3_thuc_thi/wstrade_live_execution.py")
    execution_roles={"revalidation":"loi_he_thong/execution_causal_revalidation.py" if "loi_he_thong/execution_causal_revalidation.py" in execution_owners else None,
                     "submit":"3_thuc_thi/wstrade_live_execution.py" if "3_thuc_thi/wstrade_live_execution.py" in execution_owners else None}
    guardian_active="guardian_s_tier.py" in shadow; safety_owners=[]
    if "mainnet_safety" in shadow or "mainnet_safety" in live: safety_owners.append("loi_he_thong/mainnet_safety.py")
    if guardian_active: safety_owners.append("3_thuc_thi/ve_si_lenh/guardian_s_tier.py")
    if any(x in execution for x in ("BIAS_SIDE_CHANGED","BIAS_CONFIDENCE_DROPPED","CURRENT_IMPULSE_ALREADY_CONSUMED","TRANSITION_AUTHORITY_DEPENDENCY_INVALID")): blockers.append("EXECUTION_REINTERPRETS_DIRECTION_OR_STRATEGY")
    if guardian_active and any(x in guardian for x in ("def _s1","def _s2","def _s3","S1_price","S2_executed","S3_price")): blockers.append("GUARDIAN_CAUSAL_COUNCIL_STILL_ACTIVE")
    active_text="\n".join((lean,shadow,live,execution,guardian,safety,promotion)).lower()
    if any(p in active_text for p in LEGACY_PATTERNS): blockers.append("LEGACY_BRAIN_ACTIVE_OR_FALLBACK")
    activated_shadow=[p for p in SHADOW_ONLY if Path(p).stem in active_text]
    if activated_shadow: blockers.append("SHADOW_ONLY_MODULE_ACTIVE")
    if "entry_action_policy" in active_text and ("def _action_contract" in shadow or "execution_policy" in shadow): blockers.append("ACTION_POLICY_DUPLICATE_OWNER")
    if any(x in safety for x in ("bias_state =","market_thesis =","market_thesis[","truth_direction =")): blockers.append("SAFETY_REWRITES_MARKET_TRUTH")
    auto_can_promote=bool("PromotionController" in shadow and "promote_callback" in promotion)
    manual_artifact_gate=("MANUAL_APPROVAL" in promotion and "approval_hash" in promotion)
    if auto_can_promote and not manual_artifact_gate: blockers.append("AUTO_PROMOTION_BYPASSES_MANUAL_ARTIFACT")
    config_switches=sorted(set(re.findall(r'WSTRADE_[A-Z0-9_]+|SMC_[A-Z0-9_]+',lean+"\n"+shadow+"\n"+promotion)))
    compatibility=[p for p,t in sources.items() if "compatibility" in t.lower() and p not in {ENTRYPOINT,"mainnet_tier_s_shadow_launcher.py"}]
    out={"version":VERSION,"entrypoint":ENTRYPOINT,"truth_owners":truth,"action_owners":action,"execution_owners":execution_roles,
      "guardian_owner":"3_thuc_thi/ve_si_lenh/guardian_s_tier.py" if guardian_active else None,"safety_owners":safety_owners,
      "active_imports_calls":sorted(active_edges),"compatibility_only_readers":sorted(compatibility),"shadow_only_modules":list(SHADOW_ONLY),
      "shadow_only_activated":activated_shadow,"hidden_fallback_config_switches":config_switches,
      "duplicate_question_owners":[x for x in blockers if x in ("ACTION_POLICY_DUPLICATE_OWNER","EXECUTION_REINTERPRETS_DIRECTION_OR_STRATEGY","GUARDIAN_CAUSAL_COUNCIL_STILL_ACTIVE")],
      "runtime_profile":dict(runtime_profile or {}),"status":"PASS" if not blockers else "FAIL","blockers":sorted(set(blockers)),"read_only":True}
    out["graph_hash"]=_stable(out); return out

def build(root):
    root=Path(root); sources={}
    for p in KNOWN_ACTIVE_FILES+SHADOW_ONLY:
        f=root/p
        if f.exists():
            try:sources[p]=f.read_text(encoding="utf-8")
            except OSError:pass
    return analyze_sources(sources)

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--root",type=Path,default=Path("/home/ubuntu/WStrade")); a=p.parse_args(argv)
    print(json.dumps(build(a.root),indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
