import unittest

from loi_he_thong import guardian_action_twins as twins
from ops import guardian_action_twin_report as report


def _sample(index):
    rows=[
        {"available_time_ms":1000,"adverse_wave_ledger":{
            "state":"CHALLENGED","last_observation_hash":f"a{index}"},
         "executable_bbo":{"bid":99.9,"ask":100.0}},
        {"available_time_ms":1100,"adverse_wave_ledger":{
            "state":"TRANSFER_CONFIRMED","last_observation_hash":f"b{index}"},
         "executable_bbo":{"bid":99.8,"ask":99.9}},
    ]
    return twins.replay_twins(
        side="LONG",entry_price=100.0,frozen_cost_bps=10.0,
        observations=rows,wal_identity="wal-1",
        causal_wave_id=f"wave-{index}",guardian_version="guardian-v15",
    )


class GuardianActionTwinReportTests(unittest.TestCase):
    def test_insufficient_evidence_keeps_current_guardian(self):
        result=report.build_report([_sample(1)])
        self.assertEqual(result["decision"],"KEEP_CURRENT_GUARDIAN")
        self.assertIn(
            "GUARDIAN_ACTION_TWIN_SAMPLE_INSUFFICIENT",result["blockers"],
        )
        self.assertFalse(result["authority"])

    def test_deterministic_full_sample_report_never_auto_selects(self):
        samples=[_sample(index) for index in range(30)]
        left=report.build_report(samples)
        right=report.build_report(list(reversed(samples)))
        self.assertEqual(left,right)
        self.assertEqual(left["blockers"],[])
        self.assertEqual(left["sample_count"],30)
        self.assertEqual(left["decision"],"KEEP_CURRENT_GUARDIAN")
        self.assertFalse(left["runtime_policy_selected"])
        self.assertTrue(left["manual_cutover_required"])

    def test_duplicate_or_tampered_sample_fails_closed(self):
        sample=_sample(1)
        duplicate=report.build_report([sample,sample])
        self.assertIn(
            "INVALID_OR_DUPLICATE_TWIN_SAMPLE",duplicate["blockers"],
        )
        damaged=dict(sample);damaged["deterministic_hash"]="bad"
        tampered=report.build_report([damaged])
        self.assertEqual(tampered["invalid_sample_count"],1)


if __name__=="__main__":unittest.main()
