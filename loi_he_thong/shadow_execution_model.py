"""Conservative execution model aligned with WStrade live entry semantics."""

import os


VERSION = "SHADOW_EXECUTION_PARITY_V1"


def market_fill(side, bid, ask, slippage_bps=None):
    bid, ask = float(bid or 0.0), float(ask or 0.0)
    if bid <= 0.0 or ask < bid:
        return 0.0
    slippage = float(
        os.getenv("SMC_SHADOW_MARKET_SLIPPAGE_BPS", "1.5")
        if slippage_bps is None else slippage_bps
    )
    if str(side).upper() == "LONG":
        return ask * (1.0 + max(0.0, slippage) / 10000.0)
    if str(side).upper() == "SHORT":
        return bid * (1.0 - max(0.0, slippage) / 10000.0)
    return 0.0


def maker_trade_through_volume(side, limit_price, placed_at, trades, now=None):
    side = str(side).upper()
    limit_price = float(limit_price or 0.0)
    placed_ms = float(placed_at or 0.0) * 1000.0
    now_ms = float(now) * 1000.0 if now is not None else float("inf")
    rows = trades or ()

    def accumulate(iterable, stop_at_cutoff):
        volume = 0.0
        previous_ts = None
        for row in iterable:
            try:
                ts = float(row.get("thoi_gian_ms", 0.0) or 0.0)
                if stop_at_cutoff and previous_ts is not None and ts > previous_ts:
                    return None
                previous_ts = ts
                if ts > now_ms:
                    continue
                if ts < placed_ms:
                    if stop_at_cutoff:
                        break
                    continue
                price = float(row.get("gia", 0.0) or 0.0)
                qty = max(0.0, float(row.get("khoi_luong", 0.0) or 0.0))
                seller_aggressor = bool(row.get("ban_chu_dong", False))
            except (AttributeError, TypeError, ValueError):
                continue
            if side == "LONG" and seller_aggressor and price <= limit_price:
                volume += qty
            elif side == "SHORT" and not seller_aggressor and price >= limit_price:
                volume += qty
        return volume

    # The live buffers are chronological. Walk only their recent tail; if a
    # malformed/disordered buffer is observed, retain numerical correctness via
    # a rare full forward fallback rather than silently dropping fill evidence.
    try:
        volume = accumulate(reversed(rows), True)
    except TypeError:
        volume = None
    if volume is not None:
        return volume
    volume = 0.0
    for row in rows:
        try:
            ts = float(row.get("thoi_gian_ms", 0.0) or 0.0)
            if ts < placed_ms or ts > now_ms:
                continue
            price = float(row.get("gia", 0.0) or 0.0)
            qty = max(0.0, float(row.get("khoi_luong", 0.0) or 0.0))
            seller_aggressor = bool(row.get("ban_chu_dong", False))
        except (AttributeError, TypeError, ValueError):
            continue
        if side == "LONG" and seller_aggressor and price <= limit_price:
            volume += qty
        elif side == "SHORT" and not seller_aggressor and price >= limit_price:
            volume += qty
    return volume


def maker_fill_required_volume(quantity):
    multiple = max(1.0, float(os.getenv(
        "SMC_SHADOW_MAKER_QUEUE_MULTIPLE", "5.0"
    )))
    return max(0.0, float(quantity or 0.0)) * multiple
