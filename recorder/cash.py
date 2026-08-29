"""Low-allocation helpers for causal Spot trade micro-batches."""

from datetime import datetime, timezone


class CausalClockEstimator:
    """Bounded exchange-clock correction for recorder research metadata.

    Receive time remains the freshness/no-lookahead clock.  Corrected event
    time is only an ordering estimate and is always accompanied by uncertainty.
    """

    __slots__ = (
        "samples", "base_delay_ms", "jitter_ms", "last_corrected_ms",
    )

    def __init__(self):
        self.samples = 0
        self.base_delay_ms = 0.0
        self.jitter_ms = 0.0
        self.last_corrected_ms = 0.0

    def observe(self, event_time_ms, receive_time_ms):
        event_ms = float(event_time_ms or receive_time_ms)
        receive_ms = float(receive_time_ms)
        delay = receive_ms - event_ms
        if self.samples == 0:
            self.base_delay_ms = delay
            self.jitter_ms = 5.0
        else:
            self.base_delay_ms = min(delay, self.base_delay_ms + 0.05)
            residual = abs(delay - self.base_delay_ms)
            self.jitter_ms += 0.05 * (residual - self.jitter_ms)
        corrected = event_ms + self.base_delay_ms
        valid = bool(
            corrected + 50.0 >= self.last_corrected_ms
            and event_ms <= receive_ms + 1_000.0
        )
        self.last_corrected_ms = max(self.last_corrected_ms, corrected)
        self.samples += 1
        uncertainty = (
            250.0 if self.samples < 5
            else max(5.0, min(250.0, 3.0 * self.jitter_ms))
        )
        return {
            "freshness_time_basis": "RECEIVE_TIME",
            "causal_order_time_basis": (
                "CORRECTED_EVENT_TIME_WITH_UNCERTAINTY"
            ),
            "corrected_event_time_ms": round(self.last_corrected_ms, 4),
            "clock_uncertainty_ms": round(uncertainty, 4),
            "clock_valid": valid,
            "receive_minus_event_ms": round(delay, 4),
        }


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
        "last_event_time_ms", "quantity_q", "non_rpi_quantity_nq",
        "nq_observation_count", "track_nq",
    )

    def __init__(self, batch_ms=100, track_nq=False):
        self.batch_ms = max(25, int(batch_ms))
        self.track_nq = bool(track_nq)
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
        self.quantity_q = 0.0
        self.non_rpi_quantity_nq = 0.0
        self.nq_observation_count = 0

    def _snapshot(self):
        if self.trade_count <= 0:
            return None
        nq_complete = self.nq_observation_count == self.trade_count
        payload = {
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
        if self.track_nq:
            # Binance USD-M currently exposes q and nq on aggTrade.  Other
            # venues omit nq; never manufacture q-nq from an absent field.
            payload.update({
                "quantity_q": self.quantity_q,
                "non_rpi_quantity_nq": (
                    self.non_rpi_quantity_nq if nq_complete else None
                ),
                "q_minus_nq": (
                    self.quantity_q - self.non_rpi_quantity_nq
                    if nq_complete else None
                ),
                "nq_observation_count": self.nq_observation_count,
                "nq_coverage": (
                    self.nq_observation_count / self.trade_count
                    if self.trade_count else 0.0
                ),
                "rpi_flow_research_authority": False,
            })
        return payload

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
        aggressive_buy, non_rpi_qty=None,
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
        self.quantity_q += qty
        if non_rpi_qty is not None:
            try:
                nq = float(non_rpi_qty)
            except (TypeError, ValueError):
                nq = -1.0
            if nq >= 0.0:
                self.non_rpi_quantity_nq += nq
                self.nq_observation_count += 1
        self.last_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.last_trade_id = trade_id
        self.last_event_time_ms = event_ms
        return completed
