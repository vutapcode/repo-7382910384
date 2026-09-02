"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_coinbase
- ROLE: Coinbase BTC-USD price + rolling aggressive-flow CVD.
- CONTRACT: DATA-ONLY collector. Never evaluate entry/bias strategy state.
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
SUBSCRIBE_MSG = orjson.dumps({
    "type": "subscribe",
    "product_ids": ["BTC-USD"],
    "channels": ["matches", "ticker"],
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

async def hung_coinbase_spot(product_id: str, bo_nho_ram):
    """One Coinbase socket: ticker + rolling CVD 3s/1m/5m, data only."""
    f3 = RollingFlow(3_000.0)
    f1m = RollingFlow(60_000.0)
    f5m = RollingFlow(300_000.0)

    while True:
        try:
            async with websockets.connect(
                COINBASE_WS_URL, ping_interval=20, ping_timeout=20
            ) as ws:
                await ws.send(SUBSCRIBE_MSG)
                # A reconnect starts a new causal-flow epoch. Never bridge pre-outage
                # trades into fresh 3s/1m/5m evidence.
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
                ignition_signals.reset_venue(
                    bo_nho_ram, "coinbase_spot", bo_nho_ram.coinbase_flow_epoch
                )
                logging.info("[COINBASE] Ket noi Coinbase Spot epoch=%s: %s", bo_nho_ram.coinbase_flow_epoch, product_id)

                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        msg_type = data.get("type", "")

                        if msg_type == "ticker":
                            _apply_ticker(data, bo_nho_ram)
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
                        now_ms = time.time() * 1000.0
                        receive_mono_ns = time.monotonic_ns()
                        ignition_signals.observe_trade(
                            bo_nho_ram, "coinbase_spot",
                            receive_time_ms=now_ms,
                            event_time_ms=_coinbase_event_ms(data.get("time"), now_ms),
                            price=float(data.get("price", 0.0) or 0.0),
                            qty=abs(delta), aggressive_buy=delta > 0.0,
                            receive_time_monotonic_ns=receive_mono_ns,
                            source_health="FRESH",
                        )
                        f3.push(now_ms, delta)
                        f1m.push(now_ms, delta)
                        f5m.push(now_ms, delta)
                        _publish_flow(bo_nho_ram, f3, f1m, f5m, now_ms)
                    except (KeyError, TypeError, ValueError):
                        continue
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            logging.warning("[COINBASE] Mat ket noi: %s. Ket noi lai...", exc)
            await asyncio.sleep(5)
        except Exception as exc:
            logging.error("[COINBASE] Loi: %s. Thu lai sau 5s...", exc)
            await asyncio.sleep(5)
