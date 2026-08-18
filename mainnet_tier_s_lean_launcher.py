"""Thin production wrapper: install lean app.main before risk/hardening wrap it."""
import mainnet_tier_s_shadow_launcher as shadow
from loi_he_thong import tier_s_runtime_prune as prune
from loi_he_thong import shadow_calibration_hook_v2
from loi_he_thong import entry_regime_threshold_hook
from loi_he_thong import entry_edge_calibration_hook

# Install early so risk/hardening wrappers capture the lean orchestrator.
prune.install_app(shadow.app)

import mainnet_tier_s_shadow_hardened_launcher as hardened

# Threshold adaptation is bounded and never overrides causal quorum/veto logic.
entry_regime_threshold_hook.install(hardened.runtime.base.entry_council)
# Bucketed empirical calibration only adjusts expectancy after the original causal edge report exists.
entry_edge_calibration_hook.install(hardened.runtime.edge)
# Learn only from completed shadow outcomes, using side/mode/regime/edge-class buckets.
shadow_calibration_hook_v2.install(hardened)

if __name__ == "__main__":
    hardened.runtime.base.main()
