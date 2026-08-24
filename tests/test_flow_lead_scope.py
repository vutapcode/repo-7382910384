from types import SimpleNamespace
import unittest

from loi_he_thong import flow_lead_engine


class FlowLeadScopeTests(unittest.TestCase):
    def test_warmup_cannot_claim_objective_spot_discovery(self):
        report = flow_lead_engine.analyze(SimpleNamespace(), "LONG")
        self.assertEqual(report["lead_scope"], "TRADE_RELATIVE_CASH_VS_PERP")
        self.assertEqual(report["spot_price_discovery"], "NOT_MEASURED_HERE")
        self.assertEqual(report["lead"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
