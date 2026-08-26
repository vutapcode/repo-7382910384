import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong import risk_fee_alignment_hook as hook


class RiskFeeAlignmentTest(unittest.TestCase):
    def test_refresh_fee_r_uses_round_trip_fee(self):
        p = SimpleNamespace(entry_price=100000.0, r=550.0, fee_r=0.0)
        hook._refresh_fee_r(p, 9.0)
        self.assertAlmostEqual(p.fee_r, (100000.0 * 18.0 / 10000.0) / 550.0)

    def test_env_overrides_default(self):
        with patch.dict(os.environ, {"SMC_SHADOW_FEE_BPS_PER_SIDE": "7.5"}):
            self.assertEqual(hook._fee_bps_per_side(), 7.5)

    def test_position_verified_cost_plan_overrides_flat_fallback(self):
        p = SimpleNamespace(
            entry_price=100000.0,
            r=500.0,
            fee_r=0.0,
            shadow_cost_plan={"total_cost_bps": 8.5},
        )
        hook._refresh_fee_r(p, 9.0)
        self.assertAlmostEqual(p.fee_r, (100000.0 * 8.5 / 10000.0) / 500.0)

    def test_generic_live_execution_plan_overrides_flat_fallback(self):
        p = SimpleNamespace(
            entry_price=100000.0, r=500.0, fee_r=0.0,
            execution_cost_plan={"total_cost_bps": 7.25},
        )
        hook._refresh_fee_r(p, 9.0)
        self.assertAlmostEqual(p.fee_r, (100000.0 * 7.25 / 10000.0) / 500.0)


if __name__ == "__main__":
    unittest.main()
