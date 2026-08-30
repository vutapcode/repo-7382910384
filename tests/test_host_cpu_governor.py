import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong.host_cpu_governor import HostCpuGovernor, parse_proc_stat


class HostCpuGovernorTests(unittest.TestCase):
    def test_proc_stat_and_rolling_percent_are_normalized_to_whole_host(self):
        self.assertEqual(parse_proc_stat("cpu  10 0 20 70 0 0 0 0\n"), (100, 70))
        with tempfile.TemporaryDirectory() as temp:
            external = Path(temp) / "external.json"
            history = Path(temp) / "history.json"
            with patch.dict("os.environ", {
                "WSTRADE_LIGHTSAIL_CPU_PATH": str(external),
                "WSTRADE_CPU_HISTORY_PATH": str(history),
            }):
                governor = HostCpuGovernor(cpu_count=2)
                governor.sample(now_mono=0, now_wall=1, counters=(100, 80), scan_processes=False)
                snap = governor.sample(
                    now_mono=10, now_wall=11, counters=(300, 240), scan_processes=False
                )
        self.assertAlmostEqual(snap["host_cpu_15m_pct"], 20.0)
        self.assertEqual(snap["governor_mode"], "SAFETY_ONLY")
        self.assertFalse(snap["entry_cpu_allowed"])
        self.assertFalse(snap["live_entry_cpu_allowed"])
        self.assertTrue(snap["shadow_entry_cpu_allowed"])

    def test_modes_and_state_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {
                "WSTRADE_CPU_HISTORY_PATH": str(Path(temp) / "history.json"),
            }):
                governor = HostCpuGovernor(cpu_count=2)
                self.assertEqual(governor._choose_mode(16.99), "NORMAL")
                self.assertEqual(governor._choose_mode(17.0), "CONSERVE")
                self.assertEqual(governor._choose_mode(18.5), "DEFENSIVE")
                self.assertEqual(governor._choose_mode(19.5), "SAFETY_ONLY")
                state = SimpleNamespace()
                payload = {
                    "host_cpu_15m_pct": 12.0, "host_cpu_1h_pct": 11.0,
                    "cpu_budget_15m_remaining": 10.0, "cpu_budget_1h_remaining": 20.0,
                    "governor_mode": "NORMAL", "entry_cpu_allowed": True,
                    "hard_limit_respected": True, "top_cpu_processes": [],
                    "production_blockers": [], "lightsail_cpu_last_seen": None,
                    "metric_age_seconds": None, "metric_fresh": False,
                }
                governor.publish(state, payload)
                self.assertTrue(state.host_cpu_entry_allowed)
                self.assertTrue(state.live_entry_cpu_allowed)
                self.assertTrue(state.shadow_entry_cpu_allowed)
                self.assertEqual(state.governor_mode, "NORMAL")

    def test_recent_history_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            history = Path(temp) / "history.json"
            external = Path(temp) / "external.json"
            with patch.dict("os.environ", {
                "WSTRADE_LIGHTSAIL_CPU_PATH": str(external),
                "WSTRADE_CPU_HISTORY_PATH": str(history),
            }):
                first = HostCpuGovernor(cpu_count=2)
                first.sample(now_mono=100.0, now_wall=1000.0, counters=(100, 80), scan_processes=False)
                first.sample(now_mono=110.0, now_wall=1010.0, counters=(300, 240), scan_processes=False)
                with patch('loi_he_thong.host_cpu_governor.time.time', return_value=1010.0), patch(
                    'loi_he_thong.host_cpu_governor.time.monotonic', return_value=110.0
                ):
                    first.checkpoint()
                    second = HostCpuGovernor(cpu_count=2)
                self.assertTrue(second.history_restored)
                snap = second.sample(
                    now_mono=120.0, now_wall=1020.0,
                    counters=(500, 400), scan_processes=False,
                )
                self.assertEqual(snap['coverage_15m_seconds'], 20.0)
                self.assertAlmostEqual(snap['host_cpu_15m_pct'], 20.0)
                self.assertTrue(snap['cpu_history_restored'])
                self.assertEqual(snap['cpu_history_window_start_ms'], 1_000_000)
                self.assertEqual(snap['cpu_governor_started_at_ms'], 1_010_000)
                self.assertAlmostEqual(
                    snap['post_start_coverage_15m_seconds'], 10.0
                )
                self.assertAlmostEqual(
                    snap['post_start_coverage_1h_seconds'], 10.0
                )

    def test_incomplete_rolling_history_blocks_live_but_not_shadow(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {
                "WSTRADE_CPU_HISTORY_PATH": str(Path(temp) / "history.json"),
            }):
                governor = HostCpuGovernor(cpu_count=2)
                governor.sample(
                    now_mono=0, now_wall=1, counters=(100, 90),
                    scan_processes=False,
                )
                snap = governor.sample(
                    now_mono=10, now_wall=11, counters=(300, 270),
                    scan_processes=False,
                )
        self.assertEqual(snap["governor_mode"], "NORMAL")
        self.assertFalse(snap["cpu_admission_history_complete"])
        self.assertFalse(snap["entry_cpu_allowed"])
        self.assertFalse(snap["live_entry_cpu_allowed"])
        self.assertTrue(snap["shadow_entry_cpu_allowed"])


if __name__ == "__main__":
    unittest.main()
