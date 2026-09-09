import unittest

from loi_he_thong import guardian_action_twins as twins


def _rows():
    states=(
        (1000,"HEALTHY",100.0,100.1),
        (1100,"CHALLENGED",99.8,99.9),
        (1200,"RECOVERY_TEST",99.9,100.0),
        (1300,"RECOVERED",100.1,100.2),
        (1400,"TRANSFER_CANDIDATE",99.7,99.8),
        (1500,"TRANSFER_CONFIRMED",99.5,99.6),
    )
    return [{
        "available_time_ms":at,
        "adverse_wave_ledger":{
            "state":state,"last_observation_hash":f"h{at}",
        },
        "executable_bbo":{"bid":bid,"ask":ask},
    } for at,state,bid,ask in states]


class GuardianActionTwinTests(unittest.TestCase):
    def _kwargs(self):
        return {
            "side":"LONG","entry_price":100.0,"frozen_cost_bps":10.0,
            "observations":_rows(),"wal_identity":"wal-1",
            "causal_wave_id":"wave-1","guardian_version":"guardian-v15",
        }

    def test_branches_use_current_executable_bbo_and_cost_once(self):
        result=twins.replay_twins(**self._kwargs())
        rows={row["branch"]:row for row in result["branches"]}
        self.assertEqual(rows["EXIT_AT_CHALLENGE"]["exit_available_time_ms"],1100)
        self.assertEqual(
            rows["EXIT_AT_TRANSFER_CONFIRMED"]["exit_available_time_ms"],1500,
        )
        self.assertEqual(
            rows["HOLD_THROUGH_ONE_RECOVERY"]["exit_available_time_ms"],1500,
        )
        self.assertEqual(rows["EXIT_AT_CHALLENGE"]["gross_pnl_bps"],-20.0)
        self.assertEqual(rows["EXIT_AT_CHALLENGE"]["net_pnl_bps_after_frozen_cost"],-30.0)
        self.assertTrue(all(
            row["frozen_cost_applied_once"] for row in result["branches"]
        ))
        self.assertFalse(result["runtime_policy_selected"])

    def test_replay_is_deterministic(self):
        left=twins.replay_twins(**self._kwargs())
        right=twins.replay_twins(**self._kwargs())
        self.assertEqual(left,right)

    def test_ordering_and_missing_bbo_fail_closed(self):
        kwargs=self._kwargs()
        kwargs["observations"]=list(reversed(_rows()))
        with self.assertRaisesRegex(ValueError,"AVAILABILITY_NOT_MONOTONIC"):
            twins.replay_twins(**kwargs)

        kwargs=self._kwargs()
        kwargs["observations"][1]["executable_bbo"]={}
        result=twins.replay_branch("EXIT_AT_CHALLENGE",**kwargs)
        self.assertEqual(result["status"],"UNRESOLVED_NO_EXECUTABLE_BBO")
        self.assertIsNone(result["net_pnl_bps_after_frozen_cost"])

    def test_no_exit_does_not_use_last_future_price(self):
        kwargs=self._kwargs()
        kwargs["observations"]=kwargs["observations"][:1]
        result=twins.replay_branch("EXIT_AT_TRANSFER_CONFIRMED",**kwargs)
        self.assertEqual(result["status"],"UNRESOLVED_NO_EXIT")
        self.assertIsNone(result["exit_price"])
        self.assertTrue(result["no_lookahead"])


if __name__=="__main__":
    unittest.main()
