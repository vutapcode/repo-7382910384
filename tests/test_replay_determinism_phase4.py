import copy
from pathlib import Path
import subprocess
import sys
import unittest

from recorder.phase4_lifecycle_replay import Phase4LifecycleReplay
from recorder.replay import DeterministicReplay, _replay_output_hash


def event(
    ts, name, opportunity_id, *, code_version="code-a",
    config_version="config-a", **payload
):
    body = {
        "event": name,
        "economic_opportunity_id": opportunity_id,
        **payload,
    }
    return {
        "stream": "bot_event",
        "event_time_ms": ts,
        "receive_time_ms": ts,
        "available_time_ms": ts,
        "code_version": code_version,
        "config_version": config_version,
        "payload": body,
    }


class Phase4ReplayDeterminismTests(unittest.TestCase):
    def test_documented_direct_cli_bootstraps_repository_package(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "recorder" / "replay.py"), "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--verify-determinism", result.stdout)

    def test_recorded_capture_and_consumption_validate(self):
        rows = [
            event(1000, "ECONOMIC_OPPORTUNITY_OPENED", 7),
            event(1100, "ECONOMIC_OPPORTUNITY_CONSUMED", 7),
            event(1200, "ENTRY", 7, fill_price=100.0),
        ]
        replay = Phase4LifecycleReplay()
        for row in rows:
            replay.observe(row)
        report = replay.summary()
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["authority"])

    def test_consumed_without_capture_fails(self):
        replay = Phase4LifecycleReplay()
        replay.observe(event(1000, "ECONOMIC_OPPORTUNITY_CONSUMED", 8))
        report = replay.summary()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(
            report["violations"][0]["reason"],
            "CONSUMED_WITHOUT_RECORDED_EXECUTABLE_CAPTURE",
        )

    def test_conflicting_terminal_fails(self):
        replay = Phase4LifecycleReplay()
        replay.observe(event(1000, "ECONOMIC_OPPORTUNITY_INVALIDATED", 9))
        replay.observe(event(1100, "ECONOMIC_OPPORTUNITY_CONSUMED", 9))
        self.assertEqual(replay.summary()["status"], "FAIL")

    def test_same_numeric_id_in_different_versions_is_not_merged(self):
        replay = Phase4LifecycleReplay()
        replay.observe(event(
            1000, "ECONOMIC_OPPORTUNITY_INVALIDATED", 9,
            code_version="code-old", causal_wave_id="wave-old",
        ))
        replay.observe(event(
            1100, "ECONOMIC_OPPORTUNITY_INVALIDATED", 9,
            code_version="code-new", causal_wave_id="wave-new",
        ))
        report = replay.summary()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["terminals"], 2)

    def test_same_numeric_id_in_different_waves_is_not_merged(self):
        replay = Phase4LifecycleReplay()
        replay.observe(event(
            1000, "ECONOMIC_OPPORTUNITY_INVALIDATED", 9,
            causal_wave_id="wave-one",
        ))
        replay.observe(event(
            1100, "ECONOMIC_OPPORTUNITY_INVALIDATED", 9,
            causal_wave_id="wave-two",
        ))
        self.assertEqual(replay.summary()["status"], "PASS")

    def test_same_input_has_same_complete_output_hash(self):
        rows = [
            event(1000, "ECONOMIC_OPPORTUNITY_OPENED", 10),
            event(1100, "ECONOMIC_OPPORTUNITY_INVALIDATED", 10),
        ]
        first = DeterministicReplay(
            wavefront=False, canonical_mirror=False
        ).run(copy.deepcopy(rows))
        second = DeterministicReplay(
            wavefront=False, canonical_mirror=False
        ).run(copy.deepcopy(rows))
        self.assertEqual(_replay_output_hash(first), _replay_output_hash(second))
        self.assertEqual(
            first["phase4_lifecycle"]["deterministic_hash"],
            second["phase4_lifecycle"]["deterministic_hash"],
        )


if __name__ == "__main__":
    unittest.main()
