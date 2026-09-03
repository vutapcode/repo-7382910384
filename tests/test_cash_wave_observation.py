from importlib import import_module
import unittest


wave = import_module("2_suy_luan_mapping.cash_wave_observation")


def _vote(side="ABSTAIN", reason=""):
    return {"vote": side, "reason": reason}


def _segment(start_age, end_age, price="ABSTAIN", flow="ABSTAIN"):
    return {
        "start_age_seconds": float(start_age),
        "end_age_seconds": float(end_age),
        "price": _vote(price),
        "flow": _vote(flow),
    }


class CashWaveObservationTests(unittest.TestCase):
    def test_one_new_converting_segment_is_emerging_not_actionable(self):
        result = wave.infer([
            _segment(0, 15, "LONG", "LONG"),
        ])
        self.assertEqual(result["wave_state"], "EMERGING_CONTROL")
        self.assertEqual(result["raw_side"], "LONG")
        self.assertFalse(result["meaningful_for_action"])

    def test_two_contiguous_converting_segments_establish_control(self):
        result = wave.infer([
            _segment(0, 15, "LONG", "LONG"),
            _segment(15, 60, "LONG", "LONG"),
        ])
        self.assertEqual(result["wave_state"], "CONTROLLED")
        self.assertEqual(result["raw_side"], "LONG")
        self.assertTrue(result["meaningful_for_action"])

    def test_old_long_is_not_kept_alive_by_old_displacement_after_conversion_dies(self):
        result = wave.infer([
            _segment(0, 15, "ABSTAIN", "LONG"),
            _segment(15, 60, "ABSTAIN", "ABSTAIN"),
            _segment(60, 180, "LONG", "LONG"),
        ], previous_side="LONG")
        self.assertEqual(result["wave_state"], "EXHAUSTION")
        self.assertEqual(result["raw_side"], "ABSTAIN")
        self.assertFalse(result["meaningful_for_action"])

    def test_executed_flow_nonconversion_plus_refill_is_absorption(self):
        result = wave.infer([
            _segment(0, 15, "ABSTAIN", "LONG"),
            _segment(15, 60, "LONG", "LONG"),
        ], previous_side="LONG", liquidity=[
            {"side": "LONG", "state": "REFILLING"},
        ])
        self.assertEqual(result["wave_state"], "ABSORPTION")
        self.assertEqual(result["raw_side"], "ABSTAIN")

    def test_opposite_price_without_opposite_flow_conversion_is_pullback(self):
        result = wave.infer([
            _segment(0, 15, "SHORT", "ABSTAIN"),
            _segment(15, 60, "LONG", "LONG"),
        ], previous_side="LONG")
        self.assertEqual(result["wave_state"], "PULLBACK")
        self.assertEqual(result["raw_side"], "LONG")
        self.assertFalse(result["control_transfer_confirmed"])

    def test_recent_opposite_dual_cash_conversion_can_transfer_without_waiting_for_old_180s_lens(self):
        result = wave.infer([
            _segment(0, 15, "SHORT", "SHORT"),
            _segment(15, 60, "SHORT", "SHORT"),
            _segment(60, 180, "LONG", "LONG"),
        ], previous_side="LONG")
        self.assertEqual(result["wave_state"], "CONTROL_TRANSFER")
        self.assertEqual(result["raw_side"], "SHORT")
        self.assertTrue(result["control_transfer_confirmed"])

    def test_flow_price_conflict_releases_direction(self):
        result = wave.infer([
            _segment(0, 15, "LONG", "SHORT"),
            _segment(15, 60, "LONG", "LONG"),
        ], previous_side="LONG")
        self.assertEqual(result["wave_state"], "CONTRADICTED")
        self.assertEqual(result["raw_side"], "ABSTAIN")


if __name__ == "__main__":
    unittest.main()
