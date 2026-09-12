import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mainnet_tier_s_shadow_launcher as launcher


class DecisionJournalContractTests(unittest.TestCase):
    def test_journal_event_carries_runtime_identity_and_rejects_override(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            with mock.patch.object(launcher, "EVENT_PATH", path), mock.patch.multiple(
                launcher.app.state,
                run_id="bot-run", runtime_commit="c" * 40,
                code_version="code", strategy_config_version="config",
                source_branch="main",
            ):
                launcher._append_event("ENTRY", {
                    "run_id": "forged", "runtime_commit": "forged",
                })
            row = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(row["run_id"], "bot-run")
        self.assertEqual(row["runtime_commit"], "c" * 40)
        self.assertEqual(row["code_version"], "code")
        self.assertEqual(row["config_version"], "config")
        self.assertEqual(row["runtime_mode"], launcher.RUNTIME_MODE)

    def test_full_record_is_content_addressed_and_not_duplicated(self):
        record = {
            "cycle_id": "cycle-1",
            "inputs": {
                "ignition": {"state": "PROVE"},
                "persistent_metaorder_shadow": {"status": "OBSERVING"},
                "bias_acquisition_handoff": {"status": "SEALED"},
            },
            "output": {"decision": "WAIT"},
        }
        payload = launcher._journal_decision_payload({
            "cycle_id": "cycle-1",
            "decision": "WAIT",
            "ignition": {"state": "PROVE"},
            "persistent_metaorder_shadow": {"status": "OBSERVING"},
            "bias_acquisition_handoff": {"status": "SEALED"},
            "forward_edge": {"status": "BOOTSTRAP_UNVERIFIED"},
        }, record)

        self.assertEqual(
            payload["schema_version"],
            "TIER_S_DECISION_RECORD_V8_CONTENT_ADDRESSED",
        )
        self.assertTrue(payload["decision_record_included"])
        self.assertIs(payload["decision_record"], record)
        self.assertEqual(
            payload["decision_record_hash"],
            launcher._decision_record_hash(record),
        )
        for name in launcher._DECISION_RECORD_DUPLICATE_FIELDS:
            self.assertNotIn(name, payload)

    def test_hash_is_deterministic_across_key_order(self):
        left = {"b": 2, "a": {"y": 2, "x": 1}}
        right = {"a": {"x": 1, "y": 2}, "b": 2}
        self.assertEqual(
            launcher._decision_record_hash(left),
            launcher._decision_record_hash(right),
        )
        self.assertEqual(json.dumps(left, sort_keys=True), json.dumps(right, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
