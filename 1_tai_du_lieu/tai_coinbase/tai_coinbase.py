"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_coinbase
- ROLE: WebSocket: Hung lenh khop tu Coinbase Spot (BTC-USD).
- I/O: IN: Coinbase WS | OUT: RAM (bo_nho_ram.coinbase_cvd_1m, coinbase_cvd_5m)
- TIER: S — Chan ly to chuc Pho Wall. Kho thao tung nhat.
- RULE: Chi thu thap, tinh CVD don gian, ghi vao RAM.
"""

import asyncio
import orjson
import websockets
import logging
import time
import collections


COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
SUBSCRIBE_MSG = orjson.dumps({
    "type": "subscribe",
    "product_ids": ["BTC-USD"],
    "channels": ["matches", "ticker"]
})


async def hung_coinbase_spot(product_id: str, bo_nho_ram):
    """
    [TIER S] Hung lenh khop tu Coinbase Spot WebSocket.
    Tinh CVD 1m va 5m theo thoi gian thuc.
    Ghi vao: bo_nho_ram.coinbase_cvd_1m, coinbase_cvd_5m
    """
    # Buffer luu (timestamp_ms, delta) de tinh CVD cuon
    buf_1m = collections.deque()   # 60 giay
    buf_5m = collections.deque()   # 300 giay

    while True:
        try:
            async with websockets.connect(
                COINBASE_WS_URL, ping_interval=20, ping_timeout=20
            ) as ws:
                await ws.send(SUBSCRIBE_MSG)
                logging.info(f"[COINBASE] Ket noi Coinbase Spot: {product_id} (TIER S)")

                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        msg_type = data.get("type", "")

                        # Chi xu ly lenh khop thuc su
                        if msg_type != "match" and msg_type != "last_match":
                            continue

                        size = float(data.get("size", 0.0))
                        side = str(data.get("side", "")).lower()  # "buy" or "sell"
                        now_ms = time.time() * 1000

                        # Neu buyer la taker -> Mua chu dong (delta duong)
                        # Neu seller la taker -> Ban chu dong (delta am)
                        # Coinbase: side = phia cua taker
                        delta = size if side == "buy" else -size

                        buf_1m.append((now_ms, delta))
                        buf_5m.append((now_ms, delta))

                        # Xoa gia tri qua 1 phut va 5 phut
                        cutoff_1m = now_ms - 60_000
                        cutoff_5m = now_ms - 300_000
                        while buf_1m and buf_1m[0][0] < cutoff_1m:
                            buf_1m.popleft()
                        while buf_5m and buf_5m[0][0] < cutoff_5m:
                            buf_5m.popleft()

                        # Ghi CVD vao RAM
                        bo_nho_ram.coinbase_cvd_1m = sum(d for _, d in buf_1m)
                        bo_nho_ram.coinbase_cvd_5m = sum(d for _, d in buf_5m)
                        bo_nho_ram.thoi_gian_coinbase_cuoi = time.time()

                    except (KeyError, TypeError, ValueError):
                        continue

        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"[COINBASE] Mat ket noi: {e}. Ket noi lai...")
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"[COINBASE] Loi: {e}. Thu lai sau 5s...")
            await asyncio.sleep(5)
