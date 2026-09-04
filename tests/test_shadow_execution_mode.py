import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wstrade_watchdog_execution_mode",
    ROOT / "3_thuc_thi" / "giam_sat_he_thong.py",
)
WATCHDOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCHDOG)


class ShadowExecutionModeTests(unittest.TestCase):
    def test_ready_shadow_reports_demo_enabled_and_live_blocked(self):
        state = SimpleNamespace(
            execution_allowed=False,
            trading_enabled=False,
            shadow_readiness_authoritative=True,
        )
        self.assertEqual(
            WATCHDOG.readiness_execution_mode(state, True),
            ("SHADOW_DEMO", False, True),
        )

    def test_ready_live_reports_only_live_enabled(self):
        state = SimpleNamespace(
            execution_allowed=True,
            trading_enabled=True,
            shadow_readiness_authoritative=True,
        )
        self.assertEqual(
            WATCHDOG.readiness_execution_mode(state, True),
            ("LIVE", True, False),
        )

    def test_not_ready_enables_neither_lane(self):
        state = SimpleNamespace(
            execution_allowed=False,
            trading_enabled=False,
            shadow_readiness_authoritative=True,
        )
        self.assertEqual(
            WATCHDOG.readiness_execution_mode(state, False),
            ("OBSERVE_ONLY", False, False),
        )


if __name__ == "__main__":
    unittest.main()
