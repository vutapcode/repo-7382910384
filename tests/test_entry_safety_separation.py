import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong import disk_pressure_gate
from loi_he_thong import flat_persistence_gate
from loi_he_thong import mainnet_safety


def _go(*_args, **_kwargs):
    return {
        "decision": "GO", "side": "LONG", "phase": "RELEASE",
        "entry_mode": "IGNITION", "execution_policy": "TAKER",
        "ignition": {},
    }


class EntrySafetySeparationTests(unittest.TestCase):
    def test_persistence_fault_preserves_causal_entry_result(self):
        state = SimpleNamespace(
            shadow_persistence_dirty=True,
            shadow_persistence_last_error="disk full",
        )
        wrapper = SimpleNamespace(
            base=SimpleNamespace(
                app=SimpleNamespace(state=state),
                entry_council=SimpleNamespace(evaluate=_go),
            ),
            runtime_state=SimpleNamespace(
                save=lambda _base: (_ for _ in ()).throw(OSError("disk full")),
            ),
        )
        evaluate = flat_persistence_gate.install(wrapper)
        result = evaluate(state, now=10.0)
        self.assertEqual(result["decision"], "GO")
        self.assertFalse(result["operational_safety"]["allowed"])
        self.assertIn(
            "PERSISTENCE_DIRTY_RETRY",
            result["operational_safety"]["blockers"],
        )

    def test_disk_pressure_preserves_causal_entry_result(self):
        state = SimpleNamespace()
        wrapper = SimpleNamespace(base=SimpleNamespace(
            app=SimpleNamespace(state=state),
            entry_council=SimpleNamespace(evaluate=_go),
        ))
        evaluate = disk_pressure_gate.install(wrapper)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            disk_pressure_gate, "_journal_root", return_value=directory,
        ), patch.object(
            disk_pressure_gate, "measure_storage", return_value={
                "free_bytes": 1, "free_ratio": 0.001, "pressure": True,
            },
        ):
            result = evaluate(state, now=10.0)
        self.assertEqual(result["decision"], "GO")
        self.assertFalse(result["operational_safety"]["allowed"])
        self.assertIn("DISK_PRESSURE", result["operational_safety"]["blockers"])

    def test_operational_blocker_is_safety_owner_not_entry_rewrite(self):
        import mainnet_tier_s_shadow_risk_launcher as active_launcher

        state = SimpleNamespace(wstrade_live_armed=False)
        mainnet_safety.set_entry_operational_blocker(
            state, "DISK_PRESSURE", True,
        )
        with patch.object(
            active_launcher.base.entry_council,
            "validate_frozen_entry_contract", return_value=(True, "PASS", {}),
        ), patch.object(
            active_launcher.edge, "authorize", return_value=(True, {
                "edge_class": "RESIDUAL_POSITIVE", "cost_ok": True,
            }),
        ):
            gate = active_launcher._entry_quorum_outcome(
                _go(), state, 10.0,
            )
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["owner"], "SAFETY")
        self.assertEqual(gate["reason"], "DISK_PRESSURE")

    def test_ring_saturation_does_not_fabricate_wait_payload(self):
        import mainnet_tier_s_shadow_risk_launcher as active_launcher

        state = SimpleNamespace(
            futures_flow_ring_saturated=True,
            _entry_causal_context_side="ABSTAIN",
            bias_state="LONG", bias_confidence=0.7, bias_updated_at=10.0,
        )
        with patch.object(active_launcher, "_health_eval", side_effect=_go):
            result = active_launcher._entry_evaluate_context_guard(
                state, now=10.0,
            )
        self.assertEqual(result["decision"], "GO")
        self.assertFalse(result["operational_safety"]["allowed"])
        self.assertIn(
            "FUTURES_FLOW_RING_SATURATED",
            result["operational_safety"]["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
