import time
import unittest
from pathlib import Path

from ops import shadow_stack_ctl as ctl


class ShadowStackCtlTests(unittest.TestCase):
    def test_environment_must_be_collect_only(self):
        ctl.assert_fail_closed_environment(dict(ctl.FAIL_CLOSED_ENV))
        unsafe = dict(ctl.FAIL_CLOSED_ENV)
        unsafe["SMC_ENABLE_TRADING"] = "true"
        with self.assertRaisesRegex(ctl.StackError, "REAL_MONEY_NOT_FAIL_CLOSED"):
            ctl.assert_fail_closed_environment(unsafe)

    def test_active_shadow_position_blocks_restart(self):
        ctl.assert_flat_state({"position": None})
        with self.assertRaisesRegex(ctl.StackError, "POSITION_ACTIVE"):
            ctl.assert_flat_state({"position": {"active": True}})

    def test_recorder_requires_fresh_clean_same_code_snapshot(self):
        now = time.time()
        payload = {
            "updated_at_ms": int(now * 1000),
            "code_version": "abc",
            "current_status": "OK",
            "connections": {name: True for name in ctl.REQUIRED_CONNECTIONS},
            "queue": {"dropped": 0},
            "depth": {"synced": True, "gaps": 0},
            "writer_errors": 0,
            "decision_tap_parse_errors": 0,
        }
        self.assertEqual(ctl.recorder_ready(payload, "abc", now), (True, "READY"))
        payload["depth"]["gaps"] = 1
        self.assertEqual(
            ctl.recorder_ready(payload, "abc", now)[1],
            "RECORDER_DEPTH_NOT_CLEAN",
        )

    def test_bot_requires_pid_code_shadow_and_no_live_arm(self):
        now = time.time()
        payload = {
            "updated_at_ms": int(now * 1000),
            "pid": 42,
            "code_version": "abc",
            "strategy_profile": {"mode": "MAINNET_SHADOW"},
            "trading_enabled": False,
            "wstrade_live_armed": False,
            "system_ready": True,
        }
        self.assertEqual(ctl.bot_ready(payload, "abc", 42, now), (True, "READY"))
        payload["wstrade_live_armed"] = True
        self.assertEqual(ctl.bot_ready(payload, "abc", 42, now)[1], "BOT_LIVE_ARMED")

    def test_checked_in_units_start_recorder_before_bot(self):
        root = Path(__file__).resolve().parents[1] / "ops/systemd"
        bot = (root / "wstrade-bot.service").read_text(encoding="utf-8")
        health = (root / "wstrade-health.service").read_text(encoding="utf-8")
        self.assertIn("After=network-online.target wstrade-recorder.service", bot)
        self.assertIn("Wants=network-online.target wstrade-recorder.service", bot)
        self.assertIn(
            "After=network-online.target wstrade-recorder.service wstrade-bot.service",
            health,
        )
        self.assertIn("Wants=network-online.target", health)
        self.assertNotIn(
            "Wants=network-online.target wstrade-recorder.service wstrade-bot.service",
            health,
        )


if __name__ == "__main__":
    unittest.main()
