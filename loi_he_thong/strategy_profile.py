"""Canonical metadata for the active Tier-S causal strategy.

This module contains identity and invariants only. Trading thresholds stay in
their owning strategy modules so metadata cannot silently override live logic.
"""

PROFILE_VERSION = "TIER_S_CAUSAL_2026_V1"

_PROFILE = {
    "name": "TIER_S_CAUSAL",
    "version": PROFILE_VERSION,
    "mode": "MAINNET_SHADOW",
    "market": "BTCUSDT",
    "architecture": (
        "BIAS_COUNCIL",
        "ENTRY_COUNCIL",
        "ENTRY_EDGE",
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
        "CROSS_VENUE_CONFIRMATION",
        "FAIL_NEUTRAL_ON_STALE_OPTIONAL_EVIDENCE",
        "ORIGINAL_EDGE_VETO_IMMUTABLE",
        "BOUNDED_ADAPTATION_ONLY",
        "NO_ORDERBOOK_RESILIENCY_AUTHORITY",
        "NO_LEGACY_SMC_AUTHORITY",
    ),
}


def current_profile():
    """Return a detached snapshot suitable for heartbeat/runtime metadata."""
    profile = dict(_PROFILE)
    profile["architecture"] = list(_PROFILE["architecture"])
    profile["evidence"] = list(_PROFILE["evidence"])
    profile["invariants"] = list(_PROFILE["invariants"])
    return profile
