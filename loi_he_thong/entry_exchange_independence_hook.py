"""Reduce correlated Binance double-counting without hard-requiring Coinbase availability."""
from __future__ import annotations

VERSION = "ENTRY_EXCHANGE_INDEPENDENCE_V2_CONFIDENCE_AWARE"

_BINANCE_PAIR = {"spot", "futures"}
_SOFT_PRICE_FRACTION = 0.25
_SOFT_PRICE_FLOOR_BPS = 0.10
_SOFT_FLOW_IMBALANCE = 0.04
_COINBASE_MAX_AGE_SEC = 5.0
_STALE_EXTERNAL_CONFIDENCE_FACTOR = 0.85
_STALE_EXTERNAL_CONFIDENCE_CAP = 0.68


def _soft_external_ok(result):
    votes = result.get("s_votes") or {}
    s1 = (votes.get("S1_cross_venue_price_acceptance") or {}).get("metrics") or {}
    s2 = (votes.get("S2_multi_venue_executed_flow") or {}).get("metrics") or {}
    p_support = set(s1.get("supporters") or ())
    f_support = set(s2.get("supporters") or ())
    if p_support != _BINANCE_PAIR or f_support != _BINANCE_PAIR:
        return True, {"applies": False}

    now = float(result.get("ts", 0.0) or 0.0)
    freshness = result.get("freshness") or {}
    cb_ts = float(freshness.get("coinbase", 0.0) or 0.0)
    cb_fresh = cb_ts > 0.0 and now >= cb_ts and now - cb_ts <= _COINBASE_MAX_AGE_SEC
    if not cb_fresh:
        return True, {
            "applies": True,
            "coinbase_fresh": False,
            "availability_neutral": True,
            "confidence_degraded": True,
        }

    threshold = float(result.get("price_threshold_bps", 0.0) or 0.0)
    cb_move = float((s1.get("moves") or {}).get("coinbase", 0.0) or 0.0)
    cb_flow = float((((s2.get("venues") or {}).get("coinbase") or {}).get("signed_imbalance", 0.0)) or 0.0)
    soft_price_threshold = max(_SOFT_PRICE_FLOOR_BPS, threshold * _SOFT_PRICE_FRACTION)
    price_ok = cb_move >= soft_price_threshold
    flow_ok = cb_flow >= _SOFT_FLOW_IMBALANCE
    return price_ok or flow_ok, {
        "applies": True,
        "coinbase_fresh": True,
        "coinbase_price_bps": round(cb_move, 4),
        "coinbase_flow_imbalance": round(cb_flow, 4),
        "soft_price_threshold_bps": round(soft_price_threshold, 4),
        "soft_flow_threshold": _SOFT_FLOW_IMBALANCE,
        "price_ok": price_ok,
        "flow_ok": flow_ok,
    }


def install(entry_council):
    if getattr(entry_council, "_exchange_independence_installed", False):
        return VERSION

    original = entry_council.evaluate

    def evaluate(state, now=None, side=None):
        result = original(state, now=now, side=side)
        if not isinstance(result, dict):
            return result
        if result.get("decision") != "GO" or result.get("entry_mode") != "NORMAL":
            return result

        allowed, meta = _soft_external_ok(result)
        out = dict(result)
        out["exchange_independence"] = meta
        if allowed:
            if meta.get("confidence_degraded"):
                original_conf = float(out.get("confidence", 0.0) or 0.0)
                out["confidence"] = min(
                    original_conf * _STALE_EXTERNAL_CONFIDENCE_FACTOR,
                    _STALE_EXTERNAL_CONFIDENCE_CAP,
                )
            return out

        out["decision"] = "WAIT"
        out["entry_mode"] = "NONE"
        out["phase"] = "PRESSURE_BUILDING"
        out["reason"] = "WAIT_EXTERNAL_CORROBORATION"
        out["confidence"] = min(float(out.get("confidence", 0.0) or 0.0), 0.49)
        return out

    entry_council.evaluate = evaluate
    entry_council._exchange_independence_installed = True
    return VERSION
