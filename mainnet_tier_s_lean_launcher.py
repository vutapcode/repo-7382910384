"""Canonical production entrypoint for the Tier-S causal strategy.

Trace all active authority from this file. Modules that merely exist elsewhere
in the repository are not active. See `STRATEGY_AUTHORITY.md`.
"""
import mainnet_tier_s_shadow_launcher as shadow
from loi_he_thong import tier_s_runtime_prune as prune
from loi_he_thong import shadow_calibration_hook_v2
from loi_he_thong import regime_oi_freshness_hook
from loi_he_thong import flow_weighting_hook
from loi_he_thong import regime_snapshot_hook
from loi_he_thong import flow_alignment_hook
from loi_he_thong import shadow_entry_metadata_persistence_hook
from loi_he_thong import bias_oi_freshness_hook
from loi_he_thong import disk_pressure_gate
from loi_he_thong import durable_shadow_journal
from loi_he_thong import ws_idle_recovery_hook
from loi_he_thong import shadow_dynamic_sizing_hook
from loi_he_thong import risk_ratchet_price_quality_hook
from loi_he_thong import risk_fee_alignment_hook
from loi_he_thong import entry_causal_hardening_hook

# Recover public feed half-open stalls before the lean task plan starts.
ws_idle_recovery_hook.install(shadow.app)

# Install early so risk/hardening wrappers capture the lean orchestrator.
prune.install_app(shadow.app)

import mainnet_tier_s_shadow_hardened_launcher as hardened

# Make critical ENTRY/EXIT journal transitions durable before live decisions begin.
durable_shadow_journal.install(shadow)
shadow_dynamic_sizing_hook.install(shadow)
risk_fee_alignment_hook.install(hardened.runtime.risk)
risk_ratchet_price_quality_hook.install(hardened.runtime.risk)

# Keep Bias OI freshness aligned with the 1s collector; stale OI abstains instead of voting.
bias_oi_freshness_hook.install(shadow.bias_council)
# Refuse only new shadow entries when journal storage is under pressure. Existing positions
# remain owned by Guardian/Risk, so disk protection never suppresses an exit.
disk_pressure_gate.install(hardened.runtime)

# Use two-tier arrival alignment, then weight persistence by normalized venue volume.
flow_alignment_hook.install(hardened.runtime.edge.regime_engine.flow_lead_engine)
flow_weighting_hook.install(hardened.runtime.edge.regime_engine.flow_lead_engine)
# Neutralize stale OI before any regime-dependent threshold/edge adaptation.
regime_oi_freshness_hook.install(hardened.runtime.edge.regime_engine)
# Reuse one short-lived regime snapshot across threshold and edge for the same decision.
regime_snapshot_hook.install(hardened.runtime.edge.regime_engine)
# Ignition Core owns causal independence and phase classification directly.
# Empirical calibration is consumed by residual Edge as a live-only gate; it
# never modifies the structural decision or authorizes a hard veto.
# Persist shadow entry metadata alongside active position state.
shadow_entry_metadata_persistence_hook.install(hardened.runtime.runtime_state)
# Learn only from completed outcomes; proof/proposer/execution cohorts must not
# subsidize one another.
shadow_calibration_hook_v2.install(hardened)
# Install last: validate the final active execution wrappers and causal state immediately before submit.
entry_causal_hardening_hook.install(shadow, hardened)

if __name__ == "__main__":
    hardened.runtime.base.main()
