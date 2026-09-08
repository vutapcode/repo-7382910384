import sys
import unittest

from loi_he_thong import ignition_core
from loi_he_thong import tier_s_bootstrap_modules as registry


class ModuleRegistryIdentityTests(unittest.TestCase):
    def test_runtime_alias_reuses_canonical_authority_module(self):
        alias = "ignition_core_identity_regression"
        loaded = registry.load_module(
            alias,
            registry.CURRENT_DIR / "loi_he_thong" / "ignition_core.py",
        )
        self.assertIs(loaded, ignition_core)
        self.assertIs(sys.modules[alias], ignition_core)

    def test_repeated_path_with_different_alias_is_one_object(self):
        path = registry.CURRENT_DIR / "loi_he_thong" / "entry_edge_tier.py"
        first = registry.load_module("edge_identity_a", path)
        second = registry.load_module("edge_identity_b", path)
        self.assertIs(first, second)
        self.assertIs(sys.modules["edge_identity_a"], second)
        self.assertIs(sys.modules["edge_identity_b"], second)


if __name__ == "__main__":
    unittest.main()
