from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_source(path):
    text = path.read_text(encoding="utf-8")
    compile(text, str(path), "exec")
    ns = {}
    exec(compile(text, str(path), "exec"), ns)
    return ns, text


class OpsHardeningV5Tests(unittest.TestCase):
    def test_liveness_module_and_launcher_wiring(self):
        path = ROOT / "loi_he_thong" / "critical_loop_liveness.py"
        ns, text = load_source(path)
        self.assertIn("critical_loops", text)
        self.assertIn("shadow_position_active", text)
        self.assertIn("consecutive_errors", text)
        launcher = (ROOT / "mainnet_tier_s_shadow_hardened_launcher.py").read_text(encoding="utf-8")
        compile(launcher, "launcher", "exec")
        self.assertIn("critical_loop_liveness.py", launcher)
        self.assertIn("liveness.install(runtime)", launcher)

    def test_supervisor_startup_grace_and_critical_policy(self):
        ns, text = load_source(ROOT / "loi_he_thong" / "ops_supervisor.py")
        self.assertNotIn("'--user'", text)
        ns["_BOT_PID_FIRST_SEEN"].clear()
        service = {"pid": 123}
        self.assertTrue(ns["_bot_in_startup_grace"](service, {}, 100.0))
        self.assertTrue(ns["_bot_in_startup_grace"](service, {}, 119.0))
        self.assertTrue(ns["_bot_in_startup_grace"](service, {}, 159.0))
        self.assertFalse(ns["_bot_in_startup_grace"](service, {}, 161.0))
        self.assertFalse(ns["_bot_in_startup_grace"](service, {"pid": 123}, 101.0))

        payload = {
            "critical_liveness_installed_at": 80.0,
            "critical_loops": {
                "bias": {"age_sec": 6.0, "consecutive_errors": 0},
                "entry": {"age_sec": 0.1, "consecutive_errors": 0},
                "guardian": {"age_sec": 999.0, "consecutive_errors": 99},
            },
            "shadow_position_active": False,
        }
        self.assertEqual(ns["_critical_loop_classification"](payload, 100.0), "BIAS_LOOP_STALLED")
        payload["critical_loops"]["bias"]["age_sec"] = 0.1
        self.assertIsNone(ns["_critical_loop_classification"](payload, 100.0))
        payload["shadow_position_active"] = True
        self.assertEqual(ns["_critical_loop_classification"](payload, 100.0), "GUARDIAN_LOOP_STALLED")

    def test_close_rollback_truncates_false_exit(self):
        ns, _ = load_source(ROOT / "loi_he_thong" / "close_durability_guard.py")
        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "events.jsonl"
            journal.write_bytes(b"ENTRY\n")
            state = types.SimpleNamespace(
                mainnet_shadow_balance_usdt=5.4,
                mainnet_shadow_realized_pnl=0.0,
                mainnet_shadow_trades=0,
                mainnet_shadow_wins=0,
                mainnet_shadow_losses=0,
                mainnet_shadow_breakevens=0,
            )
            pos = types.SimpleNamespace(active=True, entry_price=100.0)

            def broken_close(pos, result, now):
                with open(journal, "ab") as handle:
                    handle.write(b"EXIT\n")
                    handle.flush()
                pos.active = False
                state.mainnet_shadow_balance_usdt = 5.5
                state.mainnet_shadow_trades = 1
                raise OSError("disk full")

            base = types.SimpleNamespace(
                _close_shadow=broken_close,
                app=types.SimpleNamespace(state=state),
                EVENT_PATH=journal,
            )
            safe = ns["install"](types.SimpleNamespace(base=base))
            self.assertIsNone(safe(pos, {}, 10.0))
            self.assertTrue(pos.active)
            self.assertEqual(state.mainnet_shadow_balance_usdt, 5.4)
            self.assertEqual(state.mainnet_shadow_trades, 0)
            self.assertEqual(journal.read_bytes(), b"ENTRY\n")

    def test_close_snapshot_rotates_before_transaction_and_never_sparse_extends(self):
        ns, _ = load_source(ROOT / "loi_he_thong" / "close_durability_guard.py")
        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "events.jsonl"
            journal.write_bytes(b"ENTRY\n")
            state = types.SimpleNamespace()
            pos = types.SimpleNamespace(active=True)

            def broken_close(pos, result, now):
                with journal.open("ab") as handle:
                    handle.write(b"EXIT\n")
                pos.active = False
                raise OSError("checkpoint failed")

            base = types.SimpleNamespace(
                _close_shadow=broken_close,
                app=types.SimpleNamespace(state=state),
                EVENT_PATH=journal,
            )
            actual_prepare = ns["journal_segments"].prepare_append
            with patch.object(
                ns["journal_segments"], "prepare_append",
                side_effect=lambda path: actual_prepare(path, max_bytes=1),
            ):
                safe = ns["install"](types.SimpleNamespace(base=base))
                self.assertIsNone(safe(pos, {}, 10.0))

            self.assertTrue(pos.active)
            self.assertEqual(journal.read_bytes(), b"")
            segments = list(Path(td).glob("events.segment.*.jsonl"))
            self.assertEqual(len(segments), 1)
            self.assertEqual(segments[0].read_bytes(), b"ENTRY\n")


if __name__ == "__main__":
    unittest.main()
