import unittest
from types import SimpleNamespace

from loi_he_thong import execution_transaction as transaction


class ExecutionTransactionTests(unittest.TestCase):
    def test_protection_lifecycle_records_physical_latencies(self):
        state = SimpleNamespace()
        transaction.begin(
            state, intent_id="intent-1", side="LONG", quantity=0.001,
            wall_time=10.0, monotonic_time=100.0,
        )
        transaction.transition(
            state, "ORDER_SENT", wall_time=10.1, monotonic_time=100.1,
            detail={"decision_to_submit_ms": 20.0},
        )
        transaction.transition(
            state, "ACK_KNOWN", wall_time=10.15, monotonic_time=100.15,
            detail={"client_order_id": "entry-1", "order_id": 7},
        )
        transaction.transition(
            state, "FILL_CONFIRMED", wall_time=10.2, monotonic_time=100.2,
        )
        transaction.transition(
            state, "UNPROTECTED_EXPOSURE",
            wall_time=10.21, monotonic_time=100.21,
        )
        transaction.transition(
            state, "PROTECTION_SENT", wall_time=10.3, monotonic_time=100.3,
            detail={"client_algo_id": "stop-1"},
        )
        transaction.transition(
            state, "PROTECTION_ACKNOWLEDGED",
            wall_time=10.35, monotonic_time=100.35,
            detail={"algo_id": 9},
        )
        transaction.transition(
            state, "PROTECTION_VERIFIED",
            wall_time=10.4, monotonic_time=100.4,
            detail={"algo_id": 9},
        )
        final = transaction.transition(
            state, "POSITION_PROTECTED",
            wall_time=10.41, monotonic_time=100.41,
        )
        self.assertAlmostEqual(final["decision_to_submit_ms"], 20.0)
        self.assertAlmostEqual(final["submit_to_ack_ms"], 50.0)
        self.assertAlmostEqual(final["fill_to_protection_submit_ms"], 100.0)
        self.assertAlmostEqual(final["fill_to_protection_ack_ms"], 150.0)
        self.assertAlmostEqual(final["fill_to_protection_verified_ms"], 200.0)
        self.assertEqual(final["protection_client_algo_id"], "stop-1")
        self.assertTrue(final["invariant_ok"])

    def test_impossible_transition_latches_recovery(self):
        state = SimpleNamespace()
        transaction.begin(
            state, intent_id="intent-2", side="SHORT", quantity=0.001,
        )
        final = transaction.transition(state, "POSITION_PROTECTED")
        self.assertEqual(final["state"], "INVARIANT_BROKEN")
        self.assertFalse(final["invariant_ok"])
        self.assertTrue(state.wstrade_execution_recovery_required)
        self.assertTrue(state.execution_unknown)

    def test_repeated_recovery_transition_is_idempotent(self):
        state = SimpleNamespace()
        transaction.begin(
            state, intent_id="intent-3", side="LONG", quantity=0.001,
        )
        transaction.transition(state, "RECOVERY_REQUIRED")
        final = transaction.transition(state, "RECOVERY_REQUIRED")
        self.assertEqual(final["state"], "RECOVERY_REQUIRED")
        self.assertTrue(final["invariant_ok"])
        self.assertTrue(final["transitions"][-1]["detail"]["idempotent"])


if __name__ == "__main__":
    unittest.main()
