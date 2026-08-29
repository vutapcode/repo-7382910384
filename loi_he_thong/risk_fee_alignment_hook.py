"""Align shadow Risk fee accounting with the canonical shadow fee model."""
from __future__ import annotations

from loi_he_thong import verified_cost_model

VERSION = "RISK_FEE_ALIGNMENT_V2"

def _fee_bps_per_side():
    return verified_cost_model.fallback_fee_bps_per_side()


def _refresh_fee_r(position, fee_bps):
    try:
        entry = float(position.entry_price)
        r = float(position.r)
    except (AttributeError, TypeError, ValueError):
        return
    if entry <= 0.0 or r <= 0.0:
        return
    fallback = 2.0 * fee_bps
    total_cost_bps = verified_cost_model.position_total_cost_bps(
        position, fallback_bps=fallback
    )
    position.fee_r = (entry * total_cost_bps / 10000.0) / r


def install(risk):
    if getattr(risk, "_fee_alignment_installed", False):
        return VERSION

    fee_bps = _fee_bps_per_side()
    original_assess = risk.assess
    original_arm = risk.arm

    def arm(position, entry):
        out = original_arm(position, entry)
        _refresh_fee_r(position, fee_bps)
        return out

    def assess(position, px, guardian=None, market_state=None, now=None):
        _refresh_fee_r(position, fee_bps)
        return original_assess(
            position,
            px,
            guardian=guardian,
            market_state=market_state,
            now=now,
        )

    risk.FEE_BPS = fee_bps
    risk.arm = arm
    risk.assess = assess
    risk._fee_alignment_bps_per_side = fee_bps
    risk._fee_alignment_installed = True
    return VERSION
