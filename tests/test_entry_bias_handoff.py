from importlib import import_module
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "mainnet_tier_s_shadow_risk_launcher.py"
bias = import_module("2_suy_luan_mapping.bias_council")
ignition = import_module("loi_he_thong.ignition_core")


class EntryBiasHandoffRegressionTests(unittest.TestCase):
    def test_wrapper_compiles(self):
        source = WRAPPER.read_text(encoding="utf-8")
        compile(source, str(WRAPPER), "exec")

    def test_bias_handoff_guard_is_wired(self):
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("_entry_evaluate_context_guard", source)
        self.assertIn("BIAS_SIDE_CHANGE", source)
        self.assertIn("BIAS_INVALID_OR_EXPIRED", source)
        self.assertIn('"_ignition_episode"', source)
        self.assertIn("_ignition_seen_bucket", source)
        self.assertIn("Bias never owns causal episodes", source)
        self.assertIn("reset_causal=True", source)
        self.assertNotIn(
            '_reset_entry_context(state, current, reason, now, reset_causal=True)',
            source,
        )
        self.assertNotIn(
            '_reset_entry_context(state, "ABSTAIN", "BIAS_INVALID_OR_EXPIRED", now, reset_causal=True)',
            source,
        )
        self.assertIn("base.entry_council.evaluate = _entry_evaluate_context_guard", source)

    def test_emerging_cash_wave_is_early_information_not_entry_handoff(self):
        emerging = bias._compat_confidence(
            "EMERGING_CONTROL", "LONG", "LONG"
        )
        controlled = bias._compat_confidence(
            "CONTROLLED", "LONG", "LONG"
        )
        self.assertLess(emerging, ignition.BIAS_MIN_CONF)
        self.assertGreaterEqual(controlled, ignition.BIAS_MIN_CONF)

    def test_fees_remain_entry_economics_not_bias_truth(self):
        source = (ROOT / "2_suy_luan_mapping" / "bias_council.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("verified_cost_model", source)
        self.assertNotIn("minimum_net_edge_bps", source)
        self.assertNotIn("commission_verified", source)


if __name__ == "__main__":
    unittest.main()
