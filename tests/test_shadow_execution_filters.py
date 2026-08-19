import os
import unittest
from unittest.mock import patch

from loi_he_thong import shadow_dynamic_sizing_hook as hook


class ShadowExecutionFiltersTest(unittest.TestCase):
    def test_unverified_filters_do_not_block_or_quantize(self):
        with patch.dict(os.environ, {}, clear=True):
            qty, meta = hook._apply_execution_filters(0.00073, 100000.0)
        self.assertAlmostEqual(qty, 0.00073)
        self.assertEqual(meta["mode"], "UNVERIFIED_FILTERS")
        self.assertFalse(meta["enforced"])
        self.assertIsNone(meta["executable"])

    def test_verified_filters_quantize_and_validate(self):
        env = {
            "SMC_SHADOW_QTY_STEP_BTC": "0.0001",
            "SMC_SHADOW_MIN_QTY_BTC": "0.0005",
            "SMC_SHADOW_MIN_NOTIONAL_USDT": "5",
        }
        with patch.dict(os.environ, env, clear=True):
            qty, meta = hook._apply_execution_filters(0.00073, 100000.0)
        self.assertAlmostEqual(qty, 0.0007)
        self.assertEqual(meta["mode"], "VERIFIED_FILTERS")
        self.assertTrue(meta["enforced"])
        self.assertTrue(meta["executable"])


if __name__ == "__main__":
    unittest.main()
