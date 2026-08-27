"""Canonical metadata for the active Tier-S Ignition strategy.

This module contains identity and invariants only. Trading thresholds stay in
their owning strategy modules so metadata cannot silently override live logic.
"""

PROFILE_VERSION = "IGNITION_CORE_ENTRY_ECONOMICS_V3"

_PROFILE = {
    "name": "IGNITION_CORE_V1",
    "version": PROFILE_VERSION,
    "mode": "MAINNET_SHADOW",
    "market": "BTCUSDT",
    "canonical_entrypoint": "mainnet_tier_s_lean_launcher.py",
    "architecture": (
        "BIAS_COUNCIL",
        "IGNITION_PREDICT_PROBE_PROVE",
        "ENTRY_ECONOMICS_V3",
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
        "FUTURES_NEVER_SELF_OPENS",
        "CASH_PRICE_AND_EXECUTED_FLOW_AUTHORITY",
        "FAIL_NEUTRAL_ON_STALE_OPTIONAL_EVIDENCE",
        "FROZEN_COST_COUNTED_ONCE",
        "EMPIRICAL_GUARDIAN_NET_NOT_MFE_ALPHA",
        "EDGE_LATE_IS_NOT_A_HARD_TIME_STOP",
        "BOUNDED_ADAPTATION_ONLY",
        "NO_ORDERBOOK_RESILIENCY_AUTHORITY",
        "NO_LEGACY_SMC_AUTHORITY",
        "NO_WHALE_INTENT_AUTHORITY",
        "RECORDER_REPLAY_NOT_TRADING_AUTHORITY",
        "HEURISTIC_PRIOR_IS_NOT_EMPIRICAL_ALPHA",
        "NO_DCA_NO_PARTIAL_CLOSE",
    ),
}


def current_profile():
    """Return a detached snapshot suitable for heartbeat/runtime metadata."""
    profile = dict(_PROFILE)
    profile["architecture"] = list(_PROFILE["architecture"])
    profile["evidence"] = list(_PROFILE["evidence"])
    profile["invariants"] = list(_PROFILE["invariants"])
    return profile
