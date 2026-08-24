from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_namespace(path):
    text = path.read_text(encoding="utf-8")
    compile(text, str(path), "exec")
    ns = {}
    exec(compile(text, str(path), "exec"), ns)
    return ns, text


class OpsHardeningV4Tests(unittest.TestCase):
    def test_supervisor_uses_system_manager(self):
        path = ROOT / "loi_he_thong" / "ops_supervisor.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertNotIn("'--user'", text)
        self.assertIn("'systemctl', 'show'", text)

    def test_hardened_launcher_pins_paths_and_installs_guards(self):
        path = ROOT / "mainnet_tier_s_shadow_hardened_launcher.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        for marker in (
            "SMC_RUNTIME_DIR",
            "SMC_JOURNAL_DIR",
            "persistence_decision_guard.py",
            "close_durability_guard.py",
        ):
            self.assertIn(marker, text)

    def test_persistence_failure_preserves_risk_decision(self):
        ns, _ = load_namespace(ROOT / "loi_he_thong" / "persistence_decision_guard.py")
        state = types.SimpleNamespace()
        wrapper = types.SimpleNamespace(
            _orig_assess=lambda *a, **k: {"action": "HARD_SL"},
            _last_persist_mono=0.0,
            base=types.SimpleNamespace(app=types.SimpleNamespace(state=state)),
            runtime_state=types.SimpleNamespace(
                save=lambda base: (_ for _ in ()).throw(OSError("disk full"))
            ),
            risk=types.SimpleNamespace(),
        )
        safe = ns["install"](wrapper)
        result = safe(object(), 100.0, {}, now=10.0)
        self.assertEqual(result["action"], "HARD_SL")
        self.assertTrue(state.shadow_persistence_dirty)
        self.assertEqual(state.shadow_persistence_error_count, 1)

    def test_close_failure_rolls_back_active_position_and_accounting(self):
        ns, _ = load_namespace(ROOT / "loi_he_thong" / "close_durability_guard.py")
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
            pos.active = False
            pos.exit_price = 101.0
            state.mainnet_shadow_balance_usdt = 5.5
            state.mainnet_shadow_trades = 1
            state.mainnet_shadow_wins = 1
            raise OSError("disk full")

        wrapper = types.SimpleNamespace(
            base=types.SimpleNamespace(
                _close_shadow=broken_close,
                app=types.SimpleNamespace(state=state),
            )
        )
        safe = ns["install"](wrapper)
        self.assertIsNone(safe(pos, {"reason": "TEST"}, 10.0))
        self.assertTrue(pos.active)
        self.assertFalse(hasattr(pos, "exit_price"))
        self.assertEqual(state.mainnet_shadow_balance_usdt, 5.4)
        self.assertEqual(state.mainnet_shadow_trades, 0)
        self.assertEqual(state.mainnet_shadow_wins, 0)
        self.assertTrue(state.shadow_close_persistence_failed)

    def test_persistence_throttle_uses_monotonic_clock(self):
        path = ROOT / "loi_he_thong" / "persistence_decision_guard.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("time.monotonic()", text)
        self.assertIn("_last_persist_mono", text)
        self.assertNotIn("t - last < 1.0", text)


if __name__ == "__main__":
    unittest.main()
