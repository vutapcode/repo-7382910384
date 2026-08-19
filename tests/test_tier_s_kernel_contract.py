import unittest

import khoi_dong
from loi_he_thong import strategy_profile


class TierSKernelContractTests(unittest.TestCase):
    def test_kernel_exports_required_runtime_surface(self):
        required = (
            "CURRENT_DIR",
            "state",
            "api",
            "load_module",
            "main",
            "acquire_runtime_lock",
            "DuplicateInstanceError",
            "supervisor",
        )
        for name in required:
            self.assertTrue(hasattr(khoi_dong, name), name)

    def test_strategy_profile_is_current_tier_s_causal_metadata(self):
        profile = strategy_profile.current_profile()
        self.assertEqual(profile["name"], "TIER_S_CAUSAL")
        self.assertEqual(profile["mode"], "MAINNET_SHADOW")
        self.assertEqual(profile["market"], "BTCUSDT")
        self.assertIn("ENTRY_COUNCIL", profile["architecture"])
        self.assertIn("ORIGINAL_EDGE_VETO_IMMUTABLE", profile["invariants"])
        self.assertIn("NO_LEGACY_SMC_AUTHORITY", profile["invariants"])


if __name__ == "__main__":
    unittest.main()
