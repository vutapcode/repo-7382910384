import unittest
from loi_he_thong import phase6_execution_report as report


class Phase6ReportTests(unittest.TestCase):
    def test_insufficient_samples_are_unverified(self):
        r = report.build([], evidence_complete=False)
        self.assertEqual(r["status"], "EXECUTION_URGENCY_UNVERIFIED")
        self.assertFalse(r["authority"])
        self.assertIsNone(r["selection"])
        self.assertIsNone(r["forecast"])

    def test_metrics_do_not_select_execution_style(self):
        twin = {
            "identity": {
                "wal_identity": "wal", "candidate_population_hash": "pop",
                "causal_wave_id": "wave", "guardian_version": "g",
            },
            "branches": [{
                "branch": "TAKER_NOW", "status": "FILLED",
                "outcome": {
                    "status": "CLOSED", "net_bps": 3.0,
                    "hard_stop": False, "capture_ratio": 0.8,
                    "time_to_support": 0.1, "time_to_failure": None,
                    "exit_reason": "GUARDIAN",
                },
            }],
        }
        r = report.build([twin], evidence_complete=True)
        self.assertEqual(
            r["status"], "EXECUTION_URGENCY_OBSERVED_NOT_AUTHORIZED"
        )
        self.assertIsNone(r["selection"])
        self.assertEqual(
            r["metrics_by_execution_style"]["TAKER_NOW"]["sample_count"], 1
        )


if __name__ == "__main__":
    unittest.main()
