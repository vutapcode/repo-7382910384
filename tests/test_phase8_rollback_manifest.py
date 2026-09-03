import unittest
from ops import phase8_rollback_manifest as r
class Rollback(unittest.TestCase):
 def good(self):
  return {k:"x" for k in r.REQUIRED}|{"service_unit_versions":{"a":"1"},"schema_compatibility":{"x":1},"state_journal_compatibility":{"x":1},"current_position_requirement":"FLAT","exchange_reconciliation_requirement":"REQUIRED_BEFORE_ROLLBACK","stop_order_verification":"REQUIRED","operator_checklist":["CHECK: seal entry","CHECK: verify flat/reconciled exchange state"],"evidence_archive_locations":["/evidence"],"rollback_reason_codes":["HEALTH_FAIL"],"state_machine":list(r.STATES),"allow_concurrent_old_new_brain":False}
 def test_missing_reconcile_reject(self):
  m=self.good(); m["exchange_reconciliation_requirement"]="OPTIONAL"; self.assertIn("ROLLBACK_RECONCILIATION_REQUIRED",r.validate(m)["blockers"])
 def test_valid(self): self.assertTrue(r.validate(self.good())["valid"])
 def test_mutating_command_reject(self):
  m=self.good(); m["operator_checklist"].append("git reset --hard abc"); self.assertIn("ROLLBACK_MANIFEST_CONTAINS_MUTATING_COMMAND",r.validate(m)["blockers"])
