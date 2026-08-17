from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MainnetShadowOpsContractTests(unittest.TestCase):
    def test_mainnet_service_runs_unprivileged_with_stable_state_paths(self):
        text = (ROOT / "ops" / "systemd" / "smc2026-bot.service").read_text(encoding="utf-8")
        self.assertIn("User=ubuntu", text)
        self.assertIn("Group=ubuntu", text)
        self.assertIn("SMC_RUNTIME_DIR=/home/ubuntu/.local/state/smc2026/runtime", text)
        self.assertIn("SMC_ENABLE_TRADING=false", text)
        self.assertIn("SMC_MAINNET_ARMED=false", text)
        self.assertIn("ExecStartPre=+/usr/bin/install -d", text)
        self.assertIn("SMC_SHADOW_FEE_BPS_PER_SIDE=9.0", text)

    def test_runtime_lock_default_is_not_tmp(self):
        text = (ROOT / "loi_he_thong" / "runtime_lock.py").read_text(encoding="utf-8")
        self.assertNotIn("/tmp/smc2026-runtime", text)
        self.assertIn('".local" / "state" / "smc2026" / "runtime"', text)

    def test_collectors_are_data_only(self):
        macro = (ROOT / "1_tai_du_lieu" / "tai_vi_mo" / "tai_vi_mo.py").read_text(encoding="utf-8")
        coinbase = (ROOT / "1_tai_du_lieu" / "tai_coinbase" / "tai_coinbase.py").read_text(encoding="utf-8")
        self.assertNotIn("bias_council", macro)
        self.assertNotIn("entry_council", coinbase)
        self.assertNotIn("update_state(", macro)
        self.assertNotIn("update_state(", coinbase)

    def test_coinbase_rolling_flow_is_incremental(self):
        path = ROOT / "1_tai_du_lieu" / "tai_coinbase" / "tai_coinbase.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("class RollingFlow", text)
        self.assertIn("self.signed +=", text)
        self.assertNotIn("sum(delta for", text)

    def test_shadow_health_guards_reconnect_and_risk_first(self):
        path = ROOT / "loi_he_thong" / "shadow_runtime_health.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("def safe_ref", text)
        self.assertIn("ENTRY_FAST_LAG", text)
        self.assertIn("STALE_SPOT_CAUSAL_GUARDIAN_DISABLED", text)
        self.assertIn("rr=risk.assess", text)
        self.assertIn("STALE_OI", text)

    def test_shadow_state_persists_ratchet_and_resets_short_term_pressure(self):
        path = ROOT / "loi_he_thong" / "shadow_runtime_state.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn('"risk_best_r"', text)
        self.assertIn('"risk_profit_floor_r"', text)
        self.assertIn('"risk_whale_seen"', text)
        self.assertIn('"risk_whale_exhaustion_since"', text)
        self.assertIn("os.replace(tmp, path)", text)

    def test_hardened_wrapper_compiles_and_has_one_runtime_contract(self):
        path = ROOT / "mainnet_tier_s_shadow_risk_launcher.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("shadow_runtime_health_runtime", text)
        self.assertIn("shadow_runtime_state_runtime", text)
        self.assertIn("BIAS_INVALID_OR_EXPIRED", text)
        self.assertIn("base._bias_loop = _bias_loop", text)
        self.assertNotIn("if pos is none", text)

    def test_integrity_scanner_uses_ascii_escape_byte_literals(self):
        path = ROOT / "ops" / "repo_integrity_check.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn(r'b"\xff\xfe"', text)
        self.assertIn(r'b"\xef\xbb\xbf"', text)
        self.assertNotIn("ÿ", text)
        self.assertNotIn("ï»¿", text)


if __name__ == "__main__":
    unittest.main()
