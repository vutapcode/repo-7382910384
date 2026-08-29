import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StrategyAuthorityContractTests(unittest.TestCase):
    def text(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_canonical_launcher_is_documented_and_compiles(self):
        source = self.text("mainnet_tier_s_lean_launcher.py")
        ast.parse(source)
        self.assertIn("Canonical production entrypoint", source)
        for active in (
            "shadow_calibration_hook_v2",
        ):
            self.assertIn(active, source)
        shadow = self.text("mainnet_tier_s_shadow_launcher.py")
        self.assertIn('"ignition_core.py"', shadow)
        self.assertNotIn('"entry_council_shadow.py"', shadow)
        for retired_hook in (
            "entry_s2_snapshot_quorum_hook",
            "entry_exchange_independence_hook",
            "entry_regime_threshold_hook",
            "entry_edge_calibration_hook",
        ):
            self.assertNotIn(retired_hook, source)

    def test_retired_whale_and_depth_cannot_enter_active_chain(self):
        active = "\n".join(self.text(path) for path in (
            "mainnet_tier_s_lean_launcher.py",
            "mainnet_tier_s_shadow_launcher.py",
            "mainnet_tier_s_shadow_risk_launcher.py",
            "loi_he_thong/tier_s_runtime_prune.py",
            "loi_he_thong/tier_s_bootstrap_modules.py",
            "loi_he_thong/entry_futures_flow_scan_hook.py",
        ))
        self.assertNotIn("whale_intent.py", active)
        self.assertNotIn("WhaleIntentEngine", active)
        self.assertNotIn('"CATCH"', active)
        self.assertNotIn("tai_whale_depth", active)
        self.assertNotIn("whale_intent_snapshot", active)

    def test_non_authority_files_are_explicitly_marked(self):
        self.assertIn(
            "RETIRED EXPERIMENT",
            self.text("2_suy_luan_mapping/whale_intent.py")[:500],
        )
        self.assertIn(
            "NON-AUTHORITY",
            self.text("1_tai_du_lieu/tai_whale_depth/tai_whale_depth.py")[:500],
        )
        self.assertIn(
            "NON-AUTHORITY",
            self.text("recorder/replay.py")[:500],
        )

    def test_retired_replay_cannot_satisfy_promotion_gate(self):
        promotion = self.text("loi_he_thong/auto_promotion.py")
        retired = self.text("ops/wstrade_replay_validation.py")
        self.assertIn(
            'replay.get("strategy_authority") == "IGNITION_CORE_V1"',
            promotion,
        )
        self.assertIn("RETIRED_WHALE_EXPERIMENT_NON_AUTHORITY", retired)

    def test_active_journal_records_decision_inputs_outputs_and_misses(self):
        active = self.text("mainnet_tier_s_shadow_launcher.py")
        for marker in (
            "TIER_S_DECISION_RECORD_V3", '"cycle_id"',
            '"s1_price_quorum"', '"s2_executed_flow_quorum"',
            '"exchange_independence"', '"miss_taxonomy"',
            '"counterfactual"', "TIER_S_SHADOW_EXECUTION_V1",
            '"POSITION_STATE"',
            '"RISK_DAILY_LOCK"',
            '"ignition_proof_type"', '"residual_edge_proxy_bps"',
        ):
            self.assertIn(marker, active)
        self.assertIn('_append_event("ENTRY_SKIPPED"', active)
        self.assertIn('"FLOW_NONCONVERSION_COMPOSITE_VETO"', active)
        edge = self.text("loi_he_thong/entry_edge_tier.py")
        self.assertIn("not v5_replay_approved", edge)
        self.assertIn(
            'hard_vetoes.append("FLOW_PRICE_NONCONVERSION_VETO")', edge
        )
        self.assertIn("if would_enter and bool(basis.get", active)

    def test_risk_cannot_reintroduce_retired_whale_exit_authority(self):
        risk = self.text("loi_he_thong/shadow_risk_guard.py")
        self.assertNotIn("_whale_exhausted", risk)
        self.assertNotIn("WHALE_EXHAUSTION", risk)
        self.assertIn("must not infer Whale Intent", risk)

    def test_go_carries_one_proof_into_shared_shadow_live_revalidation(self):
        ignition = self.text("loi_he_thong/ignition_core.py")
        reservation = self.text("loi_he_thong/canonical_opportunity.py")
        execution = self.text("loi_he_thong/execution_causal_revalidation.py")
        shadow = self.text("mainnet_tier_s_shadow_launcher.py")
        for marker in (
            '"authority_basis"', '"authority_dependencies"',
            '"authority_proof_hash"',
        ):
            self.assertIn(marker, ignition)
            self.assertIn(marker, reservation)
        self.assertIn("_authority_contract", execution)
        self.assertIn("TRANSITION_CONFIRMED", execution)
        self.assertIn("SHARED_SHADOW_LIVE_CONTRACT", shadow)
        self.assertNotIn(
            'result["execution_policy"] = (', shadow,
        )

    def test_old_causal_hardening_hook_is_explicitly_non_authority(self):
        source = self.text("loi_he_thong/entry_causal_hardening_hook.py")
        self.assertIn("RETIRED / NON_AUTHORITY", source[:500])

    def test_inactive_entry_hooks_are_retired_and_not_installed(self):
        canonical = self.text("mainnet_tier_s_lean_launcher.py")
        for relative in (
            "loi_he_thong/entry_causal_hardening_hook.py",
            "loi_he_thong/entry_exchange_independence_hook.py",
            "loi_he_thong/entry_regime_threshold_hook.py",
        ):
            source = self.text(relative)
            self.assertIn("RETIRED_NON_AUTHORITY", source[:500])
            self.assertIn("RETIRED_NON_AUTHORITY = True", source[:500])
            self.assertNotIn(Path(relative).stem, canonical)

    def test_persistent_lane_documentation_matches_shadow_bootstrap_behavior(self):
        source = self.text("STRATEGY_AUTHORITY.md")
        self.assertIn("shadow/demo bootstrap", source)
        self.assertIn("shadow_bootstrap_authority=true", source)
        self.assertIn("live_authority=false", source)
        self.assertIn("CausalWaveSnapshot", source)
        self.assertNotIn(
            "persistent-metaorder lane is recorder telemetry only", source
        )
        ignition = self.text("loi_he_thong/ignition_core.py")
        self.assertIn('"shadow_bootstrap_authority": True', ignition)
        self.assertIn('"live_authority": False', ignition)
        self.assertIn(
            "PERSISTENT_METAORDER_LIVE_AUTHORITY_DISABLED", ignition
        )


if __name__ == "__main__":
    unittest.main()
