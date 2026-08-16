"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_nen_live
- ROLE: Hứng kline M1/M15 mainnet, không suy luận.
"""

import asyncio
import logging
import os
import time

import orjson
import websockets


KLINE_SILENCE_SECONDS = max(
    5.0, float(os.getenv('SMC_KLINE_MAX_AGE_SECONDS', '8.0'))
)


async def hung_nen_live_futures(symbol: str, bo_nho_ram):
    symbol_lower = symbol.lower()
    stream_url = (
        "wss://stream.binance.com:9443/stream?streams="
        f"{symbol_lower}@kline_1m/{symbol_lower}@kline_15m"
    )
    while True:
        try:
            async with websockets.connect(stream_url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("📊 [KLINE] Đã kết nối M1/M15 mainnet: %s", symbol.upper())
                while True:
                    try:
                        message = await asyncio.wait_for(
                            ws.recv(), timeout=KLINE_SILENCE_SECONDS
                        )
                    except asyncio.TimeoutError as exc:
                        raise ConnectionError(
                            f"Kline im lặng quá {KLINE_SILENCE_SECONDS:g} giây"
                        ) from exc
                    try:
                        payload = orjson.loads(message)
                        kline = payload['data']['k']
                        interval = kline['i']
                        candle = {
                            't': int(kline['t']),
                            'o': float(kline['o']),
                            'h': float(kline['h']),
                            'l': float(kline['l']),
                            'c': float(kline['c']),
                            'v': float(kline['v']),
                            'x': bool(kline['x']),
                        }
                    except (KeyError, TypeError, ValueError):
                        continue

                    package = {'khung_thoi_gian': interval, 'nen': candle}
                    if interval == '1m':
                        bo_nho_ram.nen_live_1m = candle
                    elif interval == '15m':
                        bo_nho_ram.nen_live_15m = candle
                    else:
                        continue
                    bo_nho_ram.hang_doi_nen_live.append(package)
                    bo_nho_ram.thoi_gian_nen_cuoi = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error("❌ [KLINE] Mất luồng: %s; reconnect sau 2s", exc)
            await asyncio.sleep(2)
