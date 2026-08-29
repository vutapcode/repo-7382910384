from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MainnetShadowOpsContractTests(unittest.TestCase):
    def test_coinbase_collector_uses_declared_websocket_url(self):
        path = ROOT / "1_tai_du_lieu" / "tai_coinbase" / "tai_coinbase.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn("COINBASE_WS_URL =", text)
        self.assertIn("COINBASE_WS_URL, ping_interval=20", text)
        self.assertNotIn("COINBASEE_WS_URL", text)

    def test_mainnet_service_runs_unprivileged_with_stable_state_paths(self):
        text = (ROOT / "ops" / "systemd" / "smc2026-bot.service").read_text(encoding="utf-8")
        self.assertIn("User=ubuntu", text)
        self.assertIn("Group=ubuntu", text)
        self.assertIn("SMC_RUNTIME_DIR=/home/ubuntu/.local/state/smc2026/runtime", text)
        self.assertIn("SMC_ENABLE_TRADING=false", text)
        self.assertIn("SMC_MAINNET_ARMED=false", text)
        self.assertIn("ExecStartPre=+/usr/bin/install -d", text)
        self.assertIn("SMC_SHADOW_FEE_BPS_PER_SIDE=9.0", text)
        self.assertIn("WorkingDirectory=/home/ubuntu/WStrade", text)
        self.assertIn("SMC_JOURNAL_EVENTS_PATH=", text)

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
        self.assertIn("def _oi_poll_interval", macro)
        self.assertIn("return max(5.0, min(15.0, requested))", macro)
        self.assertIn("oi_poll_interval_seconds", macro)
        self.assertIn("def request_oi_refresh", macro)
        self.assertIn("MIN_OI_POLL_SECONDS = 5.0", macro)
        self.assertIn("await asyncio.wait_for(", macro)

    def test_entry_runtime_requests_oi_only_on_urgent_transition(self):
        text = (ROOT / "mainnet_tier_s_shadow_launcher.py").read_text(encoding="utf-8")
        self.assertIn('request_oi_refresh(s, "ENTRY_CAUSAL_PHASE")', text)
        self.assertIn("previous_oi_interval > 5.0", text)

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
        self.assertIn("journal_stalled", text)
        self.assertIn("operational_blockers", text)

    def test_shadow_persistence_heartbeat_drives_journal_watchdog(self):
        path = ROOT / "loi_he_thong" / "persistence_heartbeat.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("journal_loop_heartbeat_mono", text)
        self.assertIn("journal_last_persist_mono", text)
        self.assertIn("shadow_persistence_dirty", text)
        self.assertIn("runtime_module._write_bot_heartbeat", text)

    def test_legacy_watchdog_defers_to_shadow_readiness(self):
        path = ROOT / "3_thuc_thi" / "giam_sat_he_thong.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn("shadow_readiness_authoritative", text)
        self.assertIn("mainnet_shadow_health", text)

    def test_shadow_state_persists_ratchet_and_resets_short_term_pressure(self):
        path = ROOT / "loi_he_thong" / "shadow_runtime_state.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn('"best_r"', text)
        self.assertIn('"floor_r"', text)
        self.assertIn('"whale_seen"', text)
        self.assertIn('"fee_r"', text)
        self.assertIn('"whale_exhaustion_since"', text)
        self.assertIn("SHADOW_RUNTIME_STATE_V2", text)
        self.assertIn("V1_ALIASES", text)
        self.assertIn("os.replace(tmp, path)", text)

    def test_futures_flow_ring_is_time_bounded_and_saturation_fail_closed(self):
        path = ROOT / "loi_he_thong" / "futures_flow_hardening.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("RETENTION_MS = 20_000.0", text)
        self.assertIn("HARD_MAX = 12_000", text)
        self.assertIn("futures_flow_ring_saturated", text)
        self.assertIn("exchange_time_ms", text)
        self.assertIn('@aggTrade', text)
        self.assertIn('async def liquidations', text)
        self.assertIn('@forceOrder', text)
        self.assertIn('mod.hung_force_order_futures = liquidations', text)
        self.assertIn('subscribe to aggTrade only (never forceOrder here)', text)
        self.assertNotIn('WhaleIntent', text)
        self.assertIn("state.system_ready = False", text)

    def test_hardened_wrapper_compiles_and_has_one_runtime_contract(self):
        path = ROOT / "mainnet_tier_s_shadow_risk_launcher.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn("shadow_runtime_health_runtime", text)
        self.assertIn("shadow_runtime_state_runtime", text)
        self.assertIn("BIAS_INVALID_OR_EXPIRED", text)
        self.assertIn("base._bias_loop = _bias_loop", text)
        self.assertIn("async def _account_init()", text)
        self.assertIn("await _orig_account_init()", text)
        self.assertIn(
            "base.FEE_BPS_PER_SIDE = "
            "base.verified_cost_model.fallback_fee_bps_per_side()",
            text,
        )
        self.assertIn("risk.FEE_BPS = base.FEE_BPS_PER_SIDE", text)
        self.assertNotIn("base.SHADOW_FEE_BPS_PER_SIDE", text)
        self.assertIn("futures_flow.install(base)", text)
        self.assertIn("FUTURES_FLOW_RING_SATURATED", text)
        self.assertNotIn("if pos is none", text)

    def test_integrity_scanner_uses_ascii_escape_byte_literals(self):
        path = ROOT / "ops" / "repo_integrity_check.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn(r'b"\xff\xfe"', text)
        self.assertIn(r'b"\xef\xbb\xbf"', text)
        self.assertNotIn("ÿ", text)
        self.assertNotIn("ï»¿", text)

    def test_qualified_transition_bypasses_decision_telemetry_debounce(self):
        path = ROOT / "mainnet_tier_s_shadow_launcher.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        self.assertIn('opportunity.get("qualification_transition")', text)
        self.assertIn('"qualified_now": bool(opportunity.get("qualified_now"))', text)


if __name__ == "__main__":
    unittest.main()
