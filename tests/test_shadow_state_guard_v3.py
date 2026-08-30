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

    def test_v5_current_cost_repriced_nine_field_row_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V5_VERIFIED_COST_PLAN",
            "balance": 5.4, "realized_pnl": -0.1,
            "trades": 1, "wins": 0, "losses": 1, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [[
                "LONG", "PERSISTENT_METAORDER", "NORMAL",
                "BOOTSTRAP_UNVERIFIED", "PERSISTENT_METAORDER",
                "BINANCE_SPOT", "TAKER", -3.5, 12.25,
            ]],
            "edge_calibration_code_version": "code-v3",
            "edge_calibration_config_version": "config-v3",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v6_entry_economics_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V6_ENTRY_ECONOMICS",
            "balance": 5.4, "realized_pnl": 0.1,
            "trades": 1, "wins": 1, "losses": 0, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v4",
            "edge_calibration_config_version": "config-v4",
            "entry_economics_v2_rows": [{
                "economic_contract_version": "ENTRY_ECONOMICS_V2",
                "valid": True,
                "net_pnl_bps_after_frozen_cost": 2.0,
                "execution_cost_bps": 8.0,
            }],
            "entry_economics_code_version": "code-v4",
            "entry_economics_config_version": "config-v4",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V6", result.stdout)

    def test_v7_entry_economics_v3_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V7_ENTRY_ECONOMICS_V3",
            "balance": 5.4, "realized_pnl": 0.1,
            "trades": 1, "wins": 1, "losses": 0, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v5",
            "edge_calibration_config_version": "config-v5",
            "entry_economics_v2_rows": [{
                "economic_contract_version": "ENTRY_ECONOMICS_V3",
                "valid": True,
                "net_pnl_bps_after_frozen_cost": 2.0,
                "execution_cost_bps": 8.0,
            }],
            "entry_economics_code_version": "code-v5",
            "entry_economics_config_version": "config-v5",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V7", result.stdout)

    def test_v8_entry_economics_v4_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": "SHADOW_RUNTIME_STATE_V8_ENTRY_ECONOMICS_V4",
            "balance": 5.4, "realized_pnl": 0.1,
            "trades": 1, "wins": 1, "losses": 0, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v6",
            "edge_calibration_config_version": "config-v6",
            "entry_economics_v2_rows": [{
                "economic_contract_version": "ENTRY_ECONOMICS_V4",
                "valid": True,
                "net_pnl_bps_after_frozen_cost": 2.0,
                "execution_cost_bps": 8.0,
            }],
            "entry_economics_code_version": "code-v6",
            "entry_economics_config_version": "config-v6",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V8", result.stdout)

    def test_v10_availability_time_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": (
                "SHADOW_RUNTIME_STATE_V10_ENTRY_ECONOMICS_V6_"
                "AVAILABILITY_TIME"
            ),
            "balance": 5.4, "realized_pnl": 0.1,
            "trades": 1, "wins": 1, "losses": 0, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v10",
            "edge_calibration_config_version": "config-v10",
            "entry_economics_v2_rows": [{
                "economic_contract_version": (
                    "ENTRY_ECONOMICS_V6_AVAILABILITY_TIME"
                ),
                "valid": True,
                "net_pnl_bps_after_frozen_cost": 2.0,
                "execution_cost_bps": 8.0,
            }],
            "entry_economics_code_version": "code-v10",
            "entry_economics_config_version": "config-v10",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V10", result.stdout)

    def test_v11_causal_proof_checkpoint_is_accepted(self):
        result = self.run_guard({
            "version": (
                "SHADOW_RUNTIME_STATE_V11_ENTRY_ECONOMICS_V7_"
                "CAUSAL_PROOF_SEMANTICS"
            ),
            "balance": 5.4, "realized_pnl": 0.1,
            "trades": 1, "wins": 1, "losses": 0, "breakevens": 0,
            "event_seq": 1, "decision_evaluations": 1,
            "near_misses": 0, "decision_funnel": {"READY": 1},
            "edge_calibration_rows": [],
            "edge_calibration_code_version": "code-v11",
            "edge_calibration_config_version": "config-v11",
            "entry_economics_v2_rows": [{
                "economic_contract_version": (
                    "ENTRY_ECONOMICS_V7_CAUSAL_PROOF_SEMANTICS"
                ),
                "valid": True,
                "net_pnl_bps_after_frozen_cost": 2.0,
                "execution_cost_bps": 8.0,
            }],
            "entry_economics_code_version": "code-v11",
            "entry_economics_config_version": "config-v11",
            "position": None,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK version=SHADOW_RUNTIME_STATE_V11", result.stdout)


if __name__ == "__main__":
    unittest.main()
