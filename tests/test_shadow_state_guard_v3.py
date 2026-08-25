import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "ops" / "shadow_state_guard.py"


class ShadowStateGuardV3Tests(unittest.TestCase):
    def run_guard(self, payload):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.run(
                [str(ROOT / ".venv" / "bin" / "python"), str(GUARD)],
                env={"SMC_JOURNAL_DIR": temp},
                text=True, capture_output=True, check=False,
            )

    def test_v3_flat_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V3_PROMOTION_EVIDENCE",
            "balance": 5.4, "realized_pnl": 0.0,
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "event_seq": 0, "decision_evaluations": 12,
            "near_misses": 2,
            "decision_funnel": {"COUNCIL": 9, "EDGE_OR_QUORUM": 2, "READY": 1},
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V3", result.stdout)

    def test_v3_funnel_mismatch_fails_closed(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V3_PROMOTION_EVIDENCE",
            "balance": 5.4, "realized_pnl": 0.0,
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "decision_evaluations": 12, "near_misses": 2,
            "decision_funnel": {"COUNCIL": 11}, "position": None,
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("decision_funnel_invariant", result.stderr)

    def test_v4_requires_and_accepts_calibration_provenance(self):
        payload = {
            "version": "SHADOW_RUNTIME_STATE_V4_VERSION_BOUND_CALIBRATION",
            "balance": 5.4, "realized_pnl": 0.0,
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "event_seq": 0, "decision_evaluations": 0,
            "near_misses": 0, "decision_funnel": {},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v1",
            "edge_calibration_config_version": "config-v1",
            "position": None,
        }
        accepted = self.run_guard(payload)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        del payload["edge_calibration_code_version"]
        rejected = self.run_guard(payload)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("edge_calibration_code_version:missing", rejected.stderr)

    def test_v5_verified_cost_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V5_VERIFIED_COST_PLAN",
            "balance": 5.4, "realized_pnl": 0.0,
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "event_seq": 0, "decision_evaluations": 0,
            "near_misses": 0, "decision_funnel": {},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v2",
            "edge_calibration_config_version": "config-v2",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V5", result.stdout)

    def test_v5_current_eight_field_calibration_row_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V5_VERIFIED_COST_PLAN",
            "balance": 5.4, "realized_pnl": -0.1,
            "trades": 1, "wins": 0, "losses": 1, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [[
                "SHORT", "IGNITION", "NORMAL", "BOOTSTRAP_UNVERIFIED",
                "FAILED_REVERSION", "FUTURES", "MAKER", -3.5,
            ]],
            "edge_calibration_code_version": "code-v2",
            "edge_calibration_config_version": "config-v2",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
