"""Compatibility stub for retired legacy mode selection.

Tier-S launchers provide mode/side directly. This module only keeps the old commander
importable for testnet transport compatibility.
"""
def xac_dinh_che_do(state):
    return {"modes": ["TIER-S"], "bias": getattr(state, "bias_state", "NONE")}
