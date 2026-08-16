"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_gia_tick
- ROLE: Hứng giá @bookTicker mainnet và bơm vào RAM.
"""

import asyncio
import logging
import time

import aiohttp
import orjson
import websockets


EXECUTION_IDLE_FALLBACK_SECONDS = 2.0


def _apply_execution_book_ticker(data, bo_nho_ram):
    """Nhận cả schema WS (b/a) và REST (bidPrice/askPrice)."""
    try:
        bid = float(data.get('b', data.get('bidPrice')))
        ask = float(data.get('a', data.get('askPrice')))
    except (AttributeError, TypeError, ValueError):
        return False
    if bid <= 0.0 or ask <= bid:
        return False
    bo_nho_ram.execution_best_bid = bid
    bo_nho_ram.execution_best_ask = ask
    bo_nho_ram.execution_price_time = time.time()
    return True


async def _refresh_execution_book_ticker(session, url, bo_nho_ram):
    """REST fallback chỉ khi testnet WS im lặng; giá vẫn cùng execution venue."""
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return False
            return _apply_execution_book_ticker(
                await response.json(), bo_nho_ram
            )
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False


async def hung_gia_tick_futures(symbol: str, bo_nho_ram):
    stream_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@bookTicker"
    while True:
        try:
            async with websockets.connect(stream_url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("⚡ [TICK] Đã kết nối bookTicker mainnet: %s", symbol.upper())
                async for message in ws:
                    try:
                        data = orjson.loads(message)
                        bid = float(data['b'])
                        ask = float(data['a'])
                    except (KeyError, TypeError, ValueError):
                        continue

                    old_bid = getattr(bo_nho_ram, 'best_bid', 0.0)
                    old_ask = getattr(bo_nho_ram, 'best_ask', 0.0)
                    if old_bid > 0:
                        if bid != old_bid:
                            bo_nho_ram.prev_best_bid = old_bid
                        if ask != old_ask:
                            bo_nho_ram.prev_best_ask = old_ask
                    else:
                        bo_nho_ram.prev_best_bid = bid
                        bo_nho_ram.prev_best_ask = ask
                    bo_nho_ram.best_bid = bid
                    bo_nho_ram.best_ask = ask
                    bo_nho_ram.thoi_gian_tick_cuoi = time.time()
                    # Mainnet execution and strategy share one authoritative
                    # BBO.  Do not open a duplicate WebSocket in live mode.
                    if getattr(bo_nho_ram, 'execution_venue', '') == (
                        'BINANCE_FUTURES_MAINNET'
                    ):
                        bo_nho_ram.execution_best_bid = bid
                        bo_nho_ram.execution_best_ask = ask
                        bo_nho_ram.execution_price_time = (
                            bo_nho_ram.thoi_gian_tick_cuoi
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error("❌ [TICK] Mất luồng: %s; reconnect sau 2s", exc)
            await asyncio.sleep(2)


async def hung_gia_tick_execution(symbol: str, bo_nho_ram, testnet=True):
    """Feed Testnet để audit/execute; quyết định bảo vệ vẫn dùng Mainnet."""
    base = (
        "wss://testnet.binance.vision/ws"
        if testnet else "wss://stream.binance.com:9443/ws"
    )
    stream_url = f"{base}/{symbol.lower()}@bookTicker"
    rest_base = (
        "https://testnet.binance.vision"
        if testnet else "https://api.binance.com"
    )
    rest_url = f"{rest_base}/api/v3/ticker/bookTicker?symbol={symbol.upper()}"
    venue = "TESTNET" if testnet else "MAINNET"
    timeout = aiohttp.ClientTimeout(total=2.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with websockets.connect(
                    stream_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logging.info(
                        "🧪 [EXECUTION TICK] Đã kết nối bookTicker %s: %s",
                        venue, symbol.upper(),
                    )
                    while True:
                        try:
                            message = await asyncio.wait_for(
                                ws.recv(),
                                timeout=EXECUTION_IDLE_FALLBACK_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            # Testnet bookTicker chỉ phát khi BBO đổi; thị trường
                            # im lặng không đồng nghĩa mất kết nối. Refresh BBO
                            # bằng REST cùng venue để watchdog không hủy setup giả.
                            await _refresh_execution_book_ticker(
                                session, rest_url, bo_nho_ram
                            )
                            continue
                        if message is None:
                            break
                        try:
                            data = orjson.loads(message)
                        except (TypeError, ValueError):
                            continue
                        _apply_execution_book_ticker(data, bo_nho_ram)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.error(
                    "❌ [EXECUTION TICK] Mất luồng %s: %s; reconnect sau 2s",
                    venue, exc,
                )
                await asyncio.sleep(2)
