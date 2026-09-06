import unittest

from loi_he_thong import entry_gate_outcome


class EntryGateOutcomeTests(unittest.TestCase):
    def test_structural_reject_keeps_structural_owner(self):
        row = entry_gate_outcome.structural(
            False, "CURRENT_CASH_CONVERSION_MISSING", {"venue": "spot"},
        )
        self.assertFalse(row["allowed"])
        self.assertEqual(row["owner"], "STRUCTURAL")
        self.assertEqual(row["stage"], "FROZEN_ENTRY_CONTRACT")
        self.assertEqual(row["reason"], "CURRENT_CASH_CONVERSION_MISSING")

    def test_flow_wait_is_timing_not_structural(self):
        row = entry_gate_outcome.from_edge_report(False, {
            "soft_wait_reasons": ["WAIT_PERSISTENT_FLOW_EFFICIENCY"],
            "hard_vetoes": [],
        })
        self.assertEqual(row["owner"], "TIMING")
        self.assertEqual(row["stage"], "TIMING_NOW")
        self.assertEqual(row["reason"], "WAIT_PERSISTENT_FLOW_EFFICIENCY")

    def test_absorption_is_thesis_not_timing(self):
        row = entry_gate_outcome.from_edge_report(False, {
            "soft_wait_reasons": [],
            "hard_vetoes": ["FLOW_NONCONVERSION_COMPOSITE_VETO"],
        })
        self.assertEqual(row["owner"], "THESIS")
        self.assertEqual(row["stage"], "CAUSAL_THESIS")

    def test_empirical_edge_failure_is_economics(self):
        row = entry_gate_outcome.from_edge_report(False, {
            "soft_wait_reasons": [],
            "hard_vetoes": ["EMPIRICAL_FORWARD_EDGE_FAIL"],
        })
        self.assertEqual(row["owner"], "ECONOMICS")
        self.assertEqual(row["stage"], "FORWARD_EDGE")

    def test_rejected_outcome_can_never_say_pass(self):
        row = entry_gate_outcome.outcome(
            False, "STRUCTURAL", "FROZEN_ENTRY_CONTRACT", "PASS",
        )
        self.assertEqual(row["reason"], "UNATTRIBUTED_REJECT")

    def test_bias_wait_is_market_truth_not_structural(self):
        row = entry_gate_outcome.from_entry_decision({
            "decision": "WAIT", "reason": "BIAS_NOT_READY",
        })
        self.assertEqual(row["owner"], "THESIS")
        self.assertEqual(row["stage"], "MARKET_TRUTH")

    def test_stale_wait_is_timing_not_structural(self):
        row = entry_gate_outcome.from_entry_decision({
            "decision": "WAIT", "reason": "WAIT_STALE_DATA",
        })
        self.assertEqual(row["owner"], "TIMING")
        self.assertEqual(row["stage"], "TIMING_NOW")


if __name__ == "__main__":
    unittest.main()
