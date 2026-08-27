import time
import types
import unittest
from pathlib import Path

from loi_he_thong import shadow_runtime_health


class ShadowCpuSamplingTests(unittest.TestCase):
    def _state(self, now):
        return types.SimpleNamespace(
            execution_price_time=now,
            execution_best_bid=100.0,
            execution_best_ask=101.0,
            thoi_gian_tick_cuoi=now,
            thoi_gian_coinbase_ticker_cuoi=now,
            thoi_gian_dong_tien_cuoi=now,
            coinbase_flow_3s_ts=now,
            thoi_gian_vi_mo_cuoi=now,
            danh_sach_khop_lenh_futures=[{
                "thoi_gian_ms": now * 1000.0,
                "gia": 100.5,
            }],
            host_cpu_entry_allowed=False,
        )

    def test_cpu_budget_never_censors_valid_shadow_sample(self):
        now = time.time()
        state = self._state(now)
        base = types.SimpleNamespace(app=types.SimpleNamespace(state=state))

        result = shadow_runtime_health.health(base, state, now)

        self.assertTrue(result["entry_ready"])
        self.assertTrue(state.mainnet_shadow_ready)
        self.assertFalse(result["live_entry_ready"])
        self.assertFalse(state.mainnet_live_entry_ready)
        self.assertNotIn("host_cpu_budget", result["operational_blockers"])
        self.assertIn("host_cpu_budget", result["live_operational_blockers"])

    def test_data_quality_still_blocks_shadow_and_live(self):
        now = time.time()
        state = self._state(now)
        state.thoi_gian_dong_tien_cuoi = now - 10.0
        base = types.SimpleNamespace(app=types.SimpleNamespace(state=state))

        result = shadow_runtime_health.health(base, state, now)

        self.assertFalse(result["entry_ready"])
        self.assertFalse(result["live_entry_ready"])

    def test_launcher_preserves_shadow_sampling_but_seals_live_entry(self):
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "mainnet_tier_s_shadow_launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "if _live_entry_authority(s) and not host_cpu_governor.entry_allowed(s)",
            launcher,
        )
        self.assertIn("s.shadow_entry_cpu_allowed = True", launcher)
        self.assertIn("minimum_eval_interval = _shadow_entry_eval_interval", launcher)
        self.assertIn("if bool(row.get(\"strong\")):", launcher)
        self.assertIn(
            's.wstrade_live_arm_reason = "LIVE_HOST_CPU_OR_HEALTH_BLOCKED"',
            launcher,
        )
        self.assertIn("_authority_delay(app.state, ENTRY_POLL)", launcher)

    def test_spot_flow_drain_is_time_sliced_for_guardian_fairness(self):
        root = Path(__file__).resolve().parents[1]
        runtime = (root / "loi_he_thong" / "tier_s_runtime_prune.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("SPOT_DRAIN_MAX_EVENTS = 512", runtime)
        self.assertIn("SPOT_DRAIN_BUDGET_SECONDS = 0.002", runtime)
        self.assertNotIn("processed % 4096", runtime)


if __name__ == "__main__":
    unittest.main()
