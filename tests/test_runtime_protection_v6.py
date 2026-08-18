from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_namespace(path):
    text = path.read_text(encoding="utf-8")
    compile(text, str(path), "exec")
    ns = {}
    exec(compile(text, str(path), "exec"), ns)
    return ns


class RuntimeProtectionV6Tests(unittest.TestCase):
    def test_data_gap_persistence_retries_while_feed_stays_stale(self):
        ns = load_namespace(ROOT / "loi_he_thong" / "data_gap_taint_guard.py")
        clock = {"wall": 100.0, "mono": 10.0}
        ns["time"] = types.SimpleNamespace(
            time=lambda: clock["wall"],
            monotonic=lambda: clock["mono"],
        )

        pos = types.SimpleNamespace(active=True)
        state = types.SimpleNamespace(mainnet_shadow_position=pos)
        saves = {"n": 0}

        def save(base):
            saves["n"] += 1
            if saves["n"] == 1:
                raise OSError("disk full")

        wrapper = types.SimpleNamespace(
            base=types.SimpleNamespace(
                app=types.SimpleNamespace(state=state),
            ),
            runtime_state=types.SimpleNamespace(
                PERSIST_FIELDS=(),
                save=save,
            ),
            health=types.SimpleNamespace(
                exec_price=lambda base, now=None: 0.0,
            ),
        )

        guarded = ns["install"](wrapper)
        self.assertEqual(guarded(wrapper.base, 100.0), 0.0)
        self.assertTrue(pos.calibration_tainted)
        self.assertTrue(state.shadow_data_gap_persist_pending)
        self.assertEqual(saves["n"], 1)

        clock["mono"] = 10.5
        guarded(wrapper.base, 100.5)
        self.assertEqual(saves["n"], 1)

        clock["mono"] = 11.1
        guarded(wrapper.base, 101.1)
        self.assertEqual(saves["n"], 2)
        self.assertFalse(state.shadow_data_gap_persist_pending)

    def test_integrity_fault_overrides_healthy_readiness(self):
        ns = load_namespace(ROOT / "loi_he_thong" / "integrity_readiness_guard.py")
        state = types.SimpleNamespace(
            shadow_integrity_fault=True,
            shadow_integrity_fault_reason="SHADOW_JOURNAL_DIVERGED:test",
        )

        def healthy(base, state_obj, now=None):
            state_obj.mainnet_shadow_ready = True
            state_obj.system_ready = True
            state_obj.trading_enabled = True
            state_obj.last_readiness_reason = "SHADOW_READY" 
            return {"entry_ready": True, "full_tier_s_ready": True}

        wrapper = types.SimpleNamespace(
            health=types.SimpleNamespace(health=healthy),
            base=types.SimpleNamespace(app=types.SimpleNamespace(state=state)),
        )
        guarded = ns["install"](wrapper)
        out = guarded(wrapper.base, state, 100.0)

        self.assertFalse(out["entry_ready"])
        self.assertFalse(out["full_tier_s_ready"])
        self.assertFalse(out["integrity_ok"])
        self.assertFalse(state.mainnet_shadow_ready)
        self.assertFalse(state.system_ready)
        self.assertFalse(state.trading_enabled)
        self.assertEqual(state.last_readiness_reason, "SHADOW_JOURNAL_DIVERGED:test")

    def test_hardened_launcher_wires_integrity_latch(self):
        path = ROOT / "mainnet_tier_s_shadow_hardened_launcher.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("integrity_readiness_guard.py", text)
        self.assertIn("integrity_readiness_guard.install(runtime)", text)


if __name__ == "__main__":
    unittest.main()
