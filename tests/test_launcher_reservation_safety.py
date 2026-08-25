import unittest
from types import SimpleNamespace

import mainnet_tier_s_shadow_launcher as launcher


def reserved_state(*, unknown=False, recovery=False):
    return SimpleNamespace(
        canonical_reserved_opportunity_id=7,
        canonical_reserved_at=1.0,
        canonical_reserved_context={
            "opportunity_id": 7,
            "causal_episode_id": "episode-7",
            "side": "LONG",
        },
        canonical_last_consumed_opportunity_id=0,
        canonical_last_captured_opportunity_id=0,
        canonical_opportunity_committed=0,
        canonical_opportunity_captured=0,
        canonical_opportunity_released=0,
        execution_unknown=unknown,
        wstrade_execution_recovery_required=recovery,
    )


class LauncherReservationSafetyTests(unittest.TestCase):
    def test_unknown_execution_keeps_reservation(self):
        state = reserved_state(unknown=True)
        released = launcher._release_execution_reservation_if_safe(
            state, 7, "EXECUTION_EXCEPTION"
        )
        self.assertFalse(released)
        self.assertEqual(state.canonical_reserved_opportunity_id, 7)

    def test_verified_pre_order_failure_releases_for_retry(self):
        state = reserved_state()
        released = launcher._release_execution_reservation_if_safe(
            state, 7, "EXECUTION_BBO_STALE"
        )
        self.assertTrue(released)
        self.assertEqual(state.canonical_reserved_opportunity_id, 0)

    def test_recovery_flat_releases_but_flattened_fill_is_consumed(self):
        flat = reserved_state(recovery=True)
        self.assertTrue(launcher._settle_reconciled_reservation(flat, "FLAT"))
        self.assertEqual(flat.canonical_reserved_opportunity_id, 0)
        self.assertEqual(flat.canonical_last_consumed_opportunity_id, 0)

        flattened = reserved_state(recovery=True)
        self.assertTrue(launcher._settle_reconciled_reservation(
            flattened, "UNOWNED_POSITION_FLATTENED"
        ))
        self.assertEqual(flattened.canonical_reserved_opportunity_id, 0)
        self.assertEqual(flattened.canonical_last_consumed_opportunity_id, 7)

        recovered = reserved_state(recovery=True)
        self.assertTrue(launcher._settle_reconciled_reservation(
            recovered, "RECOVERY_VERIFIED_FLAT_AFTER_FILL"
        ))
        self.assertEqual(recovered.canonical_last_consumed_opportunity_id, 7)

    def test_verified_fill_then_flatten_commits_without_position(self):
        state = reserved_state()
        state.wstrade_live_last_entry_outcome = {
            "canonical_opportunity_id": 7,
            "causal_episode_id": "episode-7",
            "capture_required": True,
        }
        result = {
            "canonical_opportunity_id": 7,
            "causal_episode_id": "episode-7",
            "side": "LONG",
            "ignition": {"last_evidence_ms": 1_000},
        }
        self.assertTrue(launcher._commit_live_execution_capture(
            state, result, position=None
        ))
        self.assertEqual(state.canonical_last_consumed_opportunity_id, 7)
        self.assertIn("episode-7", state._ignition_tombstones)


if __name__ == "__main__":
    unittest.main()
