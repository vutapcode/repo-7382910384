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


if __name__ == "__main__":
    unittest.main()
