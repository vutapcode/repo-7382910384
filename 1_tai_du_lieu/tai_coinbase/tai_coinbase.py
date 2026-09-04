"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_coinbase
- ROLE: Coinbase BTC-USD price + rolling aggressive-flow CVD + public L2 transport.
- CONTRACT: DATA-ONLY collector. Never evaluate entry/bias strategy state.
- L2 RULE: cancellation/removal is not execution; only matches establish executed flow.
"""
import asyncio
import collections
from datetime import datetime, timezone
import logging
import time

import orjson
import websockets

from loi_he_thong import ignition_signals

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
L2_PUBLISH_INTERVAL_MS = 100.0
L2_BATCH_RETENTION = 64
L2_TOP_LEVELS = 20
COINBASE_WS_MAX_SIZE = 8 * 1024 * 1024


def _subscribe_message(product_id):
    return orjson.dumps({
        "type": "subscribe",
        "product_ids": [str(product_id or "BTC-USD").upper()],
        "channels": ["matches", "ticker", "level2_batch"],
    })


def _coinbase_match_delta(data) -> float:
    """Signed taker delta. Coinbase `side` is the MAKER order side."""
    size = float(data.get("size", 0.0) or 0.0)
    side = str(data.get("side", "") or "").lower()
    if size <= 0.0 or side not in ("buy", "sell"):
        return 0.0
    return size if side == "sell" else -size


def _coinbase_event_ms(value, fallback_ms):
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000.0)
    except (TypeError, ValueError, OverflowError):
        return int(fallback_ms)


def _apply_ticker(data, state) -> bool:
    try:
        price = float(data.get("price", 0.0) or 0.0)
        bid = float(data.get("best_bid", 0.0) or 0.0)
        ask = float(data.get("best_ask", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if price <= 0.0 and bid > 0.0 and ask > bid:
        price = (bid + ask) / 2.0
    if price <= 0.0:
        return False
    state.coinbase_price = price
    state.coinbase_best_bid = bid
    state.coinbase_best_ask = ask
    now_ms = int(time.time() * 1000.0)
    state.thoi_gian_coinbase_ticker_cuoi = now_ms / 1000.0
    ignition_signals.observe_bbo(
        state, "coinbase_spot", bid=bid, ask=ask,
        receive_time_ms=now_ms,
    )
    return True


class CoinbaseL2Book:
    """Bounded local reconstruction for data-only liquidity observations."""
    __slots__ = ("bids", "asks", "synced")

    def __init__(self):
        self.bids = {}
        self.asks = {}
        self.synced = False

    def clear(self):
        self.bids.clear()
        self.asks.clear()
        self.synced = False

    @staticmethod
    def _load_side(rows):
        levels = {}
        for row in rows or ():
            try:
                price = float(row[0])
                qty = float(row[1])
            except (IndexError, TypeError, ValueError):
                continue
            if price > 0.0 and qty > 0.0:
                levels[price] = qty
        return levels

    def snapshot(self, data):
        bids = self._load_side(data.get("bids"))
        asks = self._load_side(data.get("asks"))
        if not bids or not asks or min(asks) <= max(bids):
            self.clear()
            return False
        self.bids = bids
        self.asks = asks
        self.synced = True
        return True

    def apply(self, data):
        if not self.synced:
            return False
        for row in data.get("changes") or ():
            try:
                side = str(row[0]).lower()
                price = float(row[1])
                qty = float(row[2])
            except (IndexError, TypeError, ValueError):
                continue
            book = self.bids if side == "buy" else self.asks if side == "sell" else None
            if book is None or price <= 0.0 or qty < 0.0:
                continue
            if qty == 0.0:
                book.pop(price, None)
            else:
                book[price] = qty
        if not self.bids or not self.asks or min(self.asks) <= max(self.bids):
            self.clear()
            return False
        return True

    def top(self, limit=L2_TOP_LEVELS):
        bids = sorted(self.bids.items(), reverse=True)[:int(limit)]
        asks = sorted(self.asks.items())[:int(limit)]
        return bids, asks


class RollingFlow:
    """O(1) rolling signed CVD + absolute volume for one time window."""
    __slots__ = ("window_ms", "rows", "signed", "volume", "last_ts_ms")
    def __init__(self, window_ms):
        self.window_ms = float(window_ms)
        self.rows = collections.deque()
        self.signed = 0.0
        self.volume = 0.0
        self.last_ts_ms = 0.0
    def clear(self):
        self.rows.clear()
        self.signed = 0.0
        self.volume = 0.0
        self.last_ts_ms = 0.0
    def push(self, ts_ms, delta):
        ts = float(ts_ms)
        if self.last_ts_ms > 0.0 and ts < self.last_ts_ms:
            self.clear()
        self.last_ts_ms = ts
        d = float(delta)
        self.rows.append((ts, d))
        self.signed += d
        self.volume += abs(d)
        self.trim(ts_ms)
    def trim(self, now_ms):
        cutoff = float(now_ms) - self.window_ms
        rows = self.rows
        while rows and rows[0][0] < cutoff:
            _, d = rows.popleft()
            self.signed -= d
            self.volume -= abs(d)
        if abs(self.signed) < 1e-12:
            self.signed = 0.0
        if self.volume < 1e-12:
            self.volume = 0.0


def _publish_flow(state, f3, f1m, f5m, now_ms):
    f3.trim(now_ms); f1m.trim(now_ms); f5m.trim(now_ms)
    state.coinbase_cvd_3s = f3.signed
    state.coinbase_volume_3s = f3.volume
    state.coinbase_flow_3s_ts = now_ms / 1000.0
    state.coinbase_cvd_1m = f1m.signed
    state.coinbase_volume_1m = f1m.volume
    state.coinbase_flow_1m_coverage_sec = (
        max(0.0, (f1m.rows[-1][0] - f1m.rows[0][0]) / 1000.0)
        if len(f1m.rows) >= 2 else 0.0
    )
    state.coinbase_cvd_5m = f5m.signed
    state.thoi_gian_coinbase_cuoi = now_ms / 1000.0


def _reset_l2_state(state, book):
    book.clear()
    state.coinbase_l2_epoch = int(
        getattr(state, "coinbase_l2_epoch", 0) or 0
    ) + 1
    state.coinbase_l2_synced = False
    state.coinbase_l2_source_health = "WARMUP"
    state.coinbase_l2_updated_at = 0.0
    state.coinbase_l2_receive_time_ms = 0
    state.coinbase_l2_event_time_ms = 0
    state.coinbase_l2_bids_top_20 = ()
    state.coinbase_l2_asks_top_20 = ()
    state.coinbase_l2_authority = False
    state.coinbase_l2_semantic_role = "USD_CASH_LIQUIDITY_DATA_ONLY"
    state.coinbase_l2_batches = collections.deque(maxlen=L2_BATCH_RETENTION)


def _publish_l2(state, book, data, receive_ms, force=False):
    if not book.synced:
        state.coinbase_l2_synced = False
        state.coinbase_l2_source_health = "DEGRADED"
        return False
    last_ms = float(getattr(state, "coinbase_l2_receive_time_ms", 0.0) or 0.0)
    if not force and receive_ms - last_ms < L2_PUBLISH_INTERVAL_MS:
        return False
    bids, asks = book.top()
    if not bids or not asks:
        return False
    event_ms = _coinbase_event_ms(data.get("time"), receive_ms)
    state.coinbase_l2_bids_top_20 = tuple(bids)
    state.coinbase_l2_asks_top_20 = tuple(asks)
    state.coinbase_l2_synced = True
    state.coinbase_l2_source_health = "FRESH"
    state.coinbase_l2_updated_at = receive_ms / 1000.0
    state.coinbase_l2_receive_time_ms = int(receive_ms)
    state.coinbase_l2_event_time_ms = int(event_ms)
    state.coinbase_l2_snapshot = {
        "source_id": "coinbase_btcusd_l2",
        "venue": "coinbase",
        "instrument": "BTC-USD",
        "event_type": "L2",
        "event_time_ms": int(event_ms),
        "receive_time_ms": int(receive_ms),
        "receive_time_monotonic_ns": time.monotonic_ns(),
        "epoch": int(state.coinbase_l2_epoch),
        "source_health": "FRESH",
        "authority": False,
        "semantic_role": "USD_CASH_LIQUIDITY_DATA_ONLY",
        "best_bid": bids[0][0],
        "best_ask": asks[0][0],
        "bids_top_20": tuple(bids),
        "asks_top_20": tuple(asks),
    }
    return True


def _record_l2_batch(state, data, receive_ms):
    batches = getattr(state, "coinbase_l2_batches", None)
    if not isinstance(batches, collections.deque):
        batches = collections.deque(maxlen=L2_BATCH_RETENTION)
        state.coinbase_l2_batches = batches
    changes = tuple(tuple(row[:3]) for row in (data.get("changes") or ()) if len(row) >= 3)
    batches.append({
        "source_id": "coinbase_btcusd_l2",
        "event_type": "L2_UPDATE_BATCH",
        "event_time_ms": _coinbase_event_ms(data.get("time"), receive_ms),
        "receive_time_ms": int(receive_ms),
        "receive_time_monotonic_ns": time.monotonic_ns(),
        "epoch": int(getattr(state, "coinbase_l2_epoch", 0) or 0),
        "changes": changes,
        "authority": False,
        "semantic_role": "USD_CASH_LIQUIDITY_DATA_ONLY",
    })


async def hung_coinbase_spot(product_id: str, bo_nho_ram):
    """One Coinbase socket: ticker + executed flow + L2 transport, data only."""
    f3 = RollingFlow(3_000.0)
    f1m = RollingFlow(60_000.0)
    f5m = RollingFlow(300_000.0)
    l2 = CoinbaseL2Book()
    subscribe_msg = _subscribe_message(product_id)

    while True:
        try:
            async with websockets.connect(
                COINBASE_WS_URL,
                ping_interval=20,
                ping_timeout=20,
                max_size=COINBASE_WS_MAX_SIZE,
            ) as ws:
                await ws.send(subscribe_msg)
                # Reconnect is a hard causal boundary for both flow and L2.
                f3.clear()
                f1m.clear()
                f5m.clear()
                bo_nho_ram.coinbase_cvd_3s = 0.0
                bo_nho_ram.coinbase_volume_3s = 0.0
                bo_nho_ram.coinbase_flow_3s_ts = 0.0
                bo_nho_ram.coinbase_cvd_1m = 0.0
                bo_nho_ram.coinbase_volume_1m = 0.0
                bo_nho_ram.coinbase_flow_1m_coverage_sec = 0.0
                bo_nho_ram.coinbase_cvd_5m = 0.0
                bo_nho_ram.thoi_gian_coinbase_cuoi = 0.0
                bo_nho_ram.thoi_gian_coinbase_ticker_cuoi = 0.0
                bo_nho_ram.coinbase_flow_epoch = int(getattr(bo_nho_ram, "coinbase_flow_epoch", 0) or 0) + 1
                bo_nho_ram.coinbase_flow_epoch_started_at = time.time()
                _reset_l2_state(bo_nho_ram, l2)
                ignition_signals.reset_venue(
                    bo_nho_ram, "coinbase_spot", bo_nho_ram.coinbase_flow_epoch
                )
                logging.info(
                    "[COINBASE] Ket noi Spot+L2 flow_epoch=%s l2_epoch=%s: %s",
                    bo_nho_ram.coinbase_flow_epoch,
                    bo_nho_ram.coinbase_l2_epoch,
                    product_id,
                )

                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        msg_type = data.get("type", "")
                        receive_ms = time.time() * 1000.0

                        if msg_type == "ticker":
                            _apply_ticker(data, bo_nho_ram)
                            continue
                        if msg_type == "snapshot":
                            if l2.snapshot(data):
                                _publish_l2(
                                    bo_nho_ram, l2, data, receive_ms, force=True
                                )
                            else:
                                bo_nho_ram.coinbase_l2_source_health = "DEGRADED"
                            continue
                        if msg_type == "l2update":
                            _record_l2_batch(bo_nho_ram, data, receive_ms)
                            if l2.apply(data):
                                _publish_l2(bo_nho_ram, l2, data, receive_ms)
                            else:
                                bo_nho_ram.coinbase_l2_source_health = "DEGRADED"
                            continue
                        # ``last_match`` is the subscription snapshot and may
                        # repeat across reconnects. Only live ``match`` rows
                        # may enter executed-flow authority.
                        if msg_type != "match":
                            continue

                        delta = _coinbase_match_delta(data)
                        if delta == 0.0:
                            continue
                        if delta > 0.0:
                            bo_nho_ram.coinbase_cvd_buy_total = float(
                                getattr(bo_nho_ram, 'coinbase_cvd_buy_total', 0.0) or 0.0
                            ) + delta
                        else:
                            bo_nho_ram.coinbase_cvd_sell_total = float(
                                getattr(bo_nho_ram, 'coinbase_cvd_sell_total', 0.0) or 0.0
                            ) + abs(delta)
                        receive_mono_ns = time.monotonic_ns()
                        ignition_signals.observe_trade(
                            bo_nho_ram, "coinbase_spot",
                            receive_time_ms=receive_ms,
                            event_time_ms=_coinbase_event_ms(data.get("time"), receive_ms),
                            price=float(data.get("price", 0.0) or 0.0),
                            qty=abs(delta), aggressive_buy=delta > 0.0,
                            receive_time_monotonic_ns=receive_mono_ns,
                            source_health="FRESH",
                        )
                        f3.push(receive_ms, delta)
                        f1m.push(receive_ms, delta)
                        f5m.push(receive_ms, delta)
                        _publish_flow(bo_nho_ram, f3, f1m, f5m, receive_ms)
                    except (KeyError, TypeError, ValueError):
                        continue
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            bo_nho_ram.coinbase_l2_source_health = "DEGRADED"
            logging.warning("[COINBASE] Mat ket noi: %s. Ket noi lai...", exc)
            await asyncio.sleep(5)
        except Exception as exc:
            bo_nho_ram.coinbase_l2_source_health = "DEGRADED"
            logging.error("[COINBASE] Loi: %s. Thu lai sau 5s...", exc)
            await asyncio.sleep(5)
