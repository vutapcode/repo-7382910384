import unittest
from loi_he_thong.execution_fencing_contract import TestOnlyStrongCoordinator, submission_allowed, takeover_decision, COORDINATOR_STATUS

class FencingTests(unittest.TestCase):
    def test_two_nodes_only_one_valid_token(self):
        c=TestOnlyStrongCoordinator(); c.set_time(1)
        a=c.acquire("a",10); b=c.acquire("b",10)
        self.assertIsNotNone(a); self.assertIsNone(b)
        self.assertTrue(submission_allowed(a,coordinator_reachable=True,coordinator_token=c.current_token(),coordinator_now=c.now)["allowed"])

    def test_stale_old_node_rejected(self):
        c=TestOnlyStrongCoordinator(); c.set_time(0); old=c.acquire("old",1)
        c.set_time(2); new=c.acquire("new",10)
        self.assertFalse(submission_allowed(old,coordinator_reachable=True,coordinator_token=c.current_token(),coordinator_now=c.now)["allowed"])
        self.assertTrue(submission_allowed(new,coordinator_reachable=True,coordinator_token=c.current_token(),coordinator_now=c.now)["allowed"])

    def test_partition_and_clock_jump_never_grant(self):
        c=TestOnlyStrongCoordinator(); lease=c.acquire("a",10)
        result=takeover_decision(lease,coordinator_reachable=False,coordinator_token=None,coordinator_now=0,local_clock=10**12)
        self.assertEqual(result["state"],"NO_ENTRY"); self.assertFalse(result["entry_authority"])

    def test_reconcile_incomplete_no_entry(self):
        c=TestOnlyStrongCoordinator(); lease=c.acquire("a",10)
        result=takeover_decision(lease,coordinator_reachable=True,coordinator_token=c.current_token(),coordinator_now=0,
                                 exchange_reconciled=False,orders_reconciled=False)
        self.assertEqual(result["state"],"SAFETY_ONLY")

    def test_position_missing_stop_requires_protection(self):
        c=TestOnlyStrongCoordinator(); lease=c.acquire("a",10)
        result=takeover_decision(lease,coordinator_reachable=True,coordinator_token=c.current_token(),coordinator_now=0,
                                 exchange_reconciled=True,orders_reconciled=True,exposure_present=True,hard_stop_verified=False)
        self.assertEqual(result["required_action"],"PROTECT_OR_FLATTEN"); self.assertFalse(result["entry_authority"])

    def test_restored_wal_alone_not_authority(self):
        c=TestOnlyStrongCoordinator(); lease=c.acquire("a",10)
        result=takeover_decision(lease,coordinator_reachable=True,coordinator_token=c.current_token(),coordinator_now=0,
                                 exchange_reconciled=False,orders_reconciled=False,epochs_rebuilt=True,data_health_fresh=True,
                                 previous_authority_fenced=True,manual_approval=True)
        self.assertFalse(result["entry_authority"]); self.assertEqual(COORDINATOR_STATUS,"EXTERNAL_FENCING_COORDINATOR_UNAPPROVED")

if __name__=="__main__": unittest.main()
