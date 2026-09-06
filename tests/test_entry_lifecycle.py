import unittest
from types import SimpleNamespace

from loi_he_thong import entry_lifecycle


def result(proof_hash="proof-a"):
    return {
        "decision": "GO",
        "side": "LONG",
        "causal_episode_id": "wave-1",
        "authority_dependencies": {
            "current_execution_proof": {
                "proof_hash": proof_hash,
                "observed_at_ms": 1_000,
            },
            "causal_epochs": {
                "binance_spot": 1,
                "coinbase_spot": 2,
            },
        },
        "ignition": {},
    }


class EntryLifecycleTests(unittest.TestCase):
    def test_same_proof_reuses_attempt_id(self):
        self.assertEqual(
            entry_lifecycle.timing_attempt_id(result()),
            entry_lifecycle.timing_attempt_id(result()),
        )

    def test_fresh_proof_opens_new_attempt_in_same_wave(self):
        self.assertNotEqual(
            entry_lifecycle.timing_attempt_id(result("proof-a")),
            entry_lifecycle.timing_attempt_id(result("proof-b")),
        )

    def test_timing_wait_is_nonterminal_and_can_later_pass(self):
        state = SimpleNamespace()
        first = entry_lifecycle.observe(state, result(), {
            "allowed": False, "owner": "TIMING",
            "stage": "TIMING_NOW", "reason": "FLOW_FADING",
        }, economic_opportunity_id=7)
        self.assertEqual(
            [name for name, _ in first["events"]],
            ["TIMING_ATTEMPT_OPENED", "TIMING_ATTEMPT_WAIT",
             "ECONOMIC_OPPORTUNITY_LINKED"],
        )
        second = entry_lifecycle.observe(state, result(), {
            "allowed": True, "owner": "ACTION",
            "stage": "AUTHORIZED", "reason": "PASS",
        }, economic_opportunity_id=7)
        self.assertEqual(
            [name for name, _ in second["events"]],
            ["TIMING_ATTEMPT_PASSED"],
        )

    def test_thesis_reject_has_one_terminal_close(self):
        state = SimpleNamespace()
        gate = {
            "allowed": False, "owner": "THESIS",
            "stage": "CAUSAL_THESIS", "reason": "ABSORBED",
        }
        first = entry_lifecycle.observe(
            state, result(), gate, economic_opportunity_id=3,
        )
        second = entry_lifecycle.observe(
            state, result(), gate, economic_opportunity_id=3,
        )
        self.assertIn("TIMING_ATTEMPT_CLOSED", [x[0] for x in first["events"]])
        self.assertEqual(second["events"], [])

    def test_missing_immutable_proof_has_no_timing_identity(self):
        row = result()
        row["authority_dependencies"] = {}
        self.assertIsNone(entry_lifecycle.timing_attempt_id(row))


if __name__ == "__main__":
    unittest.main()
