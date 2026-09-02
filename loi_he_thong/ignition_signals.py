"""O(1) receive-time market features for Ignition Core V1.

Collectors call this module only to publish exact 100 ms executed-flow buckets
and BBO state.  It never evaluates strategy or submits orders.  Exchange event
time is clock-corrected for audit/leader classification; receive time remains
the no-lookahead authority.
"""

from collections import deque
import hashlib
import os
import time

from loi_he_thong.market_event_contract import VERSION as EVENT_CONTRACT_VERSION


VERSION = "IGNITION_SIGNALS_V4_CANONICAL_TEMPORAL_CONTRACT"
BUCKET_MS = 100
HISTORY_BUCKETS = 64  # Fixed 6.4 s research window; bounded/O(1) memory.
WARMUP_BUCKETS = max(5, int(os.getenv("WSTRADE_IGNITION_WARMUP_BUCKETS", "20")))
VENUES = ("binance_spot", "coinbase_spot", "futures")
MIN_QTY = {
    "binance_spot": 0.015,
    "coinbase_spot": 0.002,
    "futures": 0.15,
}


def _raw_evidence_id(venue, epoch, bucket_start_ms):
    raw = f"raw|{venue}|{int(epoch)}|{int(bucket_start_ms)}".encode("utf-8")
    return "ev:" + hashlib.sha256(raw).hexdigest()[:24]


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _Venue:
    __slots__ = (
        "name", "epoch", "bucket_start_ms", "buy_qty", "sell_qty",
        "buy_quote", "sell_quote", "first_price", "last_price", "high",
        "low", "first_event_ms", "last_event_ms", "trade_count", "samples",
        "first_receive_ms", "last_trade_receive_ms",
        "first_receive_monotonic_ns", "last_receive_monotonic_ns",
        "source_health",
        "mean_abs_quote", "mean_dev", "previous_intensity",
        "previous_buy_intensity", "previous_sell_intensity",
        "previous_net_intensity", "history",
        "clock_samples", "base_delay_ms", "jitter_ms", "last_corrected_ms",
        "clock_valid", "bid", "ask", "bid_qty", "ask_qty", "bbo_receive_ms",
    )

    def __init__(self, name):
        self.name = name
        self.epoch = 0
        self.history = deque(maxlen=HISTORY_BUCKETS)
        self.samples = 0
        self.mean_abs_quote = 0.0
        self.mean_dev = 0.0
        self.previous_intensity = 0.0
        self.previous_buy_intensity = 0.0
        self.previous_sell_intensity = 0.0
        self.previous_net_intensity = 0.0
        self.clock_samples = 0
        self.base_delay_ms = 0.0
        self.jitter_ms = 0.0
        self.last_corrected_ms = 0.0
        self.clock_valid = True
        self.bid = self.ask = self.bid_qty = self.ask_qty = 0.0
        self.bbo_receive_ms = 0
        self._clear_bucket()

    def _clear_bucket(self):
        self.bucket_start_ms = None
        self.buy_qty = self.sell_qty = 0.0
        self.buy_quote = self.sell_quote = 0.0
        self.first_price = self.last_price = 0.0
        self.high = self.low = 0.0
        self.first_event_ms = self.last_event_ms = 0
        self.first_receive_ms = self.last_trade_receive_ms = 0
        self.first_receive_monotonic_ns = self.last_receive_monotonic_ns = 0
        self.source_health = "UNKNOWN"
        self.trade_count = 0

    def reset(self, epoch=None):
        self.epoch = int(self.epoch + 1 if epoch is None else epoch)
        self.history.clear()
        self.samples = 0
        self.mean_abs_quote = self.mean_dev = 0.0
        self.previous_intensity = 0.0
        self.previous_buy_intensity = 0.0
        self.previous_sell_intensity = 0.0
        self.previous_net_intensity = 0.0
        self.clock_samples = 0
        self.base_delay_ms = self.jitter_ms = self.last_corrected_ms = 0.0
        self.clock_valid = True
        self._clear_bucket()

    def _clock(self, event_ms, receive_ms):
        delay = float(receive_ms) - float(event_ms)
        if self.clock_samples == 0:
            self.base_delay_ms = delay
            self.jitter_ms = 5.0
        else:
            self.base_delay_ms = min(delay, self.base_delay_ms + 0.05)
            residual = abs(delay - self.base_delay_ms)
            self.jitter_ms += 0.05 * (residual - self.jitter_ms)
        corrected = float(event_ms) + self.base_delay_ms
        self.clock_valid = bool(
            corrected + 50.0 >= self.last_corrected_ms
            and float(event_ms) <= float(receive_ms) + 1_000.0
        )
        self.last_corrected_ms = max(self.last_corrected_ms, corrected)
        self.clock_samples += 1
        return self.last_corrected_ms

    @property
    def uncertainty_ms(self):
        if self.clock_samples < 5:
            return 250.0
        return max(5.0, min(250.0, 3.0 * self.jitter_ms))

    def bbo(self, bid, ask, bid_qty, ask_qty, receive_ms):
        bid, ask = _f(bid), _f(ask)
        if bid <= 0.0 or ask < bid:
            return False
        self.bid, self.ask = bid, ask
        self.bid_qty, self.ask_qty = max(0.0, _f(bid_qty)), max(0.0, _f(ask_qty))
        self.bbo_receive_ms = int(receive_ms)
        return True

    def push(
        self, receive_ms, event_ms, price, qty, aggressive_buy,
        receive_time_monotonic_ns=None, source_health="FRESH",
    ):
        receive_ms, event_ms = int(receive_ms), int(event_ms or receive_ms)
        receive_mono = int(receive_time_monotonic_ns or time.monotonic_ns())
        price, qty = _f(price), _f(qty)
        if price <= 0.0 or qty <= 0.0:
            return False
        bucket = receive_ms - receive_ms % BUCKET_MS
        if self.bucket_start_ms is not None and bucket != self.bucket_start_ms:
            self.finalize(available_time_ms=receive_ms)
        if self.bucket_start_ms is None:
            self.bucket_start_ms = bucket
            self.first_price = self.high = self.low = price
            self.first_event_ms = event_ms
            self.first_receive_ms = receive_ms
            self.first_receive_monotonic_ns = receive_mono
        quote = price * qty
        if aggressive_buy:
            self.buy_qty += qty
            self.buy_quote += quote
        else:
            self.sell_qty += qty
            self.sell_quote += quote
        self.trade_count += 1
        self.last_price = price
        self.high, self.low = max(self.high, price), min(self.low, price)
        self.last_event_ms = event_ms
        self.last_trade_receive_ms = receive_ms
        self.last_receive_monotonic_ns = receive_mono
        health = str(source_health or "UNKNOWN").upper()
        self.source_health = (
            health if health in {
                "FRESH", "STALE", "DEGRADED", "DEAD",
                "CONTRADICTORY", "UNKNOWN",
            } else "UNKNOWN"
        )
        self._clock(event_ms, receive_ms)
        return True

    def finalize_due(self, now_ms):
        if self.bucket_start_ms is not None and int(now_ms) >= self.bucket_start_ms + BUCKET_MS:
            self.finalize(available_time_ms=now_ms)

    def finalize(self, available_time_ms=None, available_time_monotonic_ns=None):
        if self.trade_count <= 0:
            self._clear_bucket()
            return None
        total = self.buy_qty + self.sell_qty
        signed_quote = self.buy_quote - self.sell_quote
        abs_quote = abs(signed_quote)
        imbalance = (self.buy_qty - self.sell_qty) / total if total > 0.0 else 0.0
        price_bps = (
            (self.last_price - self.first_price) / self.first_price * 10_000.0
            if self.first_price > 0.0 else 0.0
        )
        threshold = self.mean_abs_quote + 2.0 * max(
            self.mean_dev, self.mean_abs_quote * 0.10
        )
        surprise = abs_quote / max(1.0, self.mean_abs_quote + self.mean_dev)
        intensity = abs_quote / BUCKET_MS
        buy_intensity = self.buy_quote / BUCKET_MS
        sell_intensity = self.sell_quote / BUCKET_MS
        net_intensity = buy_intensity - sell_intensity
        side = "LONG" if signed_quote > 0.0 else "SHORT" if signed_quote < 0.0 else "NEUTRAL"
        sign = 1.0 if side == "LONG" else -1.0 if side == "SHORT" else 0.0
        same_side_intensity = (
            buy_intensity if sign > 0.0 else
            sell_intensity if sign < 0.0 else 0.0
        )
        previous_same_side_intensity = (
            self.previous_buy_intensity if sign > 0.0 else
            self.previous_sell_intensity if sign < 0.0 else 0.0
        )
        opposite_side_intensity = (
            sell_intensity if sign > 0.0 else
            buy_intensity if sign < 0.0 else 0.0
        )
        previous_opposite_side_intensity = (
            self.previous_sell_intensity if sign > 0.0 else
            self.previous_buy_intensity if sign < 0.0 else 0.0
        )
        same_side_delta = same_side_intensity - previous_same_side_intensity
        opposite_side_delta = (
            opposite_side_intensity - previous_opposite_side_intensity
        )
        net_directional_acceleration = sign * (
            net_intensity - self.previous_net_intensity
        )
        strong = bool(
            self.samples >= WARMUP_BUCKETS
            and total >= MIN_QTY[self.name]
            and abs(imbalance) >= 0.20
            and abs_quote >= threshold
            and surprise >= 1.50
            and sign * price_bps >= 0.15
            and self.clock_valid
            and self.source_health == "FRESH"
        )
        mid = (self.bid + self.ask) / 2.0 if self.bid > 0.0 and self.ask >= self.bid else 0.0
        qty_sum = self.bid_qty + self.ask_qty
        microprice = (
            (self.ask * self.bid_qty + self.bid * self.ask_qty) / qty_sum
            if mid > 0.0 and qty_sum > 0.0 else mid
        )
        bucket_end_ms = int(self.bucket_start_ms) + BUCKET_MS
        available_time_ms = max(
            bucket_end_ms,
            int(self.last_trade_receive_ms or 0),
            int(available_time_ms or bucket_end_ms),
        )
        available_mono = max(
            int(self.last_receive_monotonic_ns or 0),
            int(available_time_monotonic_ns or time.monotonic_ns()),
        )
        batching_uncertainty_ms = max(0.0, available_time_ms - bucket_end_ms)
        temporal_uncertainty_ms = self.uncertainty_ms + batching_uncertainty_ms
        row = {
            "venue": self.name, "source": self.name,
            "stream": "executed_flow_100ms", "epoch": self.epoch,
            "event_contract_version": EVENT_CONTRACT_VERSION,
            "bucket_start_ms": self.bucket_start_ms,
            "bucket_end_ms": bucket_end_ms,
            "first_trade_receive_ms": self.first_receive_ms,
            "last_trade_receive_ms": self.last_trade_receive_ms,
            "available_time_ms": available_time_ms,
            "receive_time_monotonic_ns": self.last_receive_monotonic_ns,
            "available_time_monotonic_ns": available_mono,
            # Compatibility alias for consumers. In V2 it means when the
            # finalized evidence became observable, never logical bucket end.
            "receive_time_ms": available_time_ms,
            "availability_delay_ms": available_time_ms - bucket_end_ms,
            "event_time_ms": self.last_event_ms,
            "exchange_event_time_ms": self.last_event_ms,
            "corrected_event_time_ms": round(self.last_corrected_ms, 4),
            "clock_uncertainty_ms": round(self.uncertainty_ms, 4),
            "clock_offset_ms": round(self.base_delay_ms, 4),
            "clock_jitter_ms": round(self.jitter_ms, 4),
            "batching_uncertainty_ms": round(batching_uncertainty_ms, 4),
            "temporal_uncertainty_ms": round(temporal_uncertainty_ms, 4),
            "temporal_status": (
                "MEASURED" if self.clock_valid and self.source_health == "FRESH"
                else "UNSAFE_OR_UNKNOWN"
            ),
            "source_health": self.source_health,
            "clock_valid": self.clock_valid, "side": side, "strong": strong,
            "buy_qty": self.buy_qty, "sell_qty": self.sell_qty,
            "buy_quote": self.buy_quote, "sell_quote": self.sell_quote,
            "total_qty": total, "signed_quote": signed_quote,
            "imbalance": round(imbalance, 6), "first_price": self.first_price,
            "price": self.last_price, "high": self.high, "low": self.low,
            "price_conversion_bps": round(price_bps, 6),
            "surprise_ratio": round(surprise, 6),
            "flow_intensity": round(intensity, 6),
            "buy_flow_intensity": round(buy_intensity, 6),
            "sell_flow_intensity": round(sell_intensity, 6),
            "same_side_intensity_delta": round(same_side_delta, 6),
            "opposite_side_intensity_delta": round(opposite_side_delta, 6),
            "net_directional_acceleration": round(
                net_directional_acceleration, 6
            ),
            # Compatibility alias. V3 semantics are explicitly directional;
            # an opposite-side burst can no longer prove continuation.
            "flow_acceleration": round(net_directional_acceleration, 6),
            "baseline_samples": self.samples,
            "baseline_abs_quote": round(self.mean_abs_quote, 6),
            "bbo_mid": mid, "microprice": microprice,
            "bbo_qty_valid": qty_sum > 0.0,
            "bbo_receive_time_ms": self.bbo_receive_ms,
            "signal_schema_version": VERSION,
            "payload_version": VERSION,
        }
        evidence_id = _raw_evidence_id(
            self.name, self.epoch, self.bucket_start_ms
        )
        row.update({
            "evidence_id": evidence_id,
            "event_id": evidence_id,
            "parent_evidence_ids": [],
            "root_evidence_id": evidence_id,
            "evidence_role": "RAW_EXECUTED_FLOW_BUCKET",
        })
        if self.samples == 0:
            self.mean_abs_quote = abs_quote
            self.mean_dev = abs_quote * 0.25
        else:
            deviation = abs(abs_quote - self.mean_abs_quote)
            self.mean_abs_quote += 0.02 * (abs_quote - self.mean_abs_quote)
            self.mean_dev += 0.02 * (deviation - self.mean_dev)
        self.samples += 1
        self.previous_intensity = intensity
        self.previous_buy_intensity = buy_intensity
        self.previous_sell_intensity = sell_intensity
        self.previous_net_intensity = net_intensity
        self.history.append(row)
        self._clear_bucket()
        return row


class SignalEngine:
    def __init__(self):
        self.venues = {name: _Venue(name) for name in VENUES}

    def snapshot(self, now_ms):
        for venue in self.venues.values():
            venue.finalize_due(now_ms)
        return {name: tuple(venue.history) for name, venue in self.venues.items()}


def engine(state):
    value = getattr(state, "_ignition_signal_engine", None)
    if not isinstance(value, SignalEngine):
        value = SignalEngine()
        state._ignition_signal_engine = value
    return value


def reset_venue(state, venue, epoch=None):
    engine(state).venues[str(venue)].reset(epoch)


def observe_trade(
    state, venue, *, receive_time_ms, event_time_ms, price, qty,
    aggressive_buy, receive_time_monotonic_ns=None, source_health="FRESH",
):
    return engine(state).venues[str(venue)].push(
        receive_time_ms, event_time_ms, price, qty, aggressive_buy,
        receive_time_monotonic_ns, source_health,
    )


def observe_bbo(state, venue, *, bid, ask, bid_qty=0.0, ask_qty=0.0, receive_time_ms):
    return engine(state).venues[str(venue)].bbo(
        bid, ask, bid_qty, ask_qty, receive_time_ms
    )


def snapshot(state, now_ms):
    return engine(state).snapshot(now_ms)
