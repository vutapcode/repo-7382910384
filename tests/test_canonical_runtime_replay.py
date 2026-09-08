from types import SimpleNamespace
import unittest

from loi_he_thong import authority_contracts
from recorder.canonical_runtime_replay import (
    CanonicalRuntimeContractReplay, _digest, bind_recording_identity,
    inspect_loaded_runtime,
)


def bundle(episode="wave-1", action="WAIT_INFORMATION"):
    rows = []
    for layer, owner, payload in (
        ("MARKET_TRUTH", "BIAS", {"side": "LONG", "status": "SUPPORTED"}),
        ("ACTION", "ENTRY", {"action": action}),
        ("EXECUTION", "EXEC", {"status": "NOT_SUBMITTED"}),
        ("SAFETY", "RISK", {"status": "SAFE"}),
    ):
        rows.append(authority_contracts.seal(layer, owner, episode, payload))
    return authority_contracts.bundle(*rows)


def manifest():
    body = {
        "adapter_version": "x", "entrypoint": "x",
        "code_version": "code-1", "config_version": "config-1",
        "profile_version": "profile", "module_versions": {},
        "object_identity_checks": {"same": True}, "verified": True,
    }
    return {**body, "manifest_hash": _digest(body)}


def decision_event(*, action="WAIT_INFORMATION", decision="WAIT"):
    contracts = bundle(action=action)
    record = {
        "cycle_id": "cycle-1",
        "strategy_code_version": "code-1",
        "strategy_config_version": "config-1",
        "authority_contracts": contracts,
        "output": {"decision": decision},
    }
    return {
        "stream": "bot_event", "available_time_ms": 1_000,
        "code_version": "code-1", "config_version": "config-1",
        "payload": {
            "event": "DECISION_EVALUATED",
            "decision_record": record,
            "decision_record_hash": _digest(record),
            "authority_contracts": contracts,
        },
    }


class CanonicalRuntimeReplayTests(unittest.TestCase):
    def test_exact_contracts_pass_but_raw_strategy_remains_unproven(self):
        replay = CanonicalRuntimeContractReplay(manifest())
        replay.observe(decision_event())
        report = replay.summary()
        self.assertEqual(report["status"], "PASS_CONTRACT_EQUIVALENCE_ONLY")
        self.assertFalse(report["authority"])
        self.assertFalse(report["promotion_eligible"])
        self.assertIn(
            "RAW_MARKET_DECISION_REEXECUTION_NOT_PROVEN", report["blockers"]
        )

    def test_corrupt_hash_and_action_contradiction_fail(self):
        event = decision_event(action="WAIT_INFORMATION", decision="GO")
        event["payload"]["decision_record_hash"] = "bad"
        replay = CanonicalRuntimeContractReplay(manifest())
        replay.observe(event)
        reasons = {row["reason"] for row in replay.summary()["violations"]}
        self.assertIn("DECISION_RECORD_HASH_INVALID", reasons)
        self.assertIn("FINAL_DECISION_ACTION_CONTRADICTION", reasons)

    def test_version_mismatch_fails_closed(self):
        event = decision_event()
        event["payload"]["decision_record"][
            "strategy_code_version"
        ] = "different"
        event["payload"]["decision_record_hash"] = _digest(
            event["payload"]["decision_record"]
        )
        replay = CanonicalRuntimeContractReplay(manifest())
        replay.observe(event)
        self.assertEqual(replay.summary()["status"], "FAIL")

    def test_runtime_manifest_checks_shared_module_identity(self):
        ignition = SimpleNamespace(INFERENCE_VERSION="IGNITION")
        edge = SimpleNamespace(VERSION="EDGE", ignition_core=ignition)
        state = SimpleNamespace(
            code_version="code-1", strategy_config_version="config-1",
            strategy_profile={
                "version": "profile",
                "canonical_entrypoint": "mainnet_tier_s_lean_launcher.py",
                "architecture": ["BIAS", "IGNITION"],
            },
        )
        shadow = SimpleNamespace(
            app=SimpleNamespace(state=state),
            bias_council=SimpleNamespace(VERSION="BIAS"),
            entry_council=ignition,
            guardian_s=SimpleNamespace(VERSION="GUARDIAN"),
        )
        runtime = SimpleNamespace(base=shadow, edge=edge)
        lean = SimpleNamespace(
            shadow=shadow,
            hardened=SimpleNamespace(runtime=runtime),
        )
        report = inspect_loaded_runtime(lean)
        self.assertTrue(report["verified"])

        rebound = bind_recording_identity(report, {
            "code_version": "code-1",
            "strategy_config_version": "service-config",
        })
        self.assertTrue(rebound["verified"])
        self.assertEqual(rebound["config_version"], "service-config")

    def test_recording_identity_rejects_old_running_code(self):
        runtime = manifest()
        rebound = bind_recording_identity(runtime, {
            "code_version": "old-code",
            "strategy_config_version": "config-1",
        })
        self.assertFalse(rebound["verified"])


if __name__ == "__main__":
    unittest.main()
