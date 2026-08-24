import unittest
from types import SimpleNamespace

from loi_he_thong import shadow_daily_loss as daily


class ShadowDailyLossTests(unittest.TestCase):
    def test_current_day_legacy_checkpoint_cannot_erase_breach(self):
        now = 1_787_440_500.0
        state = SimpleNamespace(mainnet_shadow_realized_pnl=-0.851)

        daily.initialize(
            state, now=now, restored=True, checkpoint_ts=now - 30.0,
            limit=0.60,
        )

        self.assertAlmostEqual(state.mainnet_shadow_day_realized_pnl, -0.851)
        self.assertTrue(state.mainnet_shadow_daily_locked)

    def test_new_utc7_day_unlocks_and_resets_realized_bucket(self):
        now = 1_787_440_500.0
        state = SimpleNamespace(
            mainnet_shadow_day_start_ms=daily.day_start_ms(now - 86400.0),
            mainnet_shadow_day_realized_pnl=-0.9,
            mainnet_shadow_daily_locked=True,
        )

        report = daily.report(state, limit=0.60, now=now)

        self.assertEqual(report["realized_pnl_usdt"], 0.0)
        self.assertFalse(report["locked"])

    def test_open_position_equity_breach_locks_before_next_entry(self):
        now = 1_787_440_500.0
        state = SimpleNamespace(
            mainnet_shadow_day_start_ms=daily.day_start_ms(now),
            mainnet_shadow_day_realized_pnl=-0.45,
            mainnet_shadow_daily_locked=False,
        )
        pos = SimpleNamespace(
            active=True, side="LONG", entry_price=100_000.0, qty=0.001,
        )

        report = daily.report(
            state, pos, bid=99_800.0, ask=99_801.0,
            fee_bps_per_side=9.0, limit=0.60, now=now,
        )

        self.assertLessEqual(report["equity_pnl_usdt"], -0.60)
        self.assertTrue(report["locked"])

    def test_shadow_audit_records_breach_without_locking(self):
        now = 1_787_440_500.0
        state = SimpleNamespace(
            mainnet_shadow_day_start_ms=daily.day_start_ms(now),
            mainnet_shadow_day_realized_pnl=-0.70,
            mainnet_shadow_daily_locked=True,
        )

        report = daily.report(
            state, limit=0.60, now=now, enforce=False,
        )
        daily.record_close(
            state, -0.10, limit=0.60, now=now, enforce=False,
        )

        self.assertTrue(report["would_lock_if_live"])
        self.assertFalse(report["enforced"])
        self.assertFalse(report["locked"])
        self.assertFalse(state.mainnet_shadow_daily_locked)
        self.assertEqual(
            state.mainnet_shadow_daily_lock_reason, "SHADOW_AUDIT_ONLY"
        )


if __name__ == "__main__":
    unittest.main()
