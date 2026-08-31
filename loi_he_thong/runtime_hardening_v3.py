"""Operational hardening for the Mainnet Tier-S shadow launcher."""
import time

VERSION = "TIER_S_RUNTIME_PATCH_V3_SEPARATE_BIAS_ENTRY_HORIZONS"
BIAS_FLOW_SEC = 60.0
BIAS_MIN_COVERAGE_SEC = 45.0
BIAS_MIN_IMB = 0.08
BIAS_FLOOR_MULTIPLIER = 4.0
GUARDIAN_FLOW_SEC = 3.0


def _imb(buy, sell):
    buy, sell = float(buy or 0.0), float(sell or 0.0)
    total = buy + sell
    return ((buy - sell) / total if total > 0 else 0.0), total


def _material_floor(state):
    return max(0.02, min(0.10, 0.02 * float(getattr(state, "vol_pct90", 0.0) or 0.0)))


def _spot_flow(state, now):
    buy = sell = 0.0
    oldest = newest = 0.0
    cutoff = float(now) - BIAS_FLOW_SEC
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


def _bias_fut_flow(state, now):
    buy = sell = 0.0
    oldest = newest = 0.0
    cutoff = float(now) - BIAS_FLOW_SEC
    for row in list(getattr(state, "futures_flow_1s_buffer", ()) or ()):
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


def _bias_coinbase_flow(state, now):
    ts = float(getattr(state, "thoi_gian_coinbase_cuoi", 0.0) or 0.0)
    volume = float(getattr(state, "coinbase_volume_1m", 0.0) or 0.0)
    cvd = float(getattr(state, "coinbase_cvd_1m", 0.0) or 0.0)
    coverage = float(
        getattr(state, "coinbase_flow_1m_coverage_sec", 0.0) or 0.0
    )
    if ts <= 0.0 or not (0.0 <= float(now) - ts <= 5.0) or volume <= 0.0:
        return 0.0, 0.0, 0.0
    return cvd / volume, volume, coverage


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
    bias = base.bias_council

    def s3(state, now):
        fallback_floor = _material_floor(state)
        configured_floors = dict(
            getattr(base.entry_council, "MIN_VOL_BTC_BY_VENUE", {}) or {}
        )
        floors = {
            "spot": BIAS_FLOOR_MULTIPLIER * float(
                configured_floors.get("spot", fallback_floor)
            ),
            "futures": BIAS_FLOOR_MULTIPLIER * float(
                configured_floors.get("futures", fallback_floor)
            ),
            "coinbase": BIAS_FLOOR_MULTIPLIER * float(
                configured_floors.get("coinbase", fallback_floor)
            ),
        }
        rows = []
        warmup = {}
        for name, (imbalance, volume, coverage) in {
            "spot": _spot_flow(state, now),
            "futures": _bias_fut_flow(state, now),
            "coinbase": _bias_coinbase_flow(state, now),
        }.items():
            warmup[name] = round(coverage, 4)
            if (
                coverage >= BIAS_MIN_COVERAGE_SEC
                and volume >= floors[name]
                and abs(imbalance) >= BIAS_MIN_IMB
            ):
                rows.append((name, "LONG" if imbalance > 0 else "SHORT",
                             min(1.0, abs(imbalance) / 0.35), volume, imbalance))
        metrics = [{"venue": r[0], "side": r[1], "strength": round(r[2], 6),
                    "volume_btc": round(r[3], 6), "imbalance": round(r[4], 6)}
                   for r in rows]
        agreed, family = bias.flow_family_consensus(rows)
        if not agreed:
            reason = (
                "INSUFFICIENT_MATERIAL_FLOW_CONSENSUS"
                if family == "INSUFFICIENT_FLOW_CONSENSUS" else family
            )
            return bias.vote(reason=reason,
                             venues=metrics,
                             evidence_family=family,
                             material_floor_btc_by_venue=floors,
                             coverage_sec_by_venue=warmup,
                             horizon_sec=BIAS_FLOW_SEC)
        strength = sum(r[2] for r in agreed) / len(agreed)
        return bias.vote(agreed[0][1],
                        0.52 + 0.30 * strength,
                         "CAUSAL_FAMILY_MATERIAL_FLOW",
                        venues=metrics, strength=round(strength, 6),
                        evidence_family=family,
                        material_floor_btc_by_venue=floors,
                        coverage_sec_by_venue=warmup,
                        horizon_sec=BIAS_FLOW_SEC)

    bias.s3 = s3


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
    profile = base.verified_cost_model.shadow_commission_profile(
        base.app.state
    )
    minimum_fee = (
        float(profile["taker_fee_bps"]) if profile else 9.0
    )
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
