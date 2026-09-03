import unittest
from ops import phase8_authority_graph as g
class Graph(unittest.TestCase):
 def base(self):
  return {
   "mainnet_tier_s_lean_launcher.py":"import mainnet_tier_s_shadow_launcher\nimport mainnet_tier_s_shadow_hardened_launcher",
   "mainnet_tier_s_shadow_launcher.py":"from loi_he_thong import market_thesis, execution_causal_revalidation\n# load wstrade_live_execution.py\n# load guardian_s_tier.py\ndef _action_contract(): pass",
   "loi_he_thong/market_thesis.py":"VERSION='x'","loi_he_thong/execution_causal_revalidation.py":"def validate(): pass",
   "3_thuc_thi/wstrade_live_execution.py":"from loi_he_thong import mainnet_safety","3_thuc_thi/ve_si_lenh/guardian_s_tier.py":"def guard(): pass",
   "loi_he_thong/mainnet_safety.py":"def check(): pass","loi_he_thong/auto_promotion.py":"def run(): pass"}
 def test_two_truth_owners_fail(self):
  s=self.base(); s["extra.py"]="PHASE8_TRUTH_OWNER:extra.py"; self.assertIn("MARKET_TRUTH_OWNER_COUNT_INVALID",g.analyze_sources(s)["blockers"])
 def test_execution_reinterpret_fail(self):
  s=self.base(); s["loi_he_thong/execution_causal_revalidation.py"]="BIAS_SIDE_CHANGED"; self.assertIn("EXECUTION_REINTERPRETS_DIRECTION_OR_STRATEGY",g.analyze_sources(s)["blockers"])
 def test_old_brain_fallback_fail(self):
  s=self.base(); s["mainnet_tier_s_shadow_launcher.py"]+="\nimport whale_legacy"; self.assertIn("LEGACY_BRAIN_ACTIVE_OR_FALLBACK",g.analyze_sources(s)["blockers"])
 def test_legacy_readonly_not_active_authority(self):
  s=self.base(); s["compat.py"]="read-only compatibility journal parser"; self.assertIn("compat.py",g.analyze_sources(s)["compatibility_only_readers"])
 def test_deterministic_graph_hash(self):
  s=self.base(); self.assertEqual(g.analyze_sources(s)["graph_hash"],g.analyze_sources(s)["graph_hash"])
