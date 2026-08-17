"""Operational hardening for the Mainnet Tier-S shadow launcher."""
import time

VERSION = "TIER_S_RUNTIME_PATCH_V1"
FLOW_SEC = 3.0
MIN_IMB = 0.05


def _imb(buy, sell):
    buy, sell = float(buy or 0.0), float(sell or 0.0)
    total = buy + sell
    return ((buy - sell) / total if total > 0 else 0.0), total)


def _material_floor(state):
    return max(0.02, min(0.10, 0.02 * float(getattr(state, "vol_pct90", 0.0) or 0.0)))


def _spot_flow(state, now):
    buy = sell = 0.0
    cutoff = float(now) - FLOW_SEC
    for row in list(getattr(state, "flow_1s_buffer", ()) or ()):
        try:
            if float(row.get("ts", 0.0) or 0.0) >= cutoff:
                buy += float(row.get("buy", 0.0) or 0.0)
                sell += float(row.get("sell", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
    return _imb(buy, sell)


def _fut_flow(state, now):
    buy = sell = 0.0
    newest = 0.0
    cutoff_ms = (float(now) - FLOW_SEC) * 1000.0
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


def _coinbase_flow(state, now):
    ts = float(getattr(state, "coinbase_flow_3s_ts", 0.0) or 0.0)
    vol = float(getattr(state, "coinbase_volume_3s", 0.0) or 0.0)
    cvd = float(getattr(state, "coinbase_cvd_3s", 0.0) or 0.0)
    if ts <= 0.0 or not (0.0 <= float(now) - ts <= 5.0) or vol <= 0.0:
        return 0.0, 0.0
    return cvd / vol, vol


def _install_bias(base):
    bias = base.bias_council

    def s3(state, now):
        floor = _material_floor(state)
        rows = []
        for name, (imbalance, volume) in {
            "spot": _spot_flow(state, now),
            "futures": _fut_flow(state, now),
            "coinbase": _coinbase_flow(state, now),
        }.items():
            if volume >= floor and abs(imbalance) >= MIN_IAB:
                rows.append((name, "LONG" if imbalance > 0 else "SHORT",
                             min(1.0, abs(imbalance) / 0.35), volume, imbalance))
        longs = [r for r in rows if r[1] == "LONG"]
        shorts = [r for r in rows if r[1] == "SHORT"]
        metrics = [{"venue": r[0], "side": r[1], "strength": round(r[2], 6),
                    "volume_btc": round(r[3], 6), "imbalance": round(r[4], 6)}
                   for r in rows]
        if longs and shorts:
            return bias.vote(reason="MULTI_VENUE_FLOW_CONFLICT",
                             venues=metrics, material_floor_btc=round(floor, 6))
        agreed = longs if len(longs) >= 2 else shorts if len(shorts) >= 2 else []
        if not agreed:
            return bias.vote(reason="INSUFFICIENT_MATERIAL_FLOW_CONSENSUS",
                             venues=metrics, material_floor_btc=round(floor, 6))
        strength = sum(r[2] for r in agreed) / len(agreed)
        return bias.vote(agreed[0][1],
                         0.52 + 0.12 * max(0, len(agreed) - 2) + 0.30 * strength,
                         "MULTI_VENUE_MATERIAL_FLOW",
                         venues=metrics, strength=round(strength, 6),
                         material_floor_btc=round(floor, 6), horizon_sec=FLOW_SEC)

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
    fee = max(9.0, float(getattr(base, "FEE_BPS_PER_SIDE", 0.0) or 0.0))
    base.FEE_BPS_PER_SIDE = fee
    base.SHADOW_FEE_BPS_PER_SIDE = fee
    wrapper.risk.FEE_BPS = fee

    def spot_fresh(now):
        ts = float(getattr(base.app.state, "thoi_gian_tick_cuoi", 0.0) or 0.0)
        return ts > 0.0 and 0.0 <= float(now) - ts <= 3.0
    base._spot_fresh = spot_fresh

    _install_bias(base)
    _install_guardian(base)
    base.app.state.tier_s_runtime_patch_version = VERSION
    return VERSION
