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
                "coinbase_spot": 1,
            },
        },
        "ignition": {},
    }


def waiting_result(reason, proof_hash="proof-a", *, epoch=1):
    row = result(proof_hash)
    row["decision"] = "WAIT"
    row["reason"] = reason
    row["authority_dependencies"] = {}
    row["ignition"] = {
        "causal_episode_id": "wave-1",
        "side": "LONG",
        "current_execution_proof": {
            "proof_hash": proof_hash,
            "observed_at_ms": 1_000,
        },
        "clock_quality": {
            "binance_spot": {"epoch": epoch},
            "coinbase_spot": {"epoch": epoch},
        },
    }
    return row


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
             "ECONOMIC_OPPORTUNITY_OPENED",
             "ECONOMIC_OPPORTUNITY_LINKED"],
        )
        opened = first["events"][0][1]["state_transition"]
        waiting = first["events"][1][1]["state_transition"]
        self.assertEqual(
            (opened["state_before"], opened["state_after"]),
            ("UNOBSERVED", "OPEN"),
        )
        self.assertEqual(
            (waiting["state_before"], waiting["state_after"]),
            ("OPEN", "WAIT"),
        )
        self.assertEqual(waiting["owner"], "TIMING")
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

    def test_futures_wait_expires_attempt_without_consuming_opportunity(self):
        state = SimpleNamespace(canonical_last_consumed_opportunity_id=0)
        observed = entry_lifecycle.observe(
            state,
            waiting_result("WAIT_CASH_IGNITION_FUTURES_RESPONSE"),
            {
                "allowed": False, "owner": "TIMING",
                "stage": "TIMING_NOW",
                "reason": "WAIT_CASH_IGNITION_FUTURES_RESPONSE",
            },
            economic_opportunity_id=11,
        )
        self.assertEqual(
            [name for name, _ in observed["events"]],
            [
                "TIMING_ATTEMPT_OPENED", "TIMING_ATTEMPT_EXPIRED",
                "ECONOMIC_OPPORTUNITY_OPENED",
                "ECONOMIC_OPPORTUNITY_LINKED",
            ],
        )
        self.assertEqual(observed["status"], "EXPIRED")
        self.assertEqual(state.canonical_last_consumed_opportunity_id, 0)

    def test_expired_proof_cannot_reopen_but_fresh_proof_can(self):
        state = SimpleNamespace()
        expired = waiting_result("WAIT_CURRENT_CASH_CONVERSION")
        entry_lifecycle.observe(state, expired, {
            "allowed": False, "owner": "TIMING",
            "stage": "TIMING_NOW", "reason": "WAIT_CURRENT_CASH_CONVERSION",
        }, economic_opportunity_id=12)

        reused = entry_lifecycle.observe(state, result("proof-a"), {
            "allowed": True, "owner": "ACTION",
            "stage": "AUTHORIZED", "reason": "PASS",
        }, economic_opportunity_id=12)
        self.assertEqual(
            [name for name, _ in reused["events"]],
            ["TIMING_ATTEMPT_REUSE_REJECTED"],
        )

        fresh = entry_lifecycle.observe(state, result("proof-b"), {
            "allowed": True, "owner": "ACTION",
            "stage": "AUTHORIZED", "reason": "PASS",
        }, economic_opportunity_id=12)
        self.assertEqual(
            [name for name, _ in fresh["events"]],
            [
                "TIMING_ATTEMPT_OPENED", "TIMING_ATTEMPT_PASSED",
                "ECONOMIC_OPPORTUNITY_REPRICED",
                "ECONOMIC_OPPORTUNITY_LINKED",
            ],
        )

    def test_epoch_change_cannot_stitch_same_wave(self):
        state = SimpleNamespace()
        first = waiting_result("FLOW_FADING", epoch=1)
        entry_lifecycle.observe(state, first, {
            "allowed": False, "owner": "TIMING",
            "stage": "TIMING_NOW", "reason": "FLOW_FADING",
        }, economic_opportunity_id=13)
        changed = waiting_result("FLOW_FADING", proof_hash="proof-b", epoch=2)
        observed = entry_lifecycle.observe(state, changed, {
            "allowed": False, "owner": "TIMING",
            "stage": "TIMING_NOW", "reason": "FLOW_FADING",
        }, economic_opportunity_id=13)
        self.assertIn(
            "TIMING_ATTEMPT_EXPIRED",
            [name for name, _ in observed["events"]],
        )
        self.assertIsNone(observed["timing_attempt_id"])

    def test_consumed_is_emitted_once_only_after_capture_boundary(self):
        state = SimpleNamespace(entry_economic_opportunity_link=("attempt-1", 21))
        report = entry_lifecycle.consume(
            state, 21, causal_wave_id="wave-21",
            timing_attempt_id="attempt-1",
        )
        self.assertTrue(report["accepted"])
        self.assertEqual(report["event"][0], "ECONOMIC_OPPORTUNITY_CONSUMED")
        self.assertEqual(
            report["event"][1]["state_transition"]["state_after"],
            "CONSUMED",
        )
        self.assertEqual(
            report["terminal"]["opportunity_scope"], "ECONOMIC_EXECUTABLE",
        )
        duplicate = entry_lifecycle.consume(state, 21)
        self.assertTrue(duplicate["accepted"])
        self.assertIsNone(duplicate["event"])

    def test_invalidated_is_terminal_and_cannot_become_consumed(self):
        state = SimpleNamespace()
        report = entry_lifecycle.invalidate(
            state, 22, causal_wave_id="wave-22", reason="DATA_GAP"
        )
        self.assertTrue(report["accepted"])
        self.assertEqual(report["event"][0], "ECONOMIC_OPPORTUNITY_INVALIDATED")
        conflict = entry_lifecycle.consume(state, 22)
        self.assertFalse(conflict["accepted"])
        self.assertEqual(conflict["reason"], "CONFLICTING_TERMINAL_REJECTED")

    def test_timing_and_economics_failure_do_not_terminalize_opportunity(self):
        state = SimpleNamespace()
        entry_lifecycle.observe(
            state, result(), {
                "allowed": False, "owner": "ACTION",
                "stage": "ECONOMICS", "reason": "INSUFFICIENT_EDGE",
            }, economic_opportunity_id=23,
        )
        self.assertEqual(
            getattr(state, "entry_economic_terminal_states", {}), {}
        )

    def test_opportunity_scope_requires_timing_link_or_reservation(self):
        state = SimpleNamespace()
        self.assertEqual(
            entry_lifecycle.opportunity_scope(state, 31),
            "MARKET_WAVE_RESEARCH",
        )
        state.entry_economic_opportunity_link = ("timing-31", 31)
        self.assertEqual(
            entry_lifecycle.opportunity_scope(state, 31),
            "ECONOMIC_EXECUTABLE",
        )


if __name__ == "__main__":
    unittest.main()
