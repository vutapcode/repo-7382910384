"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_coinbase
- ROLE: Coinbase BTC-USD price + rolling aggressive-flow CVD.
- BIAS: price is a light always-on S-tier input; matches are flow evidence.
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
    """Return signed taker delta. Coinbase `side` is the MAKER order side."""
    size = float(data.get("size", 0.0) or 0.0)
    side = str(data.get("side", "") or "").lower()
    if size <= 0.0 or side not in ("buy", "sell"):
        return 0.0
    # maker sell => taker buy; maker buy => taker sell
    return size if side == "sell" else -size


def _apply_ticker(data, state) -> bool:
    """Store a tiny Coinbase price oracle; no extra connection is created."""
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


def _trim(buffer, cutoff_ms):
    while buffer and buffer[0][0] < cutoff_ms:
        buffer.popleft()


def _publish_flow(state, buf_3s, buf_1m, buf_5m, now_ms):
    _trim(buf_3s, now_ms - 3_000.0)
    _trim(buf_1m, now_ms - 60_000.0)
    _trim(buf_5m, now_ms - 300_000.0)

    state.coinbase_cvd_3s = sum(delta for _, delta in buf_3s)
    state.coinbase_volume_3s = sum(abs(delta) for _, delta in buf_3s)
    state.coinbase_flow_3s_ts = now_ms / 1000.0
    state.coinbase_cvd_1m = sum(delta for _, delta in buf_1m)
    state.coinbase_cvd_5m = sum(delta for _, delta in buf_5m)
    state.thoi_gian_coinbase_cuoi = now_ms / 1000.0


async def hung_coinbase_spot(product_id: str, bo_nho_ram):
    """One Coinbase socket: lightweight ticker plus rolling CVD 3s/1m/5m."""
    buf_3s = collections.deque()
    buf_1m = collections.deque()
    buf_5m = collections.deque()

    while True:
        try:
            async with websockets.connect(
                COINBASE_WS_URL, ping_interval=20, ping_timeout=20
            ) as ws:
                await ws.send(SUBSCRIBE_MSG)
                logging.info("[COINBASE] Ket noi Coinbase Spot: %s", product_id)

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
                        row = (now_ms, delta)
                        buf_3s.append(row)
                        buf_1m.append(row)
                        buf_5m.append(row)
                        _publish_flow(bo_nho_ram, buf_3s, buf_1m, buf_5m, now_ms)
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
