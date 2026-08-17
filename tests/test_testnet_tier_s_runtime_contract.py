from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "testnet_tier_s_launcher.py"

class TestTierSRuntimeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = LAUNCHER.read_text(encoding="utf-8")
        ast.parse(cls.text)

    def test_fast_bias_and_flow_history_capacity(self):
        self.assertIn("BIAS_SCOUT = 0.25", self.text)
        self.assertIn("maxlen=256", self.text)
        self.assertIn("maxlen=5000", self.text)

    def test_entry_requires_independent_price_and_flow(self):
        self.assertIn("S1_cross_venue_price_acceptance", self.text)
        self.assertIn("S2_multi_venue_executed_flow", self.text)
        self.assertIn('s1.get("status")!="PASS"', self.text)
        self.assertIn('s2.get("status")!="PASS"', self.text)
        self.assertIn("_flow_volume_quorum", self.text)

    def test_guardian_refuses_stale_spot(self):
        self.assertIn("_spot_fresh", self.text)
        self.assertIn('guardian_s_decision="HOLD_STALE_SPOT"', self.text)

    def test_legacy_authority_is_disabled_but_readiness_feeds_survive(self):
        self.assertIn('"vong_lap_bao_ve"', self.text)
        self.assertIn('"vong_lap_trailing"', self.text)
        self.assertNotIn('_disable(getattr(app,"tai_nen_live",None),"hung_nen_live_futures")', self.text)

    def test_tp_and_trailing_suppressed_but_stop_fallback_allowed(self):
        self.assertIn('"TAKE_PROFIT"', self.text)
        self.assertIn('2TRAILING_STOP_MARKET"'[1:], self.text)
        start = self.text.index('if kind in {"TAKE_PROFIT"')
        end = self.text.index("return await orig_new", start)
        segment = self.text[start:end]
        self.assertNotIn('"STOP_MARKET"', segment)

if __name__ == "__main__":
    unittest.main()
