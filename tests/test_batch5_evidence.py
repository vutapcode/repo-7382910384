import unittest
from unittest.mock import patch

from ops import batch5_evidence


IDENTITY = {
    "wal_identity": "wal",
    "schema_version": "4",
    "guardian_version": "guardian",
    "feed_clean": True,
    "frozen_cost": {"maker_fee_bps": 2.0, "taker_fee_bps": 5.0},
}


class Batch5EvidenceTests(unittest.TestCase):
    def test_mixed_schema_is_rejected_before_ablation(self):
        rows = [
            {"schema_version": 3, "code_version": "c", "config_version": "x",
             "stream": "mark_price", "receive_time_ms": 1},
            {"schema_version": 4, "code_version": "c", "config_version": "x",
             "stream": "mark_price", "receive_time_ms": 2},
        ]
        with patch.object(batch5_evidence, "iter_merged_records", return_value=rows):
            with self.assertRaisesRegex(RuntimeError, "SCHEMA_NOT_VERSION_BOUNDED"):
                batch5_evidence._load("unused", 1, 2)

    def test_p1_18_without_submit_candidates_fails_closed(self):
        baseline = batch5_evidence._submit_report([], IDENTITY, warning=False)
        candidate = batch5_evidence._submit_report([], IDENTITY, warning=True)
        self.assertEqual(baseline["candidate_count"], 0)
        self.assertEqual(candidate["futures_only_cases_adjudicated"], 0)
        self.assertEqual(candidate["blocker"], "NO_VERSION_BOUNDED_SUBMIT_CANDIDATES")
        self.assertFalse(candidate["rollback"]["authority_changed"])

    def test_trade_batches_prove_availability_not_logical_bucket_end(self):
        rows = [{
            "stream": "binance_spot_trade_100ms",
            "event_time_ms": 1_080,
            "receive_time_ms": 1_205,
            "payload": {
                "bucket_close_ms": 1_099,
                "batch_available_time_ms": 1_205,
            },
        }]
        result = batch5_evidence._availability_checks(rows)
        self.assertTrue(result["pass"])
        self.assertEqual(result["availability_delay_ms_min"], 106)

    def test_bucket_end_masquerading_as_availability_fails_closed(self):
        rows = [{
            "stream": "futures_trade_100ms",
            "event_time_ms": 1_080,
            "receive_time_ms": 1_205,
            "payload": {
                "bucket_close_ms": 1_099,
                "batch_available_time_ms": 1_099,
            },
        }]
        self.assertFalse(batch5_evidence._availability_checks(rows)["pass"])

    def test_veto_needs_full_causal_fill_feed_and_guardian_evidence(self):
        row = {
            "stream": "decision_miss_adjudication",
            "payload": {
                "cycle_id": "c1", "causal_episode_id": "e1",
                "failed_gates": ["PERP_LED_VETO"],
                "causal_continuity_confirmed": True,
                "fill_feasible": True, "feed_clean": True,
                "guardian_counterfactual": {
                    "net_pnl_bps_after_frozen_cost": 3.0,
                },
            },
        }
        report = batch5_evidence._veto_evidence(
            [row], IDENTITY, "PERP_LED_VETO"
        )
        self.assertEqual(report["rejected_candidates"], 1)
        self.assertEqual(report["fully_adjudicated"], 1)
        self.assertEqual(report["positive_guardian_net"], 1)
        self.assertFalse(report["authority_changed"])


if __name__ == "__main__":
    unittest.main()
