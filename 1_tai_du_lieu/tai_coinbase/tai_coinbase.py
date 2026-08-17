"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_coinbase
- ROLE: WebSocket thu thap Coinbase Spot BTC-USD matches va tinh rolling CVD.
- I/O: OUT: state.coinbase_cvd_1m, state.coinbase_cvd_5m, state.thoi_gian_coinbase_cuoi
- TIER: S — nguon flow to chuc.
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
    """
    Return signed taker delta for a Coinbase Exchange `match` message.

    Coinbase `side` is the MAKER order side:
      maker sell => taker buy  => positive delta
      maker buy  => taker sell => negative delta
    """
    size = float(data.get("size", 0.0) or 0.0)
    side = str(data.get("side", "") or "").lower()
    if size <= 0.0 or side not in ("buy", "sell"):
        return 0.0
    return size if side == "sell" else -size


async def hung_coinbase_spot(product_id: str, bo_nho_ram):
    """Hang Coinbase Spot matches va cap nhat rolling CVD 1m/5m."""
    buf_1m = collections.deque()
    buf_5m = collections.deque()

    while True:
        try:
            async with websockets.connect(
                COINBASE_WS_URL, ping_interval=20, ping_timeout=20
            ) as ws:
                await ws.send(SUBSCRIBE_MSG)
                logging.info("[COINBASE] Ket noi Coinbase Spot: %s (TIER S)", product_id)

                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        msg_type = data.get("type", "")
                        if msg_type not in ("match", "last_match"):
                            continue

                        delta = _coinbase_match_delta(data)
                        if delta == 0.0:
                            continue

                        now_ms = time.time() * 1000.0
                        buf_1m.append((now_ms, delta))
                        buf_5m.append((now_ms, delta))

                        cutoff_1m = now_ms - 60_000.0
                        cutoff_5m = now_ms - 300_000.0
                        while buf_1m and buf_1m[0][0] < cutoff_1m:
                            buf_1m.popleft()
                        while buf_5m and buf_5m[0][0] < cutoff_5m:
                            buf_5m.popleft()

                        bo_nho_ram.coinbase_cvd_1m = sum(d for _, d in buf_1m)
                        bo_nho_ram.coinbase_cvd_5m = sum(d for _, d in buf_5m)
                        bo_nho_ram.thoi_gian_coinbase_cuoi = time.time()
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
