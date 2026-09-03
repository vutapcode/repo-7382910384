import unittest
from ops import phase8_retirement_manifest as r
class Retirement(unittest.TestCase):
 def good(self):
  return {k:"x" for k in r.REQUIRED}|{"exact_changed_concern":"C8.1_SHARED_THESIS_GUARDIAN","changed_authority_concerns":["C8.1_SHARED_THESIS_GUARDIAN"],"files_imports_config_flags":["a"],"empirical_metrics":{"x":1},"acceptance_thresholds":{"x":1},"post_deploy_runtime_checks":["x"],"pre_deploy_flat_state_requirement":"FLAT"}
 def test_two_concerns_reject(self):
  m=self.good(); m["changed_authority_concerns"]=["C8.1_SHARED_THESIS_GUARDIAN","C8.2_CANONICAL_ACTION_POLICY"]; self.assertIn("CUTOVER_CHANGES_MULTIPLE_AUTHORITY_CONCERNS",r.validate(m)["blockers"])
 def test_one_concern_valid(self): self.assertTrue(r.validate(self.good())["valid"])
 def test_phase5_rule_one_only(self):
  m=self.good(); m["exact_changed_concern"]="C8.4_APPROVED_PSEUDO_RULE"; m["changed_authority_concerns"]=[m["exact_changed_concern"]]; m["pseudo_rules"]=["a","b"]; m["phase5_empirical_promotion_manifest_status"]="PROMOTE"; self.assertIn("PHASE5_RULE_CUTOVER_MUST_BE_ONE_RULE",r.validate(m)["blockers"])
