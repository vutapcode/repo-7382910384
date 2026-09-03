import copy
import importlib
import unittest
from types import SimpleNamespace

from loi_he_thong import authority_contracts, market_thesis


guardian = importlib.import_module("3_thuc_thi.ve_si_lenh.guardian_s_tier")


def _entry_result():
    return {
        "decision": "GO",
        "reason": "IGNITION_PROVED",
        "side": "LONG",
        "causal_episode_id": "episode-shared-1",
        "authority_basis": "BIAS_ALIGNED",
        "ignition": {
            "causal_episode_id": "episode-shared-1",
            "side": "LONG",
            "proof_type": "PERSISTENT_METAORDER",
            "proposer": "binance_spot",
            "cash_venues": ["binance_spot", "coinbase_spot"],
            "oi_verification_state": {"status": "UNCHANGED_UNKNOWN"},
            "clock_quality": {
                "binance_spot": {"source_health": "FRESH", "epoch": 2},
                "coinbase_spot": {"source_health": "FRESH", "epoch": 3},
            },
        },
    }


def _observation(*, spot=-2.0, coinbase=-2.0, futures=-2.0,
                 spot_flow=-0.4, coinbase_flow=-0.4,
                 futures_flow=-0.4, oi="NEUTRAL", source="FRESH"):
    moves = {"spot": spot, "coinbase": coinbase, "futures": futures}
    return {
        "version": "GUARDIAN_CANONICAL_OBSERVATION_V1",
        "causal_episode_id": "episode-shared-1",
        "position_side": "LONG",
        "source_health": {
            "spot": source,
            "coinbase": source,
            "futures": source,
        },
        "price_horizons": {
            "1.0": {"moves": moves},
            "3.0": {"moves": moves},
        },
        "flow_signed_imbalances": {
            "spot": spot_flow,
            "coinbase": coinbase_flow,
            "futures": futures_flow,
        },
        "oi": {"status": oi, "fresh": True},
        "gap_or_epoch_invalid": False,
    }


class SharedThesisObservationTests(unittest.TestCase):
    def setUp(self):
        self.truth = market_thesis.build(_entry_result())

    def test_dual_cash_opposite_control_is_control_transfer(self):
        result = market_thesis.observe(self.truth, _observation())
        self.assertEqual(result["status"], "CONTROL_TRANSFER")
        self.assertTrue(result["old_thesis_falsified"])
        self.assertIn("OPPOSITE_DUAL_CASH_CONTROL", result["observed_falsifiers"])

    def test_single_cash_pullback_is_divergence_not_falsification(self):
        result = market_thesis.observe(self.truth, _observation(
            coinbase=0.2, coinbase_flow=0.2, futures=0.2,
            futures_flow=0.2,
        ))
        self.assertEqual(result["status"], "DIVERGENCE")
        self.assertFalse(result["old_thesis_falsified"])

    def test_support_requires_current_price_and_flow_conversion(self):
        result = market_thesis.observe(self.truth, _observation(
            spot=2.0, coinbase=2.0, futures=1.0,
            spot_flow=0.4, coinbase_flow=0.4, futures_flow=0.1,
        ))
        self.assertEqual(result["status"], "SUPPORT")
        self.assertFalse(result["old_thesis_falsified"])

    def test_missing_source_or_gap_is_unknown_not_falsified(self):
        stale = market_thesis.observe(
            self.truth, _observation(source="STALE"),
        )
        self.assertEqual(stale["status"], "UNKNOWN")
        self.assertFalse(stale["old_thesis_falsified"])
        row = _observation()
        row["gap_or_epoch_invalid"] = True
        gap = market_thesis.observe(self.truth, row)
        self.assertEqual(gap["status"], "UNKNOWN")
        self.assertFalse(gap["old_thesis_falsified"])

    def test_pnl_and_capital_fields_cannot_change_market_truth(self):
        observation = _observation()
        baseline = market_thesis.observe(self.truth, observation)
        polluted = copy.deepcopy(observation)
        polluted.update({
            "pnl_bps": -500.0, "best_r": 20.0, "runner_active": True,
            "capital_preference": "EXIT",
        })
        other = market_thesis.observe(self.truth, polluted)
        for name in (
            "status", "reason", "observed_falsifiers",
            "old_thesis_falsified", "observation_hash",
        ):
            self.assertEqual(baseline[name], other[name])
        self.assertFalse(other["pnl_fields_used_for_thesis"])
        self.assertFalse(other["capital_fields_used_for_thesis"])

    def test_same_truth_and_events_are_deterministic(self):
        first = market_thesis.observe(self.truth, _observation())
        second = market_thesis.observe(self.truth, _observation())
        self.assertEqual(first, second)

    def test_mutated_or_missing_entry_truth_fails_unknown(self):
        damaged = copy.deepcopy(self.truth)
        damaged["mechanism"] = "REWRITTEN"
        result = market_thesis.observe(damaged, _observation())
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "ENTRY_MARKET_THESIS_INVALID")

    def test_shadow_mapping_never_claims_authority_or_ensemble(self):
        for status, decision in (
            ("SUPPORT", "HOLD"),
            ("DIVERGENCE", "DETERIORATING"),
            ("CONTROL_TRANSFER", "EXIT"),
            ("FALSIFY", "EXIT"),
            ("UNKNOWN", "HOLD"),
        ):
            result = guardian._shared_thesis_shadow_action({"status": status})
            self.assertEqual(result["decision"], decision)
            self.assertFalse(result["authority"])
            self.assertFalse(result["weighted_ensemble"])
            self.assertTrue(result["safety_bypass_separate"])

    def test_guardian_adapter_reads_exact_frozen_handoff(self):
        action = authority_contracts.seal(
            "ACTION", "ENTRY_ACTION_POLICY", "episode-shared-1",
            {"action": "ACT_TAKER_NOW"},
        )
        bundle = authority_contracts.bundle(
            self.truth,
            action,
            authority_contracts.seal(
                "EXECUTION", "EXECUTION_REVALIDATION", "episode-shared-1",
                {"execution_action": "EXECUTE"},
            ),
            authority_contracts.seal(
                "SAFETY", "MAINNET_SAFETY", "episode-shared-1",
                {"safety_state": "SAFE"},
            ),
        )
        handoff = authority_contracts.freeze_entry_handoff(bundle)
        position = SimpleNamespace(
            side="LONG", causal_episode_id="episode-shared-1",
            entry_causal_thesis={
                "causal_episode_id": "episode-shared-1",
                "entry_thesis_handoff": handoff,
            },
        )
        state = SimpleNamespace(
            thoi_gian_tick_cuoi=100.0, thoi_gian_dong_tien_cuoi=100.0,
            thoi_gian_coinbase_ticker_cuoi=100.0,
            coinbase_flow_3s_ts=100.0, thoi_gian_vi_mo_cuoi=100.0,
            guardian_s_spot_flow_ordering="MONOTONIC",
            guardian_s_futures_flow_ordering="MONOTONIC",
            spot_flow_epoch=2, coinbase_flow_epoch=3,
            danh_sach_khop_lenh_futures=[{
                "thoi_gian_ms": 100_000, "gia": 100.0,
            }],
        )
        s1 = {"metrics": {"horizons": {"3.0": {
            "threshold_bps": 1.5,
            "moves": {"spot": 2.0, "coinbase": 2.0, "futures": 1.0},
        }}}}
        s2 = {"metrics": {"signed_imbalances": {
            "spot": 0.4, "coinbase": 0.4, "futures": 0.1,
        }}}
        s3 = {"status": "NEUTRAL", "metrics": {"oi_pct": 0.0}}

        truth, event = guardian._canonical_thesis_observation(
            state, position, 100.0, s1, s2, s3,
        )
        observed = market_thesis.observe(truth, event)

        self.assertEqual(truth["contract_hash"], self.truth["contract_hash"])
        self.assertEqual(event["causal_episode_id"], "episode-shared-1")
        self.assertEqual(observed["status"], "SUPPORT")


if __name__ == "__main__":
    unittest.main()
