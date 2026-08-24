"""Reduce correlated Binance double-counting while preserving availability with evidence-strength fallbacks."""
from __future__ import annotations

VERSION = "ENTRY_EXCHANGE_INDEPENDENCE_V6_BALANCED_EXTERNAL_CORROBORATION"

_BINANCE_PAIR = {"spot", "futures"}
_SOFT_PRICE_FRACTION = 0.25
_SOFT_PRICE_FLOOR_BPS = 0.10
_SOFT_FLOW_IMBALANCE = 0.04
_STRONG_PRICE_FRACTION = 0.75
_STRONG_PRICE_FLOOR_BPS = 0.35
_STRONG_FLOW_IMBALANCE = 0.20
_COINBASE_STRICT_AGE_SEC = 2.50
_COINBASE_MAX_AGE_SEC = 5.00
_DEGRADED_CONFIDENCE_FACTOR = 0.85
_DEGRADED_CONFIDENCE_CAP = 0.68
_ABSORPTION_STORIES = {
    "SELL_FLOW_ABSORBED_BY_LONG_BUILD",
    "BUY_FLOW_ABSORBED_BY_SHORT_BUILD",
}


def _strong_native(s1, s2):
    price = set(s1.get("strong_supporters") or ()) & _BINANCE_PAIR
    flow = set(s2.get("strong_supporters") or ()) & _BINANCE_PAIR
    cash_anchor = "spot" in price or "spot" in flow
    return bool(price), bool(flow), cash_anchor, sorted(price), sorted(flow)


def _soft_external_ok(result):
    votes = result.get("s_votes") or {}
    s1 = (votes.get("S1_cross_venue_price_acceptance") or {}).get("metrics") or {}
    s2 = (votes.get("S2_multi_venue_executed_flow") or {}).get("metrics") or {}
    p_support = set(s1.get("supporters") or ())
    f_support = set(s2.get("supporters") or ())
    # Apply whenever correlated Binance Spot+Futures carry both axes. Coinbase
    # being present as an incidental/weak third supporter must not bypass the
    # corroboration test. Genuine mixed-venue quorums remain untouched.
    binance_carries_both = bool(
        _BINANCE_PAIR.issubset(p_support)
        and _BINANCE_PAIR.issubset(f_support)
    )
    if not binance_carries_both:
        return True, {"applies": False}

    now = float(result.get("ts", 0.0) or 0.0)
    freshness = result.get("freshness") or {}
    cb_ts = float(freshness.get("coinbase", 0.0) or 0.0)
    if cb_ts <= 0.0 or now < cb_ts:
        return False, {
            "applies": True,
            "mode": "EXTERNAL_TIMESTAMP_INVALID",
            "coinbase_fresh": False,
            "confidence_degraded": True,
        }

    age = now - cb_ts
    if age > _COINBASE_MAX_AGE_SEC:
        return False, {
            "applies": True,
            "mode": "EXTERNAL_UNAVAILABLE",
            "coinbase_fresh": False,
            "coinbase_age_s": round(age, 4),
            "confidence_degraded": True,
        }

    if age > _COINBASE_STRICT_AGE_SEC:
        price_strong, flow_strong, cash_anchor, price_names, flow_names = _strong_native(s1, s2)
        strength_ok = bool(price_strong and flow_strong and cash_anchor)
        return strength_ok, {
            "applies": True,
            "mode": "DEGRADED_EXTERNAL_STRONG_NATIVE",
            "coinbase_fresh": True,
            "coinbase_strict_fresh": False,
            "coinbase_age_s": round(age, 4),
            "confidence_degraded": True,
            "native_price_strong": price_names,
            "native_flow_strong": flow_names,
            "cash_anchor_strong": cash_anchor,
            "native_strength_ok": strength_ok,
        }

    threshold = float(result.get("price_threshold_bps", 0.0) or 0.0)
    cb_move = float((s1.get("moves") or {}).get("coinbase", 0.0) or 0.0)
    cb_flow = float((((s2.get("venues") or {}).get("coinbase") or {}).get("signed_imbalance", 0.0)) or 0.0)
    soft_price_threshold = max(_SOFT_PRICE_FLOOR_BPS, threshold * _SOFT_PRICE_FRACTION)
    strong_price_threshold = max(_STRONG_PRICE_FLOOR_BPS, threshold * _STRONG_PRICE_FRACTION)
    price_ok = cb_move >= soft_price_threshold
    flow_ok = cb_flow >= _SOFT_FLOW_IMBALANCE
    strong_price_ok = cb_move >= strong_price_threshold
    strong_flow_ok = cb_flow >= _STRONG_FLOW_IMBALANCE
    # Two weak observations may corroborate one another.  A single external
    # axis is enough only when it is independently material; this prevents
    # Coinbase arbitrage noise from blessing a correlated Binance impulse.
    corroborated = (price_ok and flow_ok) or strong_price_ok or strong_flow_ok
    return corroborated, {
        "applies": True,
        "mode": "STRICT_EXTERNAL_SOFT_CORROBORATION",
        "coinbase_fresh": True,
        "coinbase_strict_fresh": True,
        "coinbase_age_s": round(age, 4),
        "coinbase_price_bps": round(cb_move, 4),
        "coinbase_flow_imbalance": round(cb_flow, 4),
        "soft_price_threshold_bps": round(soft_price_threshold, 4),
        "soft_flow_threshold": _SOFT_FLOW_IMBALANCE,
        "strong_price_threshold_bps": round(strong_price_threshold, 4),
        "strong_flow_threshold": _STRONG_FLOW_IMBALANCE,
        "price_ok": price_ok,
        "flow_ok": flow_ok,
        "strong_price_ok": strong_price_ok,
        "strong_flow_ok": strong_flow_ok,
        "corroborated": corroborated,
    }


def _absorption_cash_authority(result, state):
    """Require real Binance Spot conversion for a contradictory-flow story.

    OI growth is directionless and Coinbase/Futures can move together through
    arbitrage.  Therefore an absorption Bias may propose direction, but cannot
    authorize Entry until Binance Spot price participates in a cash-led handoff.
    """
    bias = getattr(state, "bias_council", None) or {}
    story = str((bias.get("story") or {}).get("name") or "").upper()
    if story not in _ABSORPTION_STORIES:
        return True, {"applies": False, "story": story or None}

    votes = result.get("s_votes") or {}
    price = (
        (votes.get("S1_cross_venue_price_acceptance") or {}).get("metrics")
        or {}
    )
    spot_price_support = "spot" in set(price.get("supporters") or ())
    handoff = (
        ((result.get("causal") or {}).get("handoff") or {}).get("status")
        or "UNKNOWN"
    )
    allowed = bool(spot_price_support and handoff == "SPOT_HANDOFF")
    return allowed, {
        "applies": True,
        "story": story,
        "binance_spot_price_support": spot_price_support,
        "handoff_status": handoff,
        "allowed": allowed,
        "policy": "ABSORPTION_REQUIRES_BINANCE_SPOT_PRICE_AND_CASH_HANDOFF",
    }


def install(entry_council):
    if getattr(entry_council, "_exchange_independence_installed", False):
        return VERSION

    original = entry_council.evaluate

    def evaluate(state, now=None, side=None):
        result = original(state, now=now, side=side)
        if not isinstance(result, dict):
            return result
        if result.get("decision") != "GO":
            return result

        absorption_allowed, absorption_meta = _absorption_cash_authority(
            result, state
        )
        if absorption_meta.get("applies"):
            result = dict(result)
            result["absorption_cash_authority"] = absorption_meta
        if not absorption_allowed:
            result["decision"] = "WAIT"
            result["entry_mode"] = "NONE"
            result["phase"] = "PRESSURE_BUILDING"
            result["reason"] = "WAIT_ABSORPTION_BINANCE_SPOT_HANDOFF"
            result["confidence"] = min(
                float(result.get("confidence", 0.0) or 0.0), 0.49
            )
            return result

        # FAST is the early lane, so it may never borrow the 2.5-5 second
        # degraded exception reserved for strong Binance-only NORMAL entries.
        # Its 3/3 price contract is meaningful only while Coinbase is strict-fresh.
        if result.get("entry_mode") == "FAST":
            current = float(result.get("ts", 0.0) or 0.0)
            cb_ts = float((result.get("freshness") or {}).get("coinbase", 0.0) or 0.0)
            age = current - cb_ts if cb_ts > 0.0 and current >= cb_ts else None
            meta = {
                "applies": True,
                "mode": "FAST_STRICT_EXTERNAL",
                "coinbase_fresh": bool(age is not None and age <= _COINBASE_STRICT_AGE_SEC),
                "coinbase_strict_fresh": bool(age is not None and age <= _COINBASE_STRICT_AGE_SEC),
                "coinbase_age_s": round(age, 4) if age is not None else None,
            }
            out = dict(result)
            out["exchange_independence"] = meta
            if meta["coinbase_strict_fresh"]:
                return out
            out["decision"] = "WAIT"
            out["entry_mode"] = "NONE"
            out["phase"] = "PRESSURE_BUILDING"
            out["reason"] = "WAIT_EXTERNAL_FAST_REQUIRES_FRESH_COINBASE"
            out["confidence"] = min(float(out.get("confidence", 0.0) or 0.0), 0.49)
            return out

        if result.get("entry_mode") != "NORMAL":
            return result

        allowed, meta = _soft_external_ok(result)
        out = dict(result)
        out["exchange_independence"] = meta
        if allowed:
            if meta.get("confidence_degraded"):
                original_conf = float(out.get("confidence", 0.0) or 0.0)
                out["confidence"] = min(
                    original_conf * _DEGRADED_CONFIDENCE_FACTOR,
                    _DEGRADED_CONFIDENCE_CAP,
                )
            return out

        out["decision"] = "WAIT"
        out["entry_mode"] = "NONE"
        out["phase"] = "PRESSURE_BUILDING"
        mode = meta.get("mode")
        if mode == "DEGRADED_EXTERNAL_STRONG_NATIVE":
            out["reason"] = "WAIT_EXTERNAL_DEGRADED_NEEDS_CASH_ANCHORED_STRONG_NATIVE"
        elif mode in ("EXTERNAL_UNAVAILABLE", "EXTERNAL_TIMESTAMP_INVALID"):
            out["reason"] = "WAIT_EXTERNAL_UNAVAILABLE"
        else:
            out["reason"] = "WAIT_EXTERNAL_CORROBORATION"
        out["confidence"] = min(
            float(out.get("confidence", 0.0) or 0.0),
            0.49,
        )
        return out

    entry_council.evaluate = evaluate
    entry_council._exchange_independence_installed = True
    return VERSION
