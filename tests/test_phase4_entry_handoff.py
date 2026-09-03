import asyncio
import copy
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mainnet_tier_s_shadow_launcher as launcher
from loi_he_thong import authority_contracts


def approved_result():
    return {
        "decision": "GO",
        "reason": "IGNITION_METAORDER_CONTINUATION",
        "side": "LONG",
        "execution_policy": "TAKER",
        "causal_episode_id": "episode-4a",
        "authority_basis": "BIAS_ALIGNED",
        "edge_tier": {
            "cost_ok": True,
            "economic_contract_version": "TEST_COST_V1",
            "execution_style": "TAKER",
            "forward_edge_status": "BOOTSTRAP_UNVERIFIED",
        },
        "ignition": {
            "causal_episode_id": "episode-4a",
            "side": "LONG",
            "proof_type": "PERSISTENT_METAORDER",
            "proposer": "coinbase_spot",
            "cash_venues": ["binance_spot", "coinbase_spot"],
            "bias_snapshot": {
                "direction": "LONG",
                "confidence": 0.8,
                "direction_context": {
                    "context_side": "LONG",
                    "phase": "ESTABLISHED_TREND",
                },
            },
            "clock_quality": {
                "binance_spot": {
                    "source_health": "FRESH", "epoch": 3,
                    "temporal_uncertainty_ms": 12.0,
                },
                "coinbase_spot": {
                    "source_health": "FRESH", "epoch": 4,
                    "temporal_uncertainty_ms": 18.0,
                },
            },
        },
    }


def seal_approved(result=None):
    result = dict(result or approved_result())
    state = SimpleNamespace(mainnet_shadow_health={})
    result["authority_contracts"] = launcher._authority_contract_bundle(
        state, result, True, result["causal_episode_id"],
    )
    result["entry_thesis_handoff"] = launcher._freeze_entry_handoff(
        result, result["causal_episode_id"],
    )
    return result


class Phase4EntryHandoffTests(unittest.TestCase):
    def test_action_approved_entry_freezes_exact_truth_and_action(self):
        result = seal_approved()
        handoff = result["entry_thesis_handoff"]
        self.assertTrue(authority_contracts.verify_entry_handoff(
            handoff, expected_side="LONG",
            expected_episode_id="episode-4a",
        ))
        self.assertEqual(handoff["mechanism"], "CASH_METAORDER")
        self.assertEqual(
            handoff["market_truth_hash"],
            result["authority_contracts"]["contracts"][
                "MARKET_TRUTH"
            ]["contract_hash"],
        )
        self.assertEqual(
            handoff["action_hash"],
            result["authority_contracts"]["contracts"]["ACTION"][
                "contract_hash"
            ],
        )

    def test_wait_action_and_missing_episode_cannot_create_handoff(self):
        wait = approved_result()
        wait["decision"] = "WAIT"
        wait["causal_episode_id"] = "episode-wait"
        wait["ignition"]["causal_episode_id"] = "episode-wait"
        wait["authority_contracts"] = launcher._authority_contract_bundle(
            SimpleNamespace(mainnet_shadow_health={}), wait, False,
            "episode-wait",
        )
        with self.assertRaisesRegex(ValueError, "ACTION_NOT_ENTRY_APPROVED"):
            launcher._freeze_entry_handoff(wait, "episode-wait")

        entry = approved_result()
        entry["causal_episode_id"] = ""
        entry["ignition"]["causal_episode_id"] = ""
        entry["authority_contracts"] = launcher._authority_contract_bundle(
            SimpleNamespace(mainnet_shadow_health={}), entry, True, "",
        )
        with self.assertRaisesRegex(ValueError, "ENTRY_HANDOFF_EPISODE_MISSING"):
            launcher._freeze_entry_handoff(entry, "")

    def test_truth_rewrite_invalidates_handoff(self):
        result = seal_approved()
        changed = copy.deepcopy(result["entry_thesis_handoff"])
        changed["market_thesis"]["mechanism"] = "DERIVATIVE_DISLOCATION"
        self.assertFalse(authority_contracts.verify_entry_handoff(changed))

    def test_guardian_thesis_consumes_frozen_truth_without_rebuild(self):
        result = seal_approved()
        with patch.object(
            launcher.live_execution.market_thesis, "build",
            side_effect=AssertionError("must not rebuild Truth after Action"),
        ):
            thesis = launcher.live_execution._entry_causal_thesis(result)
        self.assertEqual(
            thesis["version"],
            "ENTRY_CAUSAL_THESIS_V3_FROZEN_ACTION_HANDOFF",
        )
        self.assertEqual(thesis["primary_cash_anchor"], "coinbase")
        self.assertEqual(
            thesis["market_truth_hash"],
            result["entry_thesis_handoff"]["market_truth_hash"],
        )
        self.assertEqual(thesis["bias_thesis"]["context_side"], "LONG")

    def test_top_level_go_requires_valid_action_handoff(self):
        result = approved_result()
        state = SimpleNamespace(
            open_interest=0.0, thoi_gian_vi_mo_cuoi=0.0,
            bias_council={}, bias_state="LONG", bias_confidence=0.8,
            execution_best_bid=100.0, execution_best_ask=100.1,
            mainnet_shadow_health={},
        )
        snapshot = launcher._decision_snapshot(
            state, result, result["edge_tier"], True,
            "cycle-invalid", 100.0,
            opportunity={"causal_episode_id": "episode-4a"},
        )
        self.assertEqual(snapshot["output"]["decision"], "WAIT")
        self.assertEqual(
            snapshot["output"]["reason"],
            "ENTRY_HANDOFF_CONTRACT_INVALID",
        )
        self.assertFalse(snapshot["output"]["entry_handoff_valid"])

        approved = seal_approved()
        snapshot = launcher._decision_snapshot(
            state, approved, approved["edge_tier"], True,
            "cycle-valid", 100.0,
            opportunity={"causal_episode_id": "episode-4a"},
        )
        self.assertEqual(snapshot["output"]["decision"], "GO")
        self.assertTrue(snapshot["output"]["entry_handoff_valid"])

    def test_live_rejects_invalid_handoff_before_exchange_call(self):
        async def run():
            state = SimpleNamespace(
                run_id="run", wstrade_live_armed=True,
                wstrade_live_entry_allowed=True,
                mainnet_shadow_position=None,
                execution_unknown=False,
                wstrade_execution_recovery_required=False,
            )
            result = approved_result()
            result["canonical_opportunity_id"] = 9
            with patch.object(
                launcher.live_execution.mainnet_safety,
                "exchange_entry_gate", new=AsyncMock(),
            ) as gate:
                position = await launcher.live_execution.open_position(
                    object(), state, "LONG", result, now=1.0,
                )
            self.assertIsNone(position)
            gate.assert_not_awaited()
            self.assertEqual(
                state.wstrade_live_last_entry_outcome["reason"],
                "ENTRY_HANDOFF_CONTRACT_INVALID",
            )

        asyncio.run(run())

    def test_active_entry_loop_does_not_re_adjudicate_bias_after_action(self):
        source = inspect.getsource(launcher._entry_loop)
        self.assertNotIn("_bias_or_transition_authorized(", source)


if __name__ == "__main__":
    unittest.main()
