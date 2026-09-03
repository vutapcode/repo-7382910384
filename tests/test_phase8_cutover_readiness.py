import json, hashlib, tempfile, unittest
from pathlib import Path
from ops import phase8_cutover_readiness as r

def write_ref(root,name,payload,producer="test"):
    p=Path(root)/f"{name}.json"; p.write_text(json.dumps(payload,sort_keys=True))
    return {"path":p.name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),"producer":producer,"version":payload.get("version") or payload.get("schema_version")}

def artifacts(root):
    vals={
    "temporal_replay":{"version":"T1","wal_identity":"wal","deterministic_hash":"d","repeat_hash":"d","availability_time_no_lookahead":"PASS","epoch_gap_sequence_integrity":"PASS","versions":{"schema":"s","inference":"i","config":"c","code":"k"},"synthetic_only":False},
    "shared_thesis":{"version":"S1","sealed_entry_handoff":"PASS","guardian_same_thesis_hash":"PASS","completed_positions":3,"acceptance_min_completed_positions":2,"deterministic_trace_hash":"x","repeat_trace_hash":"x","state_semantics":{"unknown_equals_falsified":False,"system_unsafe_equals_falsified":False},"synthetic_only":False},
    "guardian_acceptance":{"version":"G1","baseline":{"wal_identity":"wal","fill_model_version":"f","frozen_cost_hash":"c","hard_risk_version":"h","premature_exit_rate":.2,"late_exit_hard_stop_rate":.1,"capture_ratio":.8},"candidate":{"wal_identity":"wal","fill_model_version":"f","frozen_cost_hash":"c","hard_risk_version":"h","premature_exit_rate":.2,"late_exit_hard_stop_rate":.1,"capture_ratio":.8}},
    "phase5_rules":{"version":"P5","expected_rule_ids":["a"],"rules":[{"rule_id":"a","changed_variables":["a"],"evidence_state":"PRIOR_ONLY","retire_proposed":False}]},
    "phase6_economics":{"version":"P6","action_execution_owner_conflict":False,"matched_twin_samples":5,"executable_fills":"PASS","cost_application_count_per_outcome":1,"all_outcomes_and_censoring":"PASS","execution_urgency_status":"EXECUTION_URGENCY_OBSERVED_NOT_AUTHORIZED"},
    "phase7_durability":{"version":"P7","offhost_backend_status":"APPROVED_PRODUCTION_OFFHOST_BACKEND","real_restore_status":"RESTORE_VERIFIED","sealed_canonical_replay_hash":"z","restored_canonical_replay_hash":"z","ha_deployed":False,"standby_execution_authority":False,"execution_authority_count":1,"secondary_transport_status":"UNAPPROVED"},
    "trading_evidence":{"version":"TE","profit_factor":1.3,"net_expectancy":.1,"lcb":0,"stress_total_cost_25bps":0,"baseline_economic_miss":2,"candidate_economic_miss":2,"baseline_false_entry":1,"candidate_false_entry":1,"distinct_sessions":3,"distinct_days":3,"distinct_regimes":2,"minimums":{"distinct_sessions":2,"distinct_days":2,"distinct_regimes":2},"mixed_version_totals":False},
    "runtime_sre":{"version":"RT","mode":"SHADOW","mainnet_armed":False,"position_state":"FLAT","cpu_15m_coverage":"COMPLETE","cpu_1h_coverage":"COMPLETE","cpu_15m_pct":10,"cpu_1h_pct":12,"latency_slo":"PASS","data_integrity_slo":"PASS","recorder_status":"CURRENT","recorder_loss_count":0,"gap_violation_count":0},
    "manual_approval":{"version":"A1","approval_kind":"MANUAL_SHADOW_CUTOVER","approver_identity":"human","approval_hash":"ah","candidate_commit":"cand","producer":"human"}}
    return {k:write_ref(root,k,v,"human" if k=="manual_approval" else "producer") for k,v in vals.items()}

class Readiness(unittest.TestCase):
 def eval(self,mut=None):
  td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); refs=artifacts(td.name)
  if mut: mut(Path(td.name),refs)
  return r.evaluate({"candidate_commit":"cand","artifacts":refs},root=td.name)
 def test_valid_only_manual_shadow(self):
  out=self.eval(); self.assertEqual(out["decision"],"READY_FOR_MANUAL_SHADOW_CUTOVER"); self.assertFalse(out["mainnet_ready"])
 def test_missing_phase4_sample(self):
  def m(root,refs):
   p=root/"shared_thesis.json"; d=json.loads(p.read_text()); d["completed_positions"]=0; refs["shared_thesis"]=write_ref(root,"shared_thesis",d)
  self.assertIn("PHASE4_SHARED_THESIS_SAMPLE_INSUFFICIENT",self.eval(m)["blockers"])
 def test_synthetic_rejected(self):
  def m(root,refs):
   p=root/"temporal_replay.json"; d=json.loads(p.read_text()); d["synthetic_only"]=True; refs["temporal_replay"]=write_ref(root,"temporal_replay",d)
  self.assertEqual(self.eval(m)["decision"],"NOT_READY")
 def test_mismatch_guardian_identity(self):
  def m(root,refs):
   p=root/"guardian_acceptance.json"; d=json.loads(p.read_text()); d["candidate"]["frozen_cost_hash"]="bad"; refs["guardian_acceptance"]=write_ref(root,"guardian_acceptance",d)
  self.assertIn("GUARDIAN_MISMATCH_FROZEN_COST_HASH",self.eval(m)["blockers"])
 def test_nondeterministic(self):
  def m(root,refs):
   p=root/"temporal_replay.json"; d=json.loads(p.read_text()); d["repeat_hash"]="q"; refs["temporal_replay"]=write_ref(root,"temporal_replay",d)
  self.assertIn("NON_DETERMINISTIC_REPLAY",self.eval(m)["blockers"])
 def test_p5_batch_removal_reject(self):
  def m(root,refs):
   p=root/"phase5_rules.json"; d=json.loads(p.read_text()); d["rules"][0]["changed_variables"]=["a","b"]; refs["phase5_rules"]=write_ref(root,"phase5_rules",d)
  self.assertIn("PHASE5_BATCH_REMOVAL_REJECTED",self.eval(m)["blockers"])
 def test_p6_zero_twins(self):
  def m(root,refs):
   p=root/"phase6_economics.json"; d=json.loads(p.read_text()); d["matched_twin_samples"]=0; d["execution_urgency_status"]="EXECUTION_URGENCY_UNVERIFIED"; refs["phase6_economics"]=write_ref(root,"phase6_economics",d)
  self.assertIn("PHASE6_TWIN_SAMPLE_INSUFFICIENT",self.eval(m)["blockers"])
 def test_missing_real_restore(self):
  def m(root,refs):
   p=root/"phase7_durability.json"; d=json.loads(p.read_text()); d["real_restore_status"]="UNPROVEN"; refs["phase7_durability"]=write_ref(root,"phase7_durability",d)
  self.assertIn("PHASE7_REAL_OFFHOST_RESTORE_UNPROVEN",self.eval(m)["blockers"])
 def test_ha_unconfigured_one_authority_ok(self): self.assertNotIn("PHASE7_UNCONFIGURED_HA_HAS_AUTHORITY",self.eval()["blockers"])
 def test_manual_missing(self):
  def m(root,refs): refs.pop("manual_approval")
  self.assertEqual(self.eval(m)["decision"],"NOT_READY")
 def test_cpu_coverage_or_limit(self):
  def m(root,refs):
   p=root/"runtime_sre.json"; d=json.loads(p.read_text()); d["cpu_15m_coverage"]="PARTIAL"; d["cpu_1h_pct"]=30; refs["runtime_sre"]=write_ref(root,"runtime_sre",d)
  b=self.eval(m)["blockers"]; self.assertIn("CPU_WINDOW_COVERAGE_INCOMPLETE",b); self.assertIn("CPU_WINDOW_LIMIT_EXCEEDED",b)
 def test_hash_reference_enforced(self):
  def m(root,refs): refs["temporal_replay"]["sha256"]="0"*64
  self.assertEqual(self.eval(m)["decision"],"NOT_READY")
 def test_deterministic_manifest_hash(self):
  a=self.eval(); b=self.eval(); self.assertEqual(a["manifest_hash"],b["manifest_hash"])
