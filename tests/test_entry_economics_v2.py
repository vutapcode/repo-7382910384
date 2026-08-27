from types import SimpleNamespace
import unittest

from loi_he_thong import entry_economics_v2
from importlib import import_module


guardian = import_module("3_thuc_thi.ve_si_lenh.guardian_s_tier")


def snapshot(**overrides):
    row = {
        "economic_contract_version": entry_economics_v2.CONTRACT_VERSION,
        "side": "LONG", "entry_mode": "IGNITION", "regime": "NORMAL",
        "proof_type": "METAORDER_CONTINUATION",
        "proposer": "BINANCE_SPOT", "execution_style": "TAKER",
        "bias_phase": "ESTABLISHED_TREND", "consumed_band": "EARLY_15_25",
        "oi_quality": "UNKNOWN",
        "flow_efficiency_state": "CONTINUING_CONFIRMED",
    }
    row.update(overrides)
    return row


class EntryEconomicsV2Tests(unittest.TestCase):
    def test_insufficient_data_never_invents_forward_edge(self):
        report = entry_economics_v2.estimate(SimpleNamespace(), snapshot())
        self.assertEqual(report["status"], "BOOTSTRAP_UNVERIFIED")
        self.assertIsNone(report["expected_guardian_net_bps"])
        self.assertFalse(report["authority"])

    def test_exact_guardian_net_cohort_activates_after_thirty(self):
        state = SimpleNamespace(code_version="code", strategy_config_version="cfg",
                                entry_economics_v3_replay_approved=True)
        for index in range(30):
            entry_economics_v2.record(
                state, snapshot(), net_bps=4.0 + index * 0.01,
                execution_cost_bps=10.0,
                time_to_positive_net_seconds=1.0 + index * 0.01,
            )
        report = entry_economics_v2.estimate(state, snapshot())
        self.assertEqual(report["level"], "EXACT")
        self.assertTrue(report["positive_net"])
        self.assertIsNotNone(report["time_to_positive_net_p80_seconds"])

    def test_parent_requires_fifty_and_does_not_claim_exact(self):
        state = SimpleNamespace(code_version="code", strategy_config_version="cfg",
                                entry_economics_v3_replay_approved=True)
        for index in range(50):
            row = snapshot(
                regime="TREND" if index % 2 else "NORMAL",
                bias_phase="PULLBACK_AGAINST_CONTEXT" if index % 3
                else "ESTABLISHED_TREND",
                consumed_band="EARLY_0_15" if index % 5 else "EARLY_25_35",
            )
            entry_economics_v2.record(
                state, row, net_bps=2.0, execution_cost_bps=10.0,
                time_to_positive_net_seconds=2.0,
            )
        report = entry_economics_v2.estimate(
            state, snapshot(regime="EXPANSION", bias_phase="UNKNOWN")
        )
        self.assertEqual(report["level"], "PARENT")
        self.assertTrue(report["positive_net"])

    def test_parent_never_cross_subsidizes_side_proposer_or_execution(self):
        state = SimpleNamespace(
            code_version="code", strategy_config_version="cfg",
            entry_economics_v3_replay_approved=True,
        )
        for _ in range(50):
            entry_economics_v2.record(
                state, snapshot(), net_bps=8.0, execution_cost_bps=10.0,
            )
        for changed in (
            snapshot(side="SHORT"),
            snapshot(proposer="COINBASE_SPOT"),
            snapshot(execution_style="MAKER"),
        ):
            report = entry_economics_v2.estimate(state, changed)
            self.assertEqual(report["status"], "BOOTSTRAP_UNVERIFIED")
            self.assertEqual(report["parent_samples"], 0)

    def test_negative_active_cohort_is_not_authorized(self):
        state = SimpleNamespace(code_version="code", strategy_config_version="cfg",
                                entry_economics_v3_replay_approved=True)
        for _ in range(30):
            entry_economics_v2.record(
                state, snapshot(), net_bps=-3.0, execution_cost_bps=10.0,
            )
        report = entry_economics_v2.estimate(state, snapshot())
        self.assertEqual(report["status"], "ACTIVE")
        self.assertFalse(report["positive_net"])

    def test_edge_late_is_suspicion_not_exit_authority(self):
        pos = SimpleNamespace(
            side="LONG", entry_price=100.0, opened_at=1.0,
            execution_cost_plan={"total_cost_bps": 8.0},
            entry_causal_thesis={
                "time_to_edge": {"authority": True, "p80_seconds": 1.0}
            },
            edge_first_positive_net_at=None,
            edge_time_to_positive_net_seconds=None,
        )
        neutral = {"status": "NEUTRAL"}
        report = guardian._time_to_edge(
            pos, 3.0, {"futures": 100.0}, neutral, neutral, neutral,
            {"broken": False},
        )
        self.assertEqual(report["status"], "EDGE_LATE")
        self.assertFalse(report["can_exit_alone"])
        self.assertFalse(report["causal_deterioration_confirmed"])


if __name__ == "__main__":
    unittest.main()
