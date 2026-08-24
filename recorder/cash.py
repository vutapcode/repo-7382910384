"""Low-allocation helpers for causal Spot trade micro-batches."""

from datetime import datetime, timezone


def coinbase_time_ms(value, fallback_ms):
    """Parse Coinbase ISO-8601 timestamps without making receipt time causal."""
    text = str(value or "").strip()
    if not text:
        return int(fallback_ms)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000.0)
    except (TypeError, ValueError, OverflowError):
        return int(fallback_ms)


def is_coinbase_live_match(message_type):
    """Exclude the subscription snapshot so reconnects cannot duplicate flow."""
    return str(message_type or "").lower() == "match"


class CashTradeBatcher:
    """Aggregate exact buy/sell totals into receive-time micro-batches.

    Exchange timestamps and trade identifiers remain in the payload for audit,
    while the local receive bucket preserves causal cross-venue ordering.
    """

    __slots__ = (
        "batch_ms", "bucket_start_ms", "trade_count", "buy_qty", "sell_qty",
        "buy_quote", "sell_quote", "first_price", "last_price", "high", "low",
        "first_trade_id", "last_trade_id", "first_event_time_ms",
        "last_event_time_ms",
    )

    def __init__(self, batch_ms=100):
        self.batch_ms = max(25, int(batch_ms))
        self._clear()

    def _clear(self):
        self.bucket_start_ms = None
        self.trade_count = 0
        self.buy_qty = 0.0
        self.sell_qty = 0.0
        self.buy_quote = 0.0
        self.sell_quote = 0.0
        self.first_price = None
        self.last_price = None
        self.high = None
        self.low = None
        self.first_trade_id = None
        self.last_trade_id = None
        self.first_event_time_ms = None
        self.last_event_time_ms = None

    def _snapshot(self):
        if self.trade_count <= 0:
            return None
        return {
            "bucket_start_ms": self.bucket_start_ms,
            "bucket_end_ms": self.bucket_start_ms + self.batch_ms - 1,
            "batch_ms": self.batch_ms,
            "trade_count": self.trade_count,
            "buy_qty": self.buy_qty,
            "sell_qty": self.sell_qty,
            "buy_quote": self.buy_quote,
            "sell_quote": self.sell_quote,
            "trade_delta_qty": self.buy_qty - self.sell_qty,
            "first_price": self.first_price,
            "last_price": self.last_price,
            "high": self.high,
            "low": self.low,
            "first_trade_id": self.first_trade_id,
            "last_trade_id": self.last_trade_id,
            "first_event_time_ms": self.first_event_time_ms,
            "last_event_time_ms": self.last_event_time_ms,
        }

    def flush(self):
        payload = self._snapshot()
        self._clear()
        return payload

    def flush_due(self, receive_time_ms):
        if (
            self.bucket_start_ms is not None
            and int(receive_time_ms) >= self.bucket_start_ms + self.batch_ms
        ):
            return self.flush()
        return None

    def push(
        self, *, receive_time_ms, event_time_ms, trade_id, price, qty,
        aggressive_buy,
    ):
        receive_ms = int(receive_time_ms)
        event_ms = int(event_time_ms or receive_ms)
        price = float(price)
        qty = float(qty)
        if price <= 0.0 or qty <= 0.0:
            return None
        bucket = receive_ms - receive_ms % self.batch_ms
        completed = None
        if self.bucket_start_ms is not None and bucket != self.bucket_start_ms:
            completed = self._snapshot()
            self._clear()
        if self.bucket_start_ms is None:
            self.bucket_start_ms = bucket
            self.first_price = price
            self.high = price
            self.low = price
            self.first_trade_id = trade_id
            self.first_event_time_ms = event_ms
        quote = price * qty
        if aggressive_buy:
            self.buy_qty += qty
            self.buy_quote += quote
        else:
            self.sell_qty += qty
            self.sell_quote += quote
        self.trade_count += 1
        self.last_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.last_trade_id = trade_id
        self.last_event_time_ms = event_ms
        return completed
