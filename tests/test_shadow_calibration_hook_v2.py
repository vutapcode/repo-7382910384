import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong import shadow_calibration_hook_v2 as hook


def _runtime(pos, state):
    def close(position, result, now):
        position.active = False
        state.mainnet_shadow_last_net_pnl = 0.01

    base = SimpleNamespace(
        app=SimpleNamespace(state=state),
        _close_shadow=close,
    )
    hardened = SimpleNamespace(runtime=SimpleNamespace(base=base))
    hook.install(hardened)
    return base


class ShadowCalibrationTaintTests(unittest.TestCase):
    def _state(self):
        return SimpleNamespace(mainnet_shadow_entry_edge={
            "entry_mode": "NORMAL",
            "micro_regime": {"regime": "TREND"},
            "edge_class": "HIGH_EDGE",
        })

    def test_gap_tainted_close_is_excluded_from_empirical_alpha(self):
        state = self._state()
        pos = SimpleNamespace(
            active=True, side="LONG", entry_price=100.0, qty=0.001,
            calibration_tainted=True,
            data_gap_reason="FUTURES_EXECUTION_FEED_STALE",
            position_cycle_id="p1", causal_episode_id="e1",
        )
        base = _runtime(pos, state)
        with patch.object(hook.edge_calibration_v2, "record") as record:
            base._close_shadow(pos, {"reason": "HARD_SL"}, 101.0)
        record.assert_not_called()
        self.assertEqual(state.edge_cal_v2_excluded_tainted, 1)
        self.assertEqual(
            state.edge_cal_v2_last_exclusion["reason"],
            "FUTURES_EXECUTION_FEED_STALE",
        )

    def test_clean_close_still_updates_empirical_alpha(self):
        state = self._state()
        saves = []
        state.wstrade_runtime_state_save = lambda: saves.append(True)
        pos = SimpleNamespace(
            active=True, side="LONG", entry_price=100.0, qty=0.001,
            calibration_tainted=False,
        )
        base = _runtime(pos, state)
        with patch.object(hook.edge_calibration_v2, "record") as record:
            base._close_shadow(pos, {"reason": "GUARDIAN"}, 101.0)
        record.assert_called_once()
        self.assertEqual(saves, [True])


if __name__ == "__main__":
    unittest.main()
