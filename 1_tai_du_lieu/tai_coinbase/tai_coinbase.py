"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_coinbase
- ROLE: Coinbase BTC-USD price + rolling aggressive-flow CVD.
- CONTRACT: DATA-ONLY collector. Never evaluate entry/bias strategy state.
"""
import asyncio
import collections
import logging
import time

import orjson
import websockets

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
    state.thoi_gian_coinbase_ticker_cuoi = time.time()
    return True

class RollingFlow:
    """O(1) rolling signed CVD + absolute volume for one time window."""
    __slots__ = ("window_ms", "rows", "signed", "volume")
    def __init__(self, window_ms):
        self.window_ms = float(window_ms)
        self.rows = collections.deque()
        self.signed = 0.0
        self.volume = 0.0
    def clear(self):
        self.rows.clear()
        self.signed = 0.0
        self.volume = 0.0
    def push(self, ts_ms, delta):
        d = float(delta)
        self.rows.append((float(ts_ms), d))
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
                bo_nho_ram.coinbase_cvd_5m = 0.0
                bo_nho_ram.thoi_gian_coinbase_cuoi = 0.0
                bo_nho_ram.thoi_gian_coinbase_ticker_cuoi = 0.0
                bo_nho_ram.coinbase_flow_epoch = int(getattr(bo_nho_ram, "coinbase_flow_epoch", 0) or 0) + 1
                bo_nho_ram.coinbase_flow_epoch_started_at = time.time()
                logging.info("[COINBASE] Ket noi Coinbase Spot epoch=%s: %s", bo_nho_ram.coinbase_flow_epoch, product_id)

                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        msg_type = data.get("type", "")

                        if msg_type == "ticker":
                            _apply_ticker(data, bo_nho_ram)
                            continue
                        if msg_type not in ("match", "last_match"):
                            continue

                        delta = _coinbase_match_delta(data)
                        if delta == 0.0:
                            continue
                        now_ms = time.time() * 1000.0
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
