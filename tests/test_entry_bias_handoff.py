from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "mainnet_tier_s_shadow_risk_launcher.py"


class EntryBiasHandoffRegressionTests(unittest.TestCase):
    def test_wrapper_compiles(self):
        source = WRAPPER.read_text(encoding="utf-8")
        compile(source, str(WRAPPER), "exec")

    def test_bias_handoff_guard_is_wired(self):
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("_entry_evaluate_context_guard", source)
        self.assertIn("BIAS_SIDE_CHANGE", source)
        self.assertIn("BIAS_INVALID_OR_EXPIRED", source)
        self.assertIn("entry_shadow_price_history", source)
        self.assertIn("entry_causal_flow_history", source)
        self.assertIn("base.entry_council.evaluate = _entry_evaluate_context_guard", source)


if __name__ == "__main__":
    unittest.main()
