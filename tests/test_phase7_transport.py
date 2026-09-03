import unittest
from loi_he_thong import execution_transport_contract as t

class TransportTests(unittest.TestCase):
    def test_unknown_never_blind_resubmit(self):
        cid=t.canonical_client_order_id("run","intent","entry","LONG")
        result=t.after_submit("UNKNOWN",client_order_id=cid)
        self.assertEqual(result["next"],"RECONCILE_REQUIRED"); self.assertFalse(result["resubmit_allowed"])

    def test_fallback_same_client_identity_only_after_reconcile(self):
        cid=t.canonical_client_order_id("run","intent","entry","LONG")
        result=t.after_submit("UNKNOWN",client_order_id=cid,reconciliation="VERIFIED_NOT_FOUND",
                              fallback_transport=t.SECONDARY_TRANSPORT,in_flight_transport=None)
        self.assertTrue(result["resubmit_allowed"]); self.assertEqual(result["required_client_order_id"],cid)

    def test_no_parallel_transport_submit(self):
        cid=t.canonical_client_order_id("run","intent","entry","LONG")
        result=t.after_submit("UNKNOWN",client_order_id=cid,reconciliation="VERIFIED_NOT_FOUND",
                              fallback_transport=t.SECONDARY_TRANSPORT,in_flight_transport=t.PRIMARY_TRANSPORT)
        self.assertFalse(result["resubmit_allowed"]); self.assertEqual(result["next"],"WAIT_IN_FLIGHT")

    def test_binance_transports_not_independent_failure_domains(self):
        status=t.transport_research_status()
        self.assertFalse(status["independent_failure_domains"])
        self.assertFalse(status["user_data_stream_is_submit_transport"])
        self.assertFalse(status["authority"])

if __name__=="__main__": unittest.main()
