import unittest
from loi_he_thong import execution_contradiction_shadow as shadow


def good_facts():
    return {name: True for name in shadow.FAIL_CLOSED_FACTS}


class Phase6ExecutionShadowTests(unittest.TestCase):
    def test_shadow_never_changes_side(self):
        r = shadow.compare(True, "PASS", good_facts(), side="SHORT")
        self.assertEqual(r["side"], "SHORT")
        self.assertEqual(r["shadow"]["side"], "SHORT")
        self.assertFalse(r["authority"])

    def test_futures_only_opposition_is_not_market_truth_contradiction(self):
        facts = good_facts()
        facts["post_go_contradiction"] = {
            "kind": "OPPOSING_FLOW", "venues": ["futures"]
        }
        r = shadow.contradiction_only(facts, side="LONG")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reason"], "FUTURES_ONLY_OPPOSITION_CONTEXT")

    def test_gap_stale_epoch_hash_mismatch_fail_closed(self):
        for field in ("gap_free", "feed_fresh", "epoch_ok", "sealed_handoff_ok"):
            facts = good_facts()
            facts[field] = False
            self.assertFalse(shadow.contradiction_only(facts, side="LONG")["ok"])
        facts = good_facts()
        facts["post_go_contradiction"] = {"market_truth_hash_mismatch": True}
        self.assertFalse(shadow.contradiction_only(facts, side="LONG")["ok"])

    def test_active_strategy_rejudgment_is_recorded_as_first_diff(self):
        r = shadow.compare(
            False, "CURRENT_IMPULSE_ALREADY_CONSUMED", good_facts(), side="LONG"
        )
        self.assertTrue(r["shadow"]["ok"])
        self.assertEqual(
            r["first_differing_reason"]["active_reason_owner"],
            "STRATEGY_REJUDGMENT",
        )


if __name__ == "__main__":
    unittest.main()
