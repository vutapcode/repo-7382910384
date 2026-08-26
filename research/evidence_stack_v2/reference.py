"""WStrade Evidence Stack V2 reference. RESEARCH ONLY; never live authority."""

from collections import deque

AUTHORITY = False
WINDOWS = (1, 3, 15, 60)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class LiquidationContextReference:
    """Consumes existing recorder feature_1s buckets; bounded to 60 seconds."""

    def __init__(self):
        self.rows = deque()
        self.last_sec = None

    def observe(self, p):
        ms = int(p.get("bucket_start_ms") or 0)
        if ms <= 0:
            raise ValueError("bucket_start_ms required")
        sec = ms // 1000
        if self.last_sec is not None and sec <= self.last_sec:
            raise ValueError("strict receive-order required")
        cash = p.get("cash_flow") or {}
        bs = cash.get("binance_spot") or {}
        cb = cash.get("coinbase_spot") or {}
        self.rows.append({
            "sec": sec,
            "long_liq": max(0.0, _f(p.get("long_liquidation_quote"))),
            "short_liq": max(0.0, _f(p.get("short_liquidation_quote"))),
            "fut_buy": max(0.0, _f(p.get("buy_quote"))),
            "fut_sell": max(0.0, _f(p.get("sell_quote"))),
            "spot_buy": max(0.0, _f(bs.get("buy_quote"))),
            "spot_sell": max(0.0, _f(bs.get("sell_quote"))),
            "cb_buy": max(0.0, _f(cb.get("buy_quote"))),
            "cb_sell": max(0.0, _f(cb.get("sell_quote"))),
            "open": _f(p.get("mid_open")) or None,
            "close": _f(p.get("mid_close")) or None,
        })
        self.last_sec = sec
        while self.rows and self.rows[0]["sec"] < sec - 59:
            self.rows.popleft()

    def window(self, side, seconds):
        side = side.upper()
        if side not in {"LONG", "SHORT"} or seconds not in WINDOWS:
            raise ValueError("invalid side/window")
        if not self.rows:
            return {"seconds": seconds, "bucket_count": 0}
        cut = self.rows[-1]["sec"] - seconds + 1
        rows = [r for r in self.rows if r["sec"] >= cut]
        if side == "LONG":
            keys = ("short_liq", "fut_buy", "spot_buy", "cb_buy")
        else:
            keys = ("long_liq", "fut_sell", "spot_sell", "cb_sell")
        liq, fut, spot, cb = (sum(r[k] for r in rows) for k in keys)
        first = next((r["open"] for r in rows if r["open"]), None)
        last = next((r["close"] for r in reversed(rows) if r["close"]), None)
        progress = None if not first or not last else (last / first - 1.0) * 10000.0
        if progress is not None and side == "SHORT":
            progress = -progress
        return {
            "seconds": seconds,
            "bucket_count": len(rows),
            "sampled_liquidation_quote": liq,
            "directional_futures_quote": fut,
            "directional_spot_quote": spot,
            "directional_coinbase_quote": cb,
            # forceOrder is sampled; never treat this as exact liquidation share.
            "sampled_liquidation_pressure": liq / fut if fut > 0.0 else None,
            "price_progress_bps": progress,
        }

    def classify(self, *, side, oi_intent, cash_acceptance,
                 liquidation_material, liquidation_decelerating,
                 futures_following, absorption_present):
        oi = oi_intent.upper()
        cash = cash_acceptance.upper()
        if liquidation_material and liquidation_decelerating and oi == "UNWIND" and cash in {"WEAK", "OPPOSING"}:
            klass = "LIQUIDATION_TAIL"
        elif cash == "CONVERTING" and futures_following and oi != "UNWIND" and not absorption_present:
            klass = "REAL_CONTINUATION"
        elif liquidation_material and oi == "UNWIND":
            klass = "LIQUIDATION_DRIVEN"
        elif absorption_present:
            klass = "ABSORPTION"
        else:
            klass = "UNKNOWN"
        return {
            "authority": AUTHORITY,
            "side": side.upper(),
            "causal_class": klass,
            "oi_intent": oi,
            "cash_acceptance": cash,
            "windows": {str(w): self.window(side, w) for w in WINDOWS},
        }


def consumed_wave_invariant(previous_max, current_measurement):
    """Same causal wave may mature; it must never become young again."""
    return max(float(previous_max), float(current_measurement))
