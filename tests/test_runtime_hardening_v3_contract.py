from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeHardeningV3ContractTests(unittest.TestCase):
    def test_runtime_patch_compiles_and_preserves_single_bias_owner(self):
        path = ROOT / "loi_he_thong" / "runtime_hardening_v3.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        for marker in (
            "TIER_S_RUNTIME_PATCH_V4_SINGLE_BIAS_OWNER",
            "CANONICAL_BIAS_OWNER_UNCHANGED",
            "BIAS_OWNER_MUTATED_BY_RUNTIME_HARDENING",
            "GUARDIAN_FLOW_SEC = 3.0",
            "reversed(rows)",
            "0.0 <= float(now) - ts <= 5.0",
            "base.FEE_BPS_PER_SIDE = fee",
            "futures_flow_ring_saturated",
            'out["entry_ready"] = False',
        ):
            self.assertIn(marker, text)
        for retired in (
            "BIAS_FLOW_SEC = 60.0",
            "BIAS_MIN_COVERAGE_SEC",
            "BIAS_MIN_IMB",
            "material_floor_btc_by_venue",
            "bias.s3 = s3",
        ):
            self.assertNotIn(retired, text)

    def test_state_guard_compiles_and_checks_v2_invariants(self):
        path = ROOT / "ops" / "shadow_state_guard.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        for marker in (
            "counter_invariant",
            "position.hard_sl:wrong_side_long",
            "position.hard_sl:wrong_side_short",
            "position.r:inconsistent_with_hard_sl",
            "position.floor_r:above_best_r",
            "position.fee_r",
        ):
            self.assertIn(marker, text)

    def test_systemd_uses_guard_and_hardened_launcher(self):
        text = (ROOT / "ops" / "systemd" / "smc2026-bot.service").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPre=/home/ubuntu/WStrade/.venv/bin/python "
            "/home/ubuntu/WStrade/ops/shadow_state_guard.py",
            text,
        )
        self.assertIn("mainnet_tier_s_lean_launcher.py", text)
        self.assertIn("SMC_SHADOW_FEE_BPS_PER_SIDE=9.0", text)
        self.assertIn(
            "SMC_SHADOW_COMMISSION_PROFILE=BINANCE_USDM_STANDARD", text
        )
        self.assertIn("SMC_SHADOW_MAKER_FEE_BPS=2.0", text)
        self.assertIn("SMC_SHADOW_TAKER_FEE_BPS=5.0", text)

    def test_hardened_launcher_self_guards_manual_runs(self):
        path = ROOT / "mainnet_tier_s_shadow_hardened_launcher.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("shadow_state_guard.py", text)
        self.assertIn("runtime_hardening_v3.py", text)

    def test_recent_runtime_and_state_fixes_stay_locked(self):
        runtime_path = ROOT / "loi_he_thong" / "runtime_hardening_v3.py"
        runtime = runtime_path.read_text(encoding="utf-8")
        compile(runtime, str(runtime_path), "exec")
        self.assertNotIn("MIN_IAB", runtime)
        self.assertIn("_install_bias(base)", runtime)
        self.assertIn("before is not after", runtime)

        guard_path = ROOT / "ops" / "shadow_state_guard.py"
        guard = guard_path.read_text(encoding="utf-8")
        compile(guard, str(guard_path), "exec")
        self.assertIn('breakevens = counter(raw, "breakevens", optional=True)', guard)
        self.assertNotIn('required.add("breakevens")', guard)

        state_path = ROOT / "loi_he_thong" / "shadow_runtime_state.py"
        state = state_path.read_text(encoding="utf-8")
        compile(state, str(state_path), "exec")
        self.assertIn("SHADOW_RUNTIME_STATE_CORRUPT", state)
        self.assertIn("SHADOW_RUNTIME_STATE_UNSUPPORTED", state)


if __name__ == "__main__":
    unittest.main()
