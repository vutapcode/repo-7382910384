"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_gia_tick
- ROLE: Hung gia @bookTicker mainnet va BBO venue execution.
"""

import asyncio
import logging
import time

import aiohttp
import orjson
import websockets


EXECUTION_IDLE_FALLBACK_SECONDS = 2.0


def _apply_execution_book_ticker(data, bo_nho_ram):
    """Nhan ca schema WS (b/a) va REST (bidPrice/askPrice)."""
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


def _apply_spot_book_ticker(data, bo_nho_ram):
    """Apply one Spot BBO update while preserving the previous coherent BBO pair."""
    try:
        bid = float(data['b'])
        ask = float(data['a'])
    except (KeyError, TypeError, ValueError):
        return False
    if bid <= 0.0 or ask <= bid:
        return False

    old_bid = float(getattr(bo_nho_ram, 'best_bid', 0.0) or 0.0)
    old_ask = float(getattr(bo_nho_ram, 'best_ask', 0.0) or 0.0)

    # Advance previous BBO atomically only when the pair actually changes.
    # Updating bid/ask independently can create a synthetic midpoint assembled
    # from two different market events.
    if bid != old_bid or ask != old_ask:
        if old_bid > 0.0 and old_ask > old_bid:
            bo_nho_ram.prev_best_bid = old_bid
            bo_nho_ram.prev_best_ask = old_ask
        else:
            bo_nho_ram.prev_best_bid = bid
            bo_nho_ram.prev_best_ask = ask
        bo_nho_ram.best_bid = bid
        bo_nho_ram.best_ask = ask

    bo_nho_ram.thoi_gian_tick_cuoi = time.time()
    return True


async def _refresh_execution_book_ticker(session, url, bo_nho_ram):
    """REST fallback khi WS im lang; gia van cung execution venue."""
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return False
            return _apply_execution_book_ticker(await response.json(), bo_nho_ram)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False


async def hung_gia_tick_futures(symbol: str, bo_nho_ram):
    """Spot Mainnet bookTicker cho structure/signal."""
    stream_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@bookTicker"
    while True:
        try:
            async with websockets.connect(
                stream_url, ping_interval=20, ping_timeout=10
            ) as ws:
                logging.info("[TICK] Da ket noi bookTicker mainnet: %s", symbol.upper())
                async for message in ws:
                    try:
                        data = orjson.loads(message)
                    except (TypeError, ValueError):
                        continue
                    _apply_spot_book_ticker(data, bo_nho_ram)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error("[TICK] Mat luong: %s; reconnect sau 2s", exc)
            await asyncio.sleep(2)


async def hung_gia_tick_execution(symbol: str, bo_nho_ram, testnet: bool = False):
    """Feed Futures BBO dung venue execution; co REST fallback cung venue."""
    if testnet:
        stream_url = (
            f"wss://stream.binancefuture.com/ws/{symbol.lower()}@bookTicker"
        )
        rest_url = (
            "https://testnet.binancefuture.com/fapi/v1/ticker/bookTicker"
            f"?symbol={symbol.upper()}"
        )
        venue = "FUTURES_TESTNET"
    else:
        stream_url = f"wss://fstream.binance.com/ws/{symbol.lower()}@bookTicker"
        rest_url = (
            "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
            f"?symbol={symbol.upper()}"
        )
        venue = "MAINNET"

    timeout = aiohttp.ClientTimeout(total=2.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with websockets.connect(
                    stream_url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logging.info(
                        "[EXECUTION TICK] Da ket noi bookTicker %s: %s",
                        venue, symbol.upper(),
                    )
                    while True:
                        try:
                            message = await asyncio.wait_for(
                                ws.recv(), timeout=EXECUTION_IDLE_FALLBACK_SECONDS
                            )
                        except asyncio.TimeoutError:
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
                    "[EXECUTION TICK] Mat luong %s: %s; reconnect sau 2s",
                    venue, exc,
                )
                await asyncio.sleep(2)
