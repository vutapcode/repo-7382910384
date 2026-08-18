"""Thin production wrapper: install lean app.main before risk/hardening wrap it."""
import mainnet_tier_s_shadow_launcher as shadow
from loi_he_thong import tier_s_runtime_prune as prune
from loi_he_thong import shadow_calibration_hook
from loi_he_thong import entry_regime_threshold_hook

# Install early so risk/hardening wrappers capture the lean orchestrator.
prune.install_app(shadow.app)

import mainnet_tier_s_shadow_hardened_launcher as hardened

# Learn only from completed shadow outcomes; never changes signal authority.
shadow_calibration_hook.install(hardened)
# Adapt Entry Council's existing price threshold in-place; all causal quorum/veto logic stays intact.
entry_regime_threshold_hook.install(hardened.runtime.base.entry_council)

if __name__ == "__main__":
    hardened.runtime.base.main()
