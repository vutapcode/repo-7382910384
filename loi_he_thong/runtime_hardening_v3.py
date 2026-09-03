"""Operational hardening for the canonical Mainnet Tier-S shadow launcher.

Bias strategy authority deliberately does not live here.  This module may harden
runtime freshness, cost and Guardian transport, but it must not replace any
Bias question owner at install time.
"""
import time


VERSION = "TIER_S_RUNTIME_PATCH_V4_SINGLE_BIAS_OWNER"
GUARDIAN_FLOW_SEC = 3.0
SPOT_CLOCK_PROBE_SEC = 190.0


def _imb(buy, sell):
    buy, sell = float(buy or 0.0), float(sell or 0.0)
    total = buy + sell
    return ((buy - sell) / total if total > 0.0 else 0.0), total


def _spot_flow(state, now):
    """Clock-safety probe only. It has no Bias strategy authority."""
    buy = sell = 0.0
    oldest = newest = 0.0
    cutoff = float(now) - SPOT_CLOCK_PROBE_SEC
    for row in list(getattr(state, "flow_1s_buffer", ()) or ()):
        try:
            ts = float(row.get("ts", 0.0) or 0.0)
            if cutoff <= ts <= float(now):
                buy += float(row.get("buy", 0.0) or 0.0)
                sell += float(row.get("sell", 0.0) or 0.0)
                oldest = ts if oldest <= 0.0 else min(oldest, ts)
                newest = max(newest, ts)
        except (AttributeError, TypeError, ValueError):
            continue
    if newest <= 0.0 or not (0.0 <= float(now) - newest <= 5.0):
        return 0.0, 0.0, 0.0
    imbalance, volume = _imb(buy, sell)
    return imbalance, volume, max(0.0, newest - oldest)


def _fut_flow(state, now):
    buy = sell = 0.0
    newest = 0.0
    cutoff_ms = (float(now) - GUARDIAN_FLOW_SEC) * 1000.0
    rows = getattr(state, "danh_sach_khop_lenh_futures", ()) or ()
    for row in reversed(rows):
        try:
            ts = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts < cutoff_ms:
                break
            newest = max(newest, ts / 1000.0)
            qty = float(row.get("khoi_luong", 0.0) or 0.0)
            if bool(row.get("ban_chu_dong", False)):
                sell += qty
            else:
                buy += qty
        except (AttributeError, TypeError, ValueError):
            continue
    if newest <= 0.0 or not (0.0 <= float(now) - newest <= 5.0):
        return 0.0, 0.0
    return _imb(buy, sell)


def _install_bias(base):
    """Compatibility hook that proves install-time Bias mutation is disabled."""
    bias = base.bias_council
    before = getattr(bias, "s3", None)
    try:
        base.app.state.bias_runtime_override = "DISABLED_CANONICAL_BIAS_OWNER"
    except AttributeError:
        pass
    after = getattr(bias, "s3", None)
    if before is not after:
        raise RuntimeError("BIAS_OWNER_MUTATED_BY_RUNTIME_HARDENING")
    return "CANONICAL_BIAS_OWNER_UNCHANGED"


def _install_guardian(base):
    guard = base.guardian_s

    def fut_flow(state, now):
        return _fut_flow(state, now)

    guard._fut_flow = fut_flow

    old_last = guard._last_fut

    def last_fut(state, now):
        px = old_last(state, now)
        if px <= 0.0:
            return 0.0
        rows = getattr(state, "danh_sach_khop_lenh_futures", ()) or ()
        try:
            ts = float(rows[-1].get("thoi_gian_ms", 0.0) or 0.0) / 1000.0
        except (IndexError, AttributeError, TypeError, ValueError):
            return 0.0
        return px if 0.0 <= float(now) - ts <= 5.0 else 0.0

    guard._last_fut = last_fut


def install(wrapper):
    base = wrapper.base
    profile = base.verified_cost_model.shadow_commission_profile(base.app.state)
    minimum_fee = float(profile["taker_fee_bps"]) if profile else 9.0
    fee = max(
        minimum_fee,
        float(getattr(base, "FEE_BPS_PER_SIDE", 0.0) or 0.0),
    )
    if profile:
        fee = float(profile["taker_fee_bps"])
    base.FEE_BPS_PER_SIDE = fee
    base.SHADOW_FEE_BPS_PER_SIDE = fee
    wrapper.risk.FEE_BPS = fee

    def spot_fresh(now):
        ts = float(getattr(base.app.state, "thoi_gian_tick_cuoi", 0.0) or 0.0)
        return ts > 0.0 and 0.0 <= float(now) - ts <= 3.0

    base._spot_fresh = spot_fresh

    _install_bias(base)
    _install_guardian(base)

    old_health = wrapper.health.health

    def health_with_saturation(base_obj, state, now=None):
        out = old_health(base_obj, state, now)
        if bool(getattr(state, "futures_flow_ring_saturated", False)):
            out = dict(out)
            out["entry_ready"] = False
            out["full_tier_s_ready"] = False
            out["futures_flow"] = False
            out["futures_flow_ring_saturated"] = True
            state.mainnet_shadow_ready = False
            state.system_ready = False
            state.last_readiness_reason = "SHADOW_FEED_DEGRADED:futures_flow_ring_saturated"
            state.mainnet_shadow_health = out
        return out

    wrapper.health.health = health_with_saturation
    base.app.state.tier_s_runtime_patch_version = VERSION
    return VERSION
