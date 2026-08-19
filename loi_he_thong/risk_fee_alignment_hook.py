"""Align Risk fee accounting with the canonical shadow fee model."""
from __future__ import annotations

import os

VERSION = "RISK_FEE_ALIGNMENT_V1"


def _fee_bps(risk):
    raw = os.getenv("SMC_SHADOW_FEE_BPS_PER_SIDE")
    if raw is None:
        return max(0.0, float(getattr(risk, "FEE_BPS", 0.0) or 0.0))
    return max(0.0, float(raw))


def _refresh_position_fee_r(p, fee_bps):
    try:
        entry = float(p.entry_price)
        r = float(p.r)
    except (AttributeError, TypeError, ValueError):
        return
    if entry <= 0.0 or r <= 0.0:
        return
    p.fee_r = (entry * 2.0 * fee_bps / 10000.0) / r


def install(risk):
    if getattr(risk, "_fee_alignment_installed", False):
        return VERSION

    fee_bps = _fee_bps(risk)
    risk.FEE_BPS = fee_bps
    original = risk.assess

    def assess(p, px, guardian=None, market_state=None, now=None):
        _refresh_position_fee_r(p, fee_bps)
        return original(p, px, guardian=guardian, market_state=market_state, now=now)

    risk.assess = assess
    risk._fee_alignment_installed = True
    risk._fee_alignment_bps_per_side = fee_bps
    return VERSION
