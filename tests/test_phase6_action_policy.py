import unittest

from loi_he_thong import entry_action_policy as policy
import mainnet_tier_s_shadow_launcher as launcher


class Phase6ActionPolicyTests(unittest.TestCase):
    def truth(self):
        return {
            "contract_hash": "truth-hash",
            "causal_episode_id": "episode-1",
            "side": "LONG",
        }

    def test_taker_mirrors_active_decision(self):
        result = policy.evaluate(
            self.truth(), {"cost_ok": True}, {},
            active_result={"decision": "GO", "execution_policy": "TAKER"},
            quorum_ok=True,
        )
        self.assertEqual(result["action"], "ACT_TAKER_NOW")
        self.assertFalse(result["authority"])

    def test_maker_mirrors_active_decision(self):
        result = policy.evaluate(
            self.truth(), {"cost_ok": True}, {},
            active_result={"decision": "GO", "execution_policy": "MAKER"},
            quorum_ok=True,
        )
        self.assertEqual(result["action"], "POST_MAKER")

    def test_non_go_or_failed_quorum_remains_wait(self):
        self.assertEqual(
            policy.mirror_active_launcher(
                {"decision": "WAIT", "execution_policy": "TAKER"}, True
            ),
            "WAIT_INFORMATION",
        )
        self.assertEqual(
            policy.mirror_active_launcher(
                {"decision": "GO", "execution_policy": "TAKER"}, False
            ),
            "WAIT_INFORMATION",
        )

    def test_expiry_and_economics_are_counterfactual_only(self):
        expired = policy.evaluate(
            self.truth(), {"cost_ok": True},
            {"opportunity_expired": True},
            active_result={"decision": "WAIT", "execution_policy": "MAKER"},
            quorum_ok=True,
        )
        self.assertEqual(expired["action"], "WAIT_INFORMATION")
        self.assertEqual(
            expired["counterfactual_action"],
            "ABANDON_OPPORTUNITY_EXPIRED",
        )

        economics = policy.evaluate(
            self.truth(), {"cost_ok": False}, {},
            active_result={"decision": "WAIT", "execution_policy": "MAKER"},
            quorum_ok=True,
        )
        self.assertEqual(economics["action"], "WAIT_INFORMATION")
        self.assertEqual(
            economics["counterfactual_action"], "ABANDON_ECONOMICS"
        )

    def test_launcher_shadow_mirror_never_changes_active_result(self):
        truth = launcher.market_thesis.build({
            "decision": "GO",
            "side": "LONG",
            "causal_episode_id": "episode-1",
            "ignition": {
                "proof_type": "METAORDER_CONTINUATION",
                "cash_venues": ["binance_spot", "coinbase_spot"],
            },
        })
        active = {
            "decision": "GO",
            "side": "LONG",
            "execution_policy": "TAKER",
            "causal_episode_id": "episode-1",
            "edge_tier": {
                "cost_ok": True,
                "economic_contract_version": "test-cost",
                "execution_urgency": {
                    "status": "EXECUTION_URGENCY_UNVERIFIED",
                    "authority": False,
                },
            },
            "authority_contracts": {
                "contracts": {"MARKET_TRUTH": truth},
            },
        }
        before = dict(active)
        observed = launcher._phase6_action_shadow(active, True)

        self.assertEqual(active, before)
        self.assertEqual(observed["action"], "ACT_TAKER_NOW")
        self.assertFalse(observed["authority"])
        self.assertEqual(
            observed["urgency_evidence"]["status"],
            "EXECUTION_URGENCY_UNVERIFIED",
        )


if __name__ == "__main__":
    unittest.main()
