import unittest

from loi_he_thong import entry_action_policy as policy


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


if __name__ == "__main__":
    unittest.main()
