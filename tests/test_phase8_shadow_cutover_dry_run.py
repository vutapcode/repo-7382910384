import unittest
from ops import phase8_shadow_cutover_dry_run as d
class DryRun(unittest.TestCase):
 def good(self):
  return {"correct_commit":True,"clean_worktree":True,"artifact_hashes_verified":True,"runtime_precheck":{"mode":"SHADOW","mainnet_armed":False,"position_state":"FLAT","recorder_status":"CURRENT","cpu_15m_coverage":"COMPLETE","cpu_1h_coverage":"COMPLETE","cpu_15m_pct":10,"cpu_1h_pct":10},"readiness":{"decision":"READY_FOR_MANUAL_SHADOW_CUTOVER"},"manual_approval":{"approval_kind":"MANUAL_SHADOW_CUTOVER","approval_hash":"a"},"authority_graph":{"status":"PASS","truth_owners":["truth"],"duplicate_question_owners":[]},"cutover_manifest":{"valid":True,"concern":"C8.1_SHARED_THESIS_GUARDIAN","retired_active_edges":[]},"rollback_manifest":{"valid":True},"postcheck_expectations":{"profile_version":"x","services":["s"],"telemetry":["t"],"sealed_thesis_guardian_trace":"expected","zero_live_exchange_mutation":True,"soak_duration":"72h","rollback_triggers":["x"]}}
 def test_dirty_live_nonflat_reject(self):
  p=self.good(); p["clean_worktree"]=False; p["runtime_precheck"]["mainnet_armed"]=True; p["runtime_precheck"]["position_state"]="LONG"; self.assertEqual(d.evaluate(p)["status"],"NOT_READY")
 def test_manual_missing_reject(self):
  p=self.good(); p["manual_approval"]={}; self.assertIn("MANUAL_APPROVAL_MISSING",d.evaluate(p)["blockers"])
 def test_tool_never_mainnet_or_mutates(self):
  out=d.evaluate(self.good()); self.assertFalse(out["mainnet_ready"]); self.assertFalse(out["mutations_performed"])
 def test_current_production_like_not_ready(self):
  p=self.good(); p["readiness"]={"decision":"NOT_READY"}; self.assertEqual(d.evaluate(p)["status"],"NOT_READY")
 def test_deterministic_hash(self):
  p=self.good(); self.assertEqual(d.evaluate(p)["dry_run_hash"],d.evaluate(p)["dry_run_hash"])
