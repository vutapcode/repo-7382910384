from importlib import import_module
import unittest


council = import_module("2_suy_luan_mapping.bias_council")


def _price(side):
    return council.vote(side, 0.64, "DUAL_CASH_PRICE_ACCEPTANCE")


def _flow(side="ABSTAIN"):
    if side in ("LONG", "SHORT"):
        return council.vote(side, 0.68, "DUAL_CASH_EXECUTED_FLOW")
    return council.vote(reason="DUAL_CASH_FLOW_BALANCED_OR_MIXED")


def _lens(seconds, price_side, flow_side="ABSTAIN"):
    return {
        "seconds": float(seconds),
        "price": _price(price_side),
        "flow": _flow(flow_side),
    }


class AdaptiveCashRegimeTests(unittest.TestCase):
    def test_micro_opposition_inside_longer_cash_control_is_pullback(self):
        report = council._adaptive_cash_regime([
            _lens(15, "SHORT", "SHORT"),
            _lens(60, "LONG", "ABSTAIN"),
            _lens(180, "LONG", "LONG"),
            _lens(600, "LONG", "LONG"),
        ])
        self.assertEqual(report["raw_side"], "LONG")
        self.assertEqual(report["regime_state"], "PULLBACK")
        self.assertEqual(report["phase"], "PULLBACK_AGAINST_CONTEXT")
        self.assertFalse(report["control_transfer_confirmed"])

    def test_control_transfer_is_evidence_driven_not_timer_driven(self):
        report = council._adaptive_cash_regime([
            _lens(15, "SHORT", "SHORT"),
            _lens(60, "SHORT", "SHORT"),
            _lens(600, "LONG", "ABSTAIN"),
        ])
        self.assertEqual(report["raw_side"], "SHORT")
        self.assertEqual(report["regime_state"], "CONTROL_TRANSFER")
        self.assertEqual(report["phase"], "REVERSAL_CANDIDATE")
        self.assertTrue(report["control_transfer_confirmed"])

    def test_no_one_hour_warmup_is_required_for_established_regime(self):
        report = council._adaptive_cash_regime([
            _lens(15, "LONG", "LONG"),
            _lens(60, "LONG", "LONG"),
            _lens(180, "LONG", "LONG"),
        ])
        self.assertEqual(report["raw_side"], "LONG")
        self.assertEqual(report["regime_state"], "ESTABLISHED")
        self.assertEqual(report["dominant_lens_seconds"], 180.0)

    def test_one_short_lens_is_emerging_not_established(self):
        report = council._adaptive_cash_regime([
            _lens(15, "LONG", "ABSTAIN"),
        ])
        self.assertEqual(report["regime_state"], "EMERGING")
        self.assertFalse(report["control_transfer_confirmed"])
        self.assertLess(
            council._compat_confidence(
                report["regime_state"], report["flow_support"], report["raw_side"]
            ),
            0.55,
        )


if __name__ == "__main__":
    unittest.main()
