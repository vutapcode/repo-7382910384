"""Canonical metadata for the active Tier-S Ignition strategy.

This module contains identity and invariants only. Trading thresholds stay in
their owning strategy modules so metadata cannot silently override live logic.
"""
from importlib import import_module

PROFILE_VERSION = "TIER_S_STRATEGY_PROFILE_V17_RUNTIME_BOUND"

_PROFILE = {
    "name": "IGNITION_CORE_V1",
    "version": PROFILE_VERSION,
    "mode": "MAINNET_SHADOW",
    "market": "BTCUSDT",
    "canonical_entrypoint": "mainnet_tier_s_lean_launcher.py",
    "architecture": (
        "MARKET_THESIS_V3_AUTHORITY_SEPARATED",
        "FOUR_AUTHORITY_CONTRACTS_V1",
        "ENTRY_THESIS_HANDOFF_V1",
        "GUARDIAN_SHARED_THESIS_SHADOW_V1",
        "GUARDIAN",
        "SHADOW_RISK",
    ),
    "evidence": (
        "BINANCE_SPOT",
        "BINANCE_FUTURES",
        "COINBASE_SPOT",
        "EXECUTED_FLOW",
        "OPEN_INTEREST",
    ),
    "invariants": (
        "CAUSAL_ONLY",
        "FROZEN_PRE_IMPULSE_BIAS",
        "BIAS_DIRECTION_ROOTS_ARE_INDEPENDENT_CASH_ONLY",
        "BIAS_LIVE_DIRECTION_COMES_FROM_ROLLING_CAUSAL_CASH_PERSISTENCE",
        "BIAS_HISTORICAL_LENSES_HAVE_ZERO_LIVE_DIRECTION_AUTHORITY",
        "BIAS_REQUIRES_EXECUTED_FLOW_TO_CONVERT_INTO_DUAL_CASH_PRICE",
        "BIAS_EMERGING_MICRO_WAVE_HAS_NO_ENTRY_HANDOFF_AUTHORITY",
        "BIAS_ACQUISITION_HANDOFF_IS_SEALED_PROVENANCE_NOT_ENTRY_AUTHORITY",
        "OVERLAPPING_CASH_ACQUISITION_EVIDENCE_REUSES_ONE_CAUSAL_WAVE",
        "ACQUISITION_ENTRY_REQUIRES_SEALED_DUAL_CASH_AND_CURRENT_CASH_CONVERSION",
        "ACQUISITION_HANDOFF_LIVE_AUTHORITY_DISABLED",
        "BIAS_EXHAUSTION_RELEASES_STALE_DIRECTION",
        "BIAS_CONTROL_TRANSFER_IS_EVIDENCE_DRIVEN_NOT_TIMER_DRIVEN",
        "TRANSITION_ONSET_IS_NOT_CONTROL_OWNERSHIP",
        "FUTURES_NEVER_SELF_OPENS",
        "FUTURES_AND_OI_ARE_CONTEXT_ONLY_FOR_BIAS_DIRECTION",
        "ONE_CAUSAL_ROOT_COUNTS_ONCE",
        "CASH_PRICE_AND_EXECUTED_FLOW_AUTHORITY",
        "STATIC_L2_AND_CANCELS_HAVE_ZERO_DIRECTION_AUTHORITY",
        "FAIL_NEUTRAL_ON_STALE_OPTIONAL_EVIDENCE",
        "FROZEN_COST_COUNTED_ONCE",
        "EMPIRICAL_GUARDIAN_NET_NOT_MFE_ALPHA",
        "EDGE_LATE_IS_NOT_A_HARD_TIME_STOP",
        "THESIS_STATUS_IS_PNL_INDEPENDENT",
        "CAPITAL_POLICY_IS_SEPARATE_FROM_CAUSAL_THESIS",
        "MARKET_ACTION_EXECUTION_SAFETY_OWNERS_SEPARATE",
        "ACTION_APPROVED_TRUTH_IS_NOT_REBUILT_DOWNSTREAM",
        "SHARED_GUARDIAN_HAS_NO_AUTHORITY_BEFORE_CANONICAL_REPLAY",
        "BOUNDED_ADAPTATION_ONLY",
        "NO_ORDERBOOK_RESILIENCY_AUTHORITY",
        "NO_LEGACY_SMC_AUTHORITY",
        "NO_WHALE_INTENT_AUTHORITY",
        "RECORDER_REPLAY_NOT_TRADING_AUTHORITY",
        "HEURISTIC_PRIOR_IS_NOT_EMPIRICAL_ALPHA",
        "NO_DCA_NO_PARTIAL_CLOSE",
    ),
}

def _active_versions():
    bias = import_module("2_suy_luan_mapping.bias_council")
    ignition = import_module("loi_he_thong.ignition_core")
    economics = import_module("loi_he_thong.entry_economics_v2")
    return (
        str(bias.VERSION),
        str(ignition.INFERENCE_VERSION),
        str(economics.CONTRACT_VERSION),
    )


def current_profile():
    """Return a detached snapshot suitable for heartbeat/runtime metadata."""
    profile = dict(_PROFILE)
    active = _active_versions()
    profile["architecture"] = [*active, *_PROFILE["architecture"]]
    profile["evidence"] = list(_PROFILE["evidence"])
    profile["invariants"] = list(_PROFILE["invariants"])
    if any(version not in profile["architecture"] for version in active):
        raise RuntimeError("STRATEGY_PROFILE_ACTIVE_VERSION_MISMATCH")
    return profile
