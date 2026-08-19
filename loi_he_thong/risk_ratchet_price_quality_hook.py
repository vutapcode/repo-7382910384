"""Protect profit ratchet from isolated Futures wicks without weakening hard-stop execution."""
from __future__ import annotations

VERSION = "RISK_RATCHET_PRICE_QUALITY_V1"

_SPOT_MAX_AGE_SEC = 3.0
_COINBASE_MAX_AGE_SEC = 5.0


def _mid(bid, ask):
    bid = float(bid or 0.0)
    ask = float(ask or 0.0)
    return (bid + ask) / 2.0 if bid > 0.0 and ask > bid else 0.0


def _fair_price(state, now):
    if state is None:
        return None, None
    spot_ts = float(getattr(state, "thoi_gian_tick_cuoi", 0.0) or 0.0)
    cb_ts = float(getattr(state, "thoi_gian_coinbase_ticker_cuoi", 0.0) or 0.0)
    spot = _mid(getattr(state, "best_bid", 0.0), getattr(state, "best_ask", 0.0))
    cb = float(getattr(state, "coinbase_price", 0.0) or 0.0)
    spot_fresh = spot > 0.0 and spot_ts > 0.0 and now >= spot_ts and now - spot_ts <= _SPOT_MAX_AGE_SEC
    cb_fresh = cb > 0.0 and cb_ts > 0.0 and now >= cb_ts and now - cb_ts <= _COINBASE_MAX_AGE_SEC
    if not (spot_fresh and cb_fresh):
        return None, None
    fair = (spot + cb) / 2.0
    atr = float(getattr(state, "atr_1m", 0.0) or 0.0)
    atr_bps = atr / fair * 10000.0 if atr > 0.0 and fair > 0.0 else 0.0
    tolerance_bps = max(4.0, min(15.0, atr_bps * 0.15 if atr_bps > 0.0 else 6.0))
    return fair, tolerance_bps


def _is_favorable_outlier(p, px, fair, tolerance_bps):
    side = str(getattr(p, "side", "") or "").upper()
    sign = 1.0 if side == "LONG" else -1.0
    if side not in ("LONG", "SHORT") or fair <= 0.0:
        return False
    favorable_gap_bps = sign * (float(px) - float(fair)) / float(fair) * 10000.0
    return favorable_gap_bps > float(tolerance_bps)


def _apply_quality_ratchet(risk, p, fair, guardian):
    mode, _, _, _ = risk.tier_mode(guardian)
    side = str(getattr(p, "side", "") or "").upper()
    sign = 1.0 if side == "LONG" else -1.0
    entry = float(p.entry_price)
    r = float(p.r)
    previous_best = float(p.best)
    quality_best = max(previous_best, fair) if sign > 0.0 else min(previous_best, fair)
    p.best = quality_best
    best_r = max(0.0, sign * (quality_best - entry) / r)
    p.best_r = best_r
    floor_r, stage = risk._candidate(best_r, p.fee_r, mode)
    if floor_r is not None:
        floor_r = max(p.floor_r, floor_r) if p.floor_r is not None else floor_r
        p.floor_r = floor_r
        p.floor = entry + sign * floor_r * r
        p.stage = stage


def install(risk):
    if getattr(risk, "_ratchet_price_quality_installed", False):
        return VERSION

    original = risk.assess

    def assess(p, px, guardian=None, market_state=None, now=None):
        previous = {
            "best": getattr(p, "best", None),
            "best_r": getattr(p, "best_r", None),
            "floor": getattr(p, "floor", None),
            "floor_r": getattr(p, "floor_r", None),
            "stage": getattr(p, "stage", None),
        }
        result = original(p, px, guardian=guardian, market_state=market_state, now=now)

        # Hard stop and any already-authorized exit remain untouched.
        if not isinstance(result, dict) or result.get("decision") != "HOLD"
            return result

        _, _, _, states = risk.tier_mode(guardian)
        if states and states[0] == "SUPPORTIVE":
            return result

        ts = float(result.get("ts", 0.0) or (now if now is not None else 0.0))
        fair, tolerance_bps = _fair_price(market_state, ts)
        if fair is None or not _is_favorable_outlier(p, px, fair, tolerance_bps):
            return result

        p.best = previous["best"]
        p.best_r = previous["best_r"]
        p.floor = previous["floor"]
        p.floor_r = previous["floor_r"]
        p.stage = previous["stage"]
        _apply_quality_ratchet(risk, p, fair, guardian)

        out = dict(result)
        out["best_r"] = getattr(p, "best_r", 0.0)
        out["profit_floor"] = getattr(p, "floor", None)
        out["floor_r"] = getattr(p, "floor_r", None)
        out["stage"] = getattr(p, "stage", None)
        out["ratchet_price_quality"] = {
            "futures_px": float(px),
            "cross_venue_fair": float(fair),
            "tolerance_bps": float(tolerance_bps),
            "mode": "CROSS_VENUE_CAPPED",
        }
        return out

    risk.assess = assess
    risk._ratchet_price_quality_installed = True
    return VERSION
