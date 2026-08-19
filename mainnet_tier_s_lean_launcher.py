"""Thin production wrapper: install lean app.main before risk/hardening wrap it."""
import mainnet_tier_s_shadow_launcher as shadow
from loi_he_thong import tier_s_runtime_prune as prune
from loi_he_thong import shadow_calibration_hook_v2
from loi_he_thong import entry_regime_threshold_hook
from loi_he_thong import entry_edge_calibration_hook
from loi_he_thong import regime_oi_freshness_hook
from loi_he_thong import flow_weighting_hook
from loi_he_thong import regime_snapshot_hook
from loi_he_thong import flow_alignment_hook
from loi_he_thong import shadow_entry_metadata_persistence_hook
from loi_he_thong import bias_oi_freshness_hook
from loi_he_thong import entry_futures_flow_scan_hook
from loi_he_thong import entry_s2_snapshot_quorum_hook

# Install early so risk/hardening wrappers capture the lean orchestrator.
prune.install_app(shadow.app)

import mainnet_tier_s_shadow_hardened_launcher as hardened

# Keep Bias OI freshness aligned with the 1s collector; stale OI abstains instead of voting.
bias_oi_freshness_hook.install(shadow.bias_council)
# Keep Futures 3s flow scans bounded to the active window; signal semantics stay unchanged.
entry_futures_flow_scan_hook.install(hardened.runtime.base.entry_council)
entry_s2_snapshot_quorum_hook.install(hardened.runtime)

# Use two-tier arrival alignment, then weight persistence by normalized venue volume.
flow_alignment_hook.install(hardened.runtime.edge.regime_engine.flow_lead_engine)
flow_weighting_hook.install(hardened.runtime.edge.regime_engine.flow_lead_engine)
# Neutralize stale OI before any regime-dependent threshold/edge adaptation.
regime_oi_freshness_hook.install(hardened.runtime.edge.regime_engine)
# Reuse one short-lived regime snapshot across threshold and edge for the same decision.
regime_snapshot_hook.install(hardened.runtime.edge.regime_engine)
# Threshold adaptation is bounded and never overrides causal quorum/veto logic.
entry_regime_threshold_hook.install(hardened.runtime.base.entry_council)
# Bucketed empirical calibration only adjusts expectancy after the original causal edge report exists.
entry_edge_calibration_hook.install(hardened.runtime.edge)
# Persist shadow entry metadata alongside active position state.
shadow_entry_metadata_persistence_hook.install(hardened.runtime.runtime_state)
# Learn only from completed shadow outcomes, using side/mode/regime/edge-class buckets.
shadow_calibration_hook_v2.install(hardened)

if __name__ == "__main__":
    hardened.runtime.base.main()
