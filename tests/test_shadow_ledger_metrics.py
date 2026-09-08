import unittest
from types import SimpleNamespace

from loi_he_thong import shadow_ledger_metrics as ledgers


class ShadowLedgerMetricsTests(unittest.TestCase):
    def test_live_like_label_requires_full_empirical_evidence(self):
        row = ledgers.assignment({
            "shadow_ledger_type": "LIVE_LIKE_SHADOW",
            "live_like_shadow_allowed": True,
            "live_empirical_ok": True,
            "cost_ok": True,
            "commission_verified": True,
            "forward_edge_status": "ACTIVE",
        })
        self.assertTrue(row["valid"])
        self.assertTrue(row["promotion_eligible"])

        forged = ledgers.assignment({
            "shadow_ledger_type": "LIVE_LIKE_SHADOW",
            "live_like_shadow_allowed": False,
        })
        self.assertFalse(forged["valid"])
        self.assertFalse(forged["promotion_eligible"])

    def test_research_probe_requires_bootstrap_authorization(self):
        row = ledgers.assignment({
            "shadow_ledger_type": "RESEARCH_PROBE",
            "research_probe_allowed": True,
            "bootstrap_shadow_allowed": True,
        })
        self.assertTrue(row["valid"])
        self.assertFalse(row["promotion_eligible"])

    def test_research_and_live_like_outcomes_are_separate(self):
        state = SimpleNamespace()
        ledgers.record_close(state, "RESEARCH_PROBE", -1.25, -2.0)
        ledgers.record_close(state, "LIVE_LIKE_SHADOW", 2.5, 1.0)

        snap = ledgers.snapshot(state)
        self.assertEqual(snap["research_probe"]["trades"], 1)
        self.assertEqual(snap["research_probe"]["losses"], 1)
        self.assertEqual(snap["live_like"]["trades"], 1)
        self.assertEqual(snap["live_like"]["wins"], 1)
        self.assertEqual(
            ledgers.promotion_totals(state),
            {
                "trades": 1,
                "gross_profit": 2.5,
                "gross_loss": 0.0,
                "realized": 2.5,
                "stress": 1.0,
            },
        )

    def test_unknown_ledger_is_conservative_research_probe(self):
        state = SimpleNamespace()
        ledgers.record_close(state, "BROKEN_LABEL", 1.0, 0.5)
        snap = ledgers.snapshot(state)
        self.assertEqual(snap["research_probe"]["trades"], 1)
        self.assertEqual(snap["live_like"]["trades"], 0)

    def test_round_trip_restore_preserves_both_ledgers(self):
        source = SimpleNamespace()
        ledgers.record_close(source, "RESEARCH_PROBE", -0.5, -0.75)
        ledgers.record_close(source, "LIVE_LIKE_SHADOW", 0.75, 0.25)
        payload = ledgers.snapshot(source)

        restored = SimpleNamespace()
        ledgers.restore(restored, payload)
        self.assertEqual(ledgers.snapshot(restored), payload)


if __name__ == "__main__":
    unittest.main()
