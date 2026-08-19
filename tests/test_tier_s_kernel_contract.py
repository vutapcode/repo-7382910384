import unittest

import khoi_dong
from loi_he_thong import strategy_profile
from loi_he_thong import tier_s_bootstrap_modules as bootstrap_modules


class TierSKernelContractTests(unittest.TestCase):
    def test_kernel_exports_required_runtime_primitives(self):
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

    def test_kernel_exports_active_data_module_surface(self):
        required = (
            "tai_gia_tick",
            "tai_dong_tien",
            "tai_coinbase",
            "tai_vi_mo",
            "tai_nen_offline",
            "delta_cvd",
            "ATR",
            "giam_sat_he_thong",
        )
        for name in required:
            self.assertTrue(hasattr(khoi_dong, name), name)
            self.assertIs(getattr(khoi_dong, name), getattr(khoi_dong.m, name))

    def test_kernel_keeps_inert_legacy_surface_for_shadow_guards_only(self):
        required = (
            "dat_lenh",
            "footprint",
            "flash_flow",
            "tri_oracle",
            "POC_VAH_VAL",
            "BOS_CHOCH",
        )
        for name in required:
            self.assertTrue(hasattr(khoi_dong, name), name)
            self.assertIs(getattr(khoi_dong, name), getattr(khoi_dong.m, name))

    def test_kernel_is_fail_closed_by_default(self):
        self.assertFalse(bool(khoi_dong.state.execution_allowed))

    def test_module_registry_is_fail_closed_before_launcher_policy(self):
        self.assertFalse(bool(bootstrap_modules.bo_nho_ram.state.execution_allowed))

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
